from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


CURRENCY_CHOICES = [
    ('USD', 'USD'),
    ('EUR', 'EUR'),
    ('BYN', 'BYN'),
    ('RUB', 'RUB'),
]


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создано')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Обновлено')

    class Meta:
        abstract = True


class Account(TimestampedModel):
    class AccountType(models.TextChoices):
        BROKERAGE = 'brokerage', 'Биржа'
        TOKEN = 'token', 'Токены / Finstore'
        DEPOSIT = 'deposit', 'Депозит'
        CASH = 'cash', 'Наличные'
        CRYPTO = 'crypto', 'Крипта'
        CFD = 'cfd', 'CFD'
        BANK = 'bank', 'Банк'
        OTHER = 'other', 'Другое'

    name = models.CharField(max_length=120, unique=True, verbose_name='Название')
    description = models.TextField(blank=True, verbose_name='Описание')
    account_type = models.CharField(max_length=24, choices=AccountType.choices, verbose_name='Тип счета')
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='USD', verbose_name='Валюта')
    opening_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'), verbose_name='Начальный остаток')

    class Meta:
        ordering = ['name']
        verbose_name = 'счет'
        verbose_name_plural = 'счета'

    def clean(self):
        self.currency = (self.currency or 'USD').upper()

    def __str__(self):
        return f'{self.name} ({self.currency})'


class Asset(TimestampedModel):
    class AssetClass(models.TextChoices):
        EXCHANGE = 'exchange', 'Биржа'
        TOKEN = 'token', 'Токены / Finstore'
        DEPOSIT = 'deposit', 'Депозит'
        CASH = 'cash', 'Наличные'
        CRYPTO = 'crypto', 'Крипта'
        CFD = 'cfd', 'CFD'
        OTHER = 'other', 'Другое'

    class DepositRevocability(models.TextChoices):
        REVOCABLE = 'revocable', 'Отзывный'
        IRREVOCABLE = 'irrevocable', 'Безотзывный'

    class DepositTermType(models.TextChoices):
        TERM = 'term', 'Срочный'
        OPEN_ENDED = 'open_ended', 'Бессрочный'

    class InterestPayoutMethod(models.TextChoices):
        TO_ACCOUNT = 'to_account', 'На счет'
        CAPITALIZATION = 'capitalization', 'Капитализация'

    class InterestPayoutFrequency(models.TextChoices):
        MONTHLY = 'monthly', '1 раз в месяц'
        SEMI_MONTHLY = 'semi_monthly', '2 раза в месяц'

    symbol = models.CharField(max_length=32, unique=True, verbose_name='Тикер / код')
    name = models.CharField(max_length=120, verbose_name='Название')
    asset_class = models.CharField(max_length=24, choices=AssetClass.choices, verbose_name='Тип актива')
    account = models.ForeignKey(
        Account,
        null=True,
        on_delete=models.PROTECT,
        related_name='assets',
        verbose_name='Счет',
    )
    deposit_revocability = models.CharField(max_length=24, choices=DepositRevocability.choices, blank=True, verbose_name='Тип депозита')
    deposit_term_type = models.CharField(max_length=24, choices=DepositTermType.choices, blank=True, verbose_name='По сроку')
    deposit_term_end = models.DateField(null=True, blank=True, verbose_name='Срок действия')
    deposit_open_date = models.DateField(null=True, blank=True, verbose_name='Дата открытия')
    deposit_annual_rate = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True, verbose_name='Годовая процентная ставка')
    deposit_interest_payout_method = models.CharField(max_length=24, choices=InterestPayoutMethod.choices, blank=True, verbose_name='Способ выплаты процентов')
    deposit_initial_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, verbose_name='Начальный взнос')
    deposit_interest_payout_frequency = models.CharField(max_length=24, choices=InterestPayoutFrequency.choices, blank=True, verbose_name='Периодичность выплаты процентов')
    deposit_weekend_rollover = models.BooleanField(default=False, verbose_name='Перенос выплаты с выходного дня')
    closed_at = models.DateField(null=True, blank=True, verbose_name='Дата закрытия')
    price_currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, blank=True, verbose_name='Валюта цены')
    current_price = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True, verbose_name='Текущая цена')

    class Meta:
        ordering = ['symbol']
        verbose_name = 'актив'
        verbose_name_plural = 'активы'

    def clean(self):
        self.symbol = (self.symbol or '').upper().strip()
        self.price_currency = (self.price_currency or '').upper().strip()
        if not self.account_id and not self.closed_at:
            raise ValidationError({'account': 'Выберите счет, к которому привязан актив.'})

        if self.asset_class == self.AssetClass.DEPOSIT:
            errors = {}
            if not self.deposit_revocability:
                errors['deposit_revocability'] = 'Укажите, отзывный депозит или безотзывный.'
            if not self.deposit_term_type:
                errors['deposit_term_type'] = 'Укажите тип депозита по сроку.'
            if not self.deposit_open_date:
                errors['deposit_open_date'] = 'Укажите дату открытия депозита.'
            if self.deposit_annual_rate is None or self.deposit_annual_rate <= 0:
                errors['deposit_annual_rate'] = 'Укажите годовую процентную ставку больше нуля.'
            if not self.deposit_interest_payout_method:
                errors['deposit_interest_payout_method'] = 'Укажите способ выплаты процентов.'
            if self.deposit_initial_amount is None or self.deposit_initial_amount <= 0:
                errors['deposit_initial_amount'] = 'Укажите начальный взнос больше нуля.'
            if not self.deposit_interest_payout_frequency:
                errors['deposit_interest_payout_frequency'] = 'Укажите периодичность выплаты процентов.'
            if self.deposit_term_type == self.DepositTermType.TERM and not self.deposit_term_end:
                errors['deposit_term_end'] = 'Для срочного депозита укажите срок действия.'
            if self.deposit_term_type == self.DepositTermType.OPEN_ENDED:
                self.deposit_term_end = None
            if errors:
                raise ValidationError(errors)

        if self.closed_at and self.deposit_open_date and self.closed_at < self.deposit_open_date:
            raise ValidationError({'closed_at': 'Дата закрытия не может быть раньше даты открытия.'})

        if self.current_price is not None and not self.price_currency:
            raise ValidationError({'price_currency': 'Укажите валюту цены.'})

    def __str__(self):
        return f'{self.symbol} - {self.name}'


