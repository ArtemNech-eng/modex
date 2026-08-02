"""
Сканер объёма: пришли ли в движение деньги, и в какой момент они пошли.

ЗАЧЕМ, словами Артёма: «обычно за минуту проходит 5 млн ₽, сейчас 18 млн ₽» и
отдельно — момент, когда объём НАЧАЛ резко расти:

    14:30 — 4 млн     14:31 — 6 млн     14:32 — 11 млн     14:33 — 19 млн

Второе интереснее первого. «Сегодня большой объём» — состояние, а «объём начал
расти четыре минуты назад» — событие с временем.

В РУБЛЯХ, А НЕ В ЛОТАХ. Лот у SBER 1, у UGLD 1000, у GAZP 10; список,
отсортированный по лотам, сравнивал бы несравнимое. Рубли считаются как
лоты × лотность × цена закрытия бара. Это ПРИБЛИЖЕНИЕ: настоящий оборот считают
по цене каждой сделки, а не по закрытию минуты. На минутном баре разница мала,
но называть это точным оборотом нельзя.

ДВЕ НОРМЫ, И ОНИ ОТВЕЧАЮТ НА РАЗНЫЕ ВОПРОСЫ:

    скользящая        медиана последних N закрытых баров. Есть всегда, но она
                      ПОЛЗЁТ: если объём растёт полчаса, норма растёт вместе с
                      ним и всплеск перестаёт быть виден

    по времени суток  медиана этой же минуты за прошлые дни. Именно её и просил
                      Артём: у объёма сильная внутридневная форма — открытие и
                      закрытие тяжёлые, середина дня пустая, и «нормальные»
                      10:05 и 14:30 отличаются в разы

ВТОРОЙ НОРМЫ СЕЙЧАС НЕТ, И ЭТО ВАЖНЕЕ, ЧЕМ КАЖЕТСЯ. Стрим поднялся 01.08, и оба
дня с тех пор — выходные: 583 и 1188 минут ДИЛЕРСКИХ котировок при нуле биржевых
за предыдущие дни. Считать «обычный объём 14:30» по двум выходным значит
выдумать норму. Поэтому механизм готов, но пока дней меньше MIN_DAYS, он молчит
и в выдаче стоит пометка, по какой норме посчитано.

ЧЕГО ЗДЕСЬ НЕТ. Ни «деньги пришли», ни «начало движения» как утверждения. 31.07
одиночный RVOL измерялся ПЛОСКИМ на всех порогах: отбор по «объём выше нормы» не
давал ничего. Связку объёма с ценой никто не мерил. Значит здесь числа и время,
а вывод — за тем, кто смотрит.
"""
from statistics import median
import os
from typing import Optional

from src.analysis.timeframes import bars

STEPS = (1, 5)         # минутка и пятиминутка: на 15м всплеск уже размазан
LOOK = 20              # сколько закрытых баров берётся за скользящую норму
NEED = 6               # меньше этого баров не считаем

#  Во сколько раз оборот должен превысить норму, чтобы считаться ВСПЛЕСКОМ.
#  Догадка. Калибровать по доле сработавших бумаг на живом рынке.
SURGE = 3.0

#  УСКОРЕНИЕ: сколько баров подряд должны расти и во сколько раз каждый.
#
#  Пример Артёма 4 → 6 → 11 → 19 это шаги ×1.5, ×1.8, ×1.7 — три подряд. Два
#  бара подряд бывают постоянно и событием не являются.
GROW_BARS = 3
GROW = 1.4

#  Ускорение обязано быть ещё и ЗНАЧИМЫМ. Ряд 1 → 1.4 → 2 → 2.8 лотов формально
#  ускоряется втрое, но это ничто. Последний бар должен превышать норму хотя бы
#  во столько раз.
GROW_MIN_MULT = 2.0

