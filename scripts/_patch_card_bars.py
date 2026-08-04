"""Патч: блок стакана из минутной агрегации. Идемпотентен."""

CARD = "src/analysis/card.py"
TEST = "tests/test_card.py"

CARD_CODE = '''

def book_minute_block(book_min: dict, lot: int = 1,
                      min_step: Optional[float] = None,
                      atr: Optional[float] = None) -> dict:
    """
    Стакан так, как он ДЕЙСТВИТЕЛЬНО хранится в памяти стрима.

    Вход — запись CURRENT.agg.snapshot(tk)["book"][источник]. Уровней по
    ценам там нет сознательно: 20 цен десять раз в секунду на 80 бумаг —
    уже не минутная таблица. Значит старый book_block со списком
    уровней здесь неприменим: любая подстановка дала бы либо нули в
    рублях, либо плиту в роли «всего бида».

    ТРИ ВЕЩИ, которые легко спутать и которые здесь разнесены:
      avg_bid_lots    средний объём бида за минуту = сумма / число пакетов
      avg_bid5_lots   то же только по пяти лучшим уровням
      top_bid_lots    максимальная ОДИНОЧНАЯ заявка за минуту

    Спред отдаётся дважды — последний и средний за минуту: на тонком
    рынке они расходятся в разы, а стоимость входа ближе к среднему.

    Рубли — приближение сверху: лоты × лотность × лучшая цена стороны,
    а заявки глубже стоят дальше от неё.
    """
    out: dict = {"kind": "minute_aggregate"}
    if not isinstance(book_min, dict) or not book_min:
        out["note"] = "минутной агрегации стакана нет"
        return out

    upd = int(_f(book_min.get("updates")) or 0)
    bb, ba = _f(book_min.get("best_bid")), _f(book_min.get("best_ask"))
    lot = int(lot or 1)
    out["note"] = ("сводка всех пакетов минуты, не мгновенный срез; "
                   "заявок по отдельным ценам здесь нет")
    out["updates"] = upd
    out["best_bid"] = bb or None
    out["best_ask"] = ba or None
    out["lot"] = lot
    if upd <= 0:
        out["note"] = "пакетов стакана за эту минуту не было"
        return out

    # СРЕДНЕЕ, а не сумма. Сумма зависит от частоты пакетов и потому
    # сравнима только с собой же в той же минуте.
    avg_bid = _f(book_min.get("bid_vol_sum")) / upd
    avg_ask = _f(book_min.get("ask_vol_sum")) / upd
    avg_bid5 = _f(book_min.get("bid5_sum")) / upd
    avg_ask5 = _f(book_min.get("ask5_sum")) / upd
    out["avg_bid_lots"] = _r(avg_bid, 1)
    out["avg_ask_lots"] = _r(avg_ask, 1)
    out["avg_bid5_lots"] = _r(avg_bid5, 1)
    out["avg_ask5_lots"] = _r(avg_ask5, 1)
    if bb > 0:
        out["avg_bid_rub"] = _r(avg_bid * lot * bb, 0)
        out["avg_bid5_rub"] = _r(avg_bid5 * lot * bb, 0)
    if ba > 0:
        out["avg_ask_rub"] = _r(avg_ask * lot * ba, 0)
        out["avg_ask5_rub"] = _r(avg_ask5 * lot * ba, 0)

    # ПЛИТА — МАКСИМУМ за минуту, и именно поэтому у неё отдельное имя:
    # важно, что заявка была, даже если её сняли через секунду.
    tb = _f(book_min.get("bid_top_max"))
    ta = _f(book_min.get("ask_top_max"))
    out["top_bid_lots"] = _r(tb, 1) or None
    out["top_ask_lots"] = _r(ta, 1) or None
    if tb > 0 and bb > 0:
        out["top_bid_rub"] = _r(tb * lot * bb, 0)
    if ta > 0 and ba > 0:
        out["top_ask_rub"] = _r(ta * lot * ba, 0)
    if tb > 0 and avg_bid > 0:
        out["top_bid_share_of_avg"] = _r(tb / avg_bid, 2)
    if ta > 0 and avg_ask > 0:
        out["top_ask_share_of_avg"] = _r(ta / avg_ask, 2)

    # Перекос: средний и РАЗМАХ. Размах важнее: 0.5 среднего при
    # колебании 0.2–0.8 и тот же 0.5 без колебаний — разные минуты.
    tot = avg_bid + avg_ask
    if tot > 0:
        out["bid_share"] = _r(avg_bid / tot, 3)
    imn, imx = book_min.get("imb_min"), book_min.get("imb_max")
    if imn is not None and imx is not None:
        out["bid_share_min"] = _r(_f(imn), 3)
        out["bid_share_max"] = _r(_f(imx), 3)
        out["bid_share_swing"] = _r(_f(imx) - _f(imn), 3)

    # СПРЕД дважды, и средний считается ДЕЛЕНИЕМ НА updates.
    last_sp = _r(ba - bb, 6) if bb > 0 and ba > 0 else None
    avg_sp = _r(_f(book_min.get("spread_sum")) / upd, 6)
    out["spread"] = last_sp
    out["avg_spread"] = avg_sp or None
    for name, val in (("spread", last_sp), ("avg_spread", avg_sp)):
        if val:
            if atr:
                out[name + "_in_atr"] = _r(val / atr, 4)
            if min_step:
                out[name + "_in_steps"] = _r(val / min_step, 2)
    return out
'''

