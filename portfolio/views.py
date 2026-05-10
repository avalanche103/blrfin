from django.contrib import messages
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST

from .forms import AccountForm, AccountTopUpForm, AssetCloseForm, AssetForm, CSVImportForm, DepositAssetForm, DepositCapitalizationAdjustmentForm, DepositInterestPayoutForm, DepositRateChangeForm, DepositTopUpForm, FXRateForm, TransactionForm, TransferForm
from .services.accounts import delete_account
from .services.dashboard import build_dashboard_context
from .services.imports import import_transactions_from_csv
from .services.rates import auto_sync_nbrb_rates_if_stale, sync_nbrb_rates
from .services.transactions import ACCOUNT_TOPUP_NOTE, add_deposit_topup, build_deposit_capitalization_history, build_deposit_interest_payout_history, close_asset, create_asset, create_transfer, create_transaction, delete_asset, delete_capitalization_adjustment, delete_deposit_interest_payout, delete_deposit_rate_change, delete_deposit_topup, get_current_asset_price, get_deposit_effective_rate, update_deposit_topup, update_asset, upsert_capitalization_adjustment, upsert_deposit_interest_payout, upsert_deposit_rate_change, upsert_fx_rate


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get('HX-Request') == 'true'


def _build_dashboard_context_with_auto_rates(forms=None):
    auto_sync_nbrb_rates_if_stale()
    return build_dashboard_context(forms)


def _render_dashboard(request: HttpRequest, forms=None, status=200) -> HttpResponse:
    context = _build_dashboard_context_with_auto_rates(forms)
    template_name = 'portfolio/partials/dashboard_content.html' if _is_htmx(request) else 'portfolio/dashboard.html'
    return render(request, template_name, context, status=status)


def _render_htmx_success(request: HttpRequest) -> HttpResponse:
    context = _build_dashboard_context_with_auto_rates()
    response = render(request, 'portfolio/partials/action_success.html', context)
    response['HX-Trigger'] = 'portfolio:close-modal'
    return response


def _render_modal(
    request: HttpRequest,
    *,
    title: str,
    form,
    action_url: str,
    submit_label: str,
    description: str = '',
    enctype: str = '',
    delete_action_url: str = '',
    delete_label: str = '',
    delete_confirm: str = '',
    status: int = 200,
) -> HttpResponse:
    context = {
        'title': title,
        'description': description,
        'form': form,
        'action_url': action_url,
        'submit_label': submit_label,
        'enctype': enctype,
        'delete_action_url': delete_action_url,
        'delete_label': delete_label,
        'delete_confirm': delete_confirm,
    }
    return render(request, 'portfolio/partials/action_modal.html', context, status=status)


def _modal_form_response(request: HttpRequest, *, title: str, form, action_name: str, submit_label: str, description: str = '', enctype: str = '') -> HttpResponse:
    context = {
        'title': title,
        'description': description,
        'form': form,
        'action_url': reverse(action_name),
        'submit_label': submit_label,
        'enctype': enctype,
    }
    return render(request, 'portfolio/partials/action_modal_form.html', context, status=400)


def _attach_validation_error(form, exc: ValidationError, *, field_name: str | None = None) -> None:
    messages = getattr(exc, 'messages', None) or [str(exc)]
    target_field = field_name if field_name in form.fields else None
    for message in messages:
        form.add_error(target_field, message)


def _account_form_description() -> str:
    return 'Создайте банковский счет с валютой и начальным остатком.'


def _account_topup_description() -> str:
    return 'Добавьте внешнее пополнение счета. Эти деньги можно будет дальше распределять по продуктам и переводам.'


def _asset_form_description() -> str:
    return 'Сначала выберите тип актива. Для депозита система пересчитает текущую сумму по ставке и дате открытия.'


def _deposit_form_description() -> str:
    return 'Заполните параметры депозитного договора. Депозит привязывается к банковскому счету и сумма пересчитывается автоматически.'


