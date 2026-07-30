"""Тесты быстрого наблюдателя сетапов.

Запуск: python3 tests/test_setup_watcher.py

ЗАЧЕМ. Сетап пробоя консолидации по Мечелу 30.07 сработал в 13:25 (вход 38.35).
Сканер с подтверждением Claude ходит раз в 45 минут, поэтому сигнал был бы увиден в
14:10 при цене уже 39.16. Фактический вход состоялся в 14:34 по 39.33 и дал 4 088₽ на
4048 акциях, тогда как вход 13:25 при том же выходе дал бы 8 056₽ — ровно вдвое.
Опоздание сканера стоило половины прибыли.

Наблюдатель не обращается к Claude вообще, поэтому ходит раз в 5 минут бесплатно.
Пять минут, а не чаще: сетапы считаются по ЗАКРЫТИЮ пятиминутного бара, и опрос
внутри бара ничего не добавляет, кроме расхода лимитов Tinkoff.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.agent import setup_watcher as sw   # noqa: E402


def _reset():
    sw._seen.clear()
    sw._recent.clear()
    sw._status.update({"passes": 0, "fired": 0, "errors": 0, "checked": 0})


# ─────────────────── что считается поводом ───────────────────────────────────

def test_opening_range_is_not_watched():
    """ГЛАВНОЕ. Пробой диапазона открытия сюда не входит: он живёт первые 90 минут
    и уже покрыт основным сканером, а его просроченные срабатывания 30.07 дали 36
    сетапов, из которых 35 имели R/R ниже единицы."""
    assert "orb" not in sw.WATCHED_SETUPS
    assert "consolidation_breakout" in sw.WATCHED_SETUPS


def test_no_claude_anywhere():
    """Наблюдатель обязан быть бесплатным: ни одного обращения к Claude."""
    import pathlib
    src = pathlib.Path(ROOT, "src", "agent", "setup_watcher.py").read_text(encoding="utf-8")
    for bad in ("claude_agent", "ClaudeAgent", "_ask(", "budget"):
        assert bad not in src, f"наблюдатель тянет платный контур: {bad}"


# ─────────────────── защита от повторов ──────────────────────────────────────

def test_duplicate_suppressed_within_window():
    """Пробой держится несколько баров. Без защиты сигнал повторялся бы на каждом
    проходе, и журнал заполнился бы копиями одного события."""
    _reset()
    assert sw._is_duplicate("MTLR", "consolidation_breakout", 30) is False
    assert sw._is_duplicate("MTLR", "consolidation_breakout", 30) is True


def test_duplicate_expires():
    _reset()
    sw._is_duplicate("MTLR", "consolidation_breakout", 30)
    sw._seen[("MTLR", "consolidation_breakout")] = (
        datetime.now(timezone.utc) - timedelta(minutes=31))
    assert sw._is_duplicate("MTLR", "consolidation_breakout", 30) is False


def test_different_tickers_independent():
    _reset()
    assert sw._is_duplicate("MTLR", "consolidation_breakout", 30) is False
    assert sw._is_duplicate("SBER", "consolidation_breakout", 30) is False


def test_different_setups_independent():
    _reset()
    assert sw._is_duplicate("MTLR", "consolidation_breakout", 30) is False
    assert sw._is_duplicate("MTLR", "news_resolution", 30) is False


# ─────────────────── качество данных блокирует сигнал ────────────────────────

def _fake_ctx(**kw):
    base = {"setup": "consolidation_breakout", "stale": False, "mismatch": False,
            "price": 38.35, "vwap": 37.9, "source": "tinkoff", "age_min": 2.0,
            "plan": {"signal": "long", "entry": 38.35, "stop_loss": 38.01,
                     "take_profit": 39.03, "risk_reward": 2.0,
                     "reason": "пробой сжатия", "width_atr": 1.17, "vol_x": 3.97}}
    base.update(kw)
    return base


def _run_check(ctx):
    import src.agent.intraday_analyst as ia
    import src.analysis.technical as ta
    orig_ctx, orig_tech = ia.build_intraday_context, ta.analyze_ticker
    async def fake_ctx(*a, **k): return ctx
    async def fake_tech(*a, **k): return None
    ia.build_intraday_context, ta.analyze_ticker = fake_ctx, fake_tech
    try:
        return asyncio.get_event_loop().run_until_complete(sw.check_ticker("MTLR"))
    finally:
        ia.build_intraday_context, ta.analyze_ticker = orig_ctx, orig_tech


def test_fires_on_real_mechel_shape():
    """Настоящая геометрия входа 13:25, который система пропустила."""
    f = _run_check(_fake_ctx())
    assert f is not None
    assert f["entry"] == 38.35 and f["stop"] == 38.01 and f["rr"] == 2.0
    assert f["width_atr"] == 1.17 and f["vol_x"] == 3.97


def test_stale_data_blocks_fire():
    """Устаревшие свечи не дают сигнала: 30.07 по пятнадцати бумагам приходила
    серия за ВЧЕРА с пометкой «свежая»."""
    assert _run_check(_fake_ctx(stale=True)) is None


def test_mismatched_series_blocks_fire():
    """Чужие свечи не дают сигнала: рукописная таблица FIGI указывала 22 бумаги на
    другие инструменты."""
    assert _run_check(_fake_ctx(mismatch=True)) is None


def test_unwatched_setup_ignored():
    assert _run_check(_fake_ctx(setup="orb")) is None


def test_plan_without_direction_ignored():
    ctx = _fake_ctx()
    ctx["plan"] = {"signal": "none", "reason": "R/R ниже минимума"}
    assert _run_check(ctx) is None


# ─────────────────── состояние и настройки ───────────────────────────────────

def test_status_exposes_recent_fires():
    _reset()
    sw._recent.append({"ticker": "MTLR"})
    assert sw.status()["recent"][0]["ticker"] == "MTLR"


def test_interval_is_one_bar():
    from config.settings import SETUP_WATCH_INTERVAL_MIN, INTRADAY_TF_MIN
    assert SETUP_WATCH_INTERVAL_MIN == INTRADAY_TF_MIN, \
        "опрос чаще одного бара ничего не добавляет, кроме расхода лимитов"


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
