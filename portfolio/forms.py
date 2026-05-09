from django import forms
from django.utils import timezone

from .models import Account, Asset, DepositCapitalizationAdjustment, DepositTopUp, FXRate, Transaction


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css_class = 'form-control'
            if isinstance(field.widget, forms.CheckboxInput):
                css_class = 'form-checkbox'
            existing = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{existing} {css_class}'.strip()


class AccountForm(StyledModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['account_type'].initial = Account.AccountType.BANK
        self.fields['account_type'].choices = [(Account.AccountType.BANK, Account.AccountType.BANK.label)]

    class Meta:
        model = Account
        fields = ['name', 'description', 'account_type', 'currency', 'opening_balance']


class AssetForm(StyledModelForm):
    deposit_open_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    deposit_term_end = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    closed_at = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    class Meta:
        model = Asset
        fields = [
            'asset_class',
            'symbol',
            'name',
            'account',
            'deposit_revocability',
            'deposit_term_type',
            'deposit_term_end',
            'deposit_open_date',
            'deposit_annual_rate',
            'deposit_interest_payout_method',
            'deposit_initial_amount',
            'deposit_interest_payout_frequency',
            'deposit_weekend_rollover',
            'closed_at',
            'price_currency',
            'current_price',
        ]


class DepositAssetForm(AssetForm):
    fund_from_account = forms.BooleanField(
        label='Списывать стартовую сумму со счета',
        required=False,
        initial=True,
        help_text='Для ретроввода можно снять галочку, если депозит уже существовал вне системы и исторического списания со счета делать не нужно.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['asset_class'].initial = Asset.AssetClass.DEPOSIT
        self.fields['asset_class'].widget = forms.HiddenInput()
        self.fields['account'].queryset = Account.objects.filter(account_type=Account.AccountType.BANK).order_by('name')
        self.fields['closed_at'].widget = forms.HiddenInput()
        self.fields['current_price'].widget = forms.HiddenInput()

    class Meta(AssetForm.Meta):
        fields = [
            'asset_class',
            'symbol',
            'name',
            'account',
            'deposit_revocability',
            'deposit_term_type',
            'deposit_term_end',
            'deposit_open_date',
            'deposit_annual_rate',
            'deposit_interest_payout_method',
            'deposit_initial_amount',
            'deposit_interest_payout_frequency',
            'deposit_weekend_rollover',
            'price_currency',
            'current_price',
            'closed_at',
        ]


class DepositTopUpForm(forms.ModelForm):
    topup_date = forms.DateField(label='Дата пополнения', initial=timezone.localdate, widget=forms.DateInput(attrs={'type': 'date'}))
    fund_from_account = forms.BooleanField(
        label='Списывать сумму пополнения со счета',
        required=False,
        initial=True,
        help_text='Для ретроввода можно снять галочку, если пополнение уже было сделано вне системы и исторического списания со счета делать не нужно.',
    )

    class Meta:
        model = DepositTopUp
        fields = ['source_account', 'topup_date', 'amount']

    def __init__(self, *args, asset=None, **kwargs):
        self.asset = asset
        super().__init__(*args, **kwargs)
        self.fields['source_account'].queryset = Account.objects.filter(currency=asset.price_currency or asset.account.currency).order_by('name') if asset else Account.objects.none()
        if self.instance.pk:
            self.fields['fund_from_account'].initial = bool(self.instance.cash_transaction_id)
        for field in self.fields.values():
            existing = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{existing} form-control'.strip()

    def clean(self):
        cleaned_data = super().clean()
        should_fund_from_account = cleaned_data.get('fund_from_account', True)
        if self.asset:
            self.instance.asset = self.asset
            self.instance.cash_transaction = self.instance.cash_transaction
            self.instance.source_account = cleaned_data.get('source_account') if should_fund_from_account else None
            self.instance.topup_date = cleaned_data.get('topup_date')
            self.instance.amount = cleaned_data.get('amount')
            if should_fund_from_account and not self.instance.source_account_id:
                self.add_error('source_account', 'Выберите счет, с которого списываются деньги.')
            self.instance.full_clean()
        return cleaned_data


class DepositCapitalizationAdjustmentForm(forms.ModelForm):
    capitalization_date = forms.DateField(label='Дата капитализации', widget=forms.DateInput(attrs={'type': 'date'}))

    class Meta:
        model = DepositCapitalizationAdjustment
        fields = ['capitalization_date', 'interest_amount', 'notes']

    def __init__(self, *args, asset=None, **kwargs):
        self.asset = asset
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            existing = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{existing} form-control'.strip()

    def clean(self):
        cleaned_data = super().clean()
        if self.asset:
            self.instance.asset = self.asset
            self.instance.capitalization_date = cleaned_data.get('capitalization_date')
            self.instance.interest_amount = cleaned_data.get('interest_amount')
            self.instance.notes = cleaned_data.get('notes') or ''
            self.instance.full_clean()
        return cleaned_data


class AssetCloseForm(forms.Form):
    destination_account = forms.ModelChoiceField(queryset=Account.objects.none(), label='Счет зачисления')
    closed_at = forms.DateField(label='Дата закрытия', initial=timezone.localdate, widget=forms.DateInput(attrs={'type': 'date'}))

    def __init__(self, *args, asset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.asset = asset
        self.fields['destination_account'].queryset = Account.objects.order_by('name')
        for field in self.fields.values():
            existing = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{existing} form-control'.strip()


class AccountTopUpForm(forms.Form):
    destination_account = forms.ModelChoiceField(queryset=Account.objects.none(), label='Счет пополнения')
    amount = forms.DecimalField(label='Сумма', min_value=0.01, decimal_places=2, max_digits=18)
    occurred_at = forms.DateField(label='Дата пополнения', initial=timezone.localdate, widget=forms.DateInput(attrs={'type': 'date'}))
    notes = forms.CharField(label='Комментарий', required=False, widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, account=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['destination_account'].queryset = Account.objects.order_by('name')
        if account:
            self.fields['destination_account'].initial = account
        for field in self.fields.values():
            existing = field.widget.attrs.get('class', '')
            field.widget.attrs['class'] = f'{existing} form-control'.strip()


class FXRateForm(StyledModelForm):
    class Meta:
        model = FXRate
        fields = ['from_currency', 'to_currency', 'rate']


class TransactionForm(StyledModelForm):
    occurred_at = forms.DateTimeField(
        initial=timezone.now,
        input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S'],
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
    )

    class Meta:
        model = Transaction
        fields = [
            'transaction_type',
            'source_account',
            'destination_account',
            'asset',
            'asset_quantity',
            'unit_price',
            'amount',
            'currency',
            'fee',
            'status',
            'occurred_at',
            'notes',
        ]


class TransferForm(StyledModelForm):
    amount = forms.DecimalField(required=False, initial=0)
    asset_quantity = forms.DecimalField(required=False, initial=0)
    occurred_at = forms.DateTimeField(
        initial=timezone.now,
        input_formats=['%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S'],
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.transaction_type = Transaction.TransactionType.TRANSFER

    class Meta:
        model = Transaction
        fields = ['source_account', 'destination_account', 'asset', 'asset_quantity', 'amount', 'currency', 'fee', 'status', 'occurred_at', 'notes']

    def clean(self):
        cleaned_data = super().clean()
        asset = cleaned_data.get('asset')
        amount = cleaned_data.get('amount')

        if asset and amount in (None, ''):
            cleaned_data['amount'] = 0
        if not asset and not amount:
            self.add_error('amount', 'Укажите сумму денежного перевода.')
        return cleaned_data


class CSVImportForm(forms.Form):
    file = forms.FileField(label='CSV-файл', widget=forms.ClearableFileInput(attrs={'accept': '.csv'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['file'].widget.attrs['class'] = 'form-control'