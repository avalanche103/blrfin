from calendar import monthrange
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils.dateparse import parse_date
from django.utils import timezone
from django.db import transaction as db_transaction

from portfolio.models import Asset, DepositCapitalizationAdjustment, DepositTopUp, FXRate, Transaction


SYSTEM_DEPOSIT_NOTE = '__system_deposit_position__'
CLOSE_ASSET_NOTE = '__asset_close_payout__'
DEPOSIT_TOPUP_NOTE = '__deposit_topup__'
ACCOUNT_TOPUP_NOTE = '__account_topup__'


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
        step = 1
        while True:
            candidate = asset.deposit_open_date + timedelta(days=15 * step)
            if candidate > effective_end:
                break
            dates.append(candidate)
            step += 1
        return dates

    step = 1
    while True:
        candidate = _add_months(asset.deposit_open_date, step)
        if candidate > effective_end:
            break
        dates.append(candidate)
        step += 1
    return dates


def _simulate_deposit_capitalization(asset):
    principal = _quantize_money(asset.deposit_initial_amount or Decimal('0'))
    if principal <= 0 or not asset.deposit_open_date or not asset.deposit_annual_rate:
        return principal, []

    effective_end = _deposit_effective_end_date(asset)
    if effective_end <= asset.deposit_open_date:
        return principal, []

    balance = principal
    rate_per_day = (asset.deposit_annual_rate / Decimal('100')) / Decimal('365')
    capitalization_dates = set(_capitalization_dates(asset, effective_end))
    topups_by_date = _deposit_topups_by_date(asset, effective_end)
    adjustments_by_date = _capitalization_adjustments_by_date(asset)

    history = []
    current_period_start = asset.deposit_open_date
    opening_balance = balance
    period_topups = Decimal('0')
    period_daily_balance_total = Decimal('0')
    current_date = asset.deposit_open_date

    while current_date <= effective_end:
        if current_date in capitalization_dates:
            computed_interest_amount = _quantize_money(period_daily_balance_total * rate_per_day)
            adjustment = adjustments_by_date.get(current_date)
            interest_amount = adjustment.interest_amount if adjustment else computed_interest_amount
            balance_before = _quantize_money(balance)
            balance_after = _quantize_money(balance_before + interest_amount)
            history.append(
                {
                    'capitalization_date': current_date,
                    'period_start': current_period_start,
                    'opening_balance': _quantize_money(opening_balance),
                    'topups_amount': _quantize_money(period_topups),
                    'daily_balance_total': _quantize_money(period_daily_balance_total),
                    'computed_interest_amount': computed_interest_amount,
                    'interest_amount': interest_amount,
                    'balance_after': balance_after,
                    'is_adjusted': bool(adjustment),
                    'adjustment_id': adjustment.id if adjustment else None,
                    'adjustment_notes': adjustment.notes if adjustment else '',
                }
            )
            balance = balance_after
            current_period_start = current_date
            opening_balance = balance
            period_topups = Decimal('0')
            period_daily_balance_total = Decimal('0')

        topup_amount = topups_by_date.get(current_date, Decimal('0'))
        if topup_amount:
            balance += topup_amount
            period_topups += topup_amount

        if current_date == current_period_start:
            opening_balance = balance

        if current_date < effective_end:
            period_daily_balance_total += balance

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


def get_current_asset_price(asset):
    if asset.asset_class == Asset.AssetClass.DEPOSIT:
        return calculate_deposit_current_amount(asset)
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
    payload = _prepare_asset_payload(data)
    asset = Asset(**payload)
    asset.full_clean()
    asset.save()
    _sync_deposit_position(asset)
    return asset


@db_transaction.atomic
def update_asset(asset, data):
    payload = _prepare_asset_payload(data, existing_asset=asset)
    for key, value in payload.items():
        setattr(asset, key, value)
    asset.full_clean()
    asset.save()
    _sync_deposit_position(asset)
    return asset


@db_transaction.atomic
def add_deposit_topup(asset, *, source_account, topup_date, amount):
    topup = DepositTopUp(asset=asset, source_account=source_account, topup_date=topup_date, amount=amount)
    topup.full_clean()
    topup.save()
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
def update_deposit_topup(topup, *, source_account, topup_date, amount):
    transaction = topup.cash_transaction
    old_asset = topup.asset
    topup.source_account = source_account
    topup.topup_date = topup_date
    topup.amount = amount
    topup.full_clean()
    topup.save()

    if transaction:
        transaction.source_account = source_account
        transaction.amount = amount
        transaction.currency = old_asset.price_currency or source_account.currency
        transaction.occurred_at = _topup_datetime(topup_date)
        transaction.full_clean()
        transaction.save()

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
    pair = FXRate.objects.filter(
        from_currency=data['from_currency'].upper(),
        to_currency=data['to_currency'].upper(),
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