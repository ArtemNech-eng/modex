"""
Минутная история с MOEX ISS — чтобы норма по времени суток была СЕГОДНЯ.

ЗАЧЕМ. day_profile требует MIN_DAYS торговых дней по MIN_BARS_DAY баров.
Стрим поднялся 01.08, и в базе один торговый день — сам профиль наберётся
к середине августа. До тех пор сканер сравнивает 10:05 с 09:45, то есть
недооценивает утро (а именно утро дало лучший сетап в бэктесте) и
переоценивает обед. ISS отдаёт прошлые дни бесплатно и без токена.

ДВЕ ЛОВУШКИ, ОБЕ МОЛЧАЛИВЫЕ.

1. ЕДИНИЦЫ:

       ISS         volume — ШТУКИ (акции), value — РУБЛИ
       стрим      volume — ЛОТЫ, и _rub сам умножает его на лотность

2. КЛЮЧ МИНУТЫ. Стрим пишет msk_minute → "%Y-%m-%dT%H:%M", без секунд.
   Здесь сначала было "...T10:05:00" — и на живой базе 05.08 сверка
   дала 500 минут у ISS, 777 в базе и НОЛЬ общих. Запись не упала бы:
   в базе просто появилась бы вторая сетка минут рядом со стримовой.

ТРИ ПРОВЕРКИ, И ОНИ ОТВЕЧАЮТ НА РАЗНОЕ:

1. `turnover_error` — сверка с value самого ISS. Ловит битые цены и смещённые
   столбцы, но НЕ ЛОВИТ НЕВЕРНУЮ ЛОТНОСТЬ:

       volume = штуки / лот,   _rub = volume × лот × цена = штуки × цена

   Лот сокращается, сумма сойдётся при любом лоте. В первой версии
   этого файла было сказано обратное, и это было неверно.

2. `compare_to_db` — сверка с тем, что СТРИМ записал за тот же день. Вот она
   ловит и единицы, и разный ключ минуты.

3. Тест сверки `_ts` с `msk_minute` — чтобы ключ не разошёлся снова.

А В БАЗУ ИДУТ ТОЛЬКО КЛЮЧИ СТРИМА (`stream_rows`). Служебные поля сверки
нужны ДО записи и только для неё.

СЕТИ И БАЗЫ ЗДЕСЬ НЕТ — только сборка адреса и чистые преобразования.
"""
from typing import Optional

ISS = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
       "{board}/securities/{ticker}/candles.json")

#  Столбцы берутся ПО ИМЕНИ из columns, а не по номеру в строке: порядок
#  у ISS нигде не обещан, а перепутанные value и volume — ровно та же
#  молчаливая ошибка, только хуже.
NEEDED = ("begin", "open", "close", "high", "low", "volume", "value")

#  Ровно то, что кладёт в базу стрим. Больше ничего.
STREAM_KEYS = ("ts", "open", "high", "low", "close", "volume")

#  Длина метки минуты стрима: "2026-08-04T10:05" — шестнадцать символов.
TS_LEN = 16

#  Меньше этого числа общих минут — сравнивать нечего.
MIN_COMMON = 20


def candles_url(ticker: str, day: str, board: str = "TQBR") -> str:
    """Адрес минутных свечей за один день. Без токена и без подписи."""
    return (f"{ISS.format(board=board, ticker=ticker)}"
            f"?iss.meta=off&interval=1&from={day}&till={day}&start=0")


def rows_of(payload: dict) -> list:
    """Словари из ответа ISS. Пустой или калечный ответ — пустой список."""
    block = ((payload or {}).get("candles") or {})
    cols = block.get("columns") or []
    out = []
    for row in block.get("data") or []:
        if not isinstance(row, (list, tuple)) or len(row) != len(cols):
            continue
        out.append(dict(zip(cols, row)))
    return out


def _ts(begin) -> Optional[str]:
    """'2026-08-03 10:05:00' -> '2026-08-03T10:05'.

    СЕКУНД НЕТ СОЗНАТЕЛЬНО. Это не косметика, а ключ строки в базе.
    Стрим пишет msk_minute → "%Y-%m-%dT%H:%M". С секундами дозаливка ложилась
    бы в СОСЕДНИЕ строки, а не в те же: ни ошибки, ни падения, просто
    две параллельные сетки минут на один день.

    Время ISS — московское, как и у стрима после MSK_SHIFT_H, поэтому
    часы здесь не сдвигаются.
    """
    s = str(begin or "")
    if len(s) < 16:
        return None
    return s[:10] + "T" + s[11:16]


def bar_of(row: dict, lot: int = 1) -> Optional[dict]:
    """
    Строка ISS — в бар того же вида, что кладёт стрим.

    volume делится на лотность ИМЕННО ПОТОМУ, что _rub потом умножит его
    на лотность обратно. Остаток не округляется: округление до целых
    лотов у UGLD (лот 1000) теряло бы до тысячи акций на каждой минуте.
    """
    if not isinstance(row, dict):
        return None
    ts = _ts(row.get("begin"))
    if not ts:
        return None
    try:
        close = float(row.get("close") or 0)
        shares = float(row.get("volume") or 0)
    except (TypeError, ValueError):
        return None
    if close <= 0 or shares < 0:
        return None
    lot = max(1, int(lot or 1))

    def num(key, default=close):
        try:
            v = float(row.get(key) or 0)
        except (TypeError, ValueError):
            return default
        return v if v > 0 else default

    try:
        value = float(row.get("value") or 0)
    except (TypeError, ValueError):
        value = 0.0
    return {"ts": ts,
            "open": num("open"),
            "high": num("high"),
            "low": num("low"),
            "close": close,
            "volume": shares / lot,        # ЛОТЫ, как в стриме
            "shares": shares,              # штуки, для сверки
            "value_rub": value,            # рубли самого ISS
            "source": "iss"}


def bars_of(payload: dict, lot: int = 1) -> list:
    """
    Весь ответ — в список баров, по времени, без повторов.

    Повторы реальны: страницы ISS берутся через start=, и края страниц
    легко склеиваются дважды. Два одинаковых бара удвоили бы минуту
    в медиане и сосчитались бы за два бара в пороге MIN_BARS_DAY.
    """
    by_ts = {}
    for row in rows_of(payload):
        bar = bar_of(row, lot)
        if bar:
            by_ts[bar["ts"]] = bar
    return [by_ts[k] for k in sorted(by_ts)]


def by_day(bars: list) -> dict:
    """Бары — в вид day -> rows, то есть ровно вход day_profile."""
    out: dict = {}
    for b in bars or ():
        out.setdefault(str(b.get("ts"))[:10], []).append(b)
    return out


def turnover_error(bars: list, lot: int = 1) -> Optional[float]:
    """
    Насколько наш пересчёт расходится с рублями самого ISS, долей.

    ЧТО ЛОВИТ: битые цены, смещённые столбцы, перепутанные value/volume.
    ЧЕГО НЕ ЛОВИТ: неверную лотность — она сокращается. Для лотности
    и для ключа минуты есть compare_to_db.

    Остаточное расхождение есть всегда и оно законное: value считается по
    цене КАЖДОЙ сделки, а _rub — по закрытию минуты.

    None — если сверять не с чем (ISS не отдал value).
    """
    from src.analysis.volume_events import _rub
    ours = theirs = 0.0
    for b in bars or ():
        v = float(b.get("value_rub") or 0)
        if v <= 0:
            continue
        theirs += v
        ours += _rub(b, lot)
    if theirs <= 0:
        return None
    return abs(ours - theirs) / theirs


def stream_rows(bars: list) -> list:
    """
    Строки для базы: ровно те ключи, что кладёт стрим, и ни одного больше.

    Служебные поля (`shares`, `value_rub`, `source`) нужны только для сверки
    единиц ДО записи. Если отправить их в merge_candle_minutes, дозаливка
    станет зависеть от того, как запись разбирает строку — от того, что
    здесь никто не проверял и что поменять могут без меня.
    """
    out = []
    for b in bars or ():
        if not b or not b.get("ts"):
            continue
        out.append({k: b.get(k) for k in STREAM_KEYS})
    return out


def _median(xs: list) -> Optional[float]:
    xs = sorted(xs)
    if not xs:
        return None
    mid = len(xs) // 2
    if len(xs) % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2


def compare_to_db(iss_bars: list, db_rows: list) -> dict:
    """
    Совпадают ли минутки ISS с тем, что стрим записал сам.

    Два независимых источника об ОДНОЙ минуте. Отношение берётся
    поминутно, итог — МЕДИАНА, а не среднее: один аукционный выброс
    или одна пропущенная стримом минута не должны решать вердикт.

    НОЛЬ ОБЩИХ МИНУТ ПРИ ПОЛНЫХ ДАННЫХ С ОБЕИХ СТОРОН — это не «мало
    данных», а разный формат ключа. Именно так 05.08 нашлись секунды в
    метке ISS против их отсутствия у стрима, поэтому ответ говорит об этом
    прямо, а не прячется за «недостаточно точек».

    Несколько процентов расхождения — норма: стрим мог подняться среди
    дня. Разы — не норма никогда.
    """
    mine = {}
    for b in iss_bars or ():
        ts = str((b or {}).get("ts") or "")
        if ts:
            mine[ts] = float(b.get("volume") or 0)
    live = {}
    for r in db_rows or ():
        ts = str((r or {}).get("ts") or "")
        if ts:
            live[ts] = float(r.get("volume") or 0)

    common = sorted(set(mine) & set(live))
    ratios = [mine[t] / live[t] for t in common if live[t] > 0 and mine[t] > 0]
    med = _median(ratios)

    #  Обе стороны полны, а пересечение пусто — говорить надо об этом,
    #  а не про нехватку точек: причина совсем другая.
    if not common and len(mine) >= MIN_COMMON and len(live) >= MIN_COMMON:
        a = sorted(mine)[0] if mine else ""
        b = sorted(live)[0] if live else ""
        return {"iss_minutes": len(mine), "db_minutes": len(live),
                "common": 0, "compared": 0, "median_ratio": None, "ok": False,
                "verdict": (f"РАЗНЫЙ КЛЮЧ МИНУТЫ: у нас {a!r}, в базе {b!r} — "
                            "запись создаст вторую сетку минут")}

    if med is None or len(ratios) < MIN_COMMON:
        verdict = (f"мало общих минут ({len(ratios)} при нужных {MIN_COMMON}) — "
                   "сравнивать нечего, это не ответ «всё хорошо»")
        ok = None
    elif 0.95 <= med <= 1.05:
        verdict = f"единицы совпадают (медиана {med:.4f})"
        ok = True
    else:
        verdict = (f"РАСХОЖДЕНИЕ в {med:.4g} раз — похоже на лотность, "
                   "запись испортит норму")
        ok = False

    return {"iss_minutes": len(mine),
            "db_minutes": len(live),
            "common": len(common),
            "compared": len(ratios),
            "median_ratio": med,
            "ok": ok,
            "verdict": verdict}
