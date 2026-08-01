"""
Локальные уровни с ГРАФИКА: где цена разворачивалась и сколько раз возвращалась.

ЗАЧЕМ ОТДЕЛЬНО ОТ СТАКАНА. Уровень в стакане — это чья-то заявка здесь и сейчас;
её могут снять за секунду. Уровень на графике — место, где цена уже разворачивалась,
и оно остаётся, даже когда в стакане на этой цене пусто. Артём сказал прямо:
«уровни делай не со стаканов а с графиков, стаканы лишь дополнение».

ЧТО ЗДЕСЬ ЕСТЬ И ЧЕГО НЕТ. Здесь факты: цена уровня, сколько раз её касались,
когда касались в первый и последний раз, выше она текущей цены или ниже, как далеко.

Здесь НЕТ пометок «сильный» и «слабый». Это соблазнительно и именно так неделю
назад появились семь шагов с придуманными формулами, не давшие ничего. Сколько
касаний делает уровень значимым — не измерено. Появится измерение — появится
пометка, не раньше.

ПРИЧИННОСТЬ. Разворот опознаётся по бару, у которого экстремум выше (или ниже)
соседей с ОБЕИХ сторон. Значит подтвердить его можно только через `right` бар после
того, как он случился, и уровень датируется минутой ПОДТВЕРЖДЕНИЯ. Иначе вышло бы,
что в 11:34 мы уже знали про 11:37 — ровно та ошибка, из-за которой 31.07 вместо
3078 событий пробоя нашлось 6.

ДОПУСК В ШАГАХ ЦЕНЫ, а не в процентах и не в рублях. Цены стоят на сетке шага, и
шаг у бумаг разный: SBER 0.01, VTBR 0.005, MVID 0.05, UGLD 0.0001. Сравнение в
рублях к тому же ломается о двоичную арифметику — 100.12 минус 100.10 даёт
0.020000000000010232, то есть БОЛЬШЕ двух шагов по 0.01.
"""
from typing import Optional

LEFT = 3            # сколько бар слева должно быть ниже (для максимума)
RIGHT = 3           # и справа — столько же, иначе это не разворот
TOL_TICKS = 3       # два разворота ближе этого числа шагов — один уровень
MIN_BARS = 15       # меньше этого истории уровней не выдаём вовсе


def _num(row: dict, key: str) -> Optional[float]:
    v = row.get(key)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def swings(rows: list, left: int = LEFT, right: int = RIGHT) -> list:
    """
    Развороты: бары, чей максимум выше соседей с обеих сторон (или минимум ниже).

    Возвращает [{ts, price, kind, confirmed_ts}], где confirmed_ts — минута, на
    которой разворот стало ВИДНО. Датировать уровень самим разворотом нельзя:
    в тот момент правых бар ещё не существовало.
    """
    out = []
    n = len(rows)
    for i in range(left, n - right):
        hi, lo = _num(rows[i], "high"), _num(rows[i], "low")
        if hi is None or lo is None:
            continue
        left_bars = rows[i - left:i]
        right_bars = rows[i + 1:i + 1 + right]
        if len(right_bars) < right:
            continue
        neigh_hi = [_num(r, "high") for r in left_bars + right_bars]
        neigh_lo = [_num(r, "low") for r in left_bars + right_bars]
        if all(x is not None and x < hi for x in neigh_hi):
            out.append({"ts": rows[i].get("ts"), "price": hi, "kind": "high",
                        "confirmed_ts": rows[i + right].get("ts")})
        if all(x is not None and x > lo for x in neigh_lo):
            out.append({"ts": rows[i].get("ts"), "price": lo, "kind": "low",
                        "confirmed_ts": rows[i + right].get("ts")})
    return out


