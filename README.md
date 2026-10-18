# Gift Upgrades Bot

Telegram-бот, который присылает подарки, **только что улучшенные** до уникальных (NFT-подарки
`t.me/nft/...`), с гибкими фильтрами.

## Как это работает

Bot API не отдаёт ленту улучшений, поэтому есть два компонента:

* **Наблюдатель** — обычный аккаунт Telegram (Telethon/MTProto). Он берёт список коллекций
  (`payments.getStarGifts`), а затем по кругу запрашивает `payments.getUniqueStarGift`.
  Уникальные подарки нумеруются по порядку (`PlushPepe-1`, `PlushPepe-2`, …), и каждый сообщает,
  сколько экземпляров уже выпущено (`availability_issued`). Все номера между последним известным
  и текущим — это новые улучшения.
* **Бот** (aiogram 3) — хранит фильтры пользователей в SQLite и рассылает подходящие подарки.

При первом запуске история не рассылается: бот запоминает текущие номера и дальше
шлёт только новые улучшения.

## Фильтры

* 🎁 коллекции (выбор из списка)
* 🧩 модель, 🎨 фон, 🔣 узор — по названию (через запятую, поиск по вхождению)
* ⭐ максимальная редкость модели / фона / узора (5%, 2%, 1%, 0.5%, 0.2%)
* 🔢 максимальный номер и «красивые номера» (≤100, 777, 2500, 12321, 1234…)
* 👤 только владельцы с @username
* 💰 только выставленные на продажу

Команды: `/filters`, `/recent [N]`, `/pause`, `/resume`, `/stats` (для админов).

## Запуск

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # заполните BOT_TOKEN, API_ID, API_HASH
python -m giftbot.login     # один раз: вход в аккаунт-наблюдатель (код из Telegram)
python -m giftbot           # запуск бота
```

* `BOT_TOKEN` — от [@BotFather](https://t.me/BotFather).
* `API_ID` / `API_HASH` — с [my.telegram.org](https://my.telegram.org).
  Для наблюдателя лучше завести отдельный аккаунт.

Слаг коллекции вычисляется из названия (`Plush Pepe` → `PlushPepe`, `Durov's Cap` → `DurovsCap`).
Если для какой-то коллекции он не совпал, в логе будет предупреждение. Тогда задайте слаг вручную:
`SLUG_OVERRIDES=Название=Slug;Другое=Slug2`.

Частоту опроса задают `POLL_INTERVAL` и `REQUEST_DELAY`. Слишком частые запросы приводят
к FloodWait; бот сам переждёт его, но уведомления придут с задержкой.

## Тесты

```bash
pip install pytest && python -m pytest -q
```

## Запуск на GitHub Actions

Workflow `.github/workflows/bot.yml` держит бота запущенным: каждый запуск работает ~5 ч 45 мин,
сохраняет базу в кэш Actions и сам запускает следующий. Между запусками бывает пауза в
пару минут; пропущенные улучшения бот догонит после старта.

1. Локально выполните `python -m giftbot.login` и скопируйте напечатанный `SESSION_STRING`.
2. В репозитории: **Settings → Secrets and variables → Actions → New repository secret**,
   добавьте `BOT_TOKEN`, `API_ID`, `API_HASH`, `SESSION_STRING` (и по желанию `ADMINS`).
3. **Actions → bot → Run workflow**.

Учтите: GitHub не рассчитан на постоянные сервисы в Actions, и правила это формально запрещают.
