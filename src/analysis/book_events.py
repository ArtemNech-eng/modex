"""
Детектор событий стакана: одиннадцать типов, все описательные.

ЧТО ЭТО И ЧЕМ НЕ ЯВЛЯЕТСЯ.

Модуль отвечает на вопрос «что произошло», а не «что будет». «В 11:34 по MTLR
было поглощение» — факт о данных, его можно проверить по числам. «Значит цена
пойдёт вверх» — утверждение, которого здесь НЕТ и не будет.

Почему это важно именно здесь. За неделю до появления этого файла были придуманы
семь шагов с десятью метриками и формулами — до всяких измерений. Ни одного
работающего правила они не дали, и всё было удалено. Ошибка была не в метриках, а
в порядке: правила раньше данных.

Размеченные события — это и есть недостающее звено. Вопрос «предшествует ли
перекос стакана движению цены» нельзя проверить, пока события не помечены. Сначала
разметка, потом измерение, и только потом — правило, если измерение его поддержит.
Поэтому в событии есть kind, есть числа, по которым оно сработало, и НЕТ поля
«направление» или «сигнал».

ПРИЧИННОСТЬ. Базовый уровень каждой метрики считается ТОЛЬКО по прошлым минутам.
Это не формальность: 31.07 определение пробоя сравнивало закрытие с максимумом,
который уже включал текущий бар, и вместо 3078 событий получилось 6. Подглядывание
вперёд ломает не точность, а сам смысл измерения.

ПОРОГИ — ДОГАДКИ. Ни один порог здесь не измерен, все выбраны по здравому смыслу и
задаются параметрами. Они относительны: сравнение идёт с собственной историей
бумаги, а не с абсолютными числами. У SBER и у UGLD «крупная заявка» отличается на
порядки, и единый порог в лотах бессмысленен.

ЧЕГО НЕ ХВАТАЕТ ДЛЯ ОДНОГО ТИПА. «Съедание уровня» в полном смысле требует ЦЕНЫ
крупной заявки, а хранится только её размер. Поэтому событие определено слабее:
плита исчезла И объём сделок минуты сопоставим с её размером, то есть её вероятнее
торговали, чем сняли. Отличить «съели» от «сняли» так можно, а сказать «съели
уровень 277.00» — нет.

ВХОД. Список минут по возрастанию времени, каждая — объединение строк потока,
стакана и свечи одной минуты. Отсутствующие поля допустимы: минута без сделок
свечи не имеет вовсе, и это не дырка в данных, а отсутствие торгов.
"""
from statistics import median
from typing import Optional

# Все пороги — ДОГАДКИ, ни один не измерен. Держатся в одном месте, чтобы их
# можно было менять и чтобы было видно, сколько их.
DEFAULTS = {
    "baseline_min": 20,        # сколько прошлых минут берётся за норму
    "baseline_need": 5,        # меньше этого — события не выдаются вовсе
    "vol_mult": 2.5,           # «много объёма» = столько раз от нормы
    "count_mult": 2.5,         # «ускорение сделок»
    "big_order_mult": 3.0,     # «крупная заявка» относительно нормы
    "delta_share": 0.6,        # односторонность потока
    "range_mult": 0.8,         # «цена на месте» = диапазон не больше нормы
    "drop_share": 0.4,         # исчезновение ликвидности: доля ушедшего объёма
    "eaten_share": 0.6,        # съедание: доля ушедшей плиты
    "traded_share": 0.5,       # её торговали, если объём сделок ≥ этой доли
    "pulled_share": 0.2,       # её сняли, если объём сделок < этой доли
    "restore_share": 0.7,      # восстановление: доля прежнего размера
    "restore_window": 10,      # за сколько минут ждём восстановления
    "follow_window": 5,        # окно для «пробоя после поглощения»
    "return_window": 5,        # окно для «ложного пробоя»
    "break_lookback": 20,      # по какому окну считается уровень пробоя
    "exhaust_min": 3,          # сколько минут односторонности для истощения
    "divergence_price": 0.001, # минимальное движение цены, доля
    "divergence_book": 0.10,   # минимальный сдвиг доли покупателей
}

