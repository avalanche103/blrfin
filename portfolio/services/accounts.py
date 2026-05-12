from datetime import datetime, time
from decimal import Decimal
from types import SimpleNamespace

from django.conf import settings
from django.utils import timezone

from django.db import transaction as db_transaction

from portfolio.models import Account, Asset, DepositTopUp, Transaction

from .portfolio import convert_amount
from .transactions import ACCOUNT_TOPUP_NOTE, CLOSE_ASSET_NOTE, DEPOSIT_PAYOUT_NOTE, DEPOSIT_TOPUP_NOTE, SYSTEM_DEPOSIT_NOTE, build_deposit_capitalization_history, build_recorded_deposit_interest_payout_history, has_system_note


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


def _as_operation_item(*, occurred_at, display_type, asset=None, source_account=None, destination_account=None, amount=Decimal('0'), currency='', fee=Decimal('0')):
    return SimpleNamespace(
        occurred_at=occurred_at,
        display_type=display_type,
        asset=asset,
        source_account=source_account,
        destination_account=destination_account,
        amount=amount,
        currency=currency,
        fee=fee,
        get_status_display=lambda: 'Исполнено',
    )


def _deposit_topup_history_items():
    items = []
    queryset = DepositTopUp.objects.filter(cash_transaction__isnull=True).select_related('asset', 'source_account')
    for topup in queryset:
        occurred_at = timezone.make_aware(datetime.combine(topup.topup_date, time.min), timezone.get_current_timezone())
        items.append(
            _as_operation_item(
                occurred_at=occurred_at,
                display_type='Пополнение депозита',
                asset=topup.asset,
                source_account=topup.source_account,
                amount=topup.amount,
                currency=topup.asset.price_currency or (topup.asset.account.currency if topup.asset.account else ''),
            )
        )
    return items


def _deposit_capitalization_history_items():
    items = []
    deposits = Asset.objects.filter(asset_class=Asset.AssetClass.DEPOSIT).select_related('account')
    for asset in deposits:
        for row in build_deposit_capitalization_history(asset):
            occurred_at = timezone.make_aware(datetime.combine(row['capitalization_date'], time.min), timezone.get_current_timezone())
            items.append(
                _as_operation_item(
                    occurred_at=occurred_at,
                    display_type='Капитализация депозита',
                    asset=asset,
                    amount=row['interest_amount'],
                    currency=asset.price_currency or (asset.account.currency if asset.account else ''),
                )
            )
    return items


def _deposit_interest_payout_history_items():
    items = []
    deposits = Asset.objects.filter(asset_class=Asset.AssetClass.DEPOSIT).select_related('account')
    for asset in deposits:
        for row in build_recorded_deposit_interest_payout_history(asset):
            if row.get('cash_transaction_id'):
                continue
            occurred_at = timezone.make_aware(datetime.combine(row['operation_date'], time.min), timezone.get_current_timezone())
            items.append(
                _as_operation_item(
                    occurred_at=occurred_at,
                    display_type='Выплата процентов по депозиту',
                    asset=asset,
                    destination_account=asset.account,
                    amount=row['interest_amount'],
                    currency=asset.price_currency or (asset.account.currency if asset.account else ''),
                )
            )
    return items


def list_transactions(limit=25):
    queryset = Transaction.objects.select_related('source_account', 'destination_account', 'asset')
    items = list(queryset)
    for item in items:
        if has_system_note(item, CLOSE_ASSET_NOTE):
            item.display_type = 'Закрытие продукта'
        elif has_system_note(item, SYSTEM_DEPOSIT_NOTE):
            item.display_type = 'Открытие продукта'
        elif has_system_note(item, DEPOSIT_TOPUP_NOTE):
            item.display_type = 'Пополнение депозита'
        elif has_system_note(item, DEPOSIT_PAYOUT_NOTE):
            item.display_type = 'Выплата процентов по депозиту'
        elif has_system_note(item, ACCOUNT_TOPUP_NOTE):
            item.display_type = 'Пополнение счета'
        else:
            item.display_type = item.get_transaction_type_display()

    items.extend(_deposit_topup_history_items())
    items.extend(_deposit_capitalization_history_items())
    items.extend(_deposit_interest_payout_history_items())
    items.sort(key=lambda item: item.occurred_at, reverse=True)
    return items[:limit] if limit else items


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