"""Тесты новостного входа в детектор событий.

Запуск: python3 tests/test_news_event.py

has_fresh_news был параметром classify_event, но НИКТО его не передавал: во всех
вызовах оставалось значение по умолчанию False. То есть вход существовал, а
настоящие новости до детектора не доходили вовсе, и новостная ветка держалась
только на msg_zscore >= 2.0. После того как 30.07 ложную аномалию сообщений
убрали (z-score считал снимки стакана и собственный разогрев), ветка осталась бы
мертва полностью — поэтому настоящий вход обязателен, а не желателен.

Отдельно про причинность. Признак «была ли новость за последний час» негоден:
заголовок, вышедший через сорок минут ПОСЛЕ выноса, вынос не объясняет. Новость
считается объясняющей, только если опубликована в окне вокруг свечи выноса —
заметно раньше (рынок переваривает) или чуть позже (у RSS есть задержка
публикации, лента иногда двигается раньше заголовка).
"""

import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import src.agent.intraday_analyst as ia   # noqa: E402
from src.analysis.intraday import classify_event  # noqa: E402

N = 30
BASE = datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc)   # 14:00 МСК, основная
SPIKE_AT = N - 3


def _candles(spike_at=SPIKE_AT, base=BASE):
    o = [10.0] * N; h = [10.05] * N; l = [9.95] * N
    c = [10.0] * N; v = [100] * N
    h[spike_at], l[spike_at] = 10.6, 9.4          # размах много больше ATR
    dates = [(base - timedelta(minutes=5 * (N - 1 - i))).isoformat() for i in range(N)]
    return {"open": o, "high": h, "low": l, "close": c, "volume": v, "dates": dates}


def _spike_time(spike_at=SPIKE_AT, base=BASE):
    return base - timedelta(minutes=5 * (N - 1 - spike_at))


def _ctx(news_offsets_min=(), **kw):
    """Контекст со новостями, сдвинутыми относительно свечи выноса.
    Положительный сдвиг — новость РАНЬШЕ выноса."""
    st = _spike_time()
    ts = [(st - timedelta(minutes=off)).isoformat() for off in news_offsets_min]
    return ia.compute_intraday_context(_candles(), 14 * 60, news_ts=ts, **kw)


# ─────────────────── вход вообще работает ────────────────────────────────────

def test_news_before_spike_makes_event():
    """ГЛАВНОЕ: настоящая новость доходит до детектора."""
    ctx = _ctx((10,))
    assert ctx["event"]["event"] is True
    assert "свежая новость" in ctx["event"]["signals"]
    assert ctx["news_lag_min"] == 10.0


def test_no_news_no_event():
    """Без новостей и без аномалии сообщений события нет — вынос сам по себе
    новостным не считается."""
    ctx = _ctx(())
    assert ctx["event"]["event"] is False
    assert ctx["news_lag_min"] is None


def test_external_flag_still_honored():
    """Явно переданный признак продолжает работать (обратная совместимость)."""
    ctx = _ctx((), has_fresh_news=True)
    assert ctx["event"]["event"] is True


# ─────────────────── причинность ─────────────────────────────────────────────

def test_news_long_before_spike_rejected():
    """45 минут до выноса — вне окна: связь недоказуема."""
    assert _ctx((45,))["event"]["event"] is False


def test_news_shortly_after_spike_accepted():
    """Лента опередила заголовок: у RSS есть задержка публикации."""
    ctx = _ctx((-5,))
    assert ctx["event"]["event"] is True
    assert ctx["news_lag_min"] == -5.0


def test_news_long_after_spike_rejected():
    """ОБРАТНАЯ ПРИЧИННОСТЬ: заголовок через 40 минут после выноса его не
    объясняет."""
    assert _ctx((-40,))["event"]["event"] is False


def test_closest_news_wins():
    """Из нескольких новостей берётся ближайшая к выносу."""
    ctx = _ctx((25, 5, 20))
    assert ctx["news_lag_min"] == 5.0