KINDS = (
    "absorption",              # поглощение: много агрессии, цена на месте
    "level_eaten",             # плита исчезла и её, похоже, торговали
    "big_order",               # появилась крупная заявка
    "liquidity_pulled",        # крупная ликвидность снята, а не исполнена
    "level_restored",          # заявка вернулась после ухода
    "aggressive_buying",       # односторонний поток вверх
    "aggressive_selling",      # односторонний поток вниз
    "trade_acceleration",      # частота сделок выросла
    "price_book_divergence",   # цена и стакан разошлись
    "breakout_after_absorption",
    "false_breakout",
    "aggressor_exhaustion",
)


def _num(row: dict, key: str, default: float = 0.0) -> float:
    v = row.get(key)
    return default if v is None else float(v)


def _baseline(rows: list, i: int, key: str, p: dict) -> Optional[float]:
    """
    Норма метрики по ПРОШЛЫМ минутам. Медиана, а не среднее: одна минута с
    выносом смещает среднее так, что следующая такая же перестаёт быть событием.

    Берутся строго строки ДО i. Никакого подглядывания вперёд: 31.07 сравнение с
    максимумом, включавшим текущий бар, дало 6 событий вместо 3078.
    """
    lo = max(0, i - p["baseline_min"])
    vals = [_num(rows[j], key) for j in range(lo, i)]
    vals = [v for v in vals if v > 0]
    if len(vals) < p["baseline_need"]:
        return None
    return median(vals)


def _ev(row: dict, kind: str, why: str, **numbers) -> dict:
    """
    Событие. Есть kind, есть числа, есть человеческое объяснение.

    НЕТ направления и НЕТ силы сигнала. Событие описывает, а не советует: стоит
    добавить сюда «вверх» — и через неделю это превратится в правило, которое
    никто не измерял.
    """
    return {"ts": row.get("ts"), "kind": kind, "why": why,
            "numbers": {k: (round(v, 6) if isinstance(v, float) else v)
                        for k, v in numbers.items()}}


