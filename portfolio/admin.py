from django.contrib import admin

from .models import Account, Asset, FXRate, Transaction


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ('name', 'account_type', 'currency', 'opening_balance', 'updated_at')
    list_filter = ('account_type', 'currency')
    search_fields = ('name', 'description')


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'name', 'asset_class', 'account', 'closed_at', 'current_price', 'price_currency', 'updated_at')
    list_filter = ('asset_class', 'price_currency', 'account', 'closed_at')
    search_fields = ('symbol', 'name', 'account__name')


@admin.register(FXRate)
class FXRateAdmin(admin.ModelAdmin):
    list_display = ('from_currency', 'to_currency', 'rate', 'updated_at')
    list_filter = ('from_currency', 'to_currency')


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        'transaction_type',
        'source_account',
        'destination_account',
        'asset',
        'amount',
        'currency',
        'fee',
        'status',
        'occurred_at',
    )
    list_filter = ('transaction_type', 'status', 'currency')
    search_fields = ('notes', 'asset__symbol', 'source_account__name', 'destination_account__name')
    autocomplete_fields = ('source_account', 'destination_account', 'asset')