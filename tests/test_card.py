"""
Карточка бумаги: что она обязана говорить и чего не имеет права говорить.

Сторожатся три решения, каждое куплено своим убытком:

  кратность при тонкой базе не отдаётся вовсе  (в проде ASTR: times=30.0)
  ни одного числа, зависящего от размера счёта
  ни одного вердикта: карточка описывает, решает читатель
"""
import json

from src.analysis import card as C
from src.analysis.trade_tape import TradeTape, BUY, SELL
from src.analysis.level_tracker import LevelTracker

BASE_TS = "2026-08-04T10:00:00+03:00"


def _bars(n=30, start=100.0, step=0.1, vol=1000, minute0=0):
    """Ровная серия минутных бар ОСНОВНОЙ сессии 04.08.2026 (вторник)."""
    out = []
    for i in range(n):
        c = start + i * step
        mm = minute0 + i
        ts = "2026-08-04T%02d:%02d:00+03:00" % (10 + mm // 60, mm % 60)
        out.append({"ts": ts, "o": c - step / 2, "h": c + step,
                    "l": c - step, "c": c, "v": vol})
    return out


def _walk(obj, path=""):
    """Все ключи и строковые значения карточки, как глубоко бы они ни лежали."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield ("%s.%s" % (path, k)).lower()
            yield from _walk(v, "%s.%s" % (path, k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk(v, "%s[%d]" % (path, i))
    elif isinstance(obj, str):
        yield obj.lower()


# ─── главное правило: кратность при тонкой базе ────────────────────────

def test_thin_base_gives_no_multiple_at_all():
    """Случай ASTR 04.08: оборот 337 тыс при базе 11 тыс — числа быть не должно."""
    card = C.build("ASTR", bars=_bars(), minute_of_day=630, weekday=1,
                   volume={"rub": 336686, "base_rub": 11223,
                           "base_thin": True, "base_source": "скользящая",
                           "step_min": 1})
    assert card["volume"]["times"] is None
    assert "тонкая" in card["volume"]["times_missing_why"]
    # Сами числа остаются: читатель вправе видеть оборот и базу.
    assert card["volume"]["minute_rub"] == 336686
    assert card["volume"]["base_rub"] == 11223


def test_normal_base_keeps_the_multiple():
    card = C.build("BELU", bars=_bars(), minute_of_day=630, weekday=1,
                   volume={"rub": 5779555, "base_rub": 212121,
                           "base_thin": False, "base_source": "скользящая",
                           "step_min": 5})
    assert card["volume"]["times"] == 27.25
    assert "times_missing_why" not in card["volume"]


def test_no_base_at_all_is_not_a_zero_multiple():
    """Нет нормы — нет кратности. Ноль солгал бы про тишину."""
    card = C.build("X5", bars=_bars(), minute_of_day=630, weekday=1,
                   volume={"rub": 500000, "base_rub": 0, "base_thin": False})
    assert card["volume"]["times"] is None
    assert card["volume"]["times_missing_why"]


# ─── геометрия не знает про депозит ───────────────────────────────

def test_card_says_nothing_about_the_size_of_the_account():
    """У каждого свой депозит: числа карточки обязаны годиться любому."""
    card = C.build("SBER", bars=_bars(), minute_of_day=630, weekday=1,
                   min_step=0.01, lot=10)
    text = json.dumps(card, ensure_ascii=False).lower()
    for forbidden in ("account", "deposit", "депозит", "position_size",
                      "размер позиции", "risk_rub", "200000"):
        assert forbidden not in text, forbidden


def test_geometry_is_given_in_atr_steps_and_percent():
    card = C.build("SBER", bars=_bars(), minute_of_day=630, weekday=1,
                   min_step=0.01, lot=10)
    g = card["geometry"]
    assert g["atr"] and g["atr_pct"]
    assert g["atr_in_steps"] > 0
    assert g["lot"] == 10 and g["min_step"] == 0.01


def test_spread_is_measured_against_atr_not_only_in_rubles():
    """Спред в половину ATR съедает половину хода при любом счёте."""
    book = {"bids": [(102.0, 50)], "asks": [(102.2, 40)]}
    card = C.build("SBER", bars=_bars(), minute_of_day=630, weekday=1,
                   min_step=0.01, book=book)
    g = card["geometry"]
    assert g["spread"] == 0.2
    assert g["spread_in_atr"] is not None
    assert g["spread_in_steps"] == 20.0


# ─── никаких вердиктов ────────────────────────────────────────

def test_card_has_no_signal_and_no_recommendation():
    tape = TradeTape()
    for i in range(30):
        tape.on_trade("SBER", "exchange", 1000 + i, 100.0 + i * 0.01,
                      10 + i, BUY if i % 2 else SELL)
    tracker = LevelTracker()
    tracker.on_book("SBER", "2026-08-04 13:30", [(101.0, 500)], [(101.5, 400)],
                    sec=1030)
    card = C.from_state("SBER", bars=_bars(), now_sec=1030, tape_obj=tape,
                        tracker=tracker, lot=1, minute_of_day=810, weekday=1,
                        min_step=0.01)
    keys = list(_walk(card))
    for bad in ("signal", "verdict", "recommend", "take_profit", "stop_loss",
                "entry", "risk_reward"):
        assert not any(k.endswith("." + bad) for k in keys), bad


def test_card_does_not_call_a_level_strong_or_weak():
    """31.07 метка «структура вверх» измерилась ВРЕДНОЙ: t=-12.57."""
    tracker = LevelTracker()
    tracker.on_book("SBER", "2026-08-04 13:30", [(101.0, 500)], [(101.5, 400)],
                    sec=1030)
    card = C.from_state("SBER", bars=_bars(), now_sec=1030, tracker=tracker,
                        lot=1, minute_of_day=810, weekday=1)
    text = json.dumps(card, ensure_ascii=False).lower()
    for word in ("сильный", "слабый", "strong", "weak",
                 "контролирует", "доминирует"):
        assert word not in text, word


# ─── честность данных ───────────────────────────────────────

def test_empty_input_is_a_card_with_a_reason_not_a_crash():
    card = C.build("SBER", bars=[], minute_of_day=630, weekday=1,
                   stream_running=True, fresh_60s=0)
    assert card["price"] == {}
    assert card["data"]["bars"] == 0
    assert card["data"]["enough_for_atr"] is False
    # Рынок ОТКРЫТ, а пакетов нет — самый дорогой случай, и он назван.
    assert "неисправность" in card["data"]["note"]


def test_three_kinds_of_silence_are_named_differently():
    closed = C.build("SBER", bars=[], minute_of_day=3, weekday=1)["data"]["note"]
    dead = C.build("SBER", bars=[], minute_of_day=630, weekday=1,
                   stream_running=False)["data"]["note"]
    open_but_quiet = C.build("SBER", bars=[], minute_of_day=630, weekday=1,
                             stream_running=True, fresh_60s=0)["data"]["note"]
    assert len({closed, dead, open_but_quiet}) == 3


def test_stale_tick_is_reported_in_seconds():
    card = C.build("KMAZ", bars=_bars(), minute_of_day=630, weekday=1,
                   now_sec=2000, last_tick_sec=1981)
    assert card["data"]["last_tick_sec_ago"] == 19


def test_few_bars_means_no_atr_instead_of_a_made_up_one():
    card = C.build("SBER", bars=_bars(n=5), minute_of_day=630, weekday=1,
                   min_step=0.01)
    assert card["geometry"]["atr"] is None
    assert "atr_in_steps" not in card["geometry"]
    assert card["data"]["enough_for_atr"] is False


def test_saturday_is_not_the_main_session():
    """01.08 в суботу в 12:34 фаза отвечала main при закрытой бирже."""
    card = C.build("SBER", bars=_bars(), minute_of_day=754, weekday=5)
    assert card["phase"] == "closed"


# ─── лента и стакан на НАСТОЯЩИХ объектах ─────────────────────────

def test_tape_windows_and_streak_come_from_the_real_tape():
    tape = TradeTape()
    # Сначала ровный фон, чтобы порог крупной сделки вообще появился.
    for i in range(25):
        tape.on_trade("GAZP", "exchange", 1000 + i, 130.0, 10, SELL)
    # Затем три крупные покупки подряд — сгущение, а не счётчик.
    for j, sec in enumerate((1050, 1052, 1054)):
        tape.on_trade("GAZP", "exchange", sec, 130.5 + j * 0.1, 500, BUY)
    card = C.from_state("GAZP", bars=_bars(), now_sec=1055, tape_obj=tape,
                        lot=10, minute_of_day=810, weekday=1)
    assert card["tape"]["w30"]["trades"] >= 3
    assert card["tape"]["streak"]["longest_run"] == 3
    assert card["tape"]["streak"]["run_side"] == "buy"
    assert len(card["tape"]["big_trades"]) == 3
    assert card["tape"]["big_threshold_lots"] is not None


def test_short_tape_says_why_there_is_no_threshold():
    tape = TradeTape()
    for i in range(3):
        tape.on_trade("GAZP", "exchange", 1000 + i, 130.0, 10, BUY)
    card = C.from_state("GAZP", bars=_bars(), now_sec=1005, tape_obj=tape,
                        lot=10, minute_of_day=810, weekday=1)
    assert card["tape"]["big_threshold_lots"] is None
    assert card["tape"]["big_threshold_missing_why"]


def test_book_is_reported_in_rubles_with_the_lot_size():
    book = {"bids": [(130.0, 100), (129.9, 50)],
            "asks": [(130.1, 40), (130.2, 60)]}
    card = C.build("GAZP", bars=_bars(), minute_of_day=630, weekday=1,
                   lot=10, book=book)
    b = card["book"]
    assert b["best_bid"] == 130.0 and b["best_ask"] == 130.1
    # 130*100*10 + 129.9*50*10 = 130000 + 64950
    assert b["bid_rub"] == 194950
    assert b["bids"][0]["rub"] == 130000
    assert 0 < b["bid_share"] < 1


def test_nearest_levels_are_measured_in_atr():
    levels = [{"side": "ask", "price": 103.5, "peak_rub": 500000,
               "now_rub": 400000, "life": {"state": "untested", "tests": 0}},
              {"side": "bid", "price": 101.0, "peak_rub": 700000,
               "now_rub": 600000, "life": {"state": "defended", "tests": 2}}]
    card = C.build("SBER", bars=_bars(), minute_of_day=630, weekday=1,
                   levels=levels)
    near = card["nearest_levels"]
    assert near["above"]["price"] == 103.5
    assert near["below"]["price"] == 101.0
    assert near["above"]["distance_atr"] is not None
    assert near["below"]["state"] == "defended"


def test_card_is_json_serializable():
    """Карточка уезжает в журнал и в контекст агента целиком."""
    tape = TradeTape()
    for i in range(25):
        tape.on_trade("SBER", "exchange", 1000 + i, 100.0, 10, BUY