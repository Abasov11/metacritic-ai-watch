Шаг 6 — упаковка и документация. Деплой на сервер сделаю я сам по твоей инструкции, ничего в системе (nginx, systemd, certbot) не трогай.

Что нужно:
1. `Dockerfile` (python:3.12-slim, non-root пользователь, ffmpeg если нужен для yt-dlp, `data/` как volume) и `docker-compose.yml` (сервис `web`, порт `127.0.0.1:8010:8010`, `env_file: .env`, `restart: unless-stopped`, healthcheck на `/healthz`). Проверь сборку и запуск локально, если docker доступен; если нет — напиши, что не проверял.
2. `deploy/metacritic-ai-watch.service` — systemd unit для запуска без docker (`.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8010`, `WorkingDirectory`, `EnvironmentFile`, `Restart=always`, `MemoryMax=1G`), и `deploy/nginx.conf` — server-блок под домен `${DOMAIN}` с проксированием на 8010, `proxy_buffering off` и увеличенным `proxy_read_timeout` для SSE на `/monitor/stream`.
3. `README.md` по-русски: что это, архитектура (одна схема текстом: scheduler → crawler → scraper/LLM/YouTube → SQLite → web/SSE), как выбираются игры (логика дня и страниц), как устроены резюме и похожие игры, выбор модели (ссылка на `docs/MODEL_CHOICE.md`), запуск локально (`make dev` или три команды), переменные окружения (таблица из `.env.example`), тесты, деплой (docker и systemd), ограничения и что бы я сделал дальше. Без маркетинга, коротко и по делу.
4. `Makefile`: `dev`, `test`, `lint`, `crawl-once`, `docker-up`.
5. Прогони `ruff check`, `ruff format --check`, `pytest -q` — приведи вывод. Обнови `docs/DOD.md`: отметь галочками пункты, которые реально закрыты, и напиши одной строкой у каждого, чем это проверено.

Коммить по частям. В конце — отчёт: сделано / проверено запуском / не получилось.
