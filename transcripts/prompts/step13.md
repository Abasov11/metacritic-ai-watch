Шаг 13 — первый прогон CI на GitHub (https://github.com/Abasov11/metacritic-ai-watch/actions/runs/34049658799): job `docker` зелёный, job `checks` упал на pytest. Дословно:

```
FAILED tests/test_covers.py::test_the_game_page_uses_the_local_copy - sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) unable to open database file
FAILED tests/test_covers.py::test_a_game_without_any_cover_says_so_on_its_card - ... unable to open database file
FAILED tests/test_covers.py::test_a_game_with_a_cover_is_untouched - ...
FAILED tests/test_covers.py::test_a_game_falling_back_to_the_remote_cover_is_untouched - ...
FAILED tests/test_covers.py::test_an_empty_title_does_not_break_the_placeholder - ...
engine = Engine(sqlite:////home/runner/work/metacritic-ai-watch/metacritic-ai-watch/data/app.db)
5 failed, 267 passed
```

Причина: эти пять тестов используют движок по умолчанию (`data/app.db` из `app.db`), а не временную базу из фикстур — локально проходили только потому, что `data/` существует на этой машине. Это дефект изоляции тестов, а не CI.

1. Почини корень: тесты веб-слоя должны получать приложение с временной базой (та же фикстура, что у остальных web-тестов); проверь, нет ли ещё тестов, которые неявно зависят от `data/` или от содержимого реальной базы. Надёжный способ проверить: прогони `pytest` с `DATA_DIR` на несуществующий путь или временно переименовав `data/` (верни обратно — сервис под systemd читает её; на время проверки лучше `DATA_DIR=/tmp/none-$$ pytest -q`).
2. Добавь защитную фикстуру уровня сессии (autouse) в `conftest.py`, которая направляет `DATA_DIR`/движок во временный каталог до импорта приложения, чтобы ни один тест физически не мог открыть реальную базу.
3. `ruff check`, `ruff format --check`, `pytest -q` (обычный) и `DATA_DIR=/tmp/none-ci pytest -q` — дословно. Коммит.
