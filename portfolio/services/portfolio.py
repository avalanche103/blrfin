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


def convert_amount(amount, from_currency, to_currency):
    amount = _to_decimal(amount)
    if amount is None:
        return None

    source = (from_currency or settings.BASE_CURRENCY).upper()
    target = (to_currency or settings.BASE_CURRENCY).upper()
    if source == target:
        return amount

    direct_rate = FXRate.objects.filter(from_currency=source, to_currency=target).first()
    if direct_rate:
        return amount * direct_rate.rate

    reverse_rate = FXRate.objects.filter(from_currency=target, to_currency=source).first()
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


def _portfolio_return_percent(start_value, end_value, net_flow):
    if start_value is None or start_value <= 0 or end_value is None:
        return None
    return _quantize_percent(((end_value - start_value - net_flow) / start_value) * Decimal('100'))


def _asset_market_values(asset, quantity, base_currency, *, on_date=None):
    from .transactions import get_asset_price_on_date

    current_price = get_asset_price_on_date(asset, on_date=on_date)
    native_value = None
    base_value = None
    if current_price is not None and asset.price_currency:
        native_value = quantity * current_price
        base_value = convert_amount(native_value, asset.price_currency, base_currency)
    return current_price, native_value, base_value


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
        base_amount = convert_amount(item.amount + (item.fee or Decimal('0')), item.currency, base_currency)
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
        base_amount = convert_amount(topup.amount, currency, base_currency)
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


def build_instrument_net_flow(base_currency=None, *, start_date, end_date):
    from .transactions import CLOSE_ASSET_NOTE, DEPOSIT_TOPUP_NOTE, SYSTEM_DEPOSIT_NOTE, has_system_note

    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    if not start_date or not end_date or end_date <= start_date:
        return Decimal('0')

    net_flow = Decimal('0')
    transactions = Transaction.objects.filter(
        status=Transaction.Status.COMPLETED,
        asset__isnull=False,
        occurred_at__date__gt=start_date,
        occurred_at__date__lte=end_date,
    ).select_related('asset')

    for item in transactions:
        amount_base = convert_amount(item.amount, item.currency, base_currency)
        fee_base = convert_amount(item.fee or Decimal('0'), item.currency, base_currency) or Decimal('0')
        if amount_base is None:
            continue

        if item.transaction_type == Transaction.TransactionType.BUY:
            net_flow += amount_base + fee_base
        elif item.transaction_type == Transaction.TransactionType.SELL:
            net_flow -= amount_base - fee_base
        elif item.transaction_type == Transaction.TransactionType.DEPOSIT and has_system_note(item, SYSTEM_DEPOSIT_NOTE):
            net_flow += amount_base
        elif item.transaction_type == Transaction.TransactionType.DEPOSIT and has_system_note(item, CLOSE_ASSET_NOTE):
            net_flow -= amount_base
        elif item.transaction_type == Transaction.TransactionType.WITHDRAW and has_system_note(item, DEPOSIT_TOPUP_NOTE):
            net_flow += amount_base

    topups = DepositTopUp.objects.filter(
        cash_transaction__isnull=True,
        topup_date__gt=start_date,
        topup_date__lte=end_date,
    ).select_related('asset__account')
    for topup in topups:
        currency = topup.asset.price_currency or (topup.asset.account.currency if topup.asset.account else None)
        amount_base = convert_amount(topup.amount, currency, base_currency)
        if amount_base is not None:
            net_flow += amount_base

    return net_flow


def build_portfolio_performance(base_currency=None, *, end_date=None):
    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    end_date = end_date or timezone.localdate()
    current_value = build_instrument_total_value(base_currency, on_date=end_date)
    previous_month_end = _previous_month_end(end_date)
    previous_year_end = _previous_year_end(end_date)

    month_start_value = build_instrument_total_value(base_currency, on_date=previous_month_end)
    year_start_value = build_instrument_total_value(base_currency, on_date=previous_year_end)
    month_flow = build_instrument_net_flow(base_currency, start_date=previous_month_end, end_date=end_date)
    year_flow = build_instrument_net_flow(base_currency, start_date=previous_year_end, end_date=end_date)

    return {
        'previous_month_end': {
            'date': previous_month_end,
            'value': month_start_value,
            'return_pct': _portfolio_return_percent(month_start_value, current_value, month_flow),
        },
        'previous_year_end': {
            'date': previous_year_end,
            'value': year_start_value,
            'return_pct': _portfolio_return_percent(year_start_value, current_value, year_flow),
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
        profitability_base_value = None
        if market_value is not None and invested_base_value is not None:
            profitability_base_value = market_value - invested_base_value
        annualized_return_pct = _annualized_profitability_percent(
            profitability_base_value,
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
            'annualized_return_pct': annualized_return_pct,
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