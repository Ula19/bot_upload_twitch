# bot_4_twitch

Telegram-бот для скачивания контента с Twitch: VOD, клипы, хайлайты.

## Стек

- Python 3.12 + aiogram 3.26
- PostgreSQL 16 + SQLAlchemy 2 (asyncpg)
- yt-dlp (Twitch поддерживается из коробки, без прокси)
- ffmpeg (сплит больших файлов)
- Local Bot API (файлы до 2 ГБ)
- Docker + docker-compose

## Деплой

```bash
cp .env.example .env
# обязательно заполнить: BOT_TOKEN, ADMIN_IDS, DB_PASSWORD, API_ID, API_HASH
docker compose up -d --build
```

## Структура

```
bot/
├── main.py           # entrypoint
├── config.py         # настройки из .env
├── i18n.py           # переводы ru/uz/en
├── emojis.py         # кастомные emoji
├── database/         # модели и CRUD (PostgreSQL)
├── handlers/         # start, admin, download (TODO)
├── middlewares/      # подписка, rate limit
├── keyboards/        # inline-кнопки
├── services/         # twitch.py (TODO)
└── utils/            # команды меню, helpers
```

## Поддерживаемые ссылки

- `https://www.twitch.tv/videos/<id>` — VOD / хайлайт
- `https://clips.twitch.tv/<slug>` — клип
- `https://www.twitch.tv/<channel>/clip/<slug>` — клип канала