class DepositTopUp(TimestampedModel):
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name='topups', verbose_name='Депозит')
    cash_transaction = models.OneToOneField(
        'Transaction',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='deposit_topup_record',
        verbose_name='Денежная операция',
    )
    source_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='deposit_topups',
        verbose_name='Счет списания',
    )
    topup_date = models.DateField(verbose_name='Дата пополнения')
    amount = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='Сумма пополнения')

    class Meta:
        ordering = ['topup_date', 'id']
        verbose_name = 'пополнение депозита'
        verbose_name_plural = 'пополнения депозитов'

    def clean(self):
        errors = {}
        if self.asset and self.asset.asset_class != Asset.AssetClass.DEPOSIT:
            errors['asset'] = 'Пополнение доступно только для депозитов.'
        if self.amount <= 0:
            errors['amount'] = 'Сумма пополнения должна быть больше нуля.'
        if self.asset and self.asset.deposit_open_date and self.topup_date < self.asset.deposit_open_date:
            errors['topup_date'] = 'Дата пополнения не может быть раньше даты открытия депозита.'
        if self.asset and self.asset.closed_at and self.topup_date > self.asset.closed_at:
            errors['topup_date'] = 'Нельзя пополнить уже закрытый депозит.'
        if self.asset and self.source_account and self.source_account.currency != (self.asset.price_currency or self.asset.account.currency):
            errors['source_account'] = 'Валюта счета списания должна совпадать с валютой депозита.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.asset.symbol} +{self.amount} ({self.topup_date})'


class DepositCapitalizationAdjustment(TimestampedModel):
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name='capitalization_adjustments', verbose_name='Депозит')
    capitalization_date = models.DateField(verbose_name='Дата капитализации')
    interest_amount = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='Сумма процентов')
    notes = models.TextField(blank=True, verbose_name='Комментарий')

    class Meta:
        ordering = ['capitalization_date', 'id']
        verbose_name = 'корректировка капитализации'
        verbose_name_plural = 'корректировки капитализации'
        constraints = [
            models.UniqueConstraint(fields=['asset', 'capitalization_date'], name='unique_capitalization_adjustment'),
        ]

    def clean(self):
        errors = {}
        if self.asset and self.asset.asset_class != Asset.AssetClass.DEPOSIT:
            errors['asset'] = 'Корректировка капитализации доступна только для депозитов.'
        if self.interest_amount is None or self.interest_amount < 0:
            errors['interest_amount'] = 'Сумма процентов не может быть отрицательной.'
        if self.asset and self.capitalization_date and self.asset.deposit_open_date and self.capitalization_date <= self.asset.deposit_open_date:
            errors['capitalization_date'] = 'Дата капитализации должна быть позже даты открытия депозита.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.asset.symbol} {self.capitalization_date} {self.interest_amount}'


