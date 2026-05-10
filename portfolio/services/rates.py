import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from django.conf import settings
from django.utils import timezone

from portfolio.models import FXRate

from .transactions import upsert_fx_rate


DEFAULT_NBRB_HISTORY_START = date(2024, 1, 1)
DEFAULT_NBRB_HISTORY_CURRENCIES = ('USD', 'EUR', 'RUB')


def _fetch_json(url):
    try:
        with urlopen(url, timeout=20) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code} при обращении к НБРБ.') from exc
    except URLError as exc:
        raise RuntimeError('Сервис НБРБ недоступен.') from exc


def _quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)


def _normalize_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _normalize_currency_codes(currencies):
    normalized = []
    seen = set()
    for item in currencies or []:
        code = str(item).upper().strip()
        if not code or code in seen:
            continue
        seen.add(code)
        normalized.append(code)
    return tuple(normalized)


@lru_cache(maxsize=32)
def _fetch_currency_meta(currency_code):
    endpoint = f"{settings.NBRB_API_URL.rstrip('/')}/rates/{currency_code}?parammode=2"
    payload = _fetch_json(endpoint)
    if not isinstance(payload, dict) or 'Cur_ID' not in payload:
        raise RuntimeError(f'НБРБ не вернул метаданные по валюте {currency_code}.')
    return {
        'id': int(payload['Cur_ID']),
        'scale': Decimal(str(payload['Cur_Scale'])),
    }


def _nearest_probe_dates(requested_date, lookaround_days):
    yield requested_date
    for offset in range(1, lookaround_days + 1):
        yield requested_date + timedelta(days=offset)
        yield requested_date - timedelta(days=offset)


def fetch_nbrb_daily_rates(target_date=None, lookaround_days=10, currencies=None):
    requested_date = target_date or date.today()
    currency_filter = set(_normalize_currency_codes(currencies)) if currencies else None

    for probe_date in _nearest_probe_dates(requested_date, lookaround_days):
        endpoint = f"{settings.NBRB_API_URL.rstrip('/')}/rates?periodicity=0&ondate={probe_date.isoformat()}"
        payload = _fetch_json(endpoint)
        if not isinstance(payload, list) or not payload:
            continue

        rates_to_byn = {'BYN': Decimal('1')}
        rate_date = None

        for item in payload:
            abbreviation = item['Cur_Abbreviation'].upper()
            if currency_filter and abbreviation not in currency_filter:
                continue
            scale = Decimal(str(item['Cur_Scale']))
            official_rate = Decimal(str(item['Cur_OfficialRate']))
            rates_to_byn[abbreviation] = official_rate / scale
            if rate_date is None:
                rate_date = date.fromisoformat(item['Date'][:10])

        return {
            'requested_date': requested_date,
            'date': rate_date,
            'rates_to_byn': rates_to_byn,
        }

    raise RuntimeError('НБРБ не вернул курсы ни на выбранную, ни на ближайшие доступные даты.')


def fetch_nbrb_rate_dynamics(currency_code, *, start_date, end_date):
    start_date = _normalize_date(start_date)
    end_date = _normalize_date(end_date)
    if end_date < start_date:
        raise RuntimeError('Дата окончания диапазона курсов не может быть раньше даты начала.')

    currency_meta = _fetch_currency_meta(currency_code)
    rates_to_byn = {}
    chunk_start = start_date
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=364), end_date)
        endpoint = (
            f"{settings.NBRB_API_URL.rstrip('/')}/rates/dynamics/{currency_meta['id']}"
            f"?startdate={chunk_start.isoformat()}&enddate={chunk_end.isoformat()}"
        )
        payload = _fetch_json(endpoint)
        if not isinstance(payload, list) or not payload:
            raise RuntimeError(
                f'НБРБ не вернул исторические курсы для {currency_code} '
                f'за период {chunk_start.isoformat()} - {chunk_end.isoformat()}.'
            )

        for item in payload:
            rate_date = date.fromisoformat(item['Date'][:10])
            official_rate = Decimal(str(item['Cur_OfficialRate']))
            rates_to_byn[rate_date] = official_rate / currency_meta['scale']

        chunk_start = chunk_end + timedelta(days=1)

    return rates_to_byn


