"""
Лента сделок: последовательность вместо минутной свёртки.

Артём сформулировал цель точно: отличать РЕАЛЬНОЕ ДАВЛЕНИЕ от просто большой
заявки в стакане. Заявку можно снять за секунду, ничего не потратив; сделку
отменить нельзя.

Четвёртый случай одной болезни: количество сделок, объёмы агрессивных покупок и
продаж, накопленная дельта в потоке БЫЛИ, но сворачивались до минуты, а порядок
внутри минуты выбрасывался.

Что защищают эти тесты:

    порог из своей ленты   у SBER лот 1, у UGLD 1000. Единого числа лотов быть
                           не может, поэтому «крупная» — процентиль собственных
                           сделок бумаги

    тавтология процентиля  если крупной считать верхние 10%, их всегда около
                           10%. Само количество не значит ничего — значит
                           СГУЩЕНИЕ, и главная выдача это серия

    мало данных — нет ответа   порог по трём сделкам назвал бы крупной каждую
                           вторую. Пустой ответ честнее

    раздельно              покупки и продажи не складываются в одну дельту:
                           окно, где купили и продали по миллиону, и окно без
                           торгов дают одинаковый ноль

    источники              дилерская сделка это сделка с брокером, а не с
                           рынком; считать её давлением нельзя

    без вердиктов          ни «сильное давление», ни «покупатель контролирует»:
                           что из этого предшествует движению — не измерено
"""
import pathlib

import pytest

from src.analysis.trade_tape import TradeTape, BUY, SELL, MIN_SAMPLE, GAP_SEC

ROOT = pathlib.Path(__file__).resolve().parents[1]
T0 = 1785000000


def tape(**kw):
    return TradeTape(**kw)


def fill(tp, n=MIN_SAMPLE, qty=10, sec=T0, side=BUY, tk="SBER",
         src="exchange"):
    """Ровный фон, чтобы у процентиля было на чём считаться."""
    for i in range(n):
        tp.on_trade(tk, src, sec, 100.0, qty, side)


# ─── агрессивные покупки и продажи ────────────────────────────────────────────

def test_buy_and_sell_are_counted_separately():
    """
    Окно, где купили и продали по миллиону, и окно, где не торговали вовсе,
    дают одинаковую дельту. Разность скрывает главное.
    """
    tp = tape()
    tp.on_trade("SBER", "exchange", T0, 100.0, 600, BUY)
    tp.on_trade("SBER", "exchange", T0, 100.0, 400, SELL)
    w = tp.window("SBER", "exchange", T0, back=30)
    assert w["buy_lots"] == 600 and w["sell_lots"] == 400
    assert w["delta_lots"] == 200
    assert w["buy_share"] == pytest.approx(0.6)


def test_trade_count_and_average_size():
    tp = tape()
    for q in (10, 20, 30, 40):
        tp.on_trade("SBER", "exchange", T0, 100.0, q, BUY)
    w = tp.window("SBER", "exchange", T0, back=30)
    assert w["trades"] == 4
    assert w["avg_size"] == pytest.approx(25.0)
    assert w["max_size"] == 40


def test_cumulative_delta_accumulates_across_windows():
    """
    Накопленная дельта считается от начала дня, а не по окну: иначе она была бы
    просто ещё одной дельтой окна.
    """
    tp = tape()
    tp.on_trade("SBER", "exchange", T0, 100.0, 500, BUY)
    tp.on_trade("SBER", "exchange", T0 + 300, 100.0, 200, SELL)
    w = tp.window("SBER", "exchange", T0 + 300, back=10)
    assert w["trades"] == 1, "в окно попала одна сделка"
    assert w["cum_delta_lots"] == 300, "а накопленная помнит обе"


def test_window_cuts_off_old_trades():
    tp = tape()
    tp.on_trade("SBER", "exchange", T0, 100.0, 999, BUY)
    tp.on_trade("SBER", "exchange", T0 + 100, 100.0, 5, BUY)
    w = tp.window("SBER", "exchange", T0 + 100, back=30)
    assert w["trades"] == 1 and w["max_size"] == 5


def test_empty_window_is_empty_not_zeros():
    """
    Пустой ответ, а не нули: «сделок не было» и «сделок на ноль лотов» это
    разные вещи, и вторая невозможна.
    """
    assert tape().window("SBER", "exchange", T0) == {}


# ─── что считать крупной сделкой ──────────────────────────────────────────────

def test_threshold_comes_from_the_tickers_own_tape():
    """
    У SBER лот 1, у UGLD 1000. Единого числа лотов быть не может: обычная сделка
    одной бумаги — событие у другой.
    """
    tp = tape()
    fill(tp, n=30, qty=10, tk="SBER")
    fill(tp, n=30, qty=10000, tk="UGLD")
    small = tp.big_threshold("SBER", "exchange")
    big = tp.big_threshold("UGLD", "exchange")
    assert small and big and big > small * 100