class DepositInterestPayout(TimestampedModel):
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name='interest_payouts', verbose_name='Депозит')
    cash_transaction = models.OneToOneField(
        'Transaction',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='deposit_interest_payout_record',
        verbose_name='Денежная операция',
    )
    payout_date = models.DateField(verbose_name='Дата выплаты')
    interest_amount = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='Сумма процентов')
    notes = models.TextField(blank=True, verbose_name='Комментарий')

    class Meta:
        ordering = ['payout_date', 'id']
        verbose_name = 'выплата процентов по депозиту'
        verbose_name_plural = 'выплаты процентов по депозитам'
        constraints = [
            models.UniqueConstraint(fields=['asset', 'payout_date'], name='unique_deposit_interest_payout'),
        ]

    def clean(self):
        errors = {}
        if self.asset and self.asset.asset_class != Asset.AssetClass.DEPOSIT:
            errors['asset'] = 'История выплат доступна только для депозитов.'
        if self.interest_amount is None or self.interest_amount < 0:
            errors['interest_amount'] = 'Сумма процентов не может быть отрицательной.'
        if self.asset and self.payout_date and self.asset.deposit_open_date and self.payout_date <= self.asset.deposit_open_date:
            errors['payout_date'] = 'Дата выплаты должна быть позже даты открытия депозита.'
        if self.asset and self.asset.closed_at and self.payout_date and self.payout_date > self.asset.closed_at:
            errors['payout_date'] = 'Нельзя сохранить выплату после даты закрытия депозита.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.asset.symbol} {self.payout_date} {self.interest_amount}'


class DepositRateChange(TimestampedModel):
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name='rate_changes', verbose_name='Депозит')
    effective_date = models.DateField(verbose_name='Дата начала действия')
    annual_rate = models.DecimalField(max_digits=7, decimal_places=4, verbose_name='Годовая ставка')
    notes = models.TextField(blank=True, verbose_name='Комментарий')

    class Meta:
        ordering = ['effective_date', 'id']
        verbose_name = 'изменение ставки депозита'
        verbose_name_plural = 'изменения ставки депозита'
        constraints = [
            models.UniqueConstraint(fields=['asset', 'effective_date'], name='unique_deposit_rate_change'),
        ]

    def clean(self):
        errors = {}
        if self.asset and self.asset.asset_class != Asset.AssetClass.DEPOSIT:
            errors['asset'] = 'Изменение ставки доступно только для депозитов.'
        if self.annual_rate is None or self.annual_rate <= 0:
            errors['annual_rate'] = 'Укажите годовую ставку больше нуля.'
        if self.asset and self.effective_date and self.asset.deposit_open_date and self.effective_date <= self.asset.deposit_open_date:
            errors['effective_date'] = 'Для даты открытия измените ставку в карточке депозита. Историческое изменение ставки должно быть позже даты открытия.'
        if self.asset and self.asset.closed_at and self.effective_date and self.effective_date > self.asset.closed_at:
            errors['effective_date'] = 'Нельзя задать изменение ставки после даты закрытия депозита.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.asset.symbol} {self.effective_date} {self.annual_rate}'


class FXRate(TimestampedModel):
    from_currency = models.CharField(max_length=3, verbose_name='Из валюты')
    to_currency = models.CharField(max_length=3, verbose_name='В валюту')
    effective_date = models.DateField(verbose_name='Дата курса')
    rate = models.DecimalField(max_digits=18, decimal_places=6, verbose_name='Курс')

    class Meta:
        ordering = ['from_currency', 'to_currency']
        verbose_name = 'валютный курс'
        verbose_name_plural = 'валютные курсы'
        constraints = [
            models.UniqueConstraint(fields=['from_currency', 'to_currency', 'effective_date'], name='unique_fx_pair_on_date'),
        ]

    def clean(self):
        self.from_currency = (self.from_currency or '').upper().strip()
        self.to_currency = (self.to_currency or '').upper().strip()
        if self.from_currency == self.to_currency:
            raise ValidationError('Валютная пара должна состоять из разных валют.')
        if self.rate <= 0:
            raise ValidationError({'rate': 'Курс должен быть больше нуля.'})

    def __str__(self):
        return f'{self.from_currency}/{self.to_currency} = {self.rate}'