def sync_nbrb_rates(base_currency=None, target_date=None, currencies=None):
    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    currency_codes = _normalize_currency_codes(currencies)
    if currency_codes and base_currency != 'BYN':
        currency_codes = tuple(dict.fromkeys([*currency_codes, base_currency]))

    snapshot = fetch_nbrb_daily_rates(target_date=target_date, currencies=currency_codes or None)
    rates_to_byn = snapshot['rates_to_byn']

    if base_currency not in rates_to_byn:
        raise RuntimeError(f'Валюта {base_currency} отсутствует в дневных курсах НБРБ.')

    base_to_byn = rates_to_byn[base_currency]
    synced_count = 0

    for currency, currency_to_byn in rates_to_byn.items():
        if currency == base_currency:
            continue
        rate_to_base = _quantize_rate(currency_to_byn / base_to_byn)
        upsert_fx_rate(
            {
                'from_currency': currency,
                'to_currency': base_currency,
                'effective_date': snapshot['date'],
                'rate': rate_to_base,
            }
        )
        synced_count += 1

    upsert_fx_rate(
        {
            'from_currency': 'BYN',
            'to_currency': base_currency,
            'effective_date': snapshot['date'],
            'rate': _quantize_rate(Decimal('1') / base_to_byn),
        }
    )

    return {
        'base_currency': base_currency,
        'requested_date': snapshot['requested_date'].isoformat() if snapshot['requested_date'] else '',
        'rate_date': snapshot['date'].isoformat() if snapshot['date'] else '',
        'synced_count': synced_count + 1,
    }


def sync_nbrb_rates_history(base_currency=None, *, start_date=None, end_date=None, currencies=None):
    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    start_date = _normalize_date(start_date or DEFAULT_NBRB_HISTORY_START)
    end_date = _normalize_date(end_date or timezone.localdate())
    if end_date < start_date:
        raise RuntimeError('Дата окончания диапазона курсов не может быть раньше даты начала.')

    currency_codes = set(_normalize_currency_codes(currencies or DEFAULT_NBRB_HISTORY_CURRENCIES))
    currency_codes.discard('BYN')
    if base_currency != 'BYN':
        currency_codes.add(base_currency)

    rates_by_currency = {
        currency: fetch_nbrb_rate_dynamics(currency, start_date=start_date, end_date=end_date)
        for currency in sorted(currency_codes)
    }

    available_dates = set()
    for rates in rates_by_currency.values():
        available_dates.update(rates.keys())

    if not available_dates:
        raise RuntimeError('НБРБ не вернул исторические курсы для выбранного диапазона.')

    synced_count = 0
    latest_rate_date = None
    for rate_date in sorted(available_dates):
        base_to_byn = Decimal('1') if base_currency == 'BYN' else rates_by_currency.get(base_currency, {}).get(rate_date)
        if base_to_byn is None:
            continue

        for currency in sorted(currency_codes):
            if currency == base_currency:
                continue
            currency_to_byn = rates_by_currency.get(currency, {}).get(rate_date)
            if currency_to_byn is None:
                continue
            rate_to_base = _quantize_rate(currency_to_byn / base_to_byn)
            upsert_fx_rate(
                {
                    'from_currency': currency,
                    'to_currency': base_currency,
                    'effective_date': rate_date,
                    'rate': rate_to_base,
                }
            )
            synced_count += 1

        if base_currency != 'BYN':
            upsert_fx_rate(
                {
                    'from_currency': 'BYN',
                    'to_currency': base_currency,
                    'effective_date': rate_date,
                    'rate': _quantize_rate(Decimal('1') / base_to_byn),
                }
            )
            synced_count += 1

        latest_rate_date = rate_date

    return {
        'base_currency': base_currency,
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'rate_date': latest_rate_date.isoformat() if latest_rate_date else '',
        'synced_count': synced_count,
    }


def _get_latest_rate_record(base_currency=None):
    target = (base_currency or settings.BASE_CURRENCY).upper()
    return FXRate.objects.filter(to_currency=target).order_by('-effective_date', '-updated_at').first()


def should_auto_sync_nbrb_rates(base_currency=None):
    latest = _get_latest_rate_record(base_currency)
    if not latest:
        return True
    return timezone.localtime(latest.updated_at).date() < timezone.localdate()


def auto_sync_nbrb_rates_if_stale(base_currency=None, target_date=None):
    if not should_auto_sync_nbrb_rates(base_currency):
        return None
    try:
        return sync_nbrb_rates(base_currency=base_currency, target_date=target_date)
    except Exception:
        return None


def get_fx_rate_snapshot(base_currency=None):
    target = (base_currency or settings.BASE_CURRENCY).upper()
    latest_by_currency = []
    seen = set()
    for item in FXRate.objects.filter(to_currency=target).order_by('from_currency', '-effective_date', '-updated_at'):
        if item.from_currency in seen:
            continue
        seen.add(item.from_currency)
        latest_by_currency.append(item)
    return latest_by_currency


def get_latest_rate_date(base_currency=None):
    latest = _get_latest_rate_record(base_currency)
    return latest.effective_date if latest else None


def get_base_currency_rate_to_byn(base_currency=None):
    target = (base_currency or settings.BASE_CURRENCY).upper()
    if target == 'BYN':
        return Decimal('1')

    byn_to_base = FXRate.objects.filter(from_currency='BYN', to_currency=target).order_by('-effective_date', '-updated_at').first()
    if not byn_to_base or not byn_to_base.rate:
        return None

    return _quantize_rate(Decimal('1') / byn_to_base.rate)