def test_no_threshold_until_there_is_a_sample():
    """
    Порог по трём сделкам назвал бы крупной каждую вторую. Пустой ответ честнее.
    """
    tp = tape()
    fill(tp, n=MIN_SAMPLE - 1)
    assert tp.big_threshold("SBER", "exchange") is None
    assert tp.big_trades("SBER", "exchange", T0) == []
    fill(tp, n=1)
    assert tp.big_threshold("SBER", "exchange") is not None


def test_big_trades_are_the_upper_tail():
    tp = tape()
    fill(tp, n=30, qty=10)
    tp.on_trade("SBER", "exchange", T0, 100.0, 5000, BUY)
    big = tp.big_trades("SBER", "exchange", T0, back=60)
    assert big and big[-1]["lots"] == 5000
    assert big[-1]["side"] == "buy"


def test_the_percentile_tautology_is_documented():
    """
    Если крупной считать верхние 10%, их всегда около 10%. Само по себе
    количество крупных сделок не значит ничего, и это должно быть написано
    рядом с кодом, а не подразумеваться.
    """
    src = (ROOT / "src/analysis/trade_tape.py").read_text()
    assert "около 10%" in src or "около 10 %" in src
    assert "СГУЩЕНИЕ" in src


# ─── серия: то, ради чего лента ───────────────────────────────────────────────

def test_consecutive_one_sided_big_trades_form_a_run():
    """
    Три крупные покупки подряд за десять секунд и три, разбросанные по минуте, —
    разные события с одинаковой суммой. Различает их только серия.
    """
    tp = tape()
    fill(tp, n=30, qty=10)
    for i in range(3):
        tp.on_trade("SBER", "exchange", T0 + i, 100.0, 5000, BUY)
    s = tp.streak("SBER", "exchange", T0 + 3, back=60)
    assert s["longest_run"] == 3 and s["run_side"] == "buy"
    assert s["run_lots"] == 15000


def test_a_side_change_breaks_the_run():
    tp = tape()
    fill(tp, n=30, qty=10)
    tp.on_trade("SBER", "exchange", T0, 100.0, 5000, BUY)
    tp.on_trade("SBER", "exchange", T0 + 1, 100.0, 5000, SELL)
    tp.on_trade("SBER", "exchange", T0 + 2, 100.0, 5000, BUY)
    s = tp.streak("SBER", "exchange", T0 + 2, back=60)
    assert s["longest_run"] == 1, "серия из одной, стороны чередуются"
    assert s["big_count"] == 3


def test_a_long_gap_breaks_the_run():
    """
    Две покупки с интервалом в минуту — не серия, а два отдельных события.
    """
    tp = tape()
    fill(tp, n=30, qty=10)
    tp.on_trade("SBER", "exchange", T0, 100.0, 5000, BUY)
    tp.on_trade("SBER", "exchange", T0 + GAP_SEC + 3, 100.0, 5000, BUY)
    s = tp.streak("SBER", "exchange", T0 + GAP_SEC + 3, back=120)
    assert s["longest_run"] == 1


def test_run_reports_when_it_ended():
    """
    Серия, кончившаяся минуту назад, и идущая прямо сейчас — разные вещи.
    """
    tp = tape()
    fill(tp, n=30, qty=10)
    for i in range(3):
        tp.on_trade("SBER", "exchange", T0 + i, 100.0, 5000, BUY)
    s = tp.streak("SBER", "exchange", T0 + 40, back=120)
    assert s["run_ended_sec_ago"] == 38


def test_big_share_of_volume_is_reported():
    """Крупные сделки как доля оборота окна — контекст для их количества."""
    tp = tape()
    fill(tp, n=30, qty=10)
    tp.on_trade("SBER", "exchange", T0, 100.0, 5000, BUY)
    s = tp.streak("SBER", "exchange", T0, back=60)
    assert 0 < s["big_share_of_volume"] <= 1


def test_streak_empty_without_big_trades():
    tp = tape()
    fill(tp, n=30, qty=10)
    assert tp.streak("SBER", "exchange", T0, back=60).get("longest_run") in (None, 1)


# ─── давление против стоящего в стакане ───────────────────────────────────────

def test_traded_versus_resting_are_shown_side_by_side():
    """
    Главный вопрос: заявку на миллион можно снять за секунду, ничего не
    потратив; миллион, прошедший сделками, потрачен. Оба числа рядом — и видно,
    чего именно много.
    """
    tp = tape()
    tp.on_trade("SBER", "exchange", T0, 100.0, 200, BUY)
    p = tp.pressure_vs_resting("SBER", "exchange", T0, resting_lots=1000)
    assert p["traded_lots"] == 200 and p["resting_lots"] == 1000
    assert p["traded_per_resting"] == pytest.approx(0.2)


def test_resting_without_trades_gives_no_ratio():
    tp = tape()
    assert tp.pressure_vs_resting("SBER", "exchange", T0, resting_lots=1000) == {}


# ─── источники ────────────────────────────────────────────────────────────────

def test_sources_do_not_mix():
    """
    Дилерская сделка — сделка с брокером, а не с рынком. Считать её давлением
    рынка значит считать давлением то, чем оно не является.
    """
    tp = tape()
    tp.on_trade("SBER", "exchange", T0, 100.0, 100, BUY)
    tp.on_trade("SBER", "dealer", T0, 100.0, 900, SELL)
    assert tp.window("SBER", "exchange", T0)["buy_lots"] == 100
    assert tp.window("SBER", "dealer", T0)["sell_lots"] == 900
    assert tp.stats()["series"] == 2


