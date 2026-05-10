from datetime import date

from django.core.management.base import BaseCommand, CommandError

from portfolio.services.rates import DEFAULT_NBRB_HISTORY_CURRENCIES, DEFAULT_NBRB_HISTORY_START, sync_nbrb_rates_history


class Command(BaseCommand):
    help = 'Загружает исторические курсы НБРБ в таблицу FXRate и дополняет ее новыми датами.'

    def add_arguments(self, parser):
        parser.add_argument('--start-date', default=DEFAULT_NBRB_HISTORY_START.isoformat(), help='Начало диапазона в формате YYYY-MM-DD.')
        parser.add_argument('--end-date', default=date.today().isoformat(), help='Конец диапазона в формате YYYY-MM-DD.')
        parser.add_argument(
            '--currencies',
            nargs='*',
            default=list(DEFAULT_NBRB_HISTORY_CURRENCIES),
            help='Список валют НБРБ для загрузки. По умолчанию: USD EUR RUB.',
        )

    def handle(self, *args, **options):
        try:
            result = sync_nbrb_rates_history(
                start_date=options['start_date'],
                end_date=options['end_date'],
                currencies=options['currencies'],
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Курсы НБРБ обновлены: {result['synced_count']} записей, базовая валюта {result['base_currency']}, диапазон {result['start_date']} - {result['end_date']}, последняя дата {result['rate_date']}."
            )
        )