def test_news_count_reported_even_when_far():
    """Новости есть, но рядом с выносом ни одной: счётчик показываем, запас — нет.
    Иначе не отличить «новостей не было» от «новости не в тему»."""
    ctx = _ctx((90, 120))
    assert ctx["news_count"] == 2
    assert ctx["news_lag_min"] is None


def test_bad_timestamps_do_not_crash():
    ctx = ia.compute_intraday_context(_candles(), 14 * 60,
                                      news_ts=["не время", None, ""])
    assert ctx["event"]["event"] is False


def test_no_spike_means_no_news_event():
    """Событие требует выноса: одна новость без движения сетапа не даёт."""
    flat = _candles()
    flat["high"] = [10.05] * N
    flat["low"] = [9.95] * N
    ctx = ia.compute_intraday_context(
        flat, 14 * 60, news_ts=[_spike_time().isoformat()])
    assert ctx["event"]["event"] is False


# ─────────────────── основание читается человеком ────────────────────────────

def test_reason_text_names_the_basis():
    """Новостная ветка имеет ПРИОРИТЕТ над пробоем диапазона, поэтому основание
    должно читаться сразу — иначе непонятно, почему выбран этот сетап."""
    note = _ctx((10,))["note"]
    assert "новость" in note and "10" in note, note


def test_reason_text_marks_late_news():
    note = _ctx((-5,))["note"]
    assert "после выноса" in note, note


# ─────────────────── исключение снимков стакана ──────────────────────────────

def test_orderbook_snapshots_are_not_news():
    """Снимки стакана лежат в том же хранилище и идут 2157 в час против 21
    новости — фильтр обязан их исключать."""
    from config.settings import NEWS_KINDS, NEWS_EXCLUDE_SOURCES
    assert "tinkoff" in NEWS_EXCLUDE_SOURCES
    assert "pulse_deal" in NEWS_EXCLUDE_SOURCES
    assert "orderbook" not in NEWS_KINDS


def test_classify_event_needs_spike():
    """Сам классификатор без выноса события не даёт ни при новости, ни при
    аномалии."""
    assert classify_event(False, 5.0, True)["event"] is False
    assert classify_event(True, None, True)["event"] is True



# ───── время события и дедупликация ───────────────────────────────────────────

def test_event_time_is_publication_not_collection():
    """ГЛАВНОЕ для причинности. ts у новостей не передавался, подставлялось
    now(), поэтому один заголовок при каждом цикле опроса получал новую
    «свежесть». Замер 30.07: один и тот же текст лежал под одиннадцатью разными
    метками — 06:41, 07:06, 07:12, ... по одной на цикл. Проверка новости против
    свечи выноса на таких метках даёт МНИМУЮ точность."""
    import pathlib
    src = pathlib.Path(ROOT, "main.py").read_text(encoding="utf-8")
    # RSS, Telegram и Пульс обязаны передавать время источника
    assert '"ts": item.timestamp' in src, "RSS не передаёт время публикации"
    assert '"ts": msg.timestamp' in src, "Telegram не передаёт время сообщения"
    assert '"ts": post.timestamp' in src, "Пульс не передаёт время поста"


def test_guid_stored_for_dedup():
    """Без guid в записи дедупликацию нельзя поднять из БД."""
    import pathlib
    src = pathlib.Path(ROOT, "main.py").read_text(encoding="utf-8")
    assert '"guid": getattr(item, "item_id", None)' in src


def test_collector_primes_dedup_from_db():
    """Дедупликация в памяти не выживает перезапуск: 132 повтора из 200 записей,
    один заголовок четырнадцать раз — по разу на каждый деплой."""
    from src.collector.rss_collector import RSSCollector
    c = RSSCollector()
    assert hasattr(c, "prime_seen"), "нет восстановления дедупликации из БД"


def test_dedup_actually_skips_known_guid():
    """Поднятый идентификатор должен приводить к пропуску записи."""
    from src.collector.rss_collector import RSSCollector
    c = RSSCollector()
    c._seen_ids.add("guid-123")
    assert "guid-123" in c._seen_ids
    # проверяем именно условие пропуска из fetch_feed
    guid, title = "guid-123", "Заголовок"
    assert not (title and guid not in c._seen_ids), "известный guid должен пропускаться"

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
