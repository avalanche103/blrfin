import csv
import io
from decimal import Decimal

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from portfolio.models import Account, Asset, Transaction

from .transactions import create_transaction


def _decimal_or_zero(value):
    if value in (None, ''):
        return Decimal('0')
    return Decimal(str(value))


def _parse_datetime(value):
    if not value:
        return timezone.now()
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError('Неверный формат даты, используйте ISO datetime.')
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _resolve_account(name):
    if not name:
        return None
    return Account.objects.get(name=name)


def _resolve_asset(row, source_account=None, destination_account=None):
    symbol = (row.get('asset_symbol') or '').strip().upper()
    if not symbol:
        return None

    asset = Asset.objects.filter(symbol=symbol).first()
    if asset:
        return asset

    return Asset.objects.create(
        symbol=symbol,
        name=row.get('asset_name') or symbol,
        asset_class=row.get('asset_class') or Asset.AssetClass.OTHER,
        account=destination_account or source_account,
        price_currency=(row.get('price_currency') or '').upper(),
        current_price=_decimal_or_zero(row.get('current_price')) if row.get('current_price') else None,
    )


def import_transactions_from_csv(uploaded_file):
    content = uploaded_file.read().decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(content))
    created = []
    errors = []

    for line_number, row in enumerate(reader, start=2):
        try:
            source_account = _resolve_account(row.get('source_account'))
            destination_account = _resolve_account(row.get('destination_account'))
            transaction = create_transaction(
                {
                    'transaction_type': (row.get('transaction_type') or Transaction.TransactionType.DEPOSIT).lower(),
                    'source_account': source_account,
                    'destination_account': destination_account,
                    'asset': _resolve_asset(row, source_account=source_account, destination_account=destination_account),
                    'asset_quantity': _decimal_or_zero(row.get('asset_quantity')),
                    'unit_price': _decimal_or_zero(row.get('unit_price')) if row.get('unit_price') else None,
                    'amount': _decimal_or_zero(row.get('amount')),
                    'currency': (row.get('currency') or 'USD').upper(),
                    'fee': _decimal_or_zero(row.get('fee')),
                    'status': (row.get('status') or Transaction.Status.COMPLETED).lower(),
                    'occurred_at': _parse_datetime(row.get('occurred_at')),
                    'notes': row.get('notes') or '',
                }
            )
            created.append(transaction)
        except Exception as exc:
            errors.append(f'Строка {line_number}: {exc}')

    return {
        'created_count': len(created),
        'errors': errors,
    }