def detect(rows: list, params: Optional[dict] = None) -> list:
    """
    Найти события в ряду минут. Ряд ожидается по возрастанию времени.

    Каждая минута — объединение полей потока, стакана и свечи:
        поток   buy_volume sell_volume trade_count max_trade
        стакан  bid_share bid_vol_sum ask_vol_sum bid_top ask_top
                bid_near_share ask_near_share updates
        свеча   open high low close volume
    """
    p = {**DEFAULTS, **(params or {})}
    out: list = []
    if not rows:
        return out

    for i, r in enumerate(rows):
        ts = r.get("ts")
        buy, sell = _num(r, "buy_volume"), _num(r, "sell_volume")
        vol = buy + sell
        base_vol = _baseline(rows, i, "buy_volume", p)
        base_sell = _baseline(rows, i, "sell_volume", p)
        base_total = (base_vol + base_sell) if (base_vol and base_sell) else None
        base_cnt = _baseline(rows, i, "trade_count", p)

        hi, lo_, cl, op = (_num(r, "high"), _num(r, "low"),
                           _num(r, "close"), _num(r, "open"))
        rng = hi - lo_ if hi and lo_ else 0.0
        base_rng = None
        if i >= p["baseline_need"]:
            prev = [(_num(rows[j], "high") - _num(rows[j], "low"))
                    for j in range(max(0, i - p["baseline_min"]), i)]
            prev = [x for x in prev if x > 0]
            if len(prev) >= p["baseline_need"]:
                base_rng = median(prev)

        loud = bool(base_total and vol >= base_total * p["vol_mult"])
        share = (max(buy, sell) / vol) if vol else 0.0
        quiet_price = bool(base_rng and rng <= base_rng * p["range_mult"])

        # 1. ПОГЛОЩЕНИЕ. Много агрессии в одну сторону, а цена не сдвинулась.
        if loud and share >= p["delta_share"] and quiet_price:
            out.append(_ev(r, "absorption",
                           "объём выше нормы, поток односторонний, "
                           "а диапазон минуты не расширился",
                           volume=vol, volume_baseline=base_total,
                           one_side_share=share, price_range=rng,
                           range_baseline=base_rng))

        # 6. АГРЕССИЯ. Односторонний поток при выросшем объёме — но цена при этом
        # двигалась, иначе это уже поглощение выше.
        if loud and share >= p["delta_share"] and not quiet_price:
            kind = "aggressive_buying" if buy > sell else "aggressive_selling"
            out.append(_ev(r, kind,
                           "объём выше нормы и поток односторонний",
                           volume=vol, volume_baseline=base_total,
                           one_side_share=share,
                           buy_volume=buy, sell_volume=sell))

        # 7. УСКОРЕНИЕ СДЕЛОК. Частота, а не объём: десять сделок по 100 и одна
        # на 1000 дают равный объём, но это разные события.
        cnt = _num(r, "trade_count")
        if base_cnt and cnt >= base_cnt * p["count_mult"]:
            out.append(_ev(r, "trade_acceleration",
                           "сделок в минуту заметно больше обычного",
                           trade_count=cnt, baseline=base_cnt,
                           average_size=round(vol / cnt, 2) if cnt else None))

        # 3-5. КРУПНАЯ ЗАЯВКА: появление, уход, возврат.
        for side, top_key, vol_key in (("bid", "bid_top", "bid_vol_sum"),
                                       ("ask", "ask_top", "ask_vol_sum")):
            top = _num(r, top_key)
            base_top = _baseline(rows, i, top_key, p)
            if base_top and top >= base_top * p["big_order_mult"]:
                out.append(_ev(r, "big_order",
                               f"на стороне {side} появилась заявка много "
                               f"крупнее обычной",
                               side=side, size=top, baseline=base_top,
                               near_share=r.get(f"{side}_near_share")))
            if i == 0:
                continue
            prev_top = _num(rows[i - 1], top_key)
            if prev_top <= 0:
                continue

            # ВОССТАНОВЛЕНИЕ проверяется ПЕРВЫМ и отдельно.
            #
            # Здесь была ошибка: блок стоял ниже, за отсечкой «заявка не
            # уменьшилась — пропускаем». Но восстановление это ровно РОСТ заявки,
            # то есть единственный случай, который та отсечка и выбрасывала.
            # Событие не могло сработать никогда, и тест это поймал.
            back = max(0, i - p["restore_window"])
            peak = max((_num(rows[j], top_key) for j in range(back, i)),
                       default=0.0)
            dip = min((_num(rows[j], top_key) for j in range(back, i)),
                      default=0.0)
            if (peak > 0 and dip <= peak * (1 - p["eaten_share"])
                    and top >= peak * p["restore_share"]
                    and prev_top < peak * p["restore_share"]):
                out.append(_ev(r, "level_restored",
                               f"заявка на стороне {side} вернулась к прежнему "
                               f"размеру после ухода",
                               side=side, peak=peak, dip=dip, now=top))

            gone = prev_top - top
            if gone <= 0:
                continue
            gone_share = gone / prev_top
            # Ушла ли она в сделки или её просто сняли — вот главное различие.
            traded_ratio = (vol / gone) if gone > 0 else 0.0
            if (gone_share >= p["eaten_share"]
                    and traded_ratio >= p["traded_share"]):
                out.append(_ev(r, "level_eaten",
                               f"крупная заявка на стороне {side} исчезла, и "
                               f"объём сделок сопоставим с её размером — "
                               f"вероятнее торговали, чем сняли",
                               side=side, was=prev_top, now=top,
                               gone_share=gone_share, minute_volume=vol,
                               traded_ratio=traded_ratio))
            elif (gone_share >= p["drop_share"]
                    and traded_ratio < p["pulled_share"]):
                out.append(_ev(r, "liquidity_pulled",
                               f"крупная ликвидность на стороне {side} ушла "
                               f"почти без сделок — снята, а не исполнена",
                               side=side, was=prev_top, now=top,
                               gone_share=gone_share, minute_volume=vol,
                               traded_ratio=traded_ratio))

        # 8. РАСХОЖДЕНИЕ ЦЕНЫ И СТАКАНА. Цена идёт в одну сторону, а перекос
        # заявок — в другую. Ровно та связка, которой не было в измерениях по
        # одной цене: там стакана не существовало вовсе.
        if i > 0:
            pc = _num(rows[i - 1], "close")
            bs, bs_prev = _num(r, "bid_share"), _num(rows[i - 1], "bid_share")
            if pc and cl and bs and bs_prev:
                dp = (cl - pc) / pc
                db = bs - bs_prev
                if (abs(dp) >= p["divergence_price"]
                        and abs(db) >= p["divergence_book"]
                        and (dp > 0) != (db > 0)):
                    out.append(_ev(r, "price_book_divergence",
                                   "цена и перекос заявок пошли в разные стороны",
                                   price_change=dp, bid_share_change=db,
                                   bid_share=bs, close=cl))

        # 11. ИСТОЩЕНИЕ АГРЕССОРА. Несколько минут подряд поток в одну сторону,
        # но давление слабеет и цена не продвинулась.
        n = p["exhaust_min"]
        if i >= n:
            window = rows[i - n + 1:i + 1]
            deltas, sides = [], []
            for w in window:
                b, s = _num(w, "buy_volume"), _num(w, "sell_volume")
                if b + s <= 0:
                    break
                deltas.append(b - s)
                sides.append(b > s)
            if len(deltas) == n and len(set(sides)) == 1:
                fading = all(abs(deltas[k]) < abs(deltas[k - 1])
                             for k in range(1, n))
                first_cl = _num(window[0], "close")
                moved = abs(cl - first_cl) / first_cl if first_cl and cl else 0.0
                if fading and moved < p["divergence_price"]:
                    out.append(_ev(r, "aggressor_exhaustion",
                                   f"{n} минуты подряд поток в одну сторону, но "
                                   f"давление слабеет, а цена не продвинулась",
                                   deltas=deltas,
                                   side="buy" if sides[0] else "sell",
                                   price_moved=moved))

    # 9-10. Последовательности: сначала одно событие, потом подтверждение или
    # опровержение ценой. Считаются ОТДЕЛЬНЫМ проходом, потому что смотрят
    # вперёд от события — но только от УЖЕ найденного, а не при его поиске.
    out.extend(_sequences(rows, out, p))
    out.sort(key=lambda e: (e["ts"] or "", e["kind"]))
    return out