TEST_CODE = '''

# ─── стакан из минутной агрегации ────────────────────────

def _book_min(upd=100):
    """Запись той же формы, что кладёт stream.py в self.book."""
    return {"ts": "2026-08-04 13:30", "session": "main", "updates": upd,
            "bid_vol_sum": 100.0 * upd, "ask_vol_sum": 50.0 * upd,
            "bid5_sum": 60.0 * upd, "ask5_sum": 30.0 * upd,
            "spread_sum": 0.05 * upd, "best_bid": 100.0, "best_ask": 100.02,
            "imb_min": 0.4, "imb_max": 0.9,
            "bid_top_max": 400, "ask_top_max": 120}


def test_average_of_the_book_is_divided_by_the_number_of_packets():
    """В памяти лежат СУММЫ по пакетам; без деления цифра бессмысленна."""
    a = C.book_minute_block(_book_min(upd=100), lot=10)
    b = C.book_minute_block(_book_min(upd=600), lot=10)
    assert a["avg_bid_lots"] == 100.0
    assert a["avg_bid_lots"] == b["avg_bid_lots"]   # частота не влияет
    assert a["avg_bid_rub"] == 100000              # 100 лотов x 10 x 100 ₽
    assert a["avg_spread"] == 0.05


def test_the_biggest_order_is_never_called_the_whole_bid():
    """Плита — максимум за минуту, а не объём стороны."""
    bk = C.book_minute_block(_book_min(), lot=10)
    assert bk["top_bid_lots"] == 400.0
    assert bk["avg_bid_lots"] == 100.0
    assert bk["top_bid_share_of_avg"] == 4.0
    assert "bid_rub" not in bk and "bids" not in bk


def test_book_swing_is_kept_next_to_the_average():
    bk = C.book_minute_block(_book_min(), lot=1)
    assert bk["bid_share"] == 0.667
    assert bk["bid_share_swing"] == 0.5


def test_book_spread_is_measured_in_atr_and_steps():
    bk = C.book_minute_block(_book_min(), lot=1, min_step=0.01, atr=0.5)
    assert bk["spread"] == 0.02
    assert bk["spread_in_steps"] == 2.0
    assert bk["avg_spread_in_steps"] == 5.0
    assert bk["avg_spread_in_atr"] == 0.1


def test_no_book_aggregate_is_a_reason_not_a_crash():
    for empty in ({}, None, {"updates": 0}):
        bk = C.book_minute_block(empty, lot=1)
        assert bk["kind"] == "minute_aggregate"
        assert bk["note"]
        assert "avg_bid_lots" not in bk


def test_book_aggregate_says_it_has_no_levels():
    """Читатель не должен искать здесь цены заявок: их не собирают."""
    bk = C.book_minute_block(_book_min(), lot=1)
    assert "заявок по отдельным ценам здесь нет" in bk["note"]
'''


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, t):
    with open(p, "w", encoding="utf-8") as f:
        f.write(t)


done = []
for path, code, mark in ((CARD, CARD_CODE, "def book_minute_block("),
                         (TEST, TEST_CODE, "def _book_min(")):
    text = read(path)
    if mark in text:
        print(" = уже есть:", path)
        continue
    write(path, text.rstrip("\n") + "\n" + code)
    done.append(path)

for d in done:
    print(" + дописано:", d)
