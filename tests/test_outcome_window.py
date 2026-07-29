"""Тесты окна оценки исхода и учёта гэпов.

Запуск: python3 tests/test_outcome_window.py

Здесь считаются деньги, поэтому проверяется именно то, что раньше было неверно:
окно оценки схлопывалось до МСК-суток сигнала (вечерний сценарий на следующую
сессию оценить было нельзя), а выход по стопу записывался по цене стопа даже
если бар открылся сильно хуже — овернайт-убытки занижались.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.agent import intraday_analyst as ia  # noqa: E402

MSK = timezone(timedelta(hours=3))


def _bars(start_msk, specs, tf_min=5):
    """specs: список (open, high, low, close). Возвращает dict как fetch_intraday."""
    out = {"dates": [], "open": [], "high": [], "low": [], "close": [],
           "volume": [], "_source": "test"}
    t = start_msk
    for o, h, l, c in specs:
        out["dates"].append(t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        out["open"].append(o); out["high"].append(h)
        out["low"].append(l); out["close"].append(c)
        out["volume"].append(1000)
        t += timedelta(minutes=tf_min)
    return out


def _run(bars, start_msk, direction, entry, stop, target,
         now_msk, horizon_hours=None):
    async def go():
        orig = ia.fetch_intraday
        ia.fetch_intraday = lambda *a, **k: _async(bars)
        try:
            return await ia.intraday_outcome(
                "TEST", start_msk.isoformat(), direction, entry, stop, target,
                now_utc=now_msk.astimezone(timezone.utc),
                horizon_hours=horizon_hours)
        finally:
            ia.fetch_intraday = orig
    return asyncio.run(go())


async def _async(v):
    return v


SIG = datetime(2026, 7, 29, 21, 0, tzinfo=MSK)      # вечер, 21:00 МСК


def test_evening_signal_unevaluable_without_horizon():
    """БЕЗ горизонта: вечерний сценарий на следующую сессию оценить нельзя —
    свечи следующего дня в окно не попадают. Это прежнее поведение."""
    bars = _bars(SIG + timedelta(minutes=5), [
        (3000, 3010, 2995, 3005),                                   # вечер, тот же день
    ])
    nxt = _bars(datetime(2026, 7, 30, 10, 0, tzinfo=MSK), [
        (3050, 3300, 3040, 3290),                                   # СЛЕДУЮЩИЙ день: цель взята
    ])
    for k in ("dates", "open", "high", "low", "close", "volume"):
        bars[k] += nxt[k]
    now = datetime(2026, 7, 30, 12, 0, tzinfo=MSK)
    r = _run(bars, SIG, "up", 3000, 2860, 3290, now)                # горизонт НЕ задан
    # окно = сутки сигнала: цель следующего дня не видна
    assert r["outcome"] == "session", r
    assert r["realized_r"] < 1.0


def test_horizon_window_crosses_sessions():
    """С горизонтом: свечи следующей сессии учитываются, цель фиксируется."""
    bars = _bars(SIG + timedelta(minutes=5), [(3000, 3010, 2995, 3005)])
    nxt = _bars(datetime(2026, 7, 30, 10, 0, tzinfo=MSK), [
        (3050, 3300, 3040, 3290),
    ])
    for k in ("dates", "open", "high", "low", "close", "volume"):
        bars[k] += nxt[k]
    now = datetime(2026, 7, 31, 2, 0, tzinfo=MSK)   # окно закрылось
    r = _run(bars, SIG, "up", 3000, 2860, 3290, now, horizon_hours=27)
    assert r["outcome"] == "target", r
    assert r["realized_r"] > 0
    assert r["window_hours"] == 27.0


def test_pending_until_horizon_expires():
    """До истечения горизонта сделка остаётся в игре, а не финализируется."""
    bars = _bars(SIG + timedelta(minutes=5), [(3000, 3010, 2995, 3005)])
    now = SIG + timedelta(hours=2)                       # горизонт 11ч ещё не вышел
    r = _run(bars, SIG, "up", 3000, 2860, 3290, now, horizon_hours=11)
    assert r["outcome"] == "pending", r


def test_gap_below_stop_uses_open_not_stop():
    """ГЛАВНОЕ: бар открылся ниже стопа — выход по открытию, а не по стопу.
    Иначе овернайт-убыток занижается."""
    bars = _bars(SIG + timedelta(minutes=5), [(3000, 3005, 2995, 3000)])
    gap = _bars(datetime(2026, 7, 30, 10, 0, tzinfo=MSK), [
        (2700, 2720, 2680, 2700),                        # гэп ВНИЗ через стоп 2860
    ])
    for k in ("dates", "open", "high", "low", "close", "volume"):
        bars[k] += gap[k]
    now = datetime(2026, 7, 30, 12, 0, tzinfo=MSK)
    r = _run(bars, SIG, "up", 3000, 2860, 3290, now, horizon_hours=27)
    assert r["outcome"] == "stop"
    # 1R = 140. Выход по стопу дал бы -1.0R; выход по открытию 2700 → -2.14R
    assert r["realized_price"] == 2700.0, r["realized_price"]
    assert r["realized_r"] < -2.0, r["realized_r"]


def test_no_gap_exits_at_stop_price():
    """Без гэпа выход по стопу — ровно −1R, как и раньше."""
    bars = _bars(SIG + timedelta(minutes=5), [
        (3000, 3005, 2995, 3000),
        (2990, 2995, 2850, 2870),                        # проколол стоп внутри бара
    ])
    now = SIG + timedelta(hours=12)
    r = _run(bars, SIG, "up", 3000, 2860, 3290, now, horizon_hours=11)
    assert r["outcome"] == "stop"
    assert r["realized_price"] == 2860.0
    assert abs(r["realized_r"] + 1.0) < 0.01


def test_gap_up_on_short_uses_open():
    """Симметрично для шорта: гэп ВВЕРХ через стоп → выход по открытию."""
    bars = _bars(SIG + timedelta(minutes=5), [(100, 101, 99, 100)])
    gap = _bars(datetime(2026, 7, 30, 10, 0, tzinfo=MSK), [(115, 118, 114, 116)])
    for k in ("dates", "open", "high", "low", "close", "volume"):
        bars[k] += gap[k]
    now = datetime(2026, 7, 30, 12, 0, tzinfo=MSK)
    r = _run(bars, SIG, "down", 100, 105, 90, now, horizon_hours=27)
    assert r["outcome"] == "stop"
    assert r["realized_price"] == 115.0
    assert r["realized_r"] < -2.0


def test_entry_touched_recorded_true():
    """Диапазон бара накрыл вход — заявка исполнилась бы, помечаем."""
    bars = _bars(SIG + timedelta(minutes=5), [(3010, 3015, 2990, 3005)])
    now = SIG + timedelta(hours=12)
    r = _run(bars, SIG, "up", 3000, 2860, 3290, now, horizon_hours=11)
    assert r["entry_touched"] is True


def test_entry_touched_recorded_false():
    """Цена не дошла до лимитки — направление могло быть верным, но заявка
    не исполнилась. Ничего не блокируем, но факт фиксируем."""
    bars = _bars(SIG + timedelta(minutes=5), [
        (3050, 3100, 3040, 3090),
        (3090, 3290, 3080, 3280),                        # ушла вверх, до 3000 не спускалась
    ])
    now = SIG + timedelta(hours=12)
    r = _run(bars, SIG, "up", 3000, 2860, 3290, now, horizon_hours=11)
    assert r["entry_touched"] is False
    # направление оценивается как раньше — это решение владельца
    assert r["outcome"] in ("target", "session")



# ─────────────── горизонт: окно должно накрывать торгуемую сессию ────────────

from src.agent.external_signal import _default_horizon_hours as _hz  # noqa: E402


def test_evening_signal_horizon_covers_next_session():
    """ГЛАВНОЕ: вечерний сигнал должен жить до конца СЛЕДУЮЩЕЙ сессии.
    Раньше окно истекало в 08:00 — до открытия в 10:00, и сценарий гарантированно
    истекал неисполнимым."""
    now = datetime(2026, 7, 29, 21, 0, tzinfo=MSK)
    h = _hz(now)
    due = now + timedelta(hours=h)
    # округление вверх может дать 00:00 31-го вместо 23:50 30-го: перебор на
    # 10 минут безвреден (торгов там нет), а недобор оставил бы сессию
    # неоценённой. Требуем именно НЕ РАНЬШЕ закрытия следующей сессии.
    assert due >= datetime(2026, 7, 30, 18, 40, tzinfo=MSK), (h, due)
    assert h >= 24, h


def test_intraday_signal_horizon_matures_next_morning():
    """Сигнал во время сессии: окно до утра следующего дня, как и было."""
    now = datetime(2026, 7, 29, 12, 0, tzinfo=MSK)
    h = _hz(now)
    due = now + timedelta(hours=h)
    assert due.day == 30 and due.hour <= 9, (h, due)


def test_premarket_signal_horizon_covers_today():
    """Сигнал рано утром: торгуем сегодняшнюю сессию, окно до вечера сегодня."""
    now = datetime(2026, 7, 29, 8, 0, tzinfo=MSK)
    h = _hz(now)
    due = now + timedelta(hours=h)
    assert due >= datetime(2026, 7, 29, 18, 40, tzinfo=MSK), (h, due)
    assert due <= datetime(2026, 7, 30, 1, 0, tzinfo=MSK), (h, due)

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