def _sequences(rows: list, events: list, p: dict) -> list:
    """
    События-последовательности: пробой после поглощения и ложный пробой.

    Смотрят вперёд от уже найденного события. Это НЕ подглядывание: событие
    датируется минутой ПОДТВЕРЖДЕНИЯ, а не минутой поглощения. Иначе получилось
    бы, что в 11:34 мы уже знали про 11:37.
    """
    idx = {r.get("ts"): i for i, r in enumerate(rows)}
    out: list = []

    absorptions = [e for e in events if e["kind"] == "absorption"]
    for e in absorptions:
        i = idx.get(e["ts"])
        if i is None:
            continue
        hi, lo_ = _num(rows[i], "high"), _num(rows[i], "low")
        if not (hi and lo_):
            continue
        for j in range(i + 1, min(len(rows), i + 1 + p["follow_window"])):
            cl = _num(rows[j], "close")
            if not cl:
                continue
            if cl > hi or cl < lo_:
                out.append(_ev(rows[j], "breakout_after_absorption",
                               "цена вышла за границы минуты поглощения",
                               absorbed_at=e["ts"], absorbed_high=hi,
                               absorbed_low=lo_, close=cl,
                               minutes_after=j - i))
                break

    # ЛОЖНЫЙ ПРОБОЙ. Уровень считается по ПРОШЛЫМ минутам, не включая текущую:
    # именно на этом 31.07 сгорело определение пробоя.
    for i in range(p["break_lookback"], len(rows)):
        lo = i - p["break_lookback"]
        prev_hi = max((_num(rows[j], "high") for j in range(lo, i)), default=0.0)
        prev_lo = min((_num(rows[j], "low") for j in range(lo, i)
                       if _num(rows[j], "low") > 0), default=0.0)
        cl = _num(rows[i], "close")
        if not cl:
            continue
        for level, up in ((prev_hi, True), (prev_lo, False)):
            if not level:
                continue
            broke = cl > level if up else cl < level
            if not broke:
                continue
            for j in range(i + 1, min(len(rows), i + 1 + p["return_window"])):
                back = _num(rows[j], "close")
                if not back:
                    continue
                if (back < level) if up else (back > level):
                    out.append(_ev(rows[j], "false_breakout",
                                   "цена вышла за уровень и вернулась обратно",
                                   direction="up" if up else "down",
                                   level=level, broke_at=rows[i].get("ts"),
                                   broke_to=cl, returned_to=back,
                                   minutes_after=j - i))
                    break
            break
    return out


def summarize(events: list) -> dict:
    """Сколько событий каждого типа. Для быстрого взгляда и для проверок."""
    counts: dict = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    return {"total": len(events), "by_kind": dict(sorted(counts.items()))}
