"""
Поток сделок собирается ПОЛНОСТЬЮ и не задваивается.

Ошибка, ради которой написан файл. Tinkoff GetLastTrades отдаёт ВСЕ сделки за
запрошенный период, а не «последние N». Прежний код запрашивал четыре часа,
оставлял из ответа последние 50 сделок и остальное выбрасывал:

    trades = sorted(data["trades"], ...)[-limit:]
    result["raw"] = trades      # в накопление уходили те же 50

При опросе раз в пять минут это означало, что по ликвидной бумаге в базу
попадали единицы процентов сделок: у SBER за пять минут их тысячи, а мы брали
пятьдесят. Данные приходили из API и терялись в коде.

Вторая половина проблемы: даже эти 50 нельзя было складывать между снимками —
окна пересекаются, идентификаторов сделок нет. 31.07 накопленная дельта,
посчитанная сложением снимков, оказалась завышена в разы, и её выбросили.
Механизм дедупа по watermark в коде БЫЛ, но применялся только к footprint,
который группирует по цене и теряет время.
"""
import pytest

from src.collector.tinkoff_client import minute_buckets


def _t(iso, price, qty, side):
    """Сделка в форме Tinkoff: цена дробится на units и nano."""
    u = int(price)
    return {"time": iso, "quantity": qty,
            "price": {"units": u, "nano": int(round((price - u) * 1e9))},
            "direction": f"TRADE_DIRECTION_{side}"}


def test_trades_land_in_their_own_minutes():
    """Разные минуты — разные строки. Ключ по времени, а не по цене."""
    tr = [_t("2026-08-01T07:00:10Z", 100.0, 5, "BUY"),
          _t("2026-08-01T07:00:50Z", 100.5, 3, "SELL"),
          _t("2026-08-01T07:01:05Z", 101.0, 7, "BUY")]
    out = minute_buckets(tr, None)
    assert [r["ts"] for r in out["rows"]] == ["2026-08-01T10:00", "2026-08-01T10:01"]
    assert out["new"] == 3


def test_time_is_converted_to_moscow():
    """
    Tinkoff отдаёт UTC, вся остальная система живёт по МСК. Две зоны в одной
    таблице — источник ошибок, которые всплывают через недели.
    """
    out = minute_buckets([_t("2026-08-01T06:59:30Z", 10.0, 1, "BUY")], None)
    assert out["rows"][0]["ts"] == "2026-08-01T09:59"


def test_sessions_are_labelled():
    """Утро, основная и вечер помечаются: их ликвидность несопоставима."""
    tr = [_t("2026-08-01T04:00:00Z", 10.0, 1, "BUY"),    # 07:00 МСК
          _t("2026-08-01T08:00:00Z", 10.0, 1, "BUY"),    # 11:00 МСК
          _t("2026-08-01T17:00:00Z", 10.0, 1, "BUY")]    # 20:00 МСК
    got = {r["ts"][11:16]: r["session"] for r in minute_buckets(tr, None)["rows"]}
    assert got == {"07:00": "morning", "11:00": "main", "20:00": "evening"}


def test_old_trades_are_skipped_by_watermark():
    """
    Главная защита. Соседние снимки перекрываются почти целиком; без отсечки
    по времени последней учтённой сделки объём задваивается.
    """
    tr = [_t("2026-08-01T07:00:10Z", 100.0, 5, "BUY"),
          _t("2026-08-01T07:00:20Z", 100.0, 5, "BUY"),
          _t("2026-08-01T07:00:30Z", 100.0, 5, "BUY")]
    out = minute_buckets(tr, "2026-08-01T07:00:20Z")
    assert out["new"] == 1
    assert out["rows"][0]["buy_volume"] == 5, "учтена только сделка новее отсечки"


def test_repeated_call_with_same_watermark_adds_nothing():
    """Повторная обработка того же ответа не должна давать ни одной новой сделки."""
    tr = [_t("2026-08-01T07:00:10Z", 100.0, 5, "BUY"),
          _t("2026-08-01T07:00:20Z", 100.0, 4, "SELL")]
    first = minute_buckets(tr, None)
    second = minute_buckets(tr, first["watermark"])
    assert first["new"] == 2
    assert second["new"] == 0 and second["rows"] == []


