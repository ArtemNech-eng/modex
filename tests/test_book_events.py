"""
Детектор событий стакана: каждый тип срабатывает и НЕ срабатывает на ровном фоне.

Главное, что защищают эти тесты, — не набор порогов, а два свойства.

ПРИЧИННОСТЬ. Норма считается только по прошлым минутам. 31.07 определение пробоя
сравнивало закрытие с максимумом, который уже включал текущий бар, и вместо 3078
событий вышло 6. Такая ошибка не портит точность, она обессмысливает измерение —
и заметить её по результату почти невозможно, потому что цифры остаются похожими
на правду.

ОПИСАНИЕ, А НЕ ПРЕДСКАЗАНИЕ. В событии нет и не должно быть направления или силы
сигнала. Стоит появиться полю «вверх» — и через неделю это станет правилом,
которого никто не измерял. Ровно так неделю назад появились семь шагов с десятью
метриками, не давшие ничего.
"""
import pytest

from src.analysis.book_events import detect, summarize, DEFAULTS, KINDS


def flat(n=25, **over):
    """Ровный фон: одинаковые минуты, на которых событий быть не должно."""
    out = []
    for i in range(n):
        r = {"ts": f"2026-08-03T10:{i:02d}", "buy_volume": 100,
             "sell_volume": 100, "trade_count": 20, "max_trade": 10,
             "bid_share": 0.5, "bid_vol_sum": 1000, "ask_vol_sum": 1000,
             "bid_top": 100, "ask_top": 100, "bid_near_share": 0.5,
             "ask_near_share": 0.5, "updates": 30,
             "open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0}
        r.update(over)
        out.append(r)
    return out


def minute(ts, **over):
    r = {"ts": ts, "buy_volume": 100, "sell_volume": 100, "trade_count": 20,
         "max_trade": 10, "bid_share": 0.5, "bid_vol_sum": 1000,
         "ask_vol_sum": 1000, "bid_top": 100, "ask_top": 100,
         "bid_near_share": 0.5, "ask_near_share": 0.5, "updates": 30,
         "open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0}
    r.update(over)
    return r


def kinds(events):
    return {e["kind"] for e in events}


# ─── тишина на ровном фоне ────────────────────────────────────────────────────

def test_flat_background_produces_nothing():
    """
    Самое важное отрицательное свойство. Детектор, который срабатывает на
    ровном месте, бесполезен: событий будет столько, что они перестанут
    что-либо означать.
    """
    assert detect(flat(40)) == []


def test_no_events_without_enough_history():
    """
    Пока нормы нет, событий нет. Иначе первые минуты дня всегда выглядели бы
    аномальными — просто потому, что сравнивать не с чем.
    """
    rows = [minute(f"2026-08-03T10:0{i}", buy_volume=9000) for i in range(4)]
    assert detect(rows) == []


def test_empty_input():
    assert detect([]) == []
    assert summarize([]) == {"total": 0, "by_kind": {}}


# ─── поглощение и агрессия ────────────────────────────────────────────────────

def test_absorption_needs_volume_one_side_and_still_price():
    """Много агрессии в одну сторону — а цена не сдвинулась."""
    rows = flat(20) + [minute("2026-08-03T10:20", buy_volume=900,
                              sell_volume=60, high=100.05, low=99.98)]
    assert "absorption" in kinds(detect(rows))


def test_same_volume_with_moving_price_is_aggression_not_absorption():
    """
    Отличие одно: сдвинулась ли цена. Если сдвинулась — это агрессия, если нет
    — поглощение. Одно и то же событие не должно попадать в оба.
    """
    rows = flat(20) + [minute("2026-08-03T10:20", buy_volume=900,
                              sell_volume=60, high=103.0, low=99.8, close=102.5)]
    k = kinds(detect(rows))
    assert "aggressive_buying" in k
    assert "absorption" not in k


def test_aggressive_selling_is_labelled_by_the_heavier_side():
    rows = flat(20) + [minute("2026-08-03T10:20", buy_volume=60,
                              sell_volume=900, high=100.2, low=96.0, close=96.5)]
    assert "aggressive_selling" in kinds(detect(rows))


def test_balanced_flow_is_not_aggression_however_loud():
    """Громкий, но ровный поток — это не односторонняя агрессия."""
    rows = flat(20) + [minute("2026-08-03T10:20", buy_volume=800,
                              sell_volume=800, high=103.0, low=97.0)]
    k = kinds(detect(rows))
    assert "aggressive_buying" not in k and "aggressive_selling" not in k


# ─── частота сделок ──────────────────────────────────────────────────────────

def test_trade_acceleration_is_about_count_not_volume():
    """
    Десять сделок по 100 и одна на 1000 дают равный объём, но это разные
    события. Ускорение — про частоту.
    """
    rows = flat(20) + [minute("2026-08-03T10:20", trade_count=200,
                              buy_volume=100, sell_volume=100)]
    assert "trade_acceleration" in kinds(detect(rows))


def test_big_volume_in_few_trades_is_not_acceleration():
    rows = flat(20) + [minute("2026-08-03T10:20", trade_count=20,
                              buy_volume=900, sell_volume=60, high=103.0)]
    assert "trade_acceleration" not in kinds(detect(rows))


# ─── крупная заявка: появление, уход, возврат ────────────────────────────────

def test_big_order_appears():
    rows = flat(20) + [minute("2026-08-03T10:20", bid_top=900)]
    ev = [e for e in detect(rows) if e["kind"] == "big_order"]
    assert ev and ev[0]["numbers"]["side"] == "bid"


def test_level_eaten_when_volume_matches_the_missing_size():
    """
    Плита исчезла, и объём сделок минуты сопоставим с её размером — значит её
    вероятнее торговали, чем сняли.
    """
    rows = flat(20)
    rows.append(minute("2026-08-03T10:20", bid_top=1000))
    rows.append(minute("2026-08-03T10:21", bid_top=50,
                       buy_volume=500, sell_volume=500, high=103.0, low=99.0))
    ev = [e for e in detect(rows) if e["kind"] == "level_eaten"]
    assert ev, "исчезла и торговалась"
    assert ev[0]["numbers"]["was"] == 1000 and ev[0]["numbers"]["now"] == 50


def test_liquidity_pulled_when_it_vanished_without_trades():
    """
    Та же пропажа, но сделок почти не было — значит заявку СНЯЛИ. Это
    противоположное по смыслу событие, и различить их можно только объёмом.
    """
    rows = flat(20)
    rows.append(minute("2026-08-03T10:20", bid_top=1000))
    rows.append(minute("2026-08-03T10:21", bid_top=50,
                       buy_volume=20, sell_volume=20))
    k = kinds(detect(rows))
    assert "liquidity_pulled" in k
    assert "level_eaten" not in k, "снятие и съедание — разные события"


def test_level_restored_after_it_went_away():
    rows = flat(18)
    rows.append(minute("2026-08-03T10:18", bid_top=1000))
    rows.append(minute("2026-08-03T10:19", bid_top=30))
    rows.append(minute("2026-08-03T10:20", bid_top=950))
    assert "level_restored" in kinds(detect(rows))


# ─── расхождение цены и стакана ──────────────────────────────────────────────

def test_price_and_book_diverge():
    """
    Цена растёт, а доля покупателей в стакане падает. Именно этой связки не было
    в измерениях по одной цене: стакана там не существовало вовсе.
    """
    rows = flat(20)
    rows.append(minute("2026-08-03T10:20", close=100.0, bid_share=0.70))
    rows.append(minute("2026-08-03T10:21", close=101.0, bid_share=0.40))
    ev = [e for e in detect(rows) if e["kind"] == "price_book_divergence"]
    assert ev
    n = ev[0]["numbers"]
    assert n["price_change"] > 0 and n["bid_share_change"] < 0


def test_price_and_book_moving_together_is_not_divergence():
    rows = flat(20)
    rows.append(minute("2026-08-03T10:20", close=100.0, bid_share=0.40))
    rows.append(minute("2026-08-03T10:21", close=101.0, bid_share=0.70))
    assert "price_book_divergence" not in kinds(detect(rows))


# ─── истощение агрессора ─────────────────────────────────────────────────────

def test_aggressor_exhaustion_needs_fading_delta_and_no_progress():
    """Давление в одну сторону слабеет, а цена не продвинулась."""
    rows = flat(20)
    for i, (b, s) in enumerate(((700, 100), (400, 100), (200, 150))):
        rows.append(minute(f"2026-08-03T10:{20 + i}", buy_volume=b,
                           sell_volume=s, close=100.0, high=100.2, low=99.9))
    assert "aggressor_exhaustion" in kinds(detect(rows))


def test_no_exhaustion_when_price_actually_moved():
    rows = flat(20)
    for i, (b, s, c) in enumerate(((700, 100, 101.0), (400, 100, 102.0),
                                   (200, 150, 103.0))):
        rows.append(minute(f"2026-08-03T10:{20 + i}", buy_volume=b,
                           sell_volume=s, close=c, high=c + 0.2, low=c - 0.2))
    assert "aggressor_exhaustion" not in kinds(detect(rows))


def test_no_exhaustion_when_pressure_is_growing():
    rows = flat(20)
    for i, (b, s) in enumerate(((200, 150), (400, 100), (700, 100))):
        rows.append(minute(f"2026-08-03T10:{20 + i}", buy_volume=b,
                           sell_volume=s, close=100.0))
    assert "aggressor_exhaustion" not in kinds(detect(rows))


# ─── последовательности ──────────────────────────────────────────────────────

def test_breakout_after_absorption_is_dated_by_the_breakout():
    """
    Событие датируется минутой ПОДТВЕРЖДЕНИЯ, а не минутой поглощения. Иначе
    вышло бы, что в 10:20 мы уже знали про 10:22.
    """
    rows = flat(20)
    rows.append(minute("2026-08-03T10:20", buy_volume=900, sell_volume=60,
                       high=100.05, low=99.98, close=100.0))
    rows.append(minute("2026-08-03T10:21", close=100.02))
    rows.append(minute("2026-08-03T10:22", close=101.5))
    ev = [e for e in detect(rows) if e["kind"] == "breakout_after_absorption"]
    assert ev
    assert ev[0]["ts"] == "2026-08-03T10:22", "дата — минута пробоя"
    assert ev[0]["numbers"]["absorbed_at"] == "2026-08-03T10:20"
    assert ev[0]["numbers"]["minutes_after"] == 2


def test_absorption_without_follow_through_gives_no_breakout():
    rows = flat(20)
    rows.append(minute("2026-08-03T10:20", buy_volume=900, sell_volume=60,
                       high=100.05, low=99.98))
    rows += [minute(f"2026-08-03T10:{21 + i}", close=100.0) for i in range(6)]
    assert "breakout_after_absorption" not in kinds(detect(rows))


def test_false_breakout_needs_the_return():
    rows = flat(20, high=100.2, low=99.8, close=100.0)
    rows.append(minute("2026-08-03T10:20", close=101.0, high=101.2))
    rows.append(minute("2026-08-03T10:21", close=99.9))
    ev = [e for e in detect(rows) if e["kind"] == "false_breakout"]
    assert ev
    assert ev[0]["numbers"]["direction"] == "up"
    assert ev[0]["ts"] == "2026-08-03T10:21", "дата — минута возврата"


def test_breakout_that_holds_is_not_false():
    rows = flat(20, high=100.2, low=99.8, close=100.0)
    rows += [minute(f"2026-08-03T10:{20 + i}", close=101.0 + i, high=101.5 + i)
             for i in range(6)]
    assert "false_breakout" not in kinds(detect(rows))


def test_breakout_level_excludes_the_current_bar():
    """
    Регрессия на конкретную ошибку 31.07: уровень пробоя сравнивался с
    максимумом, уже включавшим текущий бар. Тогда вместо 3078 событий вышло 6,
    и понять это по результату было почти нельзя.
    """
    src_rows = flat(21, high=100.2, low=99.8, close=100.0)
    # последняя минута сама ставит новый максимум и на нём же закрывается
    src_rows[-1].update(high=105.0, close=105.0)
    ev = detect(src_rows)
    # если уровень считался бы С текущим баром, пробоя не нашлось бы никогда
    fb = [e for e in ev if e["kind"] == "false_breakout"]
    assert fb == [], "возврата не было, значит ложного пробоя нет"
    # а сам факт превышения прошлого максимума должен быть виден: добавим возврат
    src_rows.append(minute("2026-08-03T10:21", close=99.9))
    assert "false_breakout" in kinds(detect(src_rows))


# ─── причинность и форма события ─────────────────────────────────────────────

def test_baseline_uses_only_past_minutes():
    """
    Норма не должна зависеть от будущего. Если дописать в конец ряда громкие
    минуты, события на РАНЕЕ найденных минутах меняться не должны.
    """
    rows = flat(20) + [minute("2026-08-03T10:20", buy_volume=900,
                              sell_volume=60, high=100.05, low=99.98)]
    before = [(e["ts"], e["kind"]) for e in detect(rows)]
    extended = rows + [minute(f"2026-08-03T10:{21 + i}", buy_volume=5000,
                              sell_volume=5000, trade_count=500)
                       for i in range(5)]
    after = [(e["ts"], e["kind"]) for e in detect(extended)
             if e["ts"] <= "2026-08-03T10:20"]
    assert before == after, "будущее не меняет прошлое"


def test_event_has_no_direction_or_signal():
    """
    Событие ОПИСЫВАЕТ. Появись здесь «вверх» или «сила сигнала» — и через неделю
    это станет правилом, которого никто не измерял. Ровно так и появились семь
    удалённых шагов.
    """
    rows = flat(20) + [minute("2026-08-03T10:20", buy_volume=900,
                              sell_volume=60, high=103.0, close=102.0)]
    for e in detect(rows):
        assert set(e.keys()) == {"ts", "kind", "why", "numbers"}
        assert "signal" not in e and "direction" not in e
        assert "recommend" not in str(e["why"]).lower()


def test_event_carries_the_numbers_that_triggered_it():
    """Без чисел событие непроверяемо: нельзя понять, почему сработало."""
    rows = flat(20) + [minute("2026-08-03T10:20", buy_volume=900,
                              sell_volume=60, high=100.05, low=99.98)]
    e = [x for x in detect(rows) if x["kind"] == "absorption"][0]
    for k in ("volume", "volume_baseline", "one_side_share", "price_range"):
        assert k in e["numbers"], k
    assert e["why"], "и человеческое объяснение тоже"


def test_thresholds_are_all_overridable():
    """
    Ни один порог не измерен. Значит все обязаны меняться извне, иначе догадка
    закрепится как истина.
    """
    rows = flat(20) + [minute("2026-08-03T10:20", buy_volume=300,
                              sell_volume=100, high=100.05, low=99.98)]
    assert "absorption" not in kinds(detect(rows))
    assert "absorption" in kinds(detect(rows, {"vol_mult": 1.5}))


def test_all_declared_kinds_are_reachable():
    """
    Объявленный тип, который невозможно получить, — мёртвый код. Каждый из
    KINDS должен встречаться хотя бы в одном тесте этого файла.
    """
    import pathlib
    text = pathlib.Path(__file__).read_text()
    missing = [k for k in KINDS if f'"{k}"' not in text]
    assert not missing, f"типы без теста: {missing}"


def test_events_come_sorted_by_time():
    rows = flat(20)
    rows.append(minute("2026-08-03T10:20", buy_volume=900, sell_volume=60,
                       high=100.05, low=99.98))
    rows.append(minute("2026-08-03T10:21", close=101.5, trade_count=200))
    ts = [e["ts"] for e in detect(rows)]
    assert ts == sorted(ts)


def test_summary_counts_by_kind():
    rows = flat(20) + [minute("2026-08-03T10:20", buy_volume=900,
                              sell_volume=60, high=100.05, low=99.98)]
    s = summarize(detect(rows))
    assert s["total"] >= 1
    assert s["by_kind"].get("absorption") == 1


def test_missing_fields_do_not_crash():
    """
    Минута без сделок свечи не имеет вовсе — это не дырка в данных, а отсутствие
    торгов. Детектор обязан это переносить.
    """
    rows = flat(20)
    rows.append({"ts": "2026-08-03T10:20"})                  # пустая минута
    rows.append(minute("2026-08-03T10:21", close=None, high=None, low=None))
    detect(rows)                                              # не падает


def test_defaults_are_documented_as_guesses():
    """Пороги — догадки, и это должно быть написано там, где они лежат."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "src/analysis/book_events.py").read_text()
    i = src.index("DEFAULTS = {")
    assert "догадк" in src[max(0, i - 400):i].lower()
    assert len(DEFAULTS) >= 15, "и их немало"


# ─── подключение к приложению ─────────────────────────────────────────────────

def test_endpoint_and_merge_exist():
    """
    Детектор смотрит на СВЯЗКИ потока, стакана и цены, а они лежат в трёх
    таблицах. Без сведения в одну строку минуты половина типов не сработает
    никогда: расхождение цены и стакана требует обоих сразу.
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    db = (root / "src/db.py").read_text()
    assert "async def minute_rows" in db
    i = db.index("async def minute_rows")
    body = db[i:i + 1400]
    for fn in ("flow_series", "book_series", "candle_series"):
        assert fn in body, f"{fn} не участвует в сведении"

    api = (root / "src/api/main.py").read_text()
    assert "/api/events/{ticker}" in api
    j = api.index("/api/events/{ticker}")
    route = api[j:j + 2600]
    assert "не предсказывает" in route, "рамка должна быть в документации маршрута"
    assert "ДОГАДКИ" in route, "и про пороги тоже"


def test_missing_minutes_are_not_invented():
    """
    Пропуск минуты означает отсутствие торгов. Подставлять туда нули нельзя:
    получились бы события из ничего — например «объём упал в десять раз».
    """
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    db = (root / "src/db.py").read_text()
    i = db.index("async def minute_rows")
    assert "нельзя" in db[i:i + 1400], "запрет должен быть записан рядом с кодом"