def levels(rows: list, tick: float = 0.01, tol_ticks: int = TOL_TICKS,
           left: int = LEFT, right: int = RIGHT,
           price_now: Optional[float] = None, top: int = 6) -> list:
    """
    Развороты, собранные в уровни. Близкие цены считаются ОДНИМ уровнем.

    Зачем собирать. Цена редко разворачивается ровно на той же копейке: 276.50 и
    276.52 это одно место, а не два уровня. Без склейки список превращается в
    перечисление всех колебаний и перестаёт что-либо означать.

    Допуск в ШАГАХ цены: у SBER шаг 0.01, у UGLD 0.0001, и единый допуск в рублях
    склеил бы у одной бумаги полдиапазона, а у другой не склеил ничего.
    """
    if len(rows) < MIN_BARS:
        return []
    step = float(tick or 0)
    sw = swings(rows, left=left, right=right)
    if not sw:
        return []
    tol = step * tol_ticks if step > 0 else 0.0

    groups: list = []
    for s in sorted(sw, key=lambda x: x["price"]):
        placed = False
        for g in groups:
            # Сравнение в ШАГАХ с округлением: в рублях оно ломается о двоичную
            # арифметику, и уровень ровно на границе допуска не склеивался.
            if step > 0:
                if int(round(abs(s["price"] - g["price"]) / step)) <= tol_ticks:
                    placed = True
            elif abs(s["price"] - g["price"]) <= 1e-9:
                placed = True
            if placed:
                g["touches"] += 1
                g["prices"].append(s["price"])
                g["price"] = sum(g["prices"]) / len(g["prices"])
                g["kinds"].add(s["kind"])
                g["first_ts"] = min(g["first_ts"], s["ts"] or "")
                g["last_ts"] = max(g["last_ts"], s["ts"] or "")
                g["confirmed_ts"] = max(g["confirmed_ts"], s["confirmed_ts"] or "")
                break
        if not placed:
            groups.append({"price": s["price"], "prices": [s["price"]],
                           "touches": 1, "kinds": {s["kind"]},
                           "first_ts": s["ts"] or "", "last_ts": s["ts"] or "",
                           "confirmed_ts": s["confirmed_ts"] or ""})

    cur = price_now
    if cur is None:
        for r in reversed(rows):
            cur = _num(r, "close")
            if cur:
                break
    out = []
    for g in groups:
        price = round(g["price"], 6)
        rec = {
            "price": price, "touches": g["touches"],
            # Уровень бывает и сопротивлением, и поддержкой: цена отталкивалась
            # от него и сверху, и снизу. Это факт, а не противоречие.
            "kind": ("both" if len(g["kinds"]) > 1 else next(iter(g["kinds"]))),
            "first_ts": g["first_ts"], "last_ts": g["last_ts"],
            "confirmed_ts": g["confirmed_ts"],
        }
        if cur:
            rec["side"] = "above" if price > cur else "below"
            rec["dist_pct"] = round((price - cur) / cur * 100, 3)
            if step > 0:
                rec["dist_ticks"] = int(round(abs(price - cur) / step))
        out.append(rec)

    # Сортировка по БЛИЗОСТИ к цене, а не по числу касаний: сколько касаний делает
    # уровень значимым — не измерено, и ставить это в основу порядка значило бы
    # выдать догадку за знание.
    out.sort(key=lambda r: abs(r.get("dist_pct") or 0))
    return out[:top]


def day_extremes(rows: list) -> dict:
    """
    Максимум и минимум дня с временем, когда они были поставлены.

    Время важно: максимум, поставленный на открытии, и максимум минуту назад — это
    разные ситуации. 30.07 по FLOT «максимум дня» был взят из окна, начинавшегося
    в 15:25, стоял он в 10:00 и по нему выдали сигнал на пробой уровня, который
    рынок прошёл и отверг тринадцатью часами ранее.
    """
    hi = lo = None
    hi_ts = lo_ts = None
    for r in rows:
        h, l = _num(r, "high"), _num(r, "low")
        if h is not None and (hi is None or h > hi):
            hi, hi_ts = h, r.get("ts")
        if l is not None and (lo is None or l < lo):
            lo, lo_ts = l, r.get("ts")
    out = {"high": hi, "high_ts": hi_ts, "low": lo, "low_ts": lo_ts}
    if hi and lo and hi > lo:
        out["range"] = round(hi - lo, 6)
        last = None
        for r in reversed(rows):
            last = _num(r, "close")
            if last:
                break
        if last:
            # Где цена внутри диапазона дня: 0 у минимума, 1 у максимума.
            out["position"] = round((last - lo) / (hi - lo), 4)
    return out


def flow_change(rows: list, window: int = 5) -> dict:
    """
    Изменение ПОТОКА, а не самого потока: как дельта последних минут отличается
    от предыдущих.

    Зачем отдельно от дельты. Дельта −500 говорит, что продают. Но −500 после
    −2000 означает, что давление СЛАБЕЕТ, а −500 после +300 — что оно только
    началось. Одно и то же число, разные ситуации.
    """
    have = [r for r in rows if r.get("buy_volume") is not None
            or r.get("sell_volume") is not None]
    if len(have) < window * 2:
        return {}
    def _d(chunk):
        return sum((r.get("buy_volume") or 0) - (r.get("sell_volume") or 0)
                   for r in chunk)
    now, prev = _d(have[-window:]), _d(have[-window * 2:-window])
    out = {"window_min": window, "delta_now": now, "delta_prev": prev,
           "delta_change": now - prev}
    if prev != 0:
        out["delta_ratio"] = round(now / abs(prev), 3)
    # Смена знака — отдельный факт: поток развернулся, а не просто ослаб.
    out["flipped"] = bool((now > 0) != (prev > 0) and now != 0 and prev != 0)
    return out