class Transaction(TimestampedModel):
    class TransactionType(models.TextChoices):
        BUY = 'buy', 'Покупка'
        SELL = 'sell', 'Продажа'
        DEPOSIT = 'deposit', 'Пополнение'
        WITHDRAW = 'withdraw', 'Вывод'
        INTEREST = 'interest', 'Доход'
        FEE = 'fee', 'Комиссия'
        TRANSFER = 'transfer', 'Перевод'

    class Status(models.TextChoices):
        PENDING = 'pending', 'В обработке'
        COMPLETED = 'completed', 'Исполнено'
        FAILED = 'failed', 'Ошибка'
        CANCELED = 'canceled', 'Отменено'

    source_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='outgoing_transactions',
        verbose_name='Счет списания',
    )
    destination_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='incoming_transactions',
        verbose_name='Счет зачисления',
    )
    asset = models.ForeignKey(Asset, null=True, blank=True, on_delete=models.PROTECT, related_name='transactions', verbose_name='Актив')
    transaction_type = models.CharField(max_length=24, choices=TransactionType.choices, verbose_name='Тип операции')
    amount = models.DecimalField(max_digits=18, decimal_places=2, verbose_name='Сумма')
    currency = models.CharField(max_length=3, default='USD', verbose_name='Валюта')
    fee = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal('0'), verbose_name='Комиссия')
    asset_quantity = models.DecimalField(max_digits=18, decimal_places=8, default=Decimal('0'), verbose_name='Количество актива')
    unit_price = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True, verbose_name='Цена за единицу')
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.COMPLETED, verbose_name='Статус')
    occurred_at = models.DateTimeField(default=timezone.now, verbose_name='Дата и время')
    notes = models.TextField(blank=True, verbose_name='Комментарий')

    class Meta:
        ordering = ['-occurred_at', '-id']
        verbose_name = 'операция'
        verbose_name_plural = 'операции'

    def clean(self):
        self.currency = (self.currency or 'USD').upper().strip()
        if self.source_account_id and self.destination_account_id and self.source_account_id == self.destination_account_id:
            raise ValidationError('Счета источника и назначения должны отличаться.')
        is_asset_transfer = self.transaction_type == self.TransactionType.TRANSFER and bool(self.asset_id or self.asset)
        if self.amount < 0:
            raise ValidationError({'amount': 'Сумма не может быть отрицательной.'})
        if not is_asset_transfer and self.amount <= 0:
            raise ValidationError({'amount': 'Сумма должна быть больше нуля.'})
        if self.fee < 0:
            raise ValidationError({'fee': 'Комиссия не может быть отрицательной.'})

        account_requirements = {
            self.TransactionType.BUY: ('source_account',),
            self.TransactionType.SELL: ('destination_account',),
            self.TransactionType.DEPOSIT: ('destination_account',),
            self.TransactionType.WITHDRAW: ('source_account',),
            self.TransactionType.INTEREST: tuple(),
            self.TransactionType.FEE: ('source_account',),
            self.TransactionType.TRANSFER: ('source_account', 'destination_account'),
        }
        for field_name in account_requirements.get(self.transaction_type, tuple()):
            if not getattr(self, field_name):
                raise ValidationError({field_name: 'Поле обязательно для этого типа операции.'})

        asset_types = {
            self.TransactionType.BUY,
            self.TransactionType.SELL,
            self.TransactionType.INTEREST,
        }
        if self.transaction_type in asset_types:
            if not self.asset:
                raise ValidationError({'asset': 'Выберите актив.'})
            if self.asset_quantity <= 0:
                raise ValidationError({'asset_quantity': 'Количество актива должно быть больше нуля.'})
        elif self.asset and self.asset_quantity < 0:
            raise ValidationError({'asset_quantity': 'Количество актива не может быть отрицательным.'})

        if is_asset_transfer:
            if self.asset_quantity <= 0:
                raise ValidationError({'asset_quantity': 'Для перевода актива укажите количество больше нуля.'})
            if self.asset and self.asset.account_id and self.source_account_id != self.asset.account_id:
                raise ValidationError({'source_account': 'Актив должен переводиться со счета, к которому он привязан.'})

    def __str__(self):
        return f'{self.get_transaction_type_display()} {self.amount} {self.currency}'