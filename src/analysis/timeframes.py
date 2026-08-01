"""
Пять таймфреймов рядом: 1м, 3м, 5м, 15м, 30м — и согласны ли они друг с другом.

ЗАЧЕМ. Одна цифра «−0.09% за 3 минуты» не отличает две совершенно разные
ситуации:

    1м вверх, 5м вниз, 15м вниз      разворот только начался, старшие против
    1м вверх, 5м вверх, 15м вверх    все согласны

Расхождение таймфреймов — ФАКТ о данных, проверяемый по числам. Именно его и
не было видно.

ГЛАВНАЯ ТОНКОСТЬ: НЕЗАКРЫТЫЙ БАР. Тридцатиминутный бар на второй минуте и
завершённый — не одно и то же. Его максимум, минимум и закрытие ещё изменятся.
Считать по нему структуру значит сравнивать несравнимое: два полных бара против
одного полного и одного огрызка.

Поэтому структура и направление считаются ТОЛЬКО по ЗАКРЫТЫМ барам, а текущий
отдаётся отдельным полем с пометкой `forming`. Это не мелочь: на минутах ошибка
незаметна, на тридцатиминутках она переворачивает картину.

СТРУКТУРА — ОПИСАНИЕ, А НЕ СОВЕТ. «Выше по максимумам и минимумам» это факт.
Но 31.07 требование «структура вверх» как условие для покупки на откате
измерялось ВРЕДНЫМ: t=-12.57, положительных дней 16%. Наличие поля не означает,
что по нему надо действовать, и метки «сильный тренд» здесь нет.

ПРИЧИННОСТЬ. Всё считается по прошлым и текущим барам, ничего из будущего. Ряд
ожидается по возрастанию времени.
"""
from typing import Optional

STEPS = (1, 3, 5, 15, 30)      # минут в баре
BARS_BACK = 3                  # сколько закрытых бар смотрим на направление
FLAT_PCT = 0.02                # меньше этого движение считается боковиком, %


def _num(row: dict, key: str) -> Optional[float]:
    v = row.get(key)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _minute_of_day(ts: str) -> Optional[int]:
    try:
        return int(ts[11:13]) * 60 + int(ts[14:16])
    except (TypeError, ValueError, IndexError):
        return None


def bars(rows: list, step: int) -> list:
    """
    Собрать минуты в бары шага `step`.

    Последний бар помечается forming, если он ещё набирается: его границы и
    закрытие изменятся. Признак — не «последний в списке», а то, что минут в нём
    меньше шага ИЛИ он содержит самую последнюю минуту ряда. Второе важно: бар
    может быть полным по числу минут, но всё ещё текущим, если тихие минуты в
    нём пропущены.
    """
    if not rows or step < 1:
        return []
    out: list = []
    last_ts = rows[-1].get("ts")
    last_key = None
    mm_last = _minute_of_day(last_ts)
    if mm_last is not None:
        last_key = mm_last // step
    for r in rows:
        mm = _minute_of_day(r.get("ts"))
        hi, lo, cl = _num(r, "high"), _num(r, "low"), _num(r, "close")
        if mm is None or hi is None or lo is None or cl is None:
            continue
        k = mm // step
        if not out or out[-1]["key"] != k:
            out.append({"key": k, "ts": r.get("ts"), "open": _num(r, "open") or cl,
                        "high": hi, "low": lo, "close": cl, "minutes": 1,
                        "volume": r.get("volume") or 0})
        else:
            b = out[-1]
            b["high"] = max(b["high"], hi)
            b["low"] = min(b["low"], lo)
            b["close"] = cl
            b["minutes"] += 1
            b["volume"] = (b["volume"] or 0) + (r.get("volume") or 0)
    for b in out:
        b["complete"] = not (b["key"] == last_key)
    return out


def structure(closed: list) -> dict:
    """
    HH/HL и LH/LL по ЗАКРЫТЫМ барам.

    Higher high и higher low вместе — движение вверх по структуре. Lower high и
    lower low вместе — вниз. Всё остальное смешанное, и это тоже ответ: сжатие
    или разворот выглядят именно так.
    """
    if len(closed) < 2:
        return {}
    a, b = closed[-2], closed[-1]
    hh, hl = b["high"] > a["high"], b["low"] > a["low"]
    lh, ll = b["high"] < a["high"], b["low"] < a["low"]
    out = {"hh": hh, "hl": hl, "lh": lh, "ll": ll}
    if hh and hl:
        out["structure"] = "up"
    elif lh and ll:
        out["structure"] = "down"
    elif hh and ll:
        out["structure"] = "expanding"      # внешний бар: шире предыдущего
    elif lh and hl:
        out["structure"] = "inside"         # внутренний бар: сжатие
    else:
        out["structure"] = "mixed"
    return out


