Шаг 14 — расшифровка летсплеев заработала: появился файл кук, и я нашёл рабочий рецепт, проверенный вживую на ролике `stK-_oN5DXg` (theRadBrad, Onimusha): получено 61 192 символа автосубтитров.

Что НЕ работает даже с куками: `youtube-transcript-api` (по-прежнему `RequestBlocked`, он куки не использует) и голый yt-dlp с куками (`The page needs to be reloaded`).

Что работает — yt-dlp с тремя добавками одновременно:
```
yt-dlp --js-runtimes node --remote-components ejs:github \
  --cookies data/youtube-cookies.txt \
  --extractor-args "youtubepot-bgutilscript:script_path=/srv/skytec/bgutil/server/build/generate_once.js" \
  --skip-download --write-auto-subs --write-subs --sub-langs en --sub-format json3 URL
```
- `--remote-components ejs:github` — yt-dlp скачивает решатель JS-челленджей (кэшируется в cachedir);
- `--js-runtimes node` — `/usr/bin/node` v22 есть;
- PO-токены — плагин `bgutil-ytdlp-pot-provider` (pip) + собранный node-скрипт `/srv/skytec/bgutil/server/build/generate_once.js` (репо Brainicism/bgutil-ytdlp-pot-provider, `npm ci && npx tsc` в `server/`). Без скрипта плагин молча неактивен.

Сделай:
1. В `app/youtube.py` источник субтитров — yt-dlp (библиотекой, те же опции: `js_runtimes`, `remote_components`, `cookiefile`, `extractor_args`, `writesubtitles`/`writeautomaticsub`, `subtitleslangs ["en","ru"]`, `subtitlesformat json3`, `skip_download`; субтитры читать из памяти или из временного каталога и сразу удалять). `youtube-transcript-api` убери из зависимостей, если после этого не нужен. Разбор json3 → текст (склейка `segs.utf8`, схлопывание пробелов).
2. Настройки: `YOUTUBE_COOKIES_FILE` (есть), новые `YOUTUBE_POT_SCRIPT` (путь к `generate_once.js`, пусто = плагин не используется), `YOUTUBE_JS_RUNTIME` (по умолчанию `node`), `YOUTUBE_REMOTE_COMPONENTS` (по умолчанию `ejs:github`). **Кэш yt-dlp (`cachedir`) обязательно внутри `DATA_DIR/yt-dlp-cache`** — сервис под systemd с `ProtectHome=read-only`, в `~/.cache` писать нельзя.
3. Плагин `bgutil-ytdlp-pot-provider` — в основные зависимости `pyproject.toml`; в `.env.example` и README (раздел «Летсплеи») — рецепт целиком: куки, node, сборка скрипта PO-токенов, какие переменные. В `Dockerfile` — node не тащи; напиши в README, что в контейнере стадия расшифровки требует смонтировать куки и скрипт, иначе работает в режиме «ролик без расшифровки».
4. Живая проверка через код приложения (не через CLI yt-dlp): `process_letsplay` для `onimusha-way-of-the-sword` и ещё одной игры с найденным роликом из базы — покажи источник текста, длину, заключение LLM и стоимость. Затем `python -m app.crawler --refresh-letsplays` (добавь такую команду: пересчитать только записи, у которых есть `video_id`, но `transcript_source = none`) — прогони по базе, приведи итог «сколько получили расшифровку / сколько нет и почему».
5. Тесты без сети: разбор json3, опции yt-dlp собираются из настроек (cookiefile/cachedir/extractor_args), отсутствие кук или скрипта не роняет стадию. `ruff`, `pytest -q` — дословно. Коммит по частям.

Значения переменных для этого сервера (в `.env` я уже добавил сам): `YOUTUBE_COOKIES_FILE=/srv/skytec/metacritic-ai-watch/data/youtube-cookies.txt`, `YOUTUBE_POT_SCRIPT=/srv/skytec/bgutil/server/build/generate_once.js`. Файл кук не печатай.
