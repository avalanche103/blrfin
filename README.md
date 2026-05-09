# MVP дашборда инвестиций

Локальный MVP дашборда личных инвестиций на Django + HTMX + SQLite.

## Что внутри

- Один Django-проект и одно приложение `portfolio`.
- HTML dashboard на Django templates с HTMX-обновлениями без полной перезагрузки.
- SQLite по умолчанию, но настройки БД вынесены в `.env` и совместимы с PostgreSQL.
- JSON API внутри того же проекта: `/api/accounts/`, `/api/transfers/`, `/api/portfolio/`, `/api/assets/`, `/api/transactions/`, `/api/prices/`.
- Учет счетов, активов, переводов, комиссий и импорт CSV.
- Официальные валютные курсы можно подтягивать из НБРБ без ключей и регистрации.

## Локальный старт

```powershell
cd e:\blrfin
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py runserver
```

Или через bat-файл:

```bat
run_local.bat
```

Дополнительные режимы:

```bat
run_local.bat check
run_local.bat migrate
run_local.bat run
```

Открыть:

- Dashboard: `http://127.0.0.1:8000/`
- Admin: `http://127.0.0.1:8000/admin/`

## Переменные окружения

Базовые значения уже лежат в `.env`:

- `DEBUG`
- `SECRET_KEY`
- `ALLOWED_HOSTS`
- `BASE_CURRENCY`
- `DB_ENGINE`
- `DB_NAME`
- `NBRB_API_URL`

Для перехода на PostgreSQL/GCP достаточно переключить `DB_ENGINE` и задать `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`.

## Курсы НБРБ

Проект умеет подтягивать официальные курсы Национального банка Республики Беларусь из открытого API:

- `https://api.nbrb.by/exrates/rates?periodicity=0`

Курсы автоматически пересчитываются к базовой валюте портфеля. По умолчанию базовая валюта `USD`, поэтому в таблицу `FXRate` сохраняются пары вида `EUR -> USD`, `BYN -> USD`, `RUB -> USD` и т.д.

Обновить курсы можно:

```powershell
.\.venv\Scripts\python.exe manage.py sync_nbrb_rates
```

Дашборд показывает дату официального курса НБРБ, ближайшую к запрошенной дате, которая реально была доступна в API.

## Импорт CSV

Поддерживаемые колонки:

```csv
transaction_type,source_account,destination_account,asset_symbol,asset_name,asset_class,asset_quantity,unit_price,amount,currency,fee,status,occurred_at,notes
deposit,,Брокерский счет USD,,, ,0,,1000,USD,0,completed,2026-05-08T10:00,Первичное пополнение
buy,Брокерский счет USD,,AAPL,Apple Inc,exchange,3,210,630,USD,1.5,completed,2026-05-08T11:00,Пример ручного импорта
transfer,Брокерский счет USD,Криптокошелек,,, ,0,,250,USD,2,completed,2026-05-08T12:00,Перевод средств
```

## Структура

```text
config/
portfolio/
  api/
  services/
  templates/portfolio/partials/
```