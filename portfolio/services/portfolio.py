from collections import defaultdict
from decimal import Decimal

from django.conf import settings

from portfolio.models import Asset, FXRate, Transaction


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


def build_asset_positions():
    positions = defaultdict(lambda: Decimal('0'))
    transactions = Transaction.objects.filter(
        status=Transaction.Status.COMPLETED,
        asset__isnull=False,
        asset__closed_at__isnull=True,
    ).select_related('asset')

    for item in transactions:
        quantity = item.asset_quantity or Decimal('0')
        if item.transaction_type in {Transaction.TransactionType.BUY, Transaction.TransactionType.INTEREST}:
            positions[item.asset_id] += quantity
        elif item.transaction_type == Transaction.TransactionType.DEPOSIT and item.asset_id:
            positions[item.asset_id] += quantity
        elif item.transaction_type == Transaction.TransactionType.SELL:
            positions[item.asset_id] -= quantity

    return positions


def build_portfolio_snapshot(base_currency=None, account_rows=None):
    from .accounts import build_account_rows
    from .transactions import get_current_asset_price

    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    account_rows = account_rows if account_rows is not None else build_account_rows(base_currency)
    positions = build_asset_positions()
    assets = Asset.objects.in_bulk(positions.keys())

    rows = []
    summary = defaultdict(lambda: Decimal('0'))

    for asset_id, quantity in positions.items():
        if not quantity:
            continue
        asset = assets[asset_id]
        current_price = get_current_asset_price(asset)
        market_value = None
        if current_price is not None and asset.price_currency:
            market_value = convert_amount(quantity * current_price, asset.price_currency, base_currency)

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
            'base_value': market_value,
        }
        rows.append(row)
        if market_value is not None:
            summary[row['asset_class']] += market_value

    for account_row in account_rows:
        row = {
            'kind': 'cash',
            'asset_id': None,
            'label': account_row['account'].name,
            'name': account_row['account'].get_account_type_display(),
            'account_name': account_row['account'].name,
            'asset_class': 'Наличные',
            'currency': account_row['account'].currency,
            'quantity': account_row['available_balance'],
            'unit_price': Decimal('1'),
            'base_value': account_row['available_base_balance'],
        }
        rows.append(row)
        if row['base_value'] is not None:
            summary[row['asset_class']] += row['base_value']

    rows.sort(key=lambda item: (item['asset_class'], item['label']))
    summary_rows = [{'asset_class': key, 'base_value': value} for key, value in summary.items()]
    total_value = sum((item['base_value'] or Decimal('0')) for item in rows)
    return {
        'rows': rows,
        'summary': summary_rows,
        'total_value': total_value,
        'base_currency': base_currency,
    }