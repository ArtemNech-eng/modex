"""Тесты видимости состояния источников данных.

Запуск: python3 tests/test_source_health.py

30.07 из пяти RSS-источников два были мертвы, и об этом никто не знал:
  • Финам Новости  — HTTP 403 Forbidden;
  • БКС Экспресс   — вместо RSS страница анти-бота (загрузчик servicepipe,
                     редирект через JS), что в ET падало как «mismatched tag».
Коллектор на отказ возвращал пустой список молча (status != 200 -> return []),
а исключения уходили в debug-лог. Диагностика показывала только суммарное число
событий, поэтому потеря 40% источников выглядела как обычный день.

Отдельно: Telegram сообщал api_configured=true и string_session=true при НУЛЕ
событий за час и 21 канале со статусом active — статус брался из конфига, а не
измерялся. «Настроен» и «производит» — разные вещи.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.collector.rss_collector import RSSCollector  # noqa: E402


def _c():
    return RSSCollector()


def test_health_empty_before_first_pass():
    assert _c().source_health == {}


def test_note_records_success_with_item_count():
    c = _c()
    c._note({"name": "Smart-lab", "url": "u"}, "ok", None, 17)
    h = c.source_health["Smart-lab"]
    assert h["status"] == "ok" and h["items"] == 17
    assert h["checked_at"]


def test_note_records_http_error():
    """Финам: HTTP 403 обязан быть видимым, а не пустым списком."""
    c = _c()
    c._note({"name": "Финам Новости", "url": "u"}, "error", "HTTP 403")
    h = c.source_health["Финам Новости"]
    assert h["status"] == "error" and "403" in h["message"]


def test_note_records_antibot_block():
    """БКС: HTML вместо RSS — это блокировка, а не сбой разбора."""
    c = _c()
    c._note({"name": "БКС Экспресс", "url": "u"}, "blocked", "вместо RSS пришёл HTML")
    assert c.source_health["БКС Экспресс"]["status"] == "blocked"


def test_dead_sources_are_countable():
    """Главное: мёртвые источники должны считаться, чтобы потеря 2 из 5 не
    выглядела обычным днём."""
    c = _c()
    c._note({"name": "A", "url": "u"}, "ok", None, 5)
    c._note({"name": "B", "url": "u"}, "ok", None, 3)
    c._note({"name": "C", "url": "u"}, "ok", None, 2)
    c._note({"name": "Финам", "url": "u"}, "error", "HTTP 403")
    c._note({"name": "БКС", "url": "u"}, "blocked", "HTML")
    dead = [n for n, h in c.source_health.items() if h["status"] != "ok"]
    assert len(dead) == 2 and set(dead) == {"Финам", "БКС"}
    assert len(c.source_health) == 5


def test_antibot_html_detected_by_marker():
    """Проверка распознавания: страница анти-бота начинается с doctype html."""
    body = ('<!DOCTYPE html>\n<html>\n<head>\n'
            '<noscript><meta http-equiv="refresh" content="0; url=/exhkqyad">'
            '</noscript></head>')
    head = body.lstrip()[:200].lower()
    assert head.startswith("<!doctype html") or "<html" in head


def test_valid_rss_not_mistaken_for_html():
    """Живой RSS не должен попасть под правило анти-бота."""
    body = '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
    head = body.lstrip()[:200].lower()
    assert not (head.startswith("<!doctype html") or "<html" in head)


if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ПАДАЕТ {name}: {e}")
        except Exception as e:                      # noqa: BLE001
            failed += 1
            print(f"  ОШИБКА {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed} из {len(tests)} пройдено")
    sys.exit(1 if failed else 0)
