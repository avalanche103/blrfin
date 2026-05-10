from calendar import monthrange
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.dateparse import parse_date
from django.utils import timezone
from django.db import transaction as db_transaction

from portfolio.models import Asset, DepositCapitalizationAdjustment, DepositInterestPayout, DepositRateChange, DepositTopUp, FXRate, Transaction


SYSTEM_DEPOSIT_NOTE = '__system_deposit_position__'
CLOSE_ASSET_NOTE = '__asset_close_payout__'
DEPOSIT_TOPUP_NOTE = '__deposit_topup__'
ACCOUNT_TOPUP_NOTE = '__account_topup__'
DEPOSIT_PAYOUT_NOTE = '__deposit_interest_payout__'
PAYOUT_CONFIRMATION_WINDOW_DAYS = 31

BELARUS_FIXED_PUBLIC_HOLIDAYS = {
    (1, 1),
    (1, 2),
    (1, 7),
    (3, 8),
    (5, 1),
    (5, 9),
    (7, 3),
    (11, 7),
    (12, 25),
}


def has_system_note(item_or_notes, marker):
    notes = item_or_notes.notes if hasattr(item_or_notes, 'notes') else item_or_notes
    if not notes:
        return False
    return str(notes).splitlines()[0] == marker


def _quantize_money(value):
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _normalize_date(value):
    if not value or hasattr(value, 'year'):
        return value
    return parse_date(str(value))


def _normalize_decimal(value):
    if value in (None, '') or isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _deposit_effective_end_date(asset):
    today = timezone.localdate()
    effective_end = today
    if asset.deposit_term_end:
        effective_end = min(effective_end, asset.deposit_term_end)
    if asset.closed_at:
        effective_end = min(effective_end, asset.closed_at)
    return effective_end


def _monthly_periods_between(start_date, end_date):
    if end_date <= start_date:
        return 0
    months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month)
    if end_date.day < start_date.day:
        months -= 1
    return max(months, 0)


