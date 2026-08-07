"""
История объёма по таймфреймам: сколько, во сколько раз, куда и с каким ускорением.

ЗАЧЕМ, словами Артёма: «цена +0.4% → объём ×1.2» — не особо интересно, а «цена
+0.4% → объём ×4.8 → новые максимумы» уже потенциальное начало движения. Связка
цены с объёмом, а не объём сам по себе.

ЧТО УЖЕ ИЗМЕРЕНО И ЧТО НЕТ. 31.07 RVOL проверялся как самостоятельный фильтр и
оказался ПЛОСКИМ на всех порогах: отбор по «объём выше нормы» не давал ничего.
Это про одиночный RVOL, и это не значит, что связка цены с объёмом тоже пуста —
её никто не мерил. Поэтому здесь описание, а не правило: ни «сильный сигнал», ни
«начало движения» в выдаче нет, только числа и то, что по ним видно.

ГЛАВНАЯ ЛОВУШКА: ЧАСТИЧНЫЙ ОБЪЁМ НЕЗАКРЫТОГО БАРА. Пятиминутный бар на первой
минуте набрал пятую часть своего объёма. Сравнить его сумму с суммами закрытых
бар — значит всегда получать «объём низкий», просто потому что бар не дожил.
Поэтому у текущего бара считается ТЕМП: объём на минуту. Его и сравниваем с
темпом закрытых.

Эта же ловушка уже поймана в `technical.volume_stats` для ДНЕВНЫХ бар — там она
называется `last_bar_may_be_partial` и решается через `day_progress`. Модули не
дублируют друг друга: `volume_stats` сравнивает день с днями, этот — минутные
бары внутри дня.

НОРМА — МЕДИАНА, а не среднее. Одна минута выноса смещает среднее так, что
следующая такая же перестаёт быть заметной. И только по ПРОШЛЫМ барам: 31.07
сравнение с максимумом, включавшим текущий бар, дало 6 событий вместо 3078.

ДВЕ ЛОВУШКИ, НАЙДЕННЫЕ НА РЕАЛЬНЫХ ДАННЫХ 01.08, а не придуманные:

    пропуск минут    у MTLR ЗАКРЫТАЯ пятиминутка 17:20 содержала одну минуту из
                     пяти: 15 лотов против нормы 476, ×0.03. Сумма по интервалу
                     верна, но пропуск означает либо «сделок не было», либо «сбор
                     данных лежал» — разные вещи. Отсюда `last_bar_partial`.

    один блок        у SBER бар 17:20 это [102, 86, 2023, 17, 68]: «×5.66 за пять
                     минут» на деле ОДНА минута. Ровный интерес и один блок дают
                     одинаковую кратность. Отсюда `top_minute_share`.

ДВА РАЗНЫХ РАЗДЕЛЕНИЯ ОБЪЁМА, и их легко спутать:

    volume_buy / volume_sell   от биржи, из свечи: кто был АГРЕССОРОМ в сделке
    объём на росте / падении   наш расчёт: куда шла ЦЕНА в барах с этим объёмом

Это не одно и то же. Минута может быть на 90% покупок по агрессору и при этом
закрыться ниже — если продавцы держали лимитами. Отдаются оба.
"""
from statistics import median

from src.analysis.timeframes import bars, _abs_minute

STEPS = (1, 3, 5, 15)
BASE_BARS = 20          # сколько прошлых закрытых бар берётся за норму
BASE_NEED = 4           # меньше этого нормы нет
HIGH_BARS = 20          # окно для «новый максимум»


def _vol(b: dict) -> float:
    try:
        return float(b.get("volume") or 0)
    except (TypeError, ValueError):
        return 0.0


def _minute_of_day(ts) -> int:
    try:
        return int(ts[11:13]) * 60 + int(ts[14:16])
    except (TypeError, ValueError, IndexError):
        return -1


def _bar_minutes(rows: list, bar: dict, step: int) -> list:
    """Объёмы отдельных минут, попавших в этот бар."""
    key = bar.get("key")
    if key is None:
        return []
    out = []
    for r in rows:
        mm = _abs_minute(r.get("ts"))
        if mm is not None and mm // step == key:
            out.append(_vol(r))
    return out


def volume_frame(rows: list, step: int, base_bars: int = BASE_BARS,
                 high_bars: int = HIGH_BARS) -> dict:
    """
    Объём одного таймфрейма: сумма, кратность к норме, ускорение, направление.

    Кратность считается для ПОСЛЕДНЕГО ЗАКРЫТОГО бара — он сравним с нормой.
    Для текущего отдельно считается темп в минуту и его кратность: иначе бар,
    не дожив до конца, всегда выглядел бы тихим.
    """
    bs = bars(rows, step)
    if not bs:
        return {"step_min": step, "bars": 0}
    closed = [b for b in bs if b.get("complete")]
    forming = next((b for b in bs if not b.get("complete")), None)
    out = {"step_min": step, "closed_bars": len(closed)}

    if closed:
        last = closed[-1]
        out["volume_last"] = round(_vol(last))

        # СКОЛЬКО МИНУТ В ИЗМЕРЯЕМОМ БАРЕ. Найдено на реальных данных: у MTLR
        # закрытая пятиминутка 17:20 содержала ОДНУ минуту из пяти — 15 лотов
        # против нормы 476, то есть ×0.03. Сумма по интервалу верна, но пропуск
        # минуты означает либо «сделок не было», либо «сбор данных лежал», и это
        # РАЗНЫЕ вещи. Без этого поля их не отличить, а staleness до 6 минут я
        # уже измерял.
        out["last_bar_minutes"] = last.get("minutes")
        if last.get("minutes") and last["minutes"] < step:
            out["last_bar_partial"] = True

        # ДОЛЯ САМОЙ КРУПНОЙ МИНУТЫ. У SBER бар 17:20 это [102, 86, 2023, 17,
        # 68]: «×5.66 за пять минут» на деле одна минута. Ровный повышенный
        # интерес и один блок дают одну и ту же кратность, а это не одно и то же.
        mins = _bar_minutes(rows, last, step)
        if mins and _vol(last) > 0:
            out["top_minute_share"] = round(max(mins) / _vol(last), 3)

    # НОРМА по прошлым закрытым барам, не включая последний.
    hist = [_vol(b) for b in closed[:-1][-base_bars:] if _vol(b) > 0]
    if len(hist) >= BASE_NEED and closed:
        base = median(hist)
        out["volume_baseline"] = round(base)
        out["base_bars_used"] = len(hist)
        if base > 0:
            out["volume_mult"] = round(_vol(closed[-1]) / base, 2)

    # УСКОРЕНИЕ: последний закрытый против предыдущего.
    if len(closed) >= 2:
        prev = _vol(closed[-2])
        out["volume_prev"] = round(prev)
        if prev > 0:
            out["volume_accel"] = round(_vol(closed[-1]) / prev, 2)

    # ТЕКУЩИЙ БАР: темп, а не сумма. Сумма частичная и несравнима.
    if forming and forming.get("minutes"):
        mins = max(1, int(forming["minutes"]))
        pace = _vol(forming) / mins
        out["forming"] = {"minutes": mins, "volume_so_far": round(_vol(forming)),
                          "pace_per_min": round(pace, 1)}
        if hist:
            base_pace = median(hist) / step
            if base_pace > 0:
                out["forming"]["pace_mult"] = round(pace / base_pace, 2)

    # ОБЪЁМ НА РОСТЕ И НА ПАДЕНИИ — куда шла ЦЕНА в барах с этим объёмом.
    # Это НЕ то же самое, что volume_buy/volume_sell от биржи: там агрессор
    # сделки, здесь направление бара. Минута может быть на 90% покупок по
    # агрессору и закрыться ниже, если продавцы держали лимитами.
    up = down = flat = 0.0
    for b in closed[-base_bars:]:
        o, c = b.get("open"), b.get("close")
        v = _vol(b)
        if o is None or c is None or v <= 0:
            continue
        if c > o:
            up += v
        elif c < o:
            down += v
        else:
            flat += v
    tot = up + down + flat
    if tot > 0:
        out["vol_up"] = round(up)
        out["vol_down"] = round(down)
        out["vol_up_share"] = round(up / tot, 4)
        out["window_bars"] = len(closed[-base_bars:])

    # НОВЫЙ МАКСИМУМ или МИНИМУМ по закрытым барам окна, не включая последний.
    if len(closed) >= 3:
        window = closed[-high_bars - 1:-1]
        if window:
            hi = max((b["high"] for b in window if b.get("high")), default=None)
            lo = min((b["low"] for b in window if b.get("low")), default=None)
            last = closed[-1]
            if hi is not None and last.get("high"):
                out["at_new_high"] = bool(last["high"] > hi)
            if lo is not None and last.get("low"):
                out["at_new_low"] = bool(last["low"] < lo)
            out["high_window_bars"] = len(window)
    return out


def profile(rows: list, steps: tuple = STEPS, **kw) -> dict:
    """
    Объём по всем таймфреймам плюс СВЯЗКА цены с объёмом.

    Связка — то, ради чего всё: «+0.4% при ×1.2» и «+0.4% при ×4.8 с новым
    максимумом» это разные картины, а объём сам по себе их не различает.

    Здесь только числа и то, что по ним прямо видно. Слов «начало движения» и
    «сигнал» нет: 31.07 RVOL как фильтр измерялся плоским на всех порогах, а
    связку никто не мерил — значит утверждать про неё нечего.
    """
    from src.analysis.timeframes import frame as price_frame
    out = {"frames": {}}
    for st in steps:
        v = volume_frame(rows, st, **kw)
        p = price_frame(rows, st)
        # СВЯЗКА: движение цены и кратность объёма в одном месте.
        if v.get("volume_mult") is not None and p.get("change_pct") is not None:
            v["price_change_pct"] = p["change_pct"]
            v["price_direction"] = p.get("direction")
        out["frames"][f"{st}m"] = v

    # Одна строка для чтения глазами: что происходит на пятиминутке.
    f5 = out["frames"].get("5m") or {}
    if f5.get("volume_mult") is not None:
        bits = []
        if f5.get("price_change_pct") is not None:
            ch = f5["price_change_pct"]
            bits.append(f"цена {'+' if ch > 0 else ''}{ch}%")
        bits.append(f"объём ×{f5['volume_mult']}")
        if f5.get("at_new_high"):
            bits.append("новый максимум")
        elif f5.get("at_new_low"):
            bits.append("новый минимум")
        if f5.get("volume_accel") is not None:
            bits.append(f"к предыдущему бару ×{f5['volume_accel']}")
        out["line_5m"] = " · ".join(bits)
    return out
