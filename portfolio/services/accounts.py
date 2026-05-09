from decimal import Decimal

from django.conf import settings

from django.db import transaction as db_transaction

from portfolio.models import Account, DepositTopUp, Transaction

from .portfolio import convert_amount
from .transactions import ACCOUNT_TOPUP_NOTE, CLOSE_ASSET_NOTE, DEPOSIT_TOPUP_NOTE, SYSTEM_DEPOSIT_NOTE, has_system_note


SYSTEM_DEPOSIT_NOTE = '__system_deposit_position__'


def _completed_transactions():
    return Transaction.objects.filter(status=Transaction.Status.COMPLETED).select_related(
        'source_account', 'destination_account', 'asset'
    )


def build_account_rows(base_currency=None):
    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    rows_by_id = {
        account.id: {
            'account': account,
            'ledger_balance': Decimal(account.opening_balance),
            'fees_paid': Decimal('0'),
            'base_balance': None,
            'available_balance': Decimal('0'),
            'available_base_balance': None,
        }
        for account in Account.objects.all().order_by('name')
    }

    for item in _completed_transactions():
        if has_system_note(item, SYSTEM_DEPOSIT_NOTE):
            continue

        if item.source_account_id and item.source_account_id in rows_by_id:
            rows_by_id[item.source_account_id]['ledger_balance'] -= item.amount + item.fee
            rows_by_id[item.source_account_id]['fees_paid'] += item.fee
            if item.transaction_type == Transaction.TransactionType.FEE:
                rows_by_id[item.source_account_id]['fees_paid'] += item.amount

        if item.destination_account_id and item.destination_account_id in rows_by_id:
            rows_by_id[item.destination_account_id]['ledger_balance'] += item.amount

    rows = []
    for row in rows_by_id.values():
        row['available_balance'] = row['ledger_balance']
        row['base_balance'] = convert_amount(row['ledger_balance'], row['account'].currency, base_currency)
        row['available_base_balance'] = convert_amount(row['available_balance'], row['account'].currency, base_currency)
        rows.append(row)
    return rows


def list_transfers(limit=25):
    queryset = Transaction.objects.filter(transaction_type=Transaction.TransactionType.TRANSFER).select_related(
        'source_account', 'destination_account'
    )
    return queryset[:limit] if limit else queryset


def list_transactions(limit=25):
    queryset = Transaction.objects.select_related('source_account', 'destination_account', 'asset')
    items = list(queryset[:limit] if limit else queryset)
    for item in items:
        if has_system_note(item, CLOSE_ASSET_NOTE):
            item.display_type = 'Закрытие продукта'
        elif has_system_note(item, SYSTEM_DEPOSIT_NOTE):
            item.display_type = 'Открытие продукта'
        elif has_system_note(item, DEPOSIT_TOPUP_NOTE):
            item.display_type = 'Пополнение депозита'
        elif has_system_note(item, ACCOUNT_TOPUP_NOTE):
            item.display_type = 'Пополнение счета'
        else:
            item.display_type = item.get_transaction_type_display()
    return items


@db_transaction.atomic
def delete_account(account):
    active_assets = account.assets.filter(closed_at__isnull=True)
    if active_assets.exists():
        raise ValueError('Нельзя удалить счет: к нему привязан незакрытый продукт.')

    account.assets.filter(closed_at__isnull=False).update(account=None)
    Transaction.objects.filter(source_account=account).update(source_account=None)
    Transaction.objects.filter(destination_account=account).update(destination_account=None)
    DepositTopUp.objects.filter(source_account=account).update(source_account=None)
    account.delete()