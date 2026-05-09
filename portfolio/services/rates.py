import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from django.conf import settings

from portfolio.models import FXRate

from .transactions import upsert_fx_rate


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


def fetch_nbrb_daily_rates(target_date=None, lookback_days=10):
    requested_date = target_date or date.today()

    for offset in range(lookback_days + 1):
        probe_date = requested_date - timedelta(days=offset)
        endpoint = f"{settings.NBRB_API_URL.rstrip('/')}/rates?periodicity=0&ondate={probe_date.isoformat()}"
        payload = _fetch_json(endpoint)
        if not isinstance(payload, list) or not payload:
            continue

        rates_to_byn = {'BYN': Decimal('1')}
        rate_date = None

        for item in payload:
            abbreviation = item['Cur_Abbreviation'].upper()
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

    raise RuntimeError('НБРБ не вернул курсы ни на выбранную, ни на ближайшие даты.')


def sync_nbrb_rates(base_currency=None, target_date=None):
    base_currency = (base_currency or settings.BASE_CURRENCY).upper()
    snapshot = fetch_nbrb_daily_rates(target_date=target_date)
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


def get_fx_rate_snapshot(base_currency=None):
    target = (base_currency or settings.BASE_CURRENCY).upper()
    return FXRate.objects.filter(to_currency=target).order_by('from_currency')


def get_latest_rate_date(base_currency=None):
    target = (base_currency or settings.BASE_CURRENCY).upper()
    latest = FXRate.objects.filter(to_currency=target).order_by('-effective_date', '-updated_at').first()
    return latest.effective_date if latest else None


def get_base_currency_rate_to_byn(base_currency=None):
    target = (base_currency or settings.BASE_CURRENCY).upper()
    if target == 'BYN':
        return Decimal('1')

    byn_to_base = FXRate.objects.filter(from_currency='BYN', to_currency=target).order_by('-effective_date', '-updated_at').first()
    if not byn_to_base or not byn_to_base.rate:
        return None

    return _quantize_rate(Decimal('1') / byn_to_base.rate)