#  Меньше стольких ТОРГОВЫХ дней норма по времени суток не строится.
#
#  Не «мало данных, но посчитаем»: две выходных с дилерскими котировками дали бы
#  норму, которая не имеет отношения к бирже. Пустой ответ честнее выдуманного.
# НИЖНЯЯ ГРАНИЦА ОБОРОТА — единственный порог здесь, взятый не из самой бумаги.
#
# Всё остальное в этом файле относительное, и это правильно: «много» у SBER и у
# UGLD разные числа. Но у относительного теста есть край, за которым он
# вырождается, и 02.08 он был виден на экране целиком:
#
#   EUTR ×99.7 — самое громкое событие доски. За ним 39 469 ₽.
#   разгон 44 → 352 → 3388 → 39777 — начинается с СОРОКА ЧЕТЫРЁХ рублей.
#   все 38 событий      оборот меньше 5 млн ₽
#   23 из 38            оборот меньше 100 тыс ₽
#
# Это та же яма, что дала 30 «крупных» сделок из 30 и «всё резко» на плоском
# ряде: когда норма ничтожна, кратность к ней ничего не значит. `_baseline`
# выбрасывает бары с НУЛЁМ, но против бара в 396 ₽ бессилен.
#
# Порог взят из размера позиции Артёма (150–250 тыс ₽), а не выбран круглым.
# Минута, в которой прошло меньше его позиции, — минута, где он был бы ВСЕЙ
# ликвидностью. Такое «пришли деньги» неисполнимо, во сколько бы раз оно ни
# превышало норму. Замер по 50 бумагам наблюдения: средняя минута p10 182 тыс ₽,
# медиана 914 тыс ₽ — то есть порог режет мёртвые минуты, а не бумаги.
#
# Умножается на длину шага: у пятиминутки оборот вчетверо больше по построению,
# и плоский порог сделал бы её вчетверо снисходительнее.
FLOOR_RUB = float(os.getenv("VOLUME_FLOOR_RUB", "200000"))

MIN_DAYS = 10


def _rub(bar: dict, lot: int) -> float:
    """
    Оборот бара в рублях: лоты × лотность × закрытие.

    Приближение. Настоящий оборот считается по цене КАЖДОЙ сделки; на минуте
    разница мала, но точным это называть нельзя.
    """
    try:
        v = float(bar.get("volume") or 0)
        c = float(bar.get("close") or 0)
    except (TypeError, ValueError):
        return 0.0
    return v * max(1, int(lot or 1)) * c


def _baseline(vals: list) -> Optional[float]:
    """
    Норма — медиана НЕНУЛЕВЫХ значений.

    Нули это минуты без сделок. Оставить их значило бы утянуть норму вниз и
    объявить всплеском любую обычную минуту — та же ловушка, что дала тридцать
    «крупных» сделок из тридцати.
    """
    vals = [v for v in vals if v > 0]
    if not vals:
        return None
    m = median(vals)
    return m if m > 0 else None


def _ev(kind: str, why: str, step: int, bar: dict, **nums) -> dict:
    """Событие. Только описание: что, когда и с какими числами."""
    out = {"kind": kind, "why": why, "step_min": step, "ts": bar.get("ts")}
    out.update({k: v for k, v in nums.items() if v is not None})
    return out


def _minute_of_day(ts) -> Optional[int]:
    try:
        return int(ts[11:13]) * 60 + int(ts[14:16])
    except (TypeError, ValueError, IndexError):
        return None


def day_profile(rows_by_day: dict, lot: int = 1,
                min_days: int = MIN_DAYS) -> dict:
    """
    Норма ОБОРОТА по минутам суток: медиана этой минуты за прошлые дни.

    `rows_by_day` — день -> список минутных баров. Дни ожидаются ПРОШЛЫЕ:
    включать сегодняшний нельзя, иначе минута сравнивалась бы сама с собой.

    Пока дней меньше `min_days`, возвращается пусто. Это не осторожность ради
    осторожности: 02.08 в базе было два дня, оба выходные, оба с дилерскими
    котировками — норма по ним не имела бы отношения к бирже.
    """
    if len(rows_by_day or {}) < min_days:
        return {}
    per: dict = {}
    for _day, rows in rows_by_day.items():
        for r in rows or ():
            mm = _minute_of_day(r.get("ts"))
            if mm is None:
                continue
            v = _rub(r, lot)
            if v > 0:
                per.setdefault(mm, []).append(v)
    return {mm: median(v) for mm, v in per.items() if v}