def test_watermark_is_the_latest_trade_time():
    """Отсечка двигается по самой поздней сделке ОТВЕТА, а не учтённой."""
    tr = [_t("2026-08-01T07:00:10Z", 100.0, 5, "BUY"),
          _t("2026-08-01T07:05:00Z", 100.0, 5, "BUY")]
    assert minute_buckets(tr, None)["watermark"] == "2026-08-01T07:05:00Z"


def test_buy_and_sell_are_split():
    """Направление берётся из флага биржи, а не из правила тика."""
    tr = [_t("2026-08-01T07:00:10Z", 100.0, 7, "BUY"),
          _t("2026-08-01T07:00:20Z", 100.0, 3, "SELL")]
    r = minute_buckets(tr, None)["rows"][0]
    assert r["buy_volume"] == 7 and r["sell_volume"] == 3


def test_unknown_direction_counts_in_volume_but_not_in_sides():
    """
    Сделка без направления попадает в число сделок, но не в buy и не в sell.
    Достраивать её тиком нельзя: 31.07 по SIBN флаг дал 79.2% покупок, а
    правило тика 39.9% — расхождение вдвое.
    """
    tr = [{"time": "2026-08-01T07:00:10Z", "quantity": 9,
           "price": {"units": 100, "nano": 0}, "direction": "TRADE_DIRECTION_UNSPECIFIED"}]
    r = minute_buckets(tr, None)["rows"][0]
    assert r["trade_count"] == 1
    assert r["buy_volume"] == 0 and r["sell_volume"] == 0


def test_biggest_trade_of_the_minute_is_kept():
    """
    Размер крупнейшей сделки — то, чего НЕТ в footprint по цене: одна сделка
    на 1000 лотов и десять по 100 дают там одинаковую корзину.
    """
    tr = [_t("2026-08-01T07:00:10Z", 100.0, 100, "BUY"),
          _t("2026-08-01T07:00:20Z", 100.0, 900, "BUY"),
          _t("2026-08-01T07:00:30Z", 100.0, 50, "BUY")]
    r = minute_buckets(tr, None)["rows"][0]
    assert r["max_trade"] == 900 and r["trade_count"] == 3


def test_vwap_numerator_lets_price_be_recovered():
    """
    Храним сумму цена*объём, а не готовый VWAP: при склейке минут в 5м и 15м
    средние нельзя усреднять, а суммы складываются.
    """
    tr = [_t("2026-08-01T07:00:10Z", 100.0, 1, "BUY"),
          _t("2026-08-01T07:00:20Z", 200.0, 3, "BUY")]
    r = minute_buckets(tr, None)["rows"][0]
    assert r["vwap_num"] == pytest.approx(100.0 * 1 + 200.0 * 3)
    assert r["vwap_num"] / (r["buy_volume"] + r["sell_volume"]) == pytest.approx(175.0)


def test_zero_and_broken_trades_are_ignored():
    """Нулевой объём и битое время не должны ломать разбор."""
    tr = [_t("2026-08-01T07:00:10Z", 100.0, 0, "BUY"),
          {"time": None, "quantity": 5, "price": {"units": 1, "nano": 0}},
          {"time": "не дата", "quantity": 5, "price": {"units": 1, "nano": 0}},
          _t("2026-08-01T07:00:40Z", 100.0, 2, "BUY")]
    out = minute_buckets(tr, None)
    assert out["new"] == 1
    assert out["rows"][0]["buy_volume"] == 2


def test_window_is_not_truncated_to_fifty():
    """
    Регрессия. Из ответа Tinkoff в накопление должны идти ВСЕ сделки окна.
    Раньше уходили последние 50, и по SBER это были единицы процентов.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1]
           / "src/collector/tinkoff_client.py").read_text()
    assert 'result["raw"] = all_trades' in src, "raw снова обрезан"
    assert "TRADES_WINDOW_MIN" in src, "окно запроса должно быть настраиваемым"


def test_incident_recorded_in_code():
    """Обстоятельства рядом с кодом, иначе обрезку вернут."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1]
           / "src/collector/tinkoff_client.py").read_text()
    assert "выбрасыв" in src or "терялись в коде" in src


# ─── склейка минут в 5m/15m/session ───────────────────────────────────────────
#
# Проверяется ЧИСТАЯ функция aggregate_flow, без базы. Первая версия теста
# поднимала свою базу через importlib.reload(src.db) — и ломала два теста в
# другом файле, потому что подменяла модуль под ними. Подмена общих модулей
# ради удобства теста стоит дороже, чем кажется.