def _semi_monthly_periods_between(start_date, end_date):
    if end_date <= start_date:
        return 0
    return max((end_date - start_date).days // 15, 0)


def _add_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _semi_monthly_anchor_days(open_day):
    primary_day = min(max(open_day, 1), 31)
    if primary_day > 15:
        secondary_day = primary_day - 15
    else:
        secondary_day = primary_day + 15
    return tuple(sorted({primary_day, secondary_day}))


def _orthodox_easter_date(year):
    a = year % 4
    b = year % 7
    c = year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = ((d + e + 114) % 31) + 1
    julian_easter = date(year, month, day)
    gregorian_offset = year // 100 - year // 400 - 2
    return julian_easter + timedelta(days=gregorian_offset)


def _belarus_public_holidays(year):
    holidays = {date(year, month, day) for month, day in BELARUS_FIXED_PUBLIC_HOLIDAYS}
    holidays.add(_orthodox_easter_date(year) + timedelta(days=9))
    return holidays


def _is_belarus_non_working_day(candidate):
    return candidate.weekday() >= 5 or candidate in _belarus_public_holidays(candidate.year)


def _roll_deposit_event_date(asset, candidate, effective_end):
    if not asset.deposit_weekend_rollover:
        return candidate

    rolled_candidate = candidate
    while _is_belarus_non_working_day(rolled_candidate) and rolled_candidate <= effective_end:
        rolled_candidate += timedelta(days=1)
    return rolled_candidate


def _contribution_label(asset, contribution_date, amount):
    if asset.deposit_open_date == contribution_date and amount == (asset.deposit_initial_amount or Decimal('0')):
        return 'Начальный взнос'
    return 'Пополнение'


def _deposit_contributions(asset):
    contributions = []
    if asset.deposit_open_date and asset.deposit_initial_amount:
        contributions.append((asset.deposit_open_date, asset.deposit_initial_amount))
    if asset.pk:
        contributions.extend(DepositTopUp.objects.filter(asset=asset).values_list('topup_date', 'amount'))
    return contributions


def _deposit_topups_by_date(asset, effective_end=None):
    if not asset.pk:
        return {}
    topups_by_date = {}
    queryset = DepositTopUp.objects.filter(asset=asset)
    if effective_end:
        queryset = queryset.filter(topup_date__lte=effective_end)
    for topup_date, amount in queryset.values_list('topup_date', 'amount'):
        topups_by_date[topup_date] = topups_by_date.get(topup_date, Decimal('0')) + amount
    return topups_by_date


def _capitalization_adjustments_by_date(asset):
    if not asset.pk:
        return {}
    return {
        item.capitalization_date: item
        for item in DepositCapitalizationAdjustment.objects.filter(asset=asset)
    }


def _interest_payouts_by_date(asset, effective_end=None):
    if not asset.pk:
        return {}
    queryset = DepositInterestPayout.objects.filter(asset=asset)
    if effective_end:
        queryset = queryset.filter(payout_date__lte=effective_end)
    return {
        item.payout_date: item
        for item in queryset.select_related('cash_transaction')
    }


def _rate_changes_by_date(asset, effective_end=None):
    if not asset.pk:
        return {}
    queryset = DepositRateChange.objects.filter(asset=asset)
    if effective_end:
        queryset = queryset.filter(effective_date__lte=effective_end)
    return {
        item.effective_date: item
        for item in queryset.order_by('effective_date', 'id')
    }


def get_deposit_effective_rate(asset, *, on_date=None):
    if asset.asset_class != Asset.AssetClass.DEPOSIT:
        return None
    if asset.deposit_annual_rate is None:
        return None

    effective_date = on_date or timezone.localdate()
    effective_rate = asset.deposit_annual_rate
    if asset.pk:
        rate_change = DepositRateChange.objects.filter(asset=asset, effective_date__lte=effective_date).order_by('effective_date', 'id').last()
        if rate_change:
            effective_rate = rate_change.annual_rate
    return effective_rate


def _compound_contribution(amount, contribution_date, effective_end, annual_rate, payout_frequency):
    if effective_end <= contribution_date:
        return _quantize_money(amount)

    if payout_frequency == Asset.InterestPayoutFrequency.SEMI_MONTHLY:
        periods = _semi_monthly_periods_between(contribution_date, effective_end)
        periods_per_year = Decimal('24')
    else:
        periods = _monthly_periods_between(contribution_date, effective_end)
        periods_per_year = Decimal('12')

    if periods <= 0:
        return _quantize_money(amount)

    period_rate = (annual_rate / Decimal('100')) / periods_per_year
    return _quantize_money(amount * ((Decimal('1') + period_rate) ** periods))


def _capitalization_dates(asset, effective_end):
    if not asset.deposit_open_date or effective_end <= asset.deposit_open_date:
        return []

    dates = []
    if asset.deposit_interest_payout_frequency == Asset.InterestPayoutFrequency.SEMI_MONTHLY:
        anchor_days = _semi_monthly_anchor_days(asset.deposit_open_date.day)
        current_month = asset.deposit_open_date.replace(day=1)
        while current_month <= effective_end.replace(day=1):
            _, days_in_month = monthrange(current_month.year, current_month.month)
            for day in anchor_days:
                candidate = current_month.replace(day=min(day, days_in_month))
                if candidate <= asset.deposit_open_date:
                    continue
                if candidate > effective_end:
                    break
                dates.append(candidate)
            current_month = _add_months(current_month, 1)
        return dates

    step = 1
    while True:
        candidate = _add_months(asset.deposit_open_date, step)
        if candidate > effective_end:
            break
        dates.append(candidate)
        step += 1
    return dates


def _resolved_deposit_event_dates(asset, effective_end, explicit_dates=None):
    resolved_dates = list(_capitalization_dates(asset, effective_end))
    if not explicit_dates:
        return resolved_dates

    for explicit_date in sorted(set(explicit_dates)):
        if explicit_date <= asset.deposit_open_date or explicit_date > effective_end:
            continue
        if explicit_date in resolved_dates:
            continue

        insert_index = 0
        while insert_index < len(resolved_dates) and resolved_dates[insert_index] < explicit_date:
            insert_index += 1

        previous_index = insert_index - 1
        next_date = resolved_dates[insert_index] if insert_index < len(resolved_dates) else None

        if previous_index >= 0 and explicit_date > resolved_dates[previous_index] and (next_date is None or explicit_date < next_date):
            resolved_dates[previous_index] = explicit_date
        else:
            resolved_dates.insert(insert_index, explicit_date)

        resolved_dates = sorted(set(resolved_dates))

    return resolved_dates


def _deposit_event_date_map(asset, effective_end, explicit_dates=None):
    event_date_map = {}
    for scheduled_date in _resolved_deposit_event_dates(asset, effective_end, explicit_dates):
        actual_date = scheduled_date
        if explicit_dates and scheduled_date in explicit_dates:
            actual_date = scheduled_date
        else:
            actual_date = _roll_deposit_event_date(asset, scheduled_date, effective_end)
        event_date_map[actual_date] = scheduled_date
    return event_date_map


def _capitalization_overlap_days(operation_date, scheduled_date):
    if not operation_date or not scheduled_date or operation_date <= scheduled_date:
        return 0
    return (operation_date - scheduled_date).days


def _uses_actual_capitalization_period_start(asset):
    account_name = (asset.account.name if asset.account else '') or ''
    open_day = asset.deposit_open_date.day if asset.deposit_open_date else 0
    return 'БЕЛВЭБ' in account_name.upper() and open_day >= 29


def _simulate_deposit_capitalization(asset):
    principal = _quantize_money(asset.deposit_initial_amount or Decimal('0'))
    if principal <= 0 or not asset.deposit_open_date or not asset.deposit_annual_rate:
        return principal, []

    effective_end = _deposit_effective_end_date(asset)
    if effective_end <= asset.deposit_open_date:
        return principal, []

    balance = principal
    current_rate = asset.deposit_annual_rate
    topups_by_date = _deposit_topups_by_date(asset, effective_end)
    adjustments_by_date = _capitalization_adjustments_by_date(asset)
    capitalization_date_map = _deposit_event_date_map(asset, effective_end, adjustments_by_date)
    capitalization_dates = set(capitalization_date_map)
    rate_changes_by_date = _rate_changes_by_date(asset, effective_end)

    history = []
    current_period_start = asset.deposit_open_date
    opening_balance = balance
    period_topups = Decimal('0')
    period_daily_balance_total = Decimal('0')
    period_interest_total = Decimal('0')
    current_date = asset.deposit_open_date

    while current_date <= effective_end:
        if current_date in capitalization_dates:
            scheduled_date = capitalization_date_map.get(current_date, current_date)
            computed_interest_amount = _quantize_money(period_interest_total)
            adjustment = adjustments_by_date.get(current_date) or adjustments_by_date.get(scheduled_date)
            interest_amount = adjustment.interest_amount if adjustment else computed_interest_amount
            balance_before = _quantize_money(balance)
            balance_after = _quantize_money(balance_before + interest_amount)
            operation_date = adjustment.capitalization_date if adjustment else current_date
            history.append(
                {
                    'capitalization_date': operation_date,
                    'period_start': current_period_start,
                    'opening_balance': _quantize_money(opening_balance),
                    'topups_amount': _quantize_money(period_topups),
                    'daily_balance_total': _quantize_money(period_daily_balance_total),
                    'computed_interest_amount': computed_interest_amount,
                    'interest_amount': interest_amount,
                    'annual_rate': current_rate,
                    'balance_after': balance_after,
                    'is_adjusted': bool(adjustment),
                    'adjustment_id': adjustment.id if adjustment else None,
                    'adjustment_notes': adjustment.notes if adjustment else '',
                }
            )
            balance = balance_after
            current_period_start = operation_date if _uses_actual_capitalization_period_start(asset) else scheduled_date
            opening_balance = balance
            period_topups = Decimal('0')
            overlap_days = Decimal('0') if _uses_actual_capitalization_period_start(asset) else _capitalization_overlap_days(operation_date, scheduled_date)
            period_daily_balance_total = balance * overlap_days
            period_interest_total = period_daily_balance_total * ((current_rate / Decimal('100')) / Decimal('365'))

        rate_change = rate_changes_by_date.get(current_date)
        if rate_change:
            current_rate = rate_change.annual_rate

        topup_amount = topups_by_date.get(current_date, Decimal('0'))
        if topup_amount:
            balance += topup_amount
            period_topups += topup_amount

        if current_date == current_period_start:
            opening_balance = balance

        if current_date < effective_end:
            period_daily_balance_total += balance
            period_interest_total += balance * ((current_rate / Decimal('100')) / Decimal('365'))

        current_date += timedelta(days=1)

    return _quantize_money(balance), history


def calculate_deposit_current_amount(asset):
    contributions = _deposit_contributions(asset)
    principal = sum((amount for _, amount in contributions), Decimal('0'))
    if principal <= 0 or not asset.deposit_open_date or not asset.deposit_annual_rate:
        return _quantize_money(principal)

    effective_end = _deposit_effective_end_date(asset)
    if effective_end <= asset.deposit_open_date:
        return _quantize_money(principal)

    if asset.deposit_interest_payout_method != Asset.InterestPayoutMethod.CAPITALIZATION:
        return _quantize_money(principal)

    current_amount, _ = _simulate_deposit_capitalization(asset)
    return current_amount


def build_deposit_capitalization_history(asset):
    if asset.asset_class != Asset.AssetClass.DEPOSIT:
        return []
    if asset.deposit_interest_payout_method != Asset.InterestPayoutMethod.CAPITALIZATION:
        return []
    if not asset.deposit_open_date or not asset.deposit_annual_rate:
        return []
    _, history = _simulate_deposit_capitalization(asset)
    return history


def build_deposit_interest_payout_history(asset):
    if asset.asset_class != Asset.AssetClass.DEPOSIT:
        return []
    if asset.deposit_interest_payout_method != Asset.InterestPayoutMethod.TO_ACCOUNT:
        return []
    if not asset.deposit_open_date or not asset.deposit_annual_rate:
        return []
    _, history = _build_deposit_projected_schedule(asset, effective_end=_deposit_effective_end_date(asset))
    return history


def _build_deposit_projected_schedule(asset, *, effective_end):
    principal = _quantize_money(asset.deposit_initial_amount or Decimal('0'))
    if principal <= 0 or not asset.deposit_open_date or not asset.deposit_annual_rate:
        return principal, []

    effective_end = _deposit_effective_end_date(asset) if effective_end is None else effective_end
    if asset.deposit_term_end:
        effective_end = min(effective_end, asset.deposit_term_end)
    if asset.closed_at:
        effective_end = min(effective_end, asset.closed_at)

    if effective_end <= asset.deposit_open_date:
        return principal, []

    balance = principal
    current_rate = asset.deposit_annual_rate
    topups_by_date = _deposit_topups_by_date(asset, effective_end)
    adjustments_by_date = _capitalization_adjustments_by_date(asset)
    payouts_by_date = _interest_payouts_by_date(asset, effective_end)
    rate_changes_by_date = _rate_changes_by_date(asset, effective_end)
    is_capitalization = asset.deposit_interest_payout_method == Asset.InterestPayoutMethod.CAPITALIZATION
    explicit_dates = adjustments_by_date if is_capitalization else payouts_by_date
    today = timezone.localdate()
    event_date_map = {}
    for scheduled_date in _resolved_deposit_event_dates(asset, effective_end, explicit_dates):
        has_explicit_record = bool(explicit_dates and scheduled_date in explicit_dates)
        trigger_date = scheduled_date
        if not has_explicit_record and (is_capitalization or scheduled_date >= today):
            trigger_date = _roll_deposit_event_date(asset, scheduled_date, effective_end)
        event_date_map[trigger_date] = scheduled_date
    event_dates = set(event_date_map)

    schedule = []
    current_period_start = asset.deposit_open_date
    opening_balance = balance
    period_topups = Decimal('0')
    period_daily_balance_total = Decimal('0')
    period_interest_total = Decimal('0')
    current_date = asset.deposit_open_date

    while current_date <= effective_end:
        if current_date in event_dates:
            scheduled_date = event_date_map.get(current_date, current_date)
            computed_interest_amount = _quantize_money(period_interest_total)
            record_source = adjustments_by_date if is_capitalization else payouts_by_date
            record = record_source.get(current_date) or record_source.get(scheduled_date)
            interest_amount = record.interest_amount if record else computed_interest_amount
            balance_before = _quantize_money(balance)
            balance_after = _quantize_money(balance_before + interest_amount) if is_capitalization else balance_before
            operation_date = getattr(record, 'payout_date', None) or getattr(record, 'capitalization_date', None) or current_date
            schedule.append(
                {
                    'operation_date': operation_date,
                    'operation_type': 'capitalization' if is_capitalization else 'payout',
                    'period_start': current_period_start,
                    'opening_balance': _quantize_money(opening_balance),
                    'topups_amount': _quantize_money(period_topups),
                    'daily_balance_total': _quantize_money(period_daily_balance_total),
                    'computed_interest_amount': computed_interest_amount,
                    'interest_amount': interest_amount,
                    'annual_rate': current_rate,
                    'balance_before': balance_before,
                    'balance_after': balance_after,
                    'is_adjusted': bool(record),
                    'adjustment_id': record.id if record else None,
                    'adjustment_notes': record.notes if record else '',
                    'cash_transaction_id': getattr(record, 'cash_transaction_id', None),
                    'credits_account': bool(getattr(record, 'cash_transaction_id', None)),
                }
            )
            balance = balance_after
            current_period_start = operation_date if (is_capitalization and _uses_actual_capitalization_period_start(asset)) else scheduled_date
            opening_balance = balance
            period_topups = Decimal('0')
            if is_capitalization:
                overlap_days = Decimal('0') if _uses_actual_capitalization_period_start(asset) else _capitalization_overlap_days(operation_date, scheduled_date)
                period_daily_balance_total = balance * overlap_days
                period_interest_total = period_daily_balance_total * ((current_rate / Decimal('100')) / Decimal('365'))
            else:
                period_daily_balance_total = Decimal('0')
                period_interest_total = Decimal('0')

        rate_change = rate_changes_by_date.get(current_date)
        if rate_change:
            current_rate = rate_change.annual_rate

        topup_amount = topups_by_date.get(current_date, Decimal('0'))
        if topup_amount:
            balance += topup_amount
            period_topups += topup_amount

        if current_date == current_period_start:
            opening_balance = balance

        if current_date < effective_end:
            period_daily_balance_total += balance
            period_interest_total += balance * ((current_rate / Decimal('100')) / Decimal('365'))

        current_date += timedelta(days=1)

    return _quantize_money(balance), schedule


def build_upcoming_operations(limit=12):
    today = timezone.localdate()
    horizon = today + timedelta(days=366)
    operations = []

    assets = Asset.objects.filter(asset_class=Asset.AssetClass.DEPOSIT, closed_at__isnull=True).select_related('account')
    for asset in assets:
        effective_end = min(asset.deposit_term_end, horizon) if asset.deposit_term_end else horizon
        projected_balance, schedule = _build_deposit_projected_schedule(asset, effective_end=effective_end)

        for item in schedule:
            if item['operation_date'] < today:
                continue
            operations.append(
                {
                    'date': item['operation_date'],
                    'asset': asset,
                    'type': item['operation_type'],
                    'type_label': 'Капитализация' if item['operation_type'] == 'capitalization' else 'Выплата',
                    'expected_amount': item['interest_amount'],
                    'currency': asset.price_currency or asset.account.currency,
                }
            )

        if asset.deposit_term_type == Asset.DepositTermType.TERM and asset.deposit_term_end and asset.deposit_term_end >= today:
            close_balance = projected_balance
            if asset.deposit_term_end < horizon:
                close_balance, _ = _build_deposit_projected_schedule(asset, effective_end=asset.deposit_term_end)
            operations.append(
                {
                    'date': asset.deposit_term_end,
                    'asset': asset,
                    'type': 'closing',
                    'type_label': 'Закрытие',
                    'expected_amount': close_balance,
                    'currency': asset.price_currency or asset.account.currency,
                }
            )

    operations.sort(key=lambda item: (item['date'], item['asset'].symbol, item['type']))
    return operations[:limit]


def build_due_payout_confirmations(limit=12):
    today = timezone.localdate()
    window_start = today - timedelta(days=PAYOUT_CONFIRMATION_WINDOW_DAYS)
    items = []

    assets = Asset.objects.filter(
        asset_class=Asset.AssetClass.DEPOSIT,
        deposit_interest_payout_method=Asset.InterestPayoutMethod.TO_ACCOUNT,
    ).select_related('account')

    for asset in assets:
        for row in build_deposit_interest_payout_history(asset):
            if row['operation_date'] > today or row['operation_date'] < window_start or row['credits_account']:
                continue
            if row['is_adjusted'] and row['operation_date'] < today:
                continue
            items.append(
                {
                    'date': row['operation_date'],
                    'asset': asset,
                    'expected_amount': row['interest_amount'],
                    'currency': asset.price_currency or asset.account.currency,
                    'is_adjusted': row['is_adjusted'],
                    'adjustment_notes': row['adjustment_notes'],
                    'is_overdue': row['operation_date'] < today,
                }
            )

    items.sort(key=lambda item: (item['date'], item['asset'].symbol), reverse=True)
    return items[:limit]


@db_transaction.atomic
def upsert_capitalization_adjustment(asset, *, capitalization_date, interest_amount, notes=''):
    adjustment, _ = DepositCapitalizationAdjustment.objects.update_or_create(
        asset=asset,
        capitalization_date=capitalization_date,
        defaults={
            'interest_amount': interest_amount,
            'notes': notes,
        },
    )
    adjustment.full_clean()
    adjustment.save()
    update_asset(asset, _asset_payload_from_instance(asset))
    return adjustment


@db_transaction.atomic
def delete_capitalization_adjustment(adjustment):
    asset = adjustment.asset
    adjustment.delete()
    update_asset(asset, _asset_payload_from_instance(asset))


@db_transaction.atomic
def upsert_deposit_interest_payout(asset, *, payout_date, interest_amount, notes='', credit_to_account=False, payout_record=None, force_credit_to_account=False):
    payout_date = _normalize_date(payout_date)
    interest_amount = _normalize_decimal(interest_amount)
    if payout_record is None:
        payout_record = DepositInterestPayout(asset=asset)

    payout_record.asset = asset
    payout_record.payout_date = payout_date
    payout_record.interest_amount = interest_amount
    payout_record.notes = notes
    payout_record.full_clean()
    payout_record.save()

    transaction = payout_record.cash_transaction
    should_credit_account = bool(
        credit_to_account and payout_date and (payout_date >= timezone.localdate() or force_credit_to_account)
    )
    if should_credit_account:
        transaction_payload = {
            'transaction_type': Transaction.TransactionType.DEPOSIT,
            'destination_account': asset.account,
            'asset': asset,
            'amount': interest_amount,
            'currency': asset.price_currency or asset.account.currency,
            'fee': Decimal('0'),
            'asset_quantity': Decimal('0'),
            'unit_price': None,
            'status': Transaction.Status.COMPLETED,
            'occurred_at': _payout_datetime(payout_date),
            'notes': DEPOSIT_PAYOUT_NOTE if not notes else f'{DEPOSIT_PAYOUT_NOTE}\n{notes}',
        }
        if transaction:
            for key, value in transaction_payload.items():
                setattr(transaction, key, value)
            transaction.full_clean()
            transaction.save()
        else:
            transaction = Transaction(**transaction_payload)
            transaction.full_clean()
            transaction.save()
            payout_record.cash_transaction = transaction
            payout_record.save(update_fields=['cash_transaction', 'updated_at'])
    elif transaction:
        transaction.delete()
        payout_record.cash_transaction = None
        payout_record.save(update_fields=['cash_transaction', 'updated_at'])

    update_asset(asset, _asset_payload_from_instance(asset))
    return payout_record


@db_transaction.atomic
def delete_deposit_interest_payout(payout_record):
    asset = payout_record.asset
    if payout_record.cash_transaction_id:
        payout_record.cash_transaction.delete()
    payout_record.delete()
    update_asset(asset, _asset_payload_from_instance(asset))


@db_transaction.atomic
def upsert_deposit_rate_change(asset, *, effective_date, annual_rate, notes='', rate_change=None):
    if rate_change is None:
        rate_change = DepositRateChange(asset=asset)

    rate_change.asset = asset
    rate_change.effective_date = effective_date
    rate_change.annual_rate = annual_rate
    rate_change.notes = notes
    rate_change.full_clean()
    rate_change.save()
    update_asset(asset, _asset_payload_from_instance(asset))
    return rate_change


@db_transaction.atomic
def delete_deposit_rate_change(rate_change):
    asset = rate_change.asset
    rate_change.delete()
    update_asset(asset, _asset_payload_from_instance(asset))


def get_current_asset_price(asset):
    if asset.asset_class == Asset.AssetClass.DEPOSIT:
        return calculate_deposit_current_amount(asset)
    return asset.current_price


def get_asset_price_on_date(asset, *, on_date=None):
    if on_date is None:
        return get_current_asset_price(asset)
    if asset.asset_class == Asset.AssetClass.DEPOSIT:
        value_on_date, _ = _build_deposit_projected_schedule(asset, effective_end=on_date)
        return value_on_date
    return asset.current_price


def _asset_payload_from_instance(asset):
    return {
        'asset_class': asset.asset_class,
        'symbol': asset.symbol,
        'name': asset.name,
        'account': asset.account,
        'deposit_revocability': asset.deposit_revocability,
        'deposit_term_type': asset.deposit_term_type,
        'deposit_term_end': asset.deposit_term_end,
        'deposit_open_date': asset.deposit_open_date,
        'deposit_annual_rate': asset.deposit_annual_rate,
        'deposit_interest_payout_method': asset.deposit_interest_payout_method,
        'deposit_initial_amount': asset.deposit_initial_amount,
        'deposit_interest_payout_frequency': asset.deposit_interest_payout_frequency,
        'deposit_weekend_rollover': asset.deposit_weekend_rollover,
        'closed_at': asset.closed_at,
        'price_currency': asset.price_currency,
        'current_price': asset.current_price,
    }


def _deposit_position_datetime(asset):
    open_date = asset.deposit_open_date or timezone.localdate()
    naive = datetime.combine(open_date, time.min)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _asset_close_datetime(closed_at):
    naive = datetime.combine(closed_at or timezone.localdate(), time.min)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _topup_datetime(topup_date):
    naive = datetime.combine(topup_date or timezone.localdate(), time.min)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _payout_datetime(payout_date):
    naive = datetime.combine(payout_date or timezone.localdate(), time.min)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _date_start_datetime(value):
    naive = datetime.combine(value or timezone.localdate(), time.min)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _account_balance_events(account, *, exclude_transaction_ids=None):
    exclude_transaction_ids = [item for item in (exclude_transaction_ids or []) if item]
    queryset = Transaction.objects.filter(
        status=Transaction.Status.COMPLETED,
    ).filter(
        Q(source_account=account) | Q(destination_account=account)
    )
    if exclude_transaction_ids:
        queryset = queryset.exclude(id__in=exclude_transaction_ids)
    return queryset.order_by('occurred_at', 'id')


def _apply_account_transaction_delta(balance, account, transaction):
    if has_system_note(transaction, SYSTEM_DEPOSIT_NOTE):
        return balance
    if transaction.source_account_id == account.id:
        balance -= transaction.amount + transaction.fee
    if transaction.destination_account_id == account.id:
        balance += transaction.amount
    return balance


def _ensure_account_timeline_non_negative(account, *, pending_events, exclude_transaction_ids=None):
    if not account or not pending_events:
        return

    balance = Decimal(account.opening_balance)
    timeline = []
    for transaction in _account_balance_events(account, exclude_transaction_ids=exclude_transaction_ids):
        timeline.append(
            (
                transaction.occurred_at,
                1,
                transaction.id,
                'transaction',
                transaction,
            )
        )

    for index, event in enumerate(pending_events):
        timeline.append(
            (
                event['occurred_at'],
                0,
                index,
                'pending',
                event,
            )
        )

    timeline.sort(key=lambda item: (item[0], item[1], item[2]))

    for occurred_at, _, _, event_type, payload in timeline:
        if event_type == 'transaction':
            balance = _apply_account_transaction_delta(balance, account, payload)
        else:
            balance += payload['delta']
            if balance < 0:
                occurred_on = timezone.localtime(occurred_at).date() if timezone.is_aware(occurred_at) else occurred_at.date()
                required_amount = _quantize_money(-payload['delta'])
                shortage = _quantize_money(-balance)
                raise ValidationError(
                    f'На счете {account.name} недостаточно средств на {occurred_on:%d.%m.%Y}: '
                    f'не хватает {shortage} {account.currency} для операции на {required_amount} {account.currency}.'
                )


def _validate_deposit_funding(account, *, deposit_open_date, amount):
    normalized_amount = _normalize_decimal(amount) or Decimal('0')
    if not account or normalized_amount <= 0 or not deposit_open_date:
        return
    _ensure_account_timeline_non_negative(
        account,
        pending_events=[
            {
                'occurred_at': _date_start_datetime(deposit_open_date),
                'delta': -normalized_amount,
            }
        ],
    )


def _validate_deposit_topup_funding(source_account, *, topup_date, amount, exclude_transaction_ids=None):
    normalized_amount = _normalize_decimal(amount) or Decimal('0')
    if not source_account or normalized_amount <= 0 or not topup_date:
        return
    _ensure_account_timeline_non_negative(
        source_account,
        pending_events=[
            {
                'occurred_at': _topup_datetime(topup_date),
                'delta': -normalized_amount,
            }
        ],
        exclude_transaction_ids=exclude_transaction_ids,
    )


def _sync_deposit_position(asset):
    system_tx = Transaction.objects.filter(asset=asset, notes=SYSTEM_DEPOSIT_NOTE).first()

    if asset.asset_class != Asset.AssetClass.DEPOSIT:
        if system_tx:
            system_tx.delete()
        return

    payload = {
        'transaction_type': Transaction.TransactionType.DEPOSIT,
        'destination_account': asset.account,
        'asset': asset,
        'amount': asset.deposit_initial_amount or Decimal('0'),
        'currency': asset.price_currency or asset.account.currency,
        'fee': Decimal('0'),
        'asset_quantity': Decimal('1'),
        'unit_price': asset.deposit_initial_amount or Decimal('0'),
        'status': Transaction.Status.COMPLETED,
        'occurred_at': _deposit_position_datetime(asset),
        'notes': SYSTEM_DEPOSIT_NOTE,
    }

    if system_tx:
        for key, value in payload.items():
            setattr(system_tx, key, value)
        system_tx.full_clean()
        system_tx.save()
        return

    system_tx = Transaction(**payload)
    system_tx.full_clean()
    system_tx.save()


def _prepare_asset_payload(data, existing_asset=None):
    payload = data.copy()
    payload.pop('fund_from_account', None)
    payload['deposit_open_date'] = _normalize_date(payload.get('deposit_open_date'))
    payload['deposit_term_end'] = _normalize_date(payload.get('deposit_term_end'))
    payload['closed_at'] = _normalize_date(payload.get('closed_at'))
    payload['deposit_annual_rate'] = _normalize_decimal(payload.get('deposit_annual_rate'))
    payload['deposit_initial_amount'] = _normalize_decimal(payload.get('deposit_initial_amount'))
    payload['current_price'] = _normalize_decimal(payload.get('current_price'))
    if payload.get('asset_class') == Asset.AssetClass.DEPOSIT:
        account = payload.get('account')
        if account and not payload.get('price_currency'):
            payload['price_currency'] = account.currency
        draft_asset = Asset(pk=getattr(existing_asset, 'pk', None), **payload)
        payload['current_price'] = calculate_deposit_current_amount(draft_asset)
    return payload


@db_transaction.atomic
def create_asset(data):
    should_fund_from_account = data.get('fund_from_account', True)
    payload = _prepare_asset_payload(data)
    if payload.get('asset_class') == Asset.AssetClass.DEPOSIT and should_fund_from_account:
        _validate_deposit_funding(
            payload.get('account'),
            deposit_open_date=payload.get('deposit_open_date'),
            amount=payload.get('deposit_initial_amount'),
        )
    asset = Asset(**payload)
    asset.full_clean()
    asset.save()
    _sync_deposit_position(asset)
    return asset


@db_transaction.atomic
def update_asset(asset, data):
    should_fund_from_account = data.get('fund_from_account')
    payload = _prepare_asset_payload(data, existing_asset=asset)
    if payload.get('asset_class') == Asset.AssetClass.DEPOSIT and should_fund_from_account is True:
        _validate_deposit_funding(
            payload.get('account'),
            deposit_open_date=payload.get('deposit_open_date'),
            amount=payload.get('deposit_initial_amount'),
        )
    for key, value in payload.items():
        setattr(asset, key, value)
    asset.full_clean()
    asset.save()
    _sync_deposit_position(asset)
    return asset


@db_transaction.atomic
def add_deposit_topup(asset, *, source_account, topup_date, amount, fund_from_account=True):
    if fund_from_account:
        _validate_deposit_topup_funding(source_account, topup_date=topup_date, amount=amount)
    topup = DepositTopUp(
        asset=asset,
        source_account=source_account if fund_from_account else None,
        topup_date=topup_date,
        amount=amount,
    )
    topup.full_clean()
    topup.save()
    if fund_from_account:
        transaction = Transaction(
            transaction_type=Transaction.TransactionType.WITHDRAW,
            source_account=source_account,
            asset=asset,
            amount=amount,
            currency=asset.price_currency or source_account.currency,
            fee=Decimal('0'),
            asset_quantity=Decimal('0'),
            unit_price=None,
            status=Transaction.Status.COMPLETED,
            occurred_at=_topup_datetime(topup_date),
            notes=DEPOSIT_TOPUP_NOTE,
        )
        transaction.full_clean()
        transaction.save()
        topup.cash_transaction = transaction
        topup.save(update_fields=['cash_transaction', 'updated_at'])
    update_asset(asset, _asset_payload_from_instance(asset))
    return topup


@db_transaction.atomic
def update_deposit_topup(topup, *, source_account, topup_date, amount, fund_from_account=True):
    transaction = topup.cash_transaction
    old_asset = topup.asset
    if fund_from_account:
        _validate_deposit_topup_funding(
            source_account,
            topup_date=topup_date,
            amount=amount,
            exclude_transaction_ids=[transaction.id] if transaction else None,
        )
    topup.source_account = source_account if fund_from_account else None
    topup.topup_date = topup_date
    topup.amount = amount
    topup.full_clean()
    topup.save()

    if fund_from_account:
        if transaction:
            transaction.source_account = source_account
            transaction.amount = amount
            transaction.currency = old_asset.price_currency or source_account.currency
            transaction.occurred_at = _topup_datetime(topup_date)
            transaction.full_clean()
            transaction.save()
        else:
            transaction = Transaction(
                transaction_type=Transaction.TransactionType.WITHDRAW,
                source_account=source_account,
                asset=old_asset,
                amount=amount,
                currency=old_asset.price_currency or source_account.currency,
                fee=Decimal('0'),
                asset_quantity=Decimal('0'),
                unit_price=None,
                status=Transaction.Status.COMPLETED,
                occurred_at=_topup_datetime(topup_date),
                notes=DEPOSIT_TOPUP_NOTE,
            )
            transaction.full_clean()
            transaction.save()
            topup.cash_transaction = transaction
            topup.save(update_fields=['cash_transaction', 'updated_at'])
    elif transaction:
        transaction.delete()
        topup.cash_transaction = None
        topup.save(update_fields=['cash_transaction', 'updated_at'])

    update_asset(old_asset, _asset_payload_from_instance(old_asset))
    return topup


@db_transaction.atomic
def delete_deposit_topup(topup):
    asset = topup.asset
    if topup.cash_transaction_id:
        topup.cash_transaction.delete()
    topup.delete()
    update_asset(asset, _asset_payload_from_instance(asset))


@db_transaction.atomic
def delete_asset(asset):
    if asset.closed_at:
        raise ValueError('Нельзя удалить уже закрытый продукт. Сначала оставьте его в истории или удалите связанные выплаты вручную.')

    Transaction.objects.filter(asset=asset).delete()
    DepositTopUp.objects.filter(asset=asset).delete()
    DepositCapitalizationAdjustment.objects.filter(asset=asset).delete()
    DepositInterestPayout.objects.filter(asset=asset).delete()
    DepositRateChange.objects.filter(asset=asset).delete()
    asset.delete()


@db_transaction.atomic
def close_asset(asset, destination_account, closed_at):
    payload = _asset_payload_from_instance(asset)
    payload['closed_at'] = closed_at
    asset = update_asset(asset, payload)

    payout = Transaction(
        transaction_type=Transaction.TransactionType.DEPOSIT,
        destination_account=destination_account,
        amount=asset.current_price or Decimal('0'),
        currency=asset.price_currency or destination_account.currency,
        fee=Decimal('0'),
        asset_quantity=Decimal('0'),
        unit_price=None,
        status=Transaction.Status.COMPLETED,
        occurred_at=_asset_close_datetime(closed_at),
        notes=CLOSE_ASSET_NOTE,
    )
    payout.full_clean()
    payout.save()
    return asset, payout


def upsert_fx_rate(data):
    effective_date = data.get('effective_date')
    pair = FXRate.objects.filter(
        from_currency=data['from_currency'].upper(),
        to_currency=data['to_currency'].upper(),
        effective_date=effective_date,
    ).first()
    if pair:
        for key, value in data.items():
            setattr(pair, key, value)
        pair.full_clean()
        pair.save()
        return pair

    pair = FXRate(**data)
    pair.full_clean()
    pair.save()
    return pair


@db_transaction.atomic
def create_transaction(data):
    payload = data.copy()
    payload['currency'] = payload.get('currency', 'USD').upper()
    payload['fee'] = payload.get('fee') or Decimal('0')
    payload['asset_quantity'] = payload.get('asset_quantity') or Decimal('0')
    transaction = Transaction(**payload)
    transaction.full_clean()
    transaction.save()

    if (
        transaction.transaction_type == Transaction.TransactionType.TRANSFER
        and transaction.status == Transaction.Status.COMPLETED
        and transaction.asset_id
        and transaction.destination_account_id
    ):
        asset = transaction.asset
        asset.account = transaction.destination_account
        asset.full_clean()
        asset.save(update_fields=['account', 'updated_at'])

    return transaction


def create_transfer(data):
    payload = data.copy()
    payload['transaction_type'] = Transaction.TransactionType.TRANSFER
    return create_transaction(payload)


def serialize_transaction(item):
    if has_system_note(item, CLOSE_ASSET_NOTE):
        transaction_type_display = 'Закрытие продукта'
    elif has_system_note(item, SYSTEM_DEPOSIT_NOTE):
        transaction_type_display = 'Открытие продукта'
    elif has_system_note(item, DEPOSIT_TOPUP_NOTE):
        transaction_type_display = 'Пополнение депозита'
    elif has_system_note(item, ACCOUNT_TOPUP_NOTE):
        transaction_type_display = 'Пополнение счета'
    elif has_system_note(item, DEPOSIT_PAYOUT_NOTE):
        transaction_type_display = 'Выплата процентов по депозиту'
    else:
        transaction_type_display = item.get_transaction_type_display()

    return {
        'id': item.id,
        'transaction_type': item.transaction_type,
        'transaction_type_display': transaction_type_display,
        'source_account': item.source_account.name if item.source_account else None,
        'destination_account': item.destination_account.name if item.destination_account else None,
        'asset': item.asset.symbol if item.asset else None,
        'asset_account': item.asset.account.name if item.asset and item.asset.account else None,
        'asset_quantity': str(item.asset_quantity),
        'unit_price': str(item.unit_price) if item.unit_price is not None else None,
        'amount': str(item.amount),
        'currency': item.currency,
        'fee': str(item.fee),
        'status': item.status,
        'occurred_at': item.occurred_at.isoformat(),
        'notes': item.notes,
    }