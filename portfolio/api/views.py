import json

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_http_methods

from portfolio.models import Account, Asset, FXRate, Transaction
from portfolio.services.accounts import build_account_rows, list_transfers
from portfolio.services.portfolio import build_portfolio_snapshot
from portfolio.services.transactions import create_transaction, serialize_transaction


def _json_error(message, status=400):
    return JsonResponse({'error': message}, status=status)


def _load_json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except json.JSONDecodeError:
        return None


def _parse_occurred_at(value):
    if not value:
        return timezone.now()
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError('Неверный формат occurred_at, используйте ISO datetime.')
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


@require_GET
def accounts_api(request):
    rows = build_account_rows(settings.BASE_CURRENCY)
    payload = [
        {
            'id': row['account'].id,
            'name': row['account'].name,
            'description': row['account'].description,
            'account_type': row['account'].account_type,
            'currency': row['account'].currency,
            'opening_balance': str(row['account'].opening_balance),
            'ledger_balance': str(row['ledger_balance']),
            'available_balance': str(row['available_balance']),
            'fees_paid': str(row['fees_paid']),
            'available_base_balance': str(row['available_base_balance']) if row['available_base_balance'] is not None else None,
        }
        for row in rows
    ]
    return JsonResponse(payload, safe=False)


@require_GET
def transfers_api(request):
    payload = [serialize_transaction(item) for item in list_transfers(limit=None)]
    return JsonResponse(payload, safe=False)


@require_GET
def portfolio_api(request):
    snapshot = build_portfolio_snapshot(settings.BASE_CURRENCY)
    return JsonResponse(
        {
            'base_currency': snapshot['base_currency'],
            'total_value': str(snapshot['total_value']),
            'rows': [
                {
                    'kind': row['kind'],
                    'asset_id': row['asset_id'],
                    'label': row['label'],
                    'name': row['name'],
                    'account_name': row['account_name'],
                    'asset_class': row['asset_class'],
                    'currency': row['currency'],
                    'quantity': str(row['quantity']),
                    'unit_price': str(row['unit_price']) if row['unit_price'] is not None else None,
                    'base_value': str(row['base_value']) if row['base_value'] is not None else None,
                }
                for row in snapshot['rows']
            ],
            'summary': [
                {'asset_class': row['asset_class'], 'base_value': str(row['base_value'])}
                for row in snapshot['summary']
            ],
        }
    )


@require_GET
def assets_api(request):
    payload = [
        {
            'id': asset.id,
            'symbol': asset.symbol,
            'name': asset.name,
            'asset_class': asset.asset_class,
            'account_id': asset.account_id,
            'account_name': asset.account.name if asset.account else None,
            'deposit_revocability': asset.deposit_revocability,
            'deposit_term_type': asset.deposit_term_type,
            'deposit_term_end': asset.deposit_term_end.isoformat() if asset.deposit_term_end else None,
            'deposit_open_date': asset.deposit_open_date.isoformat() if asset.deposit_open_date else None,
            'deposit_annual_rate': str(asset.deposit_annual_rate) if asset.deposit_annual_rate is not None else None,
            'deposit_interest_payout_method': asset.deposit_interest_payout_method,
            'deposit_initial_amount': str(asset.deposit_initial_amount) if asset.deposit_initial_amount is not None else None,
            'deposit_interest_payout_frequency': asset.deposit_interest_payout_frequency,
            'deposit_weekend_rollover': asset.deposit_weekend_rollover,
            'topups': [
                {
                    'id': topup.id,
                    'source_account_id': topup.source_account_id,
                    'source_account_name': topup.source_account.name if topup.source_account else None,
                    'topup_date': topup.topup_date.isoformat(),
                    'amount': str(topup.amount),
                }
                for topup in asset.topups.select_related('source_account').all()
            ],
            'closed_at': asset.closed_at.isoformat() if asset.closed_at else None,
            'current_price': str(asset.current_price) if asset.current_price is not None else None,
            'price_currency': asset.price_currency,
            'updated_at': asset.updated_at.isoformat(),
        }
        for asset in Asset.objects.select_related('account').all().order_by('symbol')
    ]
    return JsonResponse(payload, safe=False)


@require_http_methods(['POST'])
def transactions_api(request):
    payload = _load_json_body(request)
    if payload is None:
        return _json_error('Ожидается JSON body.')

    try:
        source_account = None
        destination_account = None
        asset = None
        if payload.get('source_account_id'):
            source_account = Account.objects.get(pk=payload['source_account_id'])
        if payload.get('destination_account_id'):
            destination_account = Account.objects.get(pk=payload['destination_account_id'])
        if payload.get('asset_id'):
            asset = Asset.objects.get(pk=payload['asset_id'])

        transaction = create_transaction(
            {
                'transaction_type': payload.get('transaction_type'),
                'source_account': source_account,
                'destination_account': destination_account,
                'asset': asset,
                'asset_quantity': payload.get('asset_quantity'),
                'unit_price': payload.get('unit_price'),
                'amount': payload.get('amount'),
                'currency': payload.get('currency', settings.BASE_CURRENCY),
                'fee': payload.get('fee', '0'),
                'status': payload.get('status', Transaction.Status.COMPLETED),
                'occurred_at': _parse_occurred_at(payload.get('occurred_at')),
                'notes': payload.get('notes', ''),
            }
        )
        return JsonResponse(serialize_transaction(transaction), status=201)
    except Exception as exc:
        return _json_error(str(exc))


@require_GET
def prices_api(request):
    payload = {
        'assets': [
            {
                'symbol': asset.symbol,
                'current_price': str(asset.current_price) if asset.current_price is not None else None,
                'price_currency': asset.price_currency,
            }
            for asset in Asset.objects.exclude(current_price__isnull=True)
        ],
        'fx_rates': [
            {
                'from_currency': item.from_currency,
                'to_currency': item.to_currency,
                'effective_date': item.effective_date.isoformat(),
                'rate': str(item.rate),
            }
            for item in FXRate.objects.all().order_by('from_currency', 'to_currency', 'effective_date')
        ],
    }
    return JsonResponse(payload)