from src.db import aggregate_flow, FLOW_RES                       # noqa: E402

MIN_ROWS = [
    {"ts": "2026-08-01T10:00", "session": "main", "buy_volume": 100,
     "sell_volume": 40, "trade_count": 7, "max_trade": 60, "vwap_num": 140.0 * 140},
    {"ts": "2026-08-01T10:01", "session": "main", "buy_volume": 20,
     "sell_volume": 80, "trade_count": 5, "max_trade": 50, "vwap_num": 140.5 * 100},
]


def test_minute_resolution_keeps_every_minute():
    out = aggregate_flow(MIN_ROWS, FLOW_RES["1m"], "SBER")
    assert len(out) == 2
    assert out[0]["delta"] == 60 and out[1]["delta"] == -60


def test_cumulative_delta_runs_in_order():
    """
    Накопленная дельта — та величина, которую 31.07 нельзя было посчитать:
    сложение перекрывающихся снимков завышало её в разы.
    """
    out = aggregate_flow(MIN_ROWS, FLOW_RES["1m"], "SBER")
    assert [r["cumulative_delta"] for r in out] == [60, 0]


def test_five_minutes_glue_into_one_bar():
    out = aggregate_flow(MIN_ROWS, FLOW_RES["5m"], "SBER")
    assert len(out) == 1
    assert out[0]["buy_volume"] == 120 and out[0]["sell_volume"] == 120
    assert out[0]["trade_count"] == 12


def test_biggest_trade_is_max_not_sum():
    """Крупнейшая сделка при склейке берётся максимумом. Сумма была бы вымыслом."""
    out = aggregate_flow(MIN_ROWS, FLOW_RES["5m"], "SBER")
    assert out[0]["max_trade"] == 60


def test_vwap_is_glued_through_the_numerator():
    """
    Минутные VWAP усреднять нельзя — усредняются суммы цена*объём.
    140.0 на 140 лотов и 140.5 на 100 дают средневзвешенную ближе к 140.0.
    """
    out = aggregate_flow(MIN_ROWS, FLOW_RES["5m"], "SBER")
    expect = (140.0 * 140 + 140.5 * 100) / 240
    assert out[0]["vwap"] == pytest.approx(expect, abs=1e-5)
    assert out[0]["vwap"] < (140.0 + 140.5) / 2, "не среднее арифметическое"


def test_session_resolution_is_one_row():
    out = aggregate_flow(MIN_ROWS, FLOW_RES["session"], "SBER")
    assert len(out) == 1 and out[0]["trade_count"] == 12


def test_empty_input_gives_empty_output():
    """Нет данных — пусто, а не выдуманные нули."""
    assert aggregate_flow([], FLOW_RES["1m"], "SBER") == []


def test_zero_volume_minute_does_not_divide_by_zero():
    rows = [{"ts": "2026-08-01T10:00", "session": "main", "buy_volume": 0,
             "sell_volume": 0, "trade_count": 0, "max_trade": 0, "vwap_num": 0.0}]
    r = aggregate_flow(rows, FLOW_RES["1m"], "SBER")[0]
    assert r["imbalance"] is None and r["vwap"] is None
    assert r["average_trade_size"] is None


def test_retention_is_longer_than_footprint():
    """
    Три дня, как у session_footprint, не годятся: минутный поток и нужен для
    проверки гипотез на истории.
    """
    from config.settings import FLOW_MINUTE_KEEP_DAYS
    assert FLOW_MINUTE_KEEP_DAYS >= 30


def test_endpoint_exists_and_is_separate_from_feed():
    """
    Регрессия. Поток отдаётся ОТДЕЛЬНЫМ маршрутом: /api/feed возвращает
    перекрывающиеся снимки, складывать их нельзя.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert '@app.get("/api/flow/{ticker}"' in (root / "src/api/main.py").read_text()
    assert "class FlowMinute" in (root / "src/db.py").read_text()


def test_collector_writes_flow_minutes():
    """Регрессия по сборщику: минутный поток пишется в цикле, с отсечкой."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "minute_buckets" in src and "merge_flow_minutes" in src
    assert "get_flow_watermark" in src, "без отсечки объём задвоится"