def frame(rows: list, step: int, bars_back: int = BARS_BACK,
          flat_pct: float = FLAT_PCT) -> dict:
    """
    Один таймфрейм: направление, изменение, структура, ускорение.

    Направление берётся по ЗАКРЫТЫМ барам за `bars_back` штук. Боковик — не
    отсутствие ответа, а отдельный ответ: движение меньше порога.

    Ускорение — сравнение последнего ЗАКРЫТОГО хода с предыдущим по модулю.
    Замедление при том же направлении и ускорение — разные состояния, и одна
    цифра изменения их не различает.
    """
    bs = bars(rows, step)
    if not bs:
        return {"step_min": step, "bars": 0}
    closed = [b for b in bs if b["complete"]]
    forming = next((b for b in bs if not b["complete"]), None)
    out = {"step_min": step, "bars": len(bs), "closed_bars": len(closed)}

    if forming:
        out["forming"] = {"ts": forming["ts"], "minutes": forming["minutes"],
                          "close": forming["close"], "high": forming["high"],
                          "low": forming["low"]}

    if len(closed) >= 2:
        out.update(structure(closed))

    if len(closed) >= bars_back + 1:
        a, b = closed[-bars_back - 1]["close"], closed[-1]["close"]
        if a:
            ch = (b - a) / a * 100
            out["change_pct"] = round(ch, 4)
            out["bars_used"] = bars_back
            out["direction"] = ("flat" if abs(ch) < flat_pct
                                else "up" if ch > 0 else "down")
    elif len(closed) >= 2:
        a, b = closed[0]["close"], closed[-1]["close"]
        if a:
            ch = (b - a) / a * 100
            out["change_pct"] = round(ch, 4)
            out["bars_used"] = len(closed) - 1
            out["direction"] = ("flat" if abs(ch) < flat_pct
                                else "up" if ch > 0 else "down")

    # УСКОРЕНИЕ: последний закрытый ход против предыдущего, по модулю.
    if len(closed) >= 3:
        m1 = closed[-1]["close"] - closed[-2]["close"]
        m0 = closed[-2]["close"] - closed[-3]["close"]
        out["move_last"] = round(m1, 6)
        out["move_prev"] = round(m0, 6)
        if abs(m0) > 0:
            out["accel_ratio"] = round(abs(m1) / abs(m0), 3)
            out["pace"] = ("accelerating" if abs(m1) > abs(m0) * 1.2
                           else "decelerating" if abs(m1) < abs(m0) * 0.8
                           else "steady")
        # Смена знака хода — разворот последнего бара, отдельный факт.
        out["turned"] = bool(m1 != 0 and m0 != 0 and (m1 > 0) != (m0 > 0))
    return out


def profile(rows: list, steps: tuple = STEPS, **kw) -> dict:
    """
    Все таймфреймы разом плюс их СОГЛАСИЕ.

    Согласие — то, ради чего всё это. «1м вверх, 5м вниз, 15м вниз» и «все вверх»
    это разные ситуации, а одна цифра изменения их не различает.

    Здесь только подсчёт: сколько таймфреймов вверх, сколько вниз, совпадают ли
    крайние. Что из этого предсказывает движение — не измерено, и вывода вида
    «сигнал на покупку» здесь нет.
    """
    out = {"frames": {}}
    dirs = {}
    for st in steps:
        f = frame(rows, st, **kw)
        out["frames"][f"{st}m"] = f
        if f.get("direction"):
            dirs[f"{st}m"] = f["direction"]
    if not dirs:
        return out
    up = sum(1 for v in dirs.values() if v == "up")
    down = sum(1 for v in dirs.values() if v == "down")
    flat = sum(1 for v in dirs.values() if v == "flat")
    keys = list(dirs)
    out["agreement"] = {
        "up": up, "down": down, "flat": flat, "total": len(dirs),
        "all_agree": len(set(dirs.values())) == 1,
        # Расхождение младшего со старшим — то, что видно в примере Артёма.
        "fastest": dirs[keys[0]], "slowest": dirs[keys[-1]],
        "fast_vs_slow": ("same" if dirs[keys[0]] == dirs[keys[-1]]
                         else "opposite" if {dirs[keys[0]], dirs[keys[-1]]} == {"up", "down"}
                         else "partial"),
        "line": " / ".join(f"{k} {dirs[k]}" for k in keys),
    }
    return out