def detect_step(rows: list, step: int, lot: int = 1,
                profile: Optional[dict] = None,
                p: Optional[dict] = None) -> list:
    """
    События объёма одного шага. Считается ТОЛЬКО по закрытым барам.

    Незакрытый бар отбрасывается: его объём ЧАСТИЧНЫЙ, пятиминутка на первой
    минуте набрала пятую часть. Сравнение такого бара с нормой всегда даёт
    «объём низкий» просто потому, что бар не дожил.
    """
    p = {**DEFAULTS, **(p or {})}
    bs = bars(rows, step)
    closed = [b for b in bs if b.get("complete")]
    if len(closed) < NEED:
        return []
    vals = [_rub(b, lot) for b in closed]
    last = closed[-1]
    now = vals[-1]
    if now <= 0:
        return []
    # Ниже собственной позиции сравнивать не с чем: он был бы всей минутой.
    if now < p["floor"] * step:
        return []

    # СКОЛЬЗЯЩАЯ НОРМА: по прошлым барам, измеряемый в неё не входит.
    roll = _baseline(vals[:-1][-p["look"]:])
    # НОРМА ПО ВРЕМЕНИ СУТОК, если история набралась.
    tod = None
    if profile:
        mm = _minute_of_day(last.get("ts"))
        if mm is not None:
            # У пятиминутки норма — сумма её минут, а не норма одной минуты.
            got = [profile.get(mm - i) for i in range(step)]
            got = [g for g in got if g]
            if got:
                tod = sum(got)

    base = tod if tod else roll
    if not base:
        return []
    out = []
    mult = now / base
    source = "время суток" if tod else "скользящая"

    # 1. НЕОБЫЧНЫЙ ОБЪЁМ.
    if mult >= p["surge"]:
        out.append(_ev(
            "volume_surge",
            f"оборот в {mult:.1f} раза выше нормы ({source})",
            step, last, rub=round(now), base_rub=round(base),
            times=round(mult, 2), base_source=source))

    # 2. УСКОРЕНИЕ ОБЪЁМА — момент, когда он ПОШЁЛ, а не «сегодня много».
    #
    #    Требуется и то, и другое: подряд растущие бары И значимость. Ряд
    #    1 → 1.4 → 2 → 2.8 формально ускоряется, но это ничто.
    gb = p["grow_bars"]
    if len(vals) >= gb + 1 and mult >= p["grow_min_mult"]:
        tail = vals[-gb - 1:]
        steps_ok = all(tail[i + 1] >= tail[i] * p["grow"] and tail[i] > 0
                       for i in range(len(tail) - 1))
        if steps_ok:
            out.append(_ev(
                "volume_accelerating",
                f"{gb} бара подряд оборот растёт, сейчас в {mult:.1f} раза выше нормы",
                step, last, rub=round(now), base_rub=round(base),
                times=round(mult, 2), bars_growing=gb,
                series=[round(v) for v in tail], base_source=source))
    return out


DEFAULTS = {"look": LOOK, "surge": SURGE, "grow_bars": GROW_BARS,
            "grow": GROW, "grow_min_mult": GROW_MIN_MULT,
            "floor": FLOOR_RUB}


def detect(rows: list, lot: int = 1, profile: Optional[dict] = None,
           steps: tuple = STEPS, p: Optional[dict] = None) -> list:
    out = []
    for st in steps:
        out.extend(detect_step(rows, st, lot=lot, profile=profile, p=p))
    return out


def scan(minutes: dict, lots: Optional[dict] = None,
         profiles: Optional[dict] = None, steps: tuple = STEPS,
         p: Optional[dict] = None) -> list:
    """
    Пройти по всем бумагам и вернуть тех, у кого объём необычен.

    Порядок — по КРАТНОСТИ к норме, а не по рублям: миллиард у SBER это обычный
    день, а сто миллионов у DATA — событие. Сортировка по рублям превратила бы
    список в рейтинг ликвидности, который и так известен.
    """
    out = []
    for tk, rows in (minutes or {}).items():
        evs = detect(list(rows or ()), lot=(lots or {}).get(tk) or 1,
                     profile=(profiles or {}).get(tk), steps=steps, p=p)
        if evs:
            top = max(e.get("times", 0) for e in evs)
            out.append({"ticker": tk, "events": evs, "count": len(evs),
                        "max_times": round(top, 2),
                        "kinds": sorted({e["kind"] for e in evs})})
    out.sort(key=lambda x: (-x["max_times"], x["ticker"]))
    return out


def below_floor(minutes: dict, lots: Optional[dict] = None,
                p: Optional[dict] = None) -> int:
    """
    Сколько бумаг тише порога прямо сейчас.

    Отсев обязан быть ВИДЕН. Иначе пустая таблица читается как «на рынке
    спокойно», хотя на деле это «мы всё выбросили» — ровно тот вид молчания,
    из-за которого сканер полчаса после деплоя показывал ноль событий, а я
    решил, что событий нет.
    """
    p = {**DEFAULTS, **(p or {})}
    n = 0
    for tk, rows in (minutes or {}).items():
        bs = [b for b in bars(list(rows or ()), 1) if b.get("complete")]
        if not bs:
            continue
        if _rub(bs[-1], (lots or {}).get(tk) or 1) < p["floor"]:
            n += 1
    return n


def rates(scanned: list, total: int) -> dict:
    """
    У какой доли бумаг сработал каждый вид.

    Вид, срабатывающий почти везде, не отмечает ничего. Доля — измерение, порог
    — догадка; калибруют по первому.
    """
    if not total:
        return {}
    per: dict = {}
    for row in scanned:
        for k in row["kinds"]:
            per[k] = per.get(k, 0) + 1
    return {k: {"tickers": n, "share": round(n / total, 3)}
            for k, n in sorted(per.items(), key=lambda kv: -kv[1])}
