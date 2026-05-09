from django.conf import settings

from portfolio.forms import AccountForm, AssetForm, CSVImportForm, FXRateForm, TransactionForm, TransferForm

from .accounts import build_account_rows, list_transactions, list_transfers
from .portfolio import build_portfolio_snapshot
from .rates import get_base_currency_rate_to_byn, get_latest_rate_date
from .transactions import build_upcoming_operations


CURRENCY_SYMBOLS = {
    'USD': '$',
    'EUR': 'EUR',
    'RUB': 'RUB',
    'BYN': 'Br',
}


def build_dashboard_context(forms=None):
    forms = forms or {}
    account_rows = build_account_rows(settings.BASE_CURRENCY)
    portfolio_snapshot = build_portfolio_snapshot(settings.BASE_CURRENCY, account_rows=account_rows)
    latest_rate_date = get_latest_rate_date(settings.BASE_CURRENCY)
    base_currency_rate_to_byn = get_base_currency_rate_to_byn(settings.BASE_CURRENCY)
    return {
        'account_form': forms.get('account_form') or AccountForm(),
        'asset_form': forms.get('asset_form') or AssetForm(),
        'fx_rate_form': forms.get('fx_rate_form') or FXRateForm(),
        'transaction_form': forms.get('transaction_form') or TransactionForm(),
        'transfer_form': forms.get('transfer_form') or TransferForm(),
        'csv_form': forms.get('csv_form') or CSVImportForm(),
        'account_rows': account_rows,
        'transactions': list_transactions(),
        'transfers': list_transfers(),
        'upcoming_operations': build_upcoming_operations(),
        'portfolio_rows': portfolio_snapshot['rows'],
        'portfolio_summary': portfolio_snapshot['summary'],
        'portfolio_assets_total': portfolio_snapshot['assets_total_value'],
        'portfolio_cash_total': portfolio_snapshot['cash_total_value'],
        'portfolio_total': portfolio_snapshot['total_value'],
        'portfolio_performance': portfolio_snapshot['performance'],
        'base_currency': portfolio_snapshot['base_currency'],
        'base_currency_symbol': CURRENCY_SYMBOLS.get(portfolio_snapshot['base_currency'], portfolio_snapshot['base_currency']),
        'base_currency_rate_to_byn': base_currency_rate_to_byn,
        'latest_rate_date': latest_rate_date,
    }