def _asset_close_description() -> str:
    return 'Выберите дату закрытия и счет, на который должны поступить деньги при закрытии продукта.'


def _deposit_topup_description() -> str:
    return 'Выберите сумму и дату пополнения. При необходимости можно снять галочку и не списывать это пополнение со счета, если это ретроввод уже существовавшего пополнения.'


def _deposit_history_description() -> str:
    return 'Здесь можно сверить стартовую сумму депозита, все пополнения и при необходимости скорректировать историю.'


def _capitalization_adjustment_description() -> str:
    return 'Если банк начислил проценты немного иначе, сохраните фактическую сумму процентов для конкретной даты капитализации.'


def _deposit_payout_description() -> str:
    return 'Сохраните фактическую выплату процентов. Для прошлых дат запись останется в истории депозита и не изменит остаток счета.'


def _deposit_rate_change_description() -> str:
    return 'Задайте новую ставку с конкретной даты. Ставка на дату открытия редактируется в карточке депозита, а все последующие изменения ведутся здесь.'


@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    return _render_dashboard(request)


@require_GET
def account_form_view(request: HttpRequest) -> HttpResponse:
    return _render_modal(
        request,
        title='Добавить банк',
        form=AccountForm(),
        action_url=reverse('portfolio:account-create'),
        submit_label='Сохранить банк',
        description=_account_form_description(),
    )


@require_GET
def account_edit_form_view(request: HttpRequest, account_id: int) -> HttpResponse:
    account = get_object_or_404(AccountForm._meta.model, pk=account_id)
    return _render_modal(
        request,
        title='Редактировать банк',
        form=AccountForm(instance=account),
        action_url=reverse('portfolio:account-update', args=[account.id]),
        submit_label='Сохранить изменения',
        description=_account_form_description(),
    )


@require_GET
def account_topup_form_view(request: HttpRequest, account_id: int | None = None) -> HttpResponse:
    account = get_object_or_404(AccountForm._meta.model, pk=account_id) if account_id else None
    form = AccountTopUpForm(account=account)
    return _render_modal(
        request,
        title='Пополнить счет',
        form=form,
        action_url=reverse('portfolio:account-topup-create'),
        submit_label='Сохранить пополнение',
        description=_account_topup_description(),
    )


@require_GET
def asset_form_view(request: HttpRequest) -> HttpResponse:
    return _render_modal(
        request,
        title='Добавить актив',
        form=AssetForm(),
        action_url=reverse('portfolio:asset-create'),
        submit_label='Сохранить актив',
        description=_asset_form_description(),
    )


@require_GET
def deposit_form_view(request: HttpRequest) -> HttpResponse:
    return _render_modal(
        request,
        title='Добавить депозит',
        form=DepositAssetForm(initial={'asset_class': 'deposit'}),
        action_url=reverse('portfolio:deposit-create'),
        submit_label='Сохранить депозит',
        description=_deposit_form_description(),
    )


@require_GET
def asset_edit_form_view(request: HttpRequest, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    return _render_modal(
        request,
        title='Редактировать актив',
        form=AssetForm(instance=asset),
        action_url=reverse('portfolio:asset-update', args=[asset.id]),
        submit_label='Сохранить изменения',
        description=_asset_form_description(),
        delete_action_url=reverse('portfolio:asset-delete', args=[asset.id]),
        delete_label='Удалить продукт',
        delete_confirm=f'Удалить продукт {asset.symbol}? Это удалит сам продукт и связанную с ним историю.',
    )


@require_GET
def asset_close_form_view(request: HttpRequest, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    form = AssetCloseForm(asset=asset, initial={'closed_at': timezone.localdate()})
    return _render_modal(
        request,
        title='Закрыть продукт',
        form=form,
        action_url=reverse('portfolio:asset-close', args=[asset.id]),
        submit_label='Закрыть продукт',
        description=_asset_close_description(),
    )


@require_GET
def deposit_topup_form_view(request: HttpRequest, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    form = DepositTopUpForm(asset=asset)
    return _render_modal(
        request,
        title='Пополнить депозит',
        form=form,
        action_url=reverse('portfolio:deposit-topup-create', args=[asset.id]),
        submit_label='Сохранить пополнение',
        description=_deposit_topup_description(),
    )


@require_GET
def deposit_history_view(request: HttpRequest, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model.objects.prefetch_related('topups__source_account', 'rate_changes', 'interest_payouts__cash_transaction'), pk=asset_id)
    asset.current_price = get_current_asset_price(asset)
    context = {
        'title': f'История депозита {asset.symbol}',
        'description': _deposit_history_description(),
        'asset': asset,
        'topups': asset.topups.select_related('source_account').all(),
        'current_annual_rate': get_deposit_effective_rate(asset),
        'rate_changes': asset.rate_changes.all(),
        'capitalization_history': build_deposit_capitalization_history(asset),
        'interest_payout_history': build_deposit_interest_payout_history(asset),
    }
    return render(request, 'portfolio/partials/deposit_history_modal.html', context)


@require_GET
def deposit_interest_payout_form_view(request: HttpRequest, asset_id: int, payout_date: str) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    parsed_date = parse_date(payout_date)
    payout_record = DepositInterestPayoutForm._meta.model.objects.filter(asset=asset, payout_date=parsed_date).first()
    initial = None
    if not payout_record:
        row = next((item for item in build_deposit_interest_payout_history(asset) if item['operation_date'] == parsed_date), None)
        initial = {
            'payout_date': parsed_date,
            'interest_amount': row['interest_amount'] if row else None,
        }
    form = DepositInterestPayoutForm(instance=payout_record, initial=initial, asset=asset)
    return _render_modal(
        request,
        title='Скорректировать выплату процентов',
        form=form,
        action_url=reverse('portfolio:deposit-interest-payout-update', args=[asset.id, payout_date]),
        submit_label='Сохранить выплату',
        description=_deposit_payout_description(),
    )


@require_GET
def deposit_rate_change_form_view(request: HttpRequest, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    form = DepositRateChangeForm(asset=asset, initial={'effective_date': timezone.localdate()})
    return _render_modal(
        request,
        title='Изменить ставку депозита',
        form=form,
        action_url=reverse('portfolio:deposit-rate-change-create', args=[asset.id]),
        submit_label='Сохранить ставку',
        description=_deposit_rate_change_description(),
    )


@require_GET
def deposit_rate_change_edit_form_view(request: HttpRequest, rate_change_id: int) -> HttpResponse:
    rate_change = get_object_or_404(DepositRateChangeForm._meta.model.objects.select_related('asset'), pk=rate_change_id)
    form = DepositRateChangeForm(instance=rate_change, asset=rate_change.asset)
    return _render_modal(
        request,
        title='Редактировать изменение ставки',
        form=form,
        action_url=reverse('portfolio:deposit-rate-change-update', args=[rate_change.id]),
        submit_label='Сохранить изменения',
        description=_deposit_rate_change_description(),
    )


@require_GET
def capitalization_adjustment_form_view(request: HttpRequest, asset_id: int, capitalization_date: str) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    parsed_date = parse_date(capitalization_date)
    adjustment = DepositCapitalizationAdjustmentForm._meta.model.objects.filter(asset=asset, capitalization_date=parsed_date).first()
    initial = None
    if not adjustment:
        row = next((item for item in build_deposit_capitalization_history(asset) if item['capitalization_date'] == parsed_date), None)
        initial = {
            'capitalization_date': parsed_date,
            'interest_amount': row['interest_amount'] if row else None,
        }
    form = DepositCapitalizationAdjustmentForm(instance=adjustment, initial=initial, asset=asset)
    return _render_modal(
        request,
        title='Скорректировать капитализацию',
        form=form,
        action_url=reverse('portfolio:capitalization-adjustment-update', args=[asset.id, capitalization_date]),
        submit_label='Сохранить корректировку',
        description=_capitalization_adjustment_description(),
    )


@require_GET
def deposit_topup_edit_form_view(request: HttpRequest, topup_id: int) -> HttpResponse:
    topup = get_object_or_404(DepositTopUpForm._meta.model.objects.select_related('asset', 'source_account'), pk=topup_id)
    form = DepositTopUpForm(instance=topup, asset=topup.asset)
    return _render_modal(
        request,
        title='Редактировать пополнение депозита',
        form=form,
        action_url=reverse('portfolio:deposit-topup-update', args=[topup.id]),
        submit_label='Сохранить изменения',
        description=_deposit_topup_description(),
    )


@require_GET
def fx_rate_form_view(request: HttpRequest) -> HttpResponse:
    return _render_modal(
        request,
        title='Добавить курс валют',
        form=FXRateForm(),
        action_url=reverse('portfolio:fx-create'),
        submit_label='Сохранить курс',
        description='Курсы используются для расчета стоимости портфеля в базовой валюте.',
    )


@require_GET
def transaction_form_view(request: HttpRequest) -> HttpResponse:
    return _render_modal(
        request,
        title='Добавить операцию',
        form=TransactionForm(),
        action_url=reverse('portfolio:transaction-create'),
        submit_label='Сохранить операцию',
        description='Покупка, продажа, пополнение, вывод, доход и комиссия сохраняются в общей истории операций.',
    )


@require_GET
def transfer_form_view(request: HttpRequest) -> HttpResponse:
    return _render_modal(
        request,
        title='Добавить перевод',
        form=TransferForm(),
        action_url=reverse('portfolio:transfer-create'),
        submit_label='Сохранить перевод',
        description='Можно перевести деньги или актив между счетами. Для актива выберите сам актив и количество.',
    )


@require_GET
def csv_import_form_view(request: HttpRequest) -> HttpResponse:
    return _render_modal(
        request,
        title='Импортировать CSV',
        form=CSVImportForm(),
        action_url=reverse('portfolio:csv-import'),
        submit_label='Загрузить CSV',
        description='Колонки CSV: transaction_type, source_account, destination_account, asset_symbol, asset_name, asset_class, asset_quantity, unit_price, amount, currency, fee, status, occurred_at, notes.',
        enctype='multipart/form-data',
    )


@require_POST
def sync_nbrb_rates_view(request: HttpRequest) -> HttpResponse:
    try:
        result = sync_nbrb_rates()
        messages.success(
            request,
            f'Курсы НБРБ обновлены: {result["synced_count"]} валют, базовая валюта портфеля {result["base_currency"]}, дата {result["rate_date"]}.',
        )
    except Exception as exc:
        messages.error(request, f'Не удалось обновить курсы НБРБ: {exc}')

    return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)


@require_POST
def create_account_view(request: HttpRequest) -> HttpResponse:
    form = AccountForm(request.POST)
    if form.is_valid():
        account = form.save()
        messages.success(request, f'Банк {account.name} создан.')
        return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _modal_form_response(
            request,
            title='Добавить банк',
            form=form,
            action_name='portfolio:account-create',
            submit_label='Сохранить банк',
            description=_account_form_description(),
        )
    return _render_dashboard(request, forms={'account_form': form}, status=400)


@require_POST
def create_account_topup_view(request: HttpRequest) -> HttpResponse:
    form = AccountTopUpForm(request.POST)
    if form.is_valid():
        account = form.cleaned_data['destination_account']
        occurred_at = timezone.make_aware(
            timezone.datetime.combine(form.cleaned_data['occurred_at'], timezone.datetime.min.time()),
            timezone.get_current_timezone(),
        )
        create_transaction(
            {
                'transaction_type': 'deposit',
                'destination_account': account,
                'amount': form.cleaned_data['amount'],
                'currency': account.currency,
                'fee': '0',
                'asset_quantity': '0',
                'status': 'completed',
                'occurred_at': occurred_at,
                'notes': ACCOUNT_TOPUP_NOTE if not form.cleaned_data['notes'] else f'{ACCOUNT_TOPUP_NOTE}\n{form.cleaned_data["notes"]}',
            }
        )
        messages.success(request, f'Счет {account.name} пополнен.')
        return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _render_modal(
            request,
            title='Пополнить счет',
            form=form,
            action_url=reverse('portfolio:account-topup-create'),
            submit_label='Сохранить пополнение',
            description=_account_topup_description(),
            status=400,
        )
    return _render_dashboard(request, status=400)


@require_POST
def update_account_view(request: HttpRequest, account_id: int) -> HttpResponse:
    account = get_object_or_404(AccountForm._meta.model, pk=account_id)
    form = AccountForm(request.POST, instance=account)
    if form.is_valid():
        account = form.save()
        messages.success(request, f'Банк {account.name} обновлен.')
        return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _render_modal(
            request,
            title='Редактировать банк',
            form=form,
            action_url=reverse('portfolio:account-update', args=[account.id]),
            submit_label='Сохранить изменения',
            description=_account_form_description(),
            status=400,
        )
    return _render_dashboard(request, forms={'account_form': form}, status=400)


@require_POST
def delete_account_view(request: HttpRequest, account_id: int) -> HttpResponse:
    account = get_object_or_404(AccountForm._meta.model, pk=account_id)
    try:
        account_name = account.name
        delete_account(account)
        messages.success(request, f'Банк {account_name} удален.')
    except ValueError as exc:
        messages.error(request, str(exc))

    return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)


@require_POST
def create_asset_view(request: HttpRequest) -> HttpResponse:
    form = AssetForm(request.POST)
    if form.is_valid():
        try:
            asset = create_asset(form.cleaned_data)
        except ValidationError as exc:
            _attach_validation_error(form, exc)
        else:
            messages.success(request, f'Актив {asset.symbol} сохранен.')
            return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _modal_form_response(
            request,
            title='Добавить актив',
            form=form,
            action_name='portfolio:asset-create',
            submit_label='Сохранить актив',
            description=_asset_form_description(),
        )
    return _render_dashboard(request, forms={'asset_form': form}, status=400)


@require_POST
def create_deposit_view(request: HttpRequest) -> HttpResponse:
    form = DepositAssetForm(request.POST)
    if form.is_valid():
        try:
            asset = create_asset(form.cleaned_data)
        except ValidationError as exc:
            _attach_validation_error(form, exc, field_name='deposit_initial_amount')
        else:
            messages.success(request, f'Депозит {asset.symbol} сохранен.')
            return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _render_modal(
            request,
            title='Добавить депозит',
            form=form,
            action_url=reverse('portfolio:deposit-create'),
            submit_label='Сохранить депозит',
            description=_deposit_form_description(),
            status=400,
        )
    return _render_dashboard(request, forms={'asset_form': form}, status=400)


@require_POST
def update_asset_view(request: HttpRequest, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    form = AssetForm(request.POST, instance=asset)
    if form.is_valid():
        try:
            asset = update_asset(asset, form.cleaned_data)
        except ValidationError as exc:
            _attach_validation_error(form, exc)
        else:
            messages.success(request, f'Актив {asset.symbol} обновлен.')
            return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _render_modal(
            request,
            title='Редактировать актив',
            form=form,
            action_url=reverse('portfolio:asset-update', args=[asset.id]),
            submit_label='Сохранить изменения',
            description=_asset_form_description(),
            delete_action_url=reverse('portfolio:asset-delete', args=[asset.id]),
            delete_label='Удалить продукт',
            delete_confirm=f'Удалить продукт {asset.symbol}? Это удалит сам продукт и связанную с ним историю.',
            status=400,
        )
    return _render_dashboard(request, forms={'asset_form': form}, status=400)


@require_POST
def delete_asset_view(request: HttpRequest, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    try:
        asset_symbol = asset.symbol
        delete_asset(asset)
        messages.success(request, f'Продукт {asset_symbol} удален.')
    except ValueError as exc:
        messages.error(request, str(exc))

    return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)


@require_POST
def close_asset_view(request: HttpRequest, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    form = AssetCloseForm(request.POST, asset=asset)
    if not form.is_valid():
        if _is_htmx(request):
            return _render_modal(
                request,
                title='Закрыть продукт',
                form=form,
                action_url=reverse('portfolio:asset-close', args=[asset.id]),
                submit_label='Закрыть продукт',
                description=_asset_close_description(),
                status=400,
            )
        return _render_dashboard(request, status=400)

    try:
        closed_asset, payout = close_asset(
            asset,
            destination_account=form.cleaned_data['destination_account'],
            closed_at=form.cleaned_data['closed_at'],
        )
        messages.success(request, f'Продукт {closed_asset.symbol} закрыт, средства зачислены на счет {payout.destination_account.name}.')
    except Exception as exc:
        messages.error(request, f'Не удалось закрыть продукт: {exc}')

    return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)


@require_POST
def create_deposit_topup_view(request: HttpRequest, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    form = DepositTopUpForm(request.POST, asset=asset)
    if form.is_valid():
        try:
            add_deposit_topup(
                asset,
                source_account=form.cleaned_data['source_account'],
                topup_date=form.cleaned_data['topup_date'],
                amount=form.cleaned_data['amount'],
                fund_from_account=form.cleaned_data['fund_from_account'],
            )
        except ValidationError as exc:
            _attach_validation_error(form, exc, field_name='amount')
        else:
            messages.success(request, f'Пополнение депозита {asset.symbol} сохранено.')
            return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _render_modal(
            request,
            title='Пополнить депозит',
            form=form,
            action_url=reverse('portfolio:deposit-topup-create', args=[asset.id]),
            submit_label='Сохранить пополнение',
            description=_deposit_topup_description(),
            status=400,
        )
    return _render_dashboard(request, status=400)


@require_POST
def update_capitalization_adjustment_view(request: HttpRequest, asset_id: int, capitalization_date: str) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    parsed_date = parse_date(capitalization_date)
    adjustment = DepositCapitalizationAdjustmentForm._meta.model.objects.filter(asset=asset, capitalization_date=parsed_date).first()
    form = DepositCapitalizationAdjustmentForm(request.POST, instance=adjustment, asset=asset)
    if form.is_valid():
        upsert_capitalization_adjustment(
            asset,
            capitalization_date=form.cleaned_data['capitalization_date'],
            interest_amount=form.cleaned_data['interest_amount'],
            notes=form.cleaned_data['notes'],
        )
        messages.success(request, f'Капитализация депозита {asset.symbol} скорректирована.')
        return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _render_modal(
            request,
            title='Скорректировать капитализацию',
            form=form,
            action_url=reverse('portfolio:capitalization-adjustment-update', args=[asset.id, capitalization_date]),
            submit_label='Сохранить корректировку',
            description=_capitalization_adjustment_description(),
            status=400,
        )
    return _render_dashboard(request, status=400)


@require_POST
def update_deposit_interest_payout_view(request: HttpRequest, asset_id: int, payout_date: str) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    parsed_date = parse_date(payout_date)
    payout_record = DepositInterestPayoutForm._meta.model.objects.filter(asset=asset, payout_date=parsed_date).first()
    form = DepositInterestPayoutForm(request.POST, instance=payout_record, asset=asset)
    if form.is_valid():
        upsert_deposit_interest_payout(
            asset,
            payout_date=form.cleaned_data['payout_date'],
            interest_amount=form.cleaned_data['interest_amount'],
            notes=form.cleaned_data['notes'],
            credit_to_account=form.cleaned_data['credit_to_account'],
            payout_record=payout_record,
        )
        messages.success(request, f'Выплата процентов по депозиту {asset.symbol} сохранена.')
        return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _render_modal(
            request,
            title='Скорректировать выплату процентов',
            form=form,
            action_url=reverse('portfolio:deposit-interest-payout-update', args=[asset.id, payout_date]),
            submit_label='Сохранить выплату',
            description=_deposit_payout_description(),
            status=400,
        )
    return _render_dashboard(request, status=400)


@require_POST
def create_deposit_rate_change_view(request: HttpRequest, asset_id: int) -> HttpResponse:
    asset = get_object_or_404(AssetForm._meta.model, pk=asset_id)
    form = DepositRateChangeForm(request.POST, asset=asset)
    if form.is_valid():
        upsert_deposit_rate_change(
            asset,
            effective_date=form.cleaned_data['effective_date'],
            annual_rate=form.cleaned_data['annual_rate'],
            notes=form.cleaned_data['notes'],
        )
        messages.success(request, f'Ставка депозита {asset.symbol} обновлена.')
        return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _render_modal(
            request,
            title='Изменить ставку депозита',
            form=form,
            action_url=reverse('portfolio:deposit-rate-change-create', args=[asset.id]),
            submit_label='Сохранить ставку',
            description=_deposit_rate_change_description(),
            status=400,
        )
    return _render_dashboard(request, status=400)


@require_POST
def update_deposit_rate_change_view(request: HttpRequest, rate_change_id: int) -> HttpResponse:
    rate_change = get_object_or_404(DepositRateChangeForm._meta.model.objects.select_related('asset'), pk=rate_change_id)
    form = DepositRateChangeForm(request.POST, instance=rate_change, asset=rate_change.asset)
    if form.is_valid():
        upsert_deposit_rate_change(
            rate_change.asset,
            effective_date=form.cleaned_data['effective_date'],
            annual_rate=form.cleaned_data['annual_rate'],
            notes=form.cleaned_data['notes'],
            rate_change=rate_change,
        )
        messages.success(request, f'Ставка депозита {rate_change.asset.symbol} обновлена.')
        return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _render_modal(
            request,
            title='Редактировать изменение ставки',
            form=form,
            action_url=reverse('portfolio:deposit-rate-change-update', args=[rate_change.id]),
            submit_label='Сохранить изменения',
            description=_deposit_rate_change_description(),
            status=400,
        )
    return _render_dashboard(request, status=400)


@require_POST
def delete_deposit_rate_change_view(request: HttpRequest, rate_change_id: int) -> HttpResponse:
    rate_change = get_object_or_404(DepositRateChangeForm._meta.model.objects.select_related('asset'), pk=rate_change_id)
    asset_symbol = rate_change.asset.symbol
    delete_deposit_rate_change(rate_change)
    messages.success(request, f'Изменение ставки депозита {asset_symbol} удалено.')
    return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)


@require_POST
def delete_capitalization_adjustment_view(request: HttpRequest, adjustment_id: int) -> HttpResponse:
    adjustment = get_object_or_404(DepositCapitalizationAdjustmentForm._meta.model.objects.select_related('asset'), pk=adjustment_id)
    asset_symbol = adjustment.asset.symbol
    delete_capitalization_adjustment(adjustment)
    messages.success(request, f'Корректировка капитализации депозита {asset_symbol} удалена.')
    return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)


@require_POST
def delete_deposit_interest_payout_view(request: HttpRequest, payout_id: int) -> HttpResponse:
    payout_record = get_object_or_404(DepositInterestPayoutForm._meta.model.objects.select_related('asset'), pk=payout_id)
    asset_symbol = payout_record.asset.symbol
    delete_deposit_interest_payout(payout_record)
    messages.success(request, f'Выплата процентов по депозиту {asset_symbol} удалена.')
    return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)


@require_POST
def update_deposit_topup_view(request: HttpRequest, topup_id: int) -> HttpResponse:
    topup = get_object_or_404(DepositTopUpForm._meta.model.objects.select_related('asset', 'source_account'), pk=topup_id)
    form = DepositTopUpForm(request.POST, instance=topup, asset=topup.asset)
    if form.is_valid():
        try:
            update_deposit_topup(
                topup,
                source_account=form.cleaned_data['source_account'],
                topup_date=form.cleaned_data['topup_date'],
                amount=form.cleaned_data['amount'],
                fund_from_account=form.cleaned_data['fund_from_account'],
            )
        except ValidationError as exc:
            _attach_validation_error(form, exc, field_name='amount')
        else:
            messages.success(request, f'Пополнение депозита {topup.asset.symbol} обновлено.')
            return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _render_modal(
            request,
            title='Редактировать пополнение депозита',
            form=form,
            action_url=reverse('portfolio:deposit-topup-update', args=[topup.id]),
            submit_label='Сохранить изменения',
            description=_deposit_topup_description(),
            status=400,
        )
    return _render_dashboard(request, status=400)


@require_POST
def delete_deposit_topup_view(request: HttpRequest, topup_id: int) -> HttpResponse:
    topup = get_object_or_404(DepositTopUpForm._meta.model.objects.select_related('asset'), pk=topup_id)
    asset_symbol = topup.asset.symbol
    delete_deposit_topup(topup)
    messages.success(request, f'Пополнение депозита {asset_symbol} удалено.')
    return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)


@require_POST
def create_fx_rate_view(request: HttpRequest) -> HttpResponse:
    form = FXRateForm(request.POST)
    if form.is_valid():
        pair = upsert_fx_rate(form.cleaned_data)
        messages.success(request, f'Курс {pair.from_currency}/{pair.to_currency} обновлен.')
        return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _modal_form_response(
            request,
            title='Добавить курс валют',
            form=form,
            action_name='portfolio:fx-create',
            submit_label='Сохранить курс',
            description='Курсы используются для расчета стоимости портфеля в базовой валюте.',
        )
    return _render_dashboard(request, forms={'fx_rate_form': form}, status=400)


@require_POST
def create_transaction_view(request: HttpRequest) -> HttpResponse:
    form = TransactionForm(request.POST)
    if form.is_valid():
        create_transaction(form.cleaned_data)
        messages.success(request, 'Операция сохранена.')
        return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _modal_form_response(
            request,
            title='Добавить операцию',
            form=form,
            action_name='portfolio:transaction-create',
            submit_label='Сохранить операцию',
            description='Покупка, продажа, пополнение, вывод, доход и комиссия сохраняются в общей истории операций.',
        )
    return _render_dashboard(request, forms={'transaction_form': form}, status=400)


@require_POST
def create_transfer_view(request: HttpRequest) -> HttpResponse:
    form = TransferForm(request.POST)
    if form.is_valid():
        create_transfer(form.cleaned_data)
        messages.success(request, 'Перевод сохранен.')
        return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _modal_form_response(
            request,
            title='Добавить перевод',
            form=form,
            action_name='portfolio:transfer-create',
            submit_label='Сохранить перевод',
            description='Можно перевести деньги или актив между счетами. Для актива выберите сам актив и количество.',
        )
    return _render_dashboard(request, forms={'transfer_form': form}, status=400)


@require_POST
def import_csv_view(request: HttpRequest) -> HttpResponse:
    form = CSVImportForm(request.POST, request.FILES)
    if form.is_valid():
        result = import_transactions_from_csv(form.cleaned_data['file'])
        if result['errors']:
            for error in result['errors']:
                messages.error(request, error)
        messages.success(request, f'Импортировано записей: {result["created_count"]}.')
        return redirect('portfolio:dashboard') if not _is_htmx(request) else _render_htmx_success(request)
    if _is_htmx(request):
        return _modal_form_response(
            request,
            title='Импортировать CSV',
            form=form,
            action_name='portfolio:csv-import',
            submit_label='Загрузить CSV',
            description='Колонки CSV: transaction_type, source_account, destination_account, asset_symbol, asset_name, asset_class, asset_quantity, unit_price, amount, currency, fee, status, occurred_at, notes.',
            enctype='multipart/form-data',
        )
    return _render_dashboard(request, forms={'csv_form': form}, status=400)