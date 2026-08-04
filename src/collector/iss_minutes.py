"""
Минутная история с MOEX ISS — чтобы норма по времени суток была СЕГОДНЯ.

ЗАЧЕМ. day_profile требует MIN_DAYS торговых дней по MIN_BARS_DAY баров.
Стрим поднялся 01.08, и в базе один торговый день — сам профиль наберётся
к середине августа. До тех пор сканер сравнивает 10:05 с 09:45, то есть
недооценивает утро (а именно утро дало лучший сетап в бэктесте) и
переоценивает обед. ISS отдаёт прошлые дни бесплатно и без токена.

ГЛАВНАЯ ЛОВУШКА — ЕДИНИЦЫ, и она молчаливая:

    ISS         volume — ШТУКИ (акции), value — РУБЛИ
    стрим      volume — ЛОТЫ, и _rub сам умножает его на лотность

Залить сырые строки ISS значит раздуть оборот в лотность раз: у SBER и
GAZP ×10, у UGLD ×1000. Профиль не упал бы и ошибки не выдал — он бы тихо
врал: норма вдесятеро завышена, и любая живая минута выглядит тишью.
Такие ошибки дороже падений.

ПОЭТОМУ ПЕРЕВОД СВЕРЯЕТСЯ С САМИМ ИСТОЧНИКОМ. В ответе ISS есть готовое
value в рублях, то есть контрольная сумма приезжает вместе с данными.
`turnover_error` сравнивает наш пересчёт с ним и даёт число, а не веру.

А В БАЗУ ИДУТ ТОЛЬКО КЛЮЧИ СТРИМА (`stream_rows`). Служебные поля сверки
нужны ДО записи и только для неё: дозаливка, кладущая лишние ключи,
зависела бы от того, как именно merge_candle_minutes разбирает строку.

СЕТИ И БАЗЫ ЗДЕСЬ НЕТ — только сборка адреса и чистые преобразования,
чтобы всю арифметику можно было проверить тестом без рынка и без ключей.
"""
from typing import Optional

ISS = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
       "{board}/securities/{ticker}/candles.json")

#  Столбцы берутся ПО ИМЕНИ из columns, а не по номеру в строке: порядок
#  у ISS нигде не обещан, а перепутанные value и volume — ровно та же
#  молчаливая ошибка на лотность, только хуже.
NEEDED = ("begin", "open", "close", "high", "low", "volume", "value")

#  Ровно то, что кладёт в базу стрим. Больше ничего.
STREAM_KEYS = ("ts", "open", "high", "low", "close", "volume")


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
    """'2026-08-03 10:05:00' -> '2026-08-03T10:05:00'.

    Время ISS — московское, и в стриме минутные ключи тоже московские
    (MSK_SHIFT_H). Пересчёт часов здесь был бы ошибкой: профиль кладёт
    минуту суток в ключ, и сдвинутый день сравнивал бы 10:05 с 13:05.
    """
    s = str(begin or "")
    if len(s) < 16:
        return None
    return s[:10] + "T" + s[11:19]


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

    Главная защита от ошибки на лотность: перепутаешь единицы — число
    станет порядка 9 (лот 10) или 0.9, а не около нуля.

    Остаточное расхождение есть всегда и оно законное: value считается по
    цене КАЖДОЙ сделки, а _rub — по закрытию минуты. На минуте это
    проценты, а не разы, и именно поэтому проверка работает.

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
