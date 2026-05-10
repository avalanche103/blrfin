from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from portfolio.models import Asset, DepositTopUp, FXRate, Transaction


PERCENT_QUANTIZER = Decimal('0.01')


def _to_decimal(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _get_fx_rate(source, target, *, on_date=None):
    queryset = FXRate.objects.filter(from_currency=source, to_currency=target)
    if on_date is not None:
        historical = queryset.filter(Q(effective_date__lte=on_date) | Q(effective_date__isnull=True)).order_by('-effective_date', '-updated_at').first()
        if historical:
            return historical
    return queryset.order_by('-effective_date', '-updated_at').first()


def convert_amount(amount, from_currency, to_currency, *, on_date=None):
    amount = _to_decimal(amount)
    if amount is None:
        return None

    source = (from_currency or settings.BASE_CURRENCY).upper()
    target = (to_currency or settings.BASE_CURRENCY).upper()
    if source == target:
        return amount

    direct_rate = _get_fx_rate(source, target, on_date=on_date)
    if direct_rate:
        return amount * direct_rate.rate

    reverse_rate = _get_fx_rate(target, source, on_date=on_date)
    if reverse_rate:
        return amount / reverse_rate.rate

    return None


def _active_asset_filter(on_date=None):
    if on_date is None:
        return Q(asset__closed_at__isnull=True)
    return Q(asset__closed_at__isnull=True) | Q(asset__closed_at__gt=on_date)


def _quantize_percent(value):
    if value is None:
        return None
    return value.quantize(PERCENT_QUANTIZER)


def _previous_month_end(value):
    return value.replace(day=1) - timedelta(days=1)


def _previous_year_end(value):
    return date(value.year - 1, 12, 31)


def _trailing_twelve_month_anchor(value):
    last_day = monthrange(value.year - 1, value.month)[1]
    return date(value.year - 1, value.month, last_day)


def _annualized_profitability_percent(profitability_base_value, invested_base_value, *, start_date, end_date):
    if (
        profitability_base_value is None
        or invested_base_value is None
        or invested_base_value <= 0
        or not start_date
        or not end_date
        or end_date <= start_date
    ):
        return None
    holding_days = Decimal((end_date - start_date).days)
    if holding_days <= 0:
        return None
    annualized = (profitability_base_value / invested_base_value) * (Decimal('365') / holding_days) * Decimal('100')
    return _quantize_percent(annualized)


def _portfolio_weighted_capital_base(start_value, *, start_date, end_date, flow_events=None):
    if (
        start_value is None
        or start_value <= 0
        or not start_date
        or not end_date
        or end_date <= start_date
    ):
        return None

    flow_events = flow_events or []
    total_days = Decimal((end_date - start_date).days)
    if total_days <= 0:
        return None

    weighted_base = start_value
    for flow_date, amount in flow_events:
        if amount is None:
            continue
        remaining_days = Decimal((end_date - flow_date).days)
        if remaining_days < 0:
            continue
        weighted_base += amount * (remaining_days / total_days)

    if weighted_base <= 0:
        return None
    return weighted_base


def _portfolio_return_percent(return_base_value, weighted_capital_base):
    if return_base_value is None or weighted_capital_base is None or weighted_capital_base <= 0:
        return None
    return _quantize_percent((return_base_value / weighted_capital_base) * Decimal('100'))


def _portfolio_component_percent(component_value, weighted_capital_base):
    if component_value is None or weighted_capital_base is None or weighted_capital_base <= 0:
        return None
    return _quantize_percent((component_value / weighted_capital_base) * Decimal('100'))


def _portfolio_return_breakdown(start_value, end_value, flow_events, income_component_value, *, start_date, end_date):
    if start_value is None or end_value is None:
        return {
            'return_pct': None,
            'return_base_value': None,
            'weighted_capital_base': None,
            'revaluation_return_pct': None,
            'revaluation_base_value': None,
            'income_return_pct': None,
            'income_component_base_value': income_component_value,
        }

    net_flow = sum((amount for _, amount in (flow_events or [])), Decimal('0'))
    return_base_value = end_value - start_value - net_flow
    income_component_value = income_component_value or Decimal('0')
    revaluation_base_value = return_base_value - income_component_value
    weighted_capital_base = _portfolio_weighted_capital_base(
        start_value,
        start_date=start_date,
        end_date=end_date,
        flow_events=flow_events,
    )

    return {
        'return_pct': _portfolio_return_percent(return_base_value, weighted_capital_base),
        'return_base_value': return_base_value,
        'weighted_capital_base': weighted_capital_base,
        'revaluation_return_pct': _portfolio_component_percent(revaluation_base_value, weighted_capital_base),
        'revaluation_base_value': revaluation_base_value,
        'income_return_pct': _portfolio_component_percent(income_component_value, weighted_capital_base),
        'income_component_base_value': income_component_value,
    }


def _asset_market_values(asset, quantity, base_currency, *, on_date=None):
    from .transactions import get_asset_price_on_date

    current_price = get_asset_price_on_date(asset, on_date=on_date)
    native_value = None
    base_value = None
    if current_price is not None and asset.price_currency:
        native_value = quantity * current_price
        base_value = convert_amount(native_value, asset.price_currency, base_currency, on_date=on_date)
    return current_price, native_value, base_value


def _deposit_income_component_base_value(asset, base_currency, *, on_date=None):
    from .transactions import build_deposit_capitalization_history, build_deposit_interest_payout_history

    if asset.asset_class != Asset.AssetClass.DEPOSIT:
        return Decimal('0')

    currency = asset.price_currency or (asset.account.currency if asset.account else None)
    if not currency:
        return Decimal('0')

    total = Decimal('0')
    if asset.deposit_interest_payout_method == Asset.InterestPayoutMethod.CAPITALIZATION:
        for row in build_deposit_capitalization_history(asset):
            capitalization_date = row['capitalization_date']
            if on_date is not None and capitalization_date > on_date:
                continue
            amount_base = convert_amount(row['interest_amount'], currency, base_currency, on_date=capitalization_date)
            if amount_base is not None:
                total += amount_base
        return total

    for row in build_deposit_interest_payout_history(asset):
        payout_date = row['operation_date']
        if on_date is not None and payout_date > on_date:
            continue
        amount_base = convert_amount(row['interest_amount'], currency, base_currency, on_date=payout_date)
        if amount_base is not None:
            total += amount_base
    return total


def _asset_profitability_breakdown(asset, *, market_value, invested_base_value, base_currency, on_date=None):
    if market_value is None or invested_base_value is None:
        return None, None, Decimal('0')

    income_component_base_value = _deposit_income_component_base_value(asset, base_currency, on_date=on_date)
    profitability_base_value = market_value - invested_base_value
    if asset.asset_class == Asset.AssetClass.DEPOSIT and asset.deposit_interest_payout_method == Asset.InterestPayoutMethod.TO_ACCOUNT:
        profitability_base_value += income_component_base_value

    revaluation_base_value = profitability_base_value - income_component_base_value
    return profitability_base_value, revaluation_base_value, income_component_base_value


def _convert_base_value_to_usd(value, *, base_currency, on_date=None):
    if value is None:
        return None
    return convert_amount(value, base_currency, 'USD', on_date=on_date)


def build_asset_positions(on_date=None):
    positions = defaultdict(lambda: Decimal('0'))
    transactions = Transaction.objects.filter(
        status=Transaction.Status.COMPLETED,
        asset__isnull=False,
    ).filter(_active_asset_filter(on_date)).select_related('asset')
    if on_date is not None:
        transactions = transactions.filter(occurred_at__date__lte=on_date)

    for item in transactions:
        quantity = item.asset_quantity or Decimal('0')
        if item.transaction_type in {Transaction.TransactionType.BUY, Transaction.TransactionType.INTEREST}:
            positions[item.asset_id] += quantity
        elif item.transaction_type == Transaction.TransactionType.DEPOSIT and item.asset_id:
            positions[item.asset_id] += quantity
        elif item.transaction_type == Transaction.TransactionType.SELL:
            positions[item.asset_id] -= quantity

    return positions


def build_asset_invested_base_values(base_currency=None, *, on_date=None):
    from .transactions import has_system_note, SYSTEM_DEPOSIT_NOTE, DEPOSIT_TOPUP_NOTE

    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    invested = defaultdict(lambda: Decimal('0'))

    transactions = Transaction.objects.filter(
        status=Transaction.Status.COMPLETED,
        asset__isnull=False,
    ).filter(_active_asset_filter(on_date)).select_related('asset')
    if on_date is not None:
        transactions = transactions.filter(occurred_at__date__lte=on_date)

    for item in transactions:
        base_amount = convert_amount(item.amount + (item.fee or Decimal('0')), item.currency, base_currency, on_date=item.occurred_at.date())
        if base_amount is None:
            continue

        if item.transaction_type == Transaction.TransactionType.BUY:
            invested[item.asset_id] += base_amount
        elif item.transaction_type == Transaction.TransactionType.SELL:
            invested[item.asset_id] -= base_amount
        elif item.transaction_type == Transaction.TransactionType.WITHDRAW and has_system_note(item, DEPOSIT_TOPUP_NOTE):
            invested[item.asset_id] += base_amount
        elif item.transaction_type == Transaction.TransactionType.DEPOSIT and has_system_note(item, SYSTEM_DEPOSIT_NOTE):
            invested[item.asset_id] += base_amount

    topups = DepositTopUp.objects.filter(cash_transaction__isnull=True).select_related('asset__account')
    if on_date is not None:
        topups = topups.filter(topup_date__lte=on_date)

    for topup in topups:
        currency = topup.asset.price_currency or (topup.asset.account.currency if topup.asset.account else None)
        base_amount = convert_amount(topup.amount, currency, base_currency, on_date=topup.topup_date)
        if base_amount is None:
            continue
        invested[topup.asset_id] += base_amount

    return invested


def build_asset_start_dates(*, on_date=None):
    start_dates = {}
    transactions = Transaction.objects.filter(
        status=Transaction.Status.COMPLETED,
        asset__isnull=False,
    ).filter(_active_asset_filter(on_date)).order_by('occurred_at', 'id')
    if on_date is not None:
        transactions = transactions.filter(occurred_at__date__lte=on_date)

    for asset_id, occurred_at in transactions.values_list('asset_id', 'occurred_at'):
        start_dates.setdefault(asset_id, occurred_at.date())

    return start_dates


def build_instrument_total_value(base_currency=None, *, on_date=None):
    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    positions = build_asset_positions(on_date=on_date)
    assets = Asset.objects.in_bulk(positions.keys())
    total_value = Decimal('0')

    for asset_id, quantity in positions.items():
        if not quantity:
            continue
        asset = assets.get(asset_id)
        if asset is None:
            continue
        _, _, base_value = _asset_market_values(asset, quantity, base_currency, on_date=on_date)
        if base_value is not None:
            total_value += base_value

    return total_value


def build_instrument_income_component(base_currency=None, *, on_date=None):
    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    total = Decimal('0')
    deposits = Asset.objects.filter(asset_class=Asset.AssetClass.DEPOSIT).select_related('account')
    if on_date is None:
        deposits = deposits.filter(closed_at__isnull=True)
    else:
        deposits = deposits.filter(Q(closed_at__isnull=True) | Q(closed_at__gt=on_date))

    for asset in deposits:
        total += _deposit_income_component_base_value(asset, base_currency, on_date=on_date)

    return total


def build_instrument_flow_events(base_currency=None, *, start_date, end_date):
    from .transactions import CLOSE_ASSET_NOTE, DEPOSIT_TOPUP_NOTE, SYSTEM_DEPOSIT_NOTE, build_deposit_interest_payout_history, has_system_note

    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    if not start_date or not end_date or end_date <= start_date:
        return []

    flow_events = []
    transactions = Transaction.objects.filter(
        status=Transaction.Status.COMPLETED,
        asset__isnull=False,
        occurred_at__date__gt=start_date,
        occurred_at__date__lte=end_date,
    ).select_related('asset')

    for item in transactions:
        amount_base = convert_amount(item.amount, item.currency, base_currency, on_date=item.occurred_at.date())
        fee_base = convert_amount(item.fee or Decimal('0'), item.currency, base_currency, on_date=item.occurred_at.date()) or Decimal('0')
        if amount_base is None:
            continue

        if item.transaction_type == Transaction.TransactionType.BUY:
            flow_events.append((item.occurred_at.date(), amount_base + fee_base))
        elif item.transaction_type == Transaction.TransactionType.SELL:
            flow_events.append((item.occurred_at.date(), -(amount_base - fee_base)))
        elif item.transaction_type == Transaction.TransactionType.DEPOSIT and has_system_note(item, SYSTEM_DEPOSIT_NOTE):
            flow_events.append((item.occurred_at.date(), amount_base))
        elif item.transaction_type == Transaction.TransactionType.DEPOSIT and has_system_note(item, CLOSE_ASSET_NOTE):
            flow_events.append((item.occurred_at.date(), -amount_base))
        elif item.transaction_type == Transaction.TransactionType.WITHDRAW and has_system_note(item, DEPOSIT_TOPUP_NOTE):
            flow_events.append((item.occurred_at.date(), amount_base))

    topups = DepositTopUp.objects.filter(
        cash_transaction__isnull=True,
        topup_date__gt=start_date,
        topup_date__lte=end_date,
    ).select_related('asset__account')
    for topup in topups:
        currency = topup.asset.price_currency or (topup.asset.account.currency if topup.asset.account else None)
        amount_base = convert_amount(topup.amount, currency, base_currency, on_date=topup.topup_date)
        if amount_base is not None:
            flow_events.append((topup.topup_date, amount_base))

    deposits = Asset.objects.filter(
        asset_class=Asset.AssetClass.DEPOSIT,
        deposit_interest_payout_method=Asset.InterestPayoutMethod.TO_ACCOUNT,
    ).select_related('account')
    for asset in deposits:
        currency = asset.price_currency or (asset.account.currency if asset.account else None)
        if not currency:
            continue
        for row in build_deposit_interest_payout_history(asset):
            payout_date = row['operation_date']
            if payout_date <= start_date or payout_date > end_date:
                continue
            amount_base = convert_amount(row['interest_amount'], currency, base_currency, on_date=payout_date)
            if amount_base is not None:
                flow_events.append((payout_date, -amount_base))

    return flow_events


def build_instrument_net_flow(base_currency=None, *, start_date, end_date):
    return sum(
        (amount for _, amount in build_instrument_flow_events(base_currency, start_date=start_date, end_date=end_date)),
        Decimal('0'),
    )


def build_portfolio_performance(base_currency=None, *, end_date=None):
    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    end_date = end_date or timezone.localdate()
    current_value = build_instrument_total_value(base_currency, on_date=end_date)
    current_income_component = build_instrument_income_component(base_currency, on_date=end_date)
    previous_month_end = _previous_month_end(end_date)
    previous_year_end = _previous_year_end(end_date)
    trailing_twelve_month_anchor = _trailing_twelve_month_anchor(end_date)

    month_start_value = build_instrument_total_value(base_currency, on_date=previous_month_end)
    year_start_value = build_instrument_total_value(base_currency, on_date=previous_year_end)
    trailing_twelve_month_start_value = build_instrument_total_value(base_currency, on_date=trailing_twelve_month_anchor)
    month_start_income_component = build_instrument_income_component(base_currency, on_date=previous_month_end)
    year_start_income_component = build_instrument_income_component(base_currency, on_date=previous_year_end)
    trailing_twelve_month_start_income_component = build_instrument_income_component(base_currency, on_date=trailing_twelve_month_anchor)
    month_flows = build_instrument_flow_events(base_currency, start_date=previous_month_end, end_date=end_date)
    year_flows = build_instrument_flow_events(base_currency, start_date=previous_year_end, end_date=end_date)
    trailing_twelve_month_flows = build_instrument_flow_events(base_currency, start_date=trailing_twelve_month_anchor, end_date=end_date)
    month_breakdown = _portfolio_return_breakdown(
        month_start_value,
        current_value,
        month_flows,
        current_income_component - month_start_income_component,
        start_date=previous_month_end,
        end_date=end_date,
    )
    year_breakdown = _portfolio_return_breakdown(
        year_start_value,
        current_value,
        year_flows,
        current_income_component - year_start_income_component,
        start_date=previous_year_end,
        end_date=end_date,
    )
    trailing_twelve_month_breakdown = _portfolio_return_breakdown(
        trailing_twelve_month_start_value,
        current_value,
        trailing_twelve_month_flows,
        current_income_component - trailing_twelve_month_start_income_component,
        start_date=trailing_twelve_month_anchor,
        end_date=end_date,
    )

    month_breakdown['return_usd_value'] = _convert_base_value_to_usd(
        month_breakdown['return_base_value'],
        base_currency=base_currency,
        on_date=end_date,
    )
    year_breakdown['return_usd_value'] = _convert_base_value_to_usd(
        year_breakdown['return_base_value'],
        base_currency=base_currency,
        on_date=end_date,
    )
    trailing_twelve_month_breakdown['return_usd_value'] = _convert_base_value_to_usd(
        trailing_twelve_month_breakdown['return_base_value'],
        base_currency=base_currency,
        on_date=end_date,
    )

    return {
        'previous_month_end': {
            'date': previous_month_end,
            'value': month_start_value,
            **month_breakdown,
        },
        'previous_year_end': {
            'date': previous_year_end,
            'value': year_start_value,
            **year_breakdown,
        },
        'trailing_twelve_months': {
            'date': trailing_twelve_month_anchor,
            'value': trailing_twelve_month_start_value,
            **trailing_twelve_month_breakdown,
        },
    }


def build_portfolio_snapshot(base_currency=None, account_rows=None):
    from .accounts import build_account_rows

    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    as_of_date = timezone.localdate()
    account_rows = account_rows if account_rows is not None else build_account_rows(base_currency)
    positions = build_asset_positions(on_date=as_of_date)
    invested_base_values = build_asset_invested_base_values(base_currency, on_date=as_of_date)
    asset_start_dates = build_asset_start_dates(on_date=as_of_date)
    assets = Asset.objects.in_bulk(positions.keys())

    rows = []
    summary = defaultdict(lambda: Decimal('0'))

    for asset_id, quantity in positions.items():
        if not quantity:
            continue
        asset = assets[asset_id]
        current_price, native_value, market_value = _asset_market_values(asset, quantity, base_currency, on_date=as_of_date)
        invested_base_value = invested_base_values.get(asset.id)
        profitability_base_value, revaluation_base_value, income_component_base_value = _asset_profitability_breakdown(
            asset,
            market_value=market_value,
            invested_base_value=invested_base_value,
            base_currency=base_currency,
            on_date=as_of_date,
        )
        annualized_return_pct = _annualized_profitability_percent(
            profitability_base_value,
            invested_base_value,
            start_date=asset_start_dates.get(asset.id),
            end_date=as_of_date,
        )
        revaluation_return_pct = _annualized_profitability_percent(
            revaluation_base_value,
            invested_base_value,
            start_date=asset_start_dates.get(asset.id),
            end_date=as_of_date,
        )
        income_return_pct = _annualized_profitability_percent(
            income_component_base_value,
            invested_base_value,
            start_date=asset_start_dates.get(asset.id),
            end_date=as_of_date,
        )

        row = {
            'kind': 'asset',
            'asset_id': asset.id,
            'label': asset.symbol,
            'name': asset.name,
            'account_name': asset.account.name if asset.account else '-',
            'asset_class': asset.get_asset_class_display(),
            'currency': asset.price_currency or '',
            'quantity': quantity,
            'unit_price': current_price,
            'native_value': native_value,
            'invested_base_value': invested_base_value,
            'profitability_base_value': profitability_base_value,
            'profitability_usd_value': _convert_base_value_to_usd(
                profitability_base_value,
                base_currency=base_currency,
                on_date=as_of_date,
            ),
            'annualized_return_pct': annualized_return_pct,
            'revaluation_base_value': revaluation_base_value,
            'revaluation_return_pct': revaluation_return_pct,
            'income_component_base_value': income_component_base_value,
            'income_return_pct': income_return_pct,
            'base_value': market_value,
        }
        rows.append(row)
        if market_value is not None:
            summary[row['asset_class']] += market_value

    for account_row in account_rows:
        account = account_row['account']
        available_base_balance = account_row.get('available_base_balance')
        if available_base_balance is None:
            continue
        summary[account.get_account_type_display()] += available_base_balance

    rows.sort(key=lambda item: (item['asset_class'], item['label']))
    summary_rows = [{'asset_class': key, 'base_value': value} for key, value in summary.items()]
    assets_total_value = sum((item['base_value'] or Decimal('0')) for item in rows)
    cash_total_value = sum((item['available_base_balance'] or Decimal('0')) for item in account_rows)
    total_value = assets_total_value + cash_total_value
    performance = build_portfolio_performance(base_currency, end_date=as_of_date)
    return {
        'rows': rows,
        'summary': summary_rows,
        'assets_total_value': assets_total_value,
        'cash_total_value': cash_total_value,
        'total_value': total_value,
        'performance': performance,
        'base_currency': base_currency,
    }