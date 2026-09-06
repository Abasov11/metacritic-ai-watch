Привет. Делаем тестовое задание для Skytec Games — сервис-наблюдатель за новыми играми на Metacritic. Прочитай `CLAUDE.md`, `docs/TASK.md` и `docs/DOD.md`.

Шаг 1 из плана — каркас проекта и парсер Metacritic. Веб, база и LLM будут следующими шагами, сейчас их не делай.

Что нужно:
1. Каркас: `pyproject.toml` (пакет `app`, зависимости из CLAUDE.md, extras `dev` с pytest, ruff), `.venv`, `app/__init__.py`, `app/config.py` (pydantic-settings, читает `.env`), `.env.example`.
2. Модуль `app/scraper/metacritic.py`:
   - `fetch_new_releases() -> list[str]` — слаги первых 20 игр из раздела «New Releases» на https://www.metacritic.com/game/
   - `fetch_browse_new(page: int) -> list[str]` — слаги со страницы https://www.metacritic.com/browse/game/all/all/all-time/new/?page=N
   - `fetch_game(slug) -> GameData` — dataclass/pydantic: title, slug, cover_url, description, developer, publisher, release_date, genres, platforms: list[{name, metascore, userscore}] (платформ много: PS5, Switch 2, PC …; счёта может не быть — None), video_url (трейлер), metacritic_url.
   - `fetch_reviews(slug, kind: Literal["critic","user"], limit=40) -> list[Review]`: text, score, author (издание или ник), date. Страницы: `/game/<slug>/critic-reviews/` и `/game/<slug>/user-reviews/`.
   - Общий HTTP-клиент: httpx, браузерный User-Agent, таймаут 20с, 3 ретрая с backoff, пауза ≥1с между запросами.
3. Что я уже выяснил про сайт (проверь сам, но не начинай с нуля): страницы — Nuxt SSR, curl с браузерным UA отдаёт 200 и полный HTML. В `<head>` карточки игры есть JSON-LD (`application/ld+json`) с description, publisher и `trailer` типа VideoObject. Основные данные (платформы со счётами, разработчик, картинки с `bucketPath`, releaseDate, genres) лежат в Nuxt-payload внутри страницы: это devalue-массив, где значения ссылаются друг на друга по индексам — его надо честно раскодировать (функция-декодер с тестом), а не искать регулярками. В списках слаги видны как `href="/game/<slug>/"`. Если найдёшь внутренний JSON-API, которым сама страница подгружает отзывы (apiKey виден в HTML), — можно использовать его, но с фолбэком на HTML.
4. Тесты: скачай реальные страницы (главная, browse page 2, карточка `hollow-knight-silksong`, её critic- и user-reviews) в `tests/fixtures/` и напиши pytest на парсинг фикстур. Тесты в сеть не ходят.
5. Живая проверка: `python -m app.scraper.metacritic hollow-knight-silksong` печатает JSON карточки + по 3 первых отзыва каждого вида; ещё покажи вывод для первых 3 слагов из New Releases. В ответе приведи дословный вывод.

Закоммить по-частям (каркас, парсер, тесты). В конце — краткий отчёт: что сделано, что проверено запуском, что не получилось.