# ─── границы ──────────────────────────────────────────────────────────────────

def test_tape_is_bounded():
    tp = tape(max_trades=50)
    for i in range(500):
        tp.on_trade("SBER", "exchange", T0 + i, 100.0, 10, BUY)
    assert tp.stats()["trades_held"] <= 50


def test_broken_input_does_not_crash():
    tp = tape()
    tp.on_trade("", "exchange", T0, 100.0, 10, BUY)
    tp.on_trade("SBER", "exchange", T0, 100.0, 0, BUY)
    tp.on_trade("SBER", "exchange", T0, 100.0, -5, BUY)
    assert tp.window("SBER", "exchange", T0) == {}


def test_day_reset_clears_cumulative_delta():
    tp = tape()
    tp.on_trade("SBER", "exchange", T0, 100.0, 500, BUY)
    tp.reset_day()
    assert tp.window("SBER", "exchange", T0)["cum_delta_lots"] == 0


# ─── описание, а не совет ─────────────────────────────────────────────────────

def test_no_verdict_fields():
    tp = tape()
    fill(tp, n=30, qty=10)
    for i in range(3):
        tp.on_trade("SBER", "exchange", T0 + i, 100.0, 5000, BUY)
    blob = (str(tp.window("SBER", "exchange", T0 + 3))
            + str(tp.streak("SBER", "exchange", T0 + 3))
            + str(tp.big_trades("SBER", "exchange", T0 + 3))).lower()
    for bad in ("signal", "strong", "pressure_high", "recommend", "entry",
                "control", "bullish", "bearish"):
        assert bad not in blob, bad


# ─── подключение ──────────────────────────────────────────────────────────────

def test_tape_is_fed_by_the_stream():
    src = (ROOT / "src/collector/stream.py").read_text()
    assert "self.tape.on_trade(" in src
    assert "TradeTape()" in src
    i = src.index("self.tape.on_trade(")
    assert "t.direction" in src[i:i + 260], "сторона агрессора передаётся"


def test_tape_stats_are_in_health():
    src = (ROOT / "src/collector/stream.py").read_text()
    assert '"tape": self.tape.stats()' in src


def test_flat_tape_has_no_big_trades_at_all():
    """
    НАЙДЕНО ТЕСТОМ, а не рассуждением. Тридцать одинаковых сделок по десять
    лотов давали ТРИДЦАТЬ крупных из тридцати: p90 на ровном распределении равен
    медиане, и «верхние 10%» превращаются в «все».

    На реальной ленте это частый случай — круглые лоты дают много одинаковых
    сделок, и весь признак выродился бы в шум.
    """
    tp = tape()
    fill(tp, n=30, qty=10)
    assert tp.big_trades("SBER", "exchange", T0, back=60) == []
    assert tp.streak("SBER", "exchange", T0, back=60) == {}


def test_a_single_outlier_among_equals_is_still_found():
    """
    Обратная беда одного процентиля: единственный выброс среди тридцати
    одинаковых сделок p90 не сдвигает, и настоящая крупная сделка осталась бы
    незамеченной. Кратность медианы её ловит.
    """
    tp = tape()
    fill(tp, n=30, qty=10)
    tp.on_trade("SBER", "exchange", T0, 100.0, 900, BUY)
    big = tp.big_trades("SBER", "exchange", T0, back=60)
    assert len(big) == 1 and big[0]["lots"] == 900


def test_threshold_adapts_to_a_heavy_tail():
    """Когда хвост есть, работает процентиль, а не кратность медианы."""
    tp = tape()
    for q in [10] * 20 + [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]:
        tp.on_trade("SBER", "exchange", T0, 100.0, q, BUY)
    thr = tp.big_threshold("SBER", "exchange")
    assert thr > 10 * 3, "процентиль перевесил кратность медианы"


def test_both_parts_of_the_threshold_are_documented_as_guesses():
    src = (ROOT / "src/analysis/trade_tape.py").read_text()
    i = src.index("BIG_MULT = ")
    assert "ДОГАДКА" in src[max(0, i - 900):i]
    assert "30 крупных сделок из 30" in src, "случай вырождения записан рядом"


def test_wired_into_the_card():
    api = (ROOT / "src/api/main.py").read_text()
    for f in ('out["tape_30s"]', 'out["big_streak"]', 'out["big_trades"]',
              'out["pressure"]'):
        assert f in api, f
    # Лента идёт в ЛЁГКИЙ ответ: он опрашивается раз в секунду, а сделки идут
    # чаще, чем раз в минуту.
    i = api.index('out["tape_30s"]')
    j = api.index("if light:")
    assert i < j, "лента отдаётся до выхода по light"


def test_page_shows_pressure_against_resting():
    page = (ROOT / "dashboard/market-watch.html").read_text()
    assert "исполнено / стоит в стакане" in page
    assert "серия крупных" in page
    assert "все тридцать" in page, "случай вырождения порога описан на экране"
