from django.core.management.base import BaseCommand, CommandError

from portfolio.services.rates import sync_nbrb_rates


class Command(BaseCommand):
    help = 'Загружает официальные дневные курсы НБРБ в таблицу FXRate.'

    def handle(self, *args, **options):
        try:
            result = sync_nbrb_rates()
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Курсы НБРБ обновлены: {result['synced_count']} валют, базовая валюта {result['base_currency']}, дата {result['rate_date']}."
            )
        )