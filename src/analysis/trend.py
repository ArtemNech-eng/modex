"""
Направление тренда по таймфреймам: измерение того, что ЕСТЬ.

Важное разделение, которое я весь день 31.07 путал:

    ОПРЕДЕЛИТЬ тренд   — измерение. Надёжно, воспроизводимо.
    ПРЕДСКАЗАТЬ ход    — прогноз. Померено, и в основном не работает.

Этот модуль делает только первое. Он отвечает «куда бумага идёт сейчас», а не
«куда пойдёт». Второе проверялось на 248 днях и 48 бумагах:

    дневной тренд ВНИЗ  -> шорт   +0.149R, t=4.98, батарею прошёл
    дневной тренд ВВЕРХ -> лонг   -0.195R на растущем рынке, не прошёл

То есть направление вниз продолжается, вверх — гасится. Пользоваться выводом
модуля надо с этой поправкой, и она НЕ его часть: он описывает, а не советует.

─── ОШИБКА, РАДИ КОТОРОЙ МОДУЛЬ ВЫНЕСЕН ОТДЕЛЬНО ───

31.07 я выдал владельцу таблицу трендов по 25 бумагам, где у ВСЕХ двадцати пяти
60-минутный тренд был NEUTRAL. Причина: часовые бары брались ВНУТРИ одного дня,
а их там всего десять (10:00-19:00). Для EMA20 плюс проверка структуры нужно
минимум 29 бар. Функция возвращала ноль, а печаталось NEUTRAL — то есть треть
таблицы была молча пустой и выглядела как осмысленный ответ.

Ту же ошибку я перед этим допустил в режимной проверке и не исправил, а
повторил через два часа. Поэтому здесь стоит явная проверка достаточности с
понятной причиной отказа: `insufficient` вместо тихого нуля.

ПРАВИЛО: старший таймфрейм требует истории ЗА НЕСКОЛЬКО ДНЕЙ. Часовому нужно
минимум три дня, лучше пять.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

EMA_N = 20
STRUCT_BARS = 9        # три отрезка по три бара: сравниваем экстремумы
MIN_BARS = EMA_N + STRUCT_BARS     # 29

LABELS = {3: "STRONG LONG", 2: "LONG", 1: "LONG", 0: "NEUTRAL",
          -1: "SHORT", -2: "SHORT", -3: "STRONG SHORT"}

# Сколько торговых дней истории нужно каждому таймфрейму, чтобы набрать MIN_BARS.
# Считано от числа бар в дне: 1м ~ 540, 5м ~ 108, 15м ~ 36, 60м ~ 10, дневной 1.
DAYS_NEEDED = {1: 1, 5: 1, 15: 1, 60: 3, 1440: 29}


def ema_last(values: list, n: int = EMA_N) -> Optional[float]:
    if not values:
        return None
    k = 2 / (n + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def trend_score(closes: list, highs: list, lows: list,
                ema_n: int = EMA_N) -> dict:
    """
    Направление от -3 до +3 по трём независимым признакам.

    Возвращает {"score", "label", "why", "enough"}. Когда бар мало —
    enough=False и явная причина, а НЕ ноль под видом NEUTRAL.
    """
    n = min(len(closes), len(highs), len(lows))
    if n < MIN_BARS:
        return {"score": None, "label": "НЕТ ДАННЫХ", "enough": False,
                "why": [f"бар {n}, нужно {MIN_BARS} — старшему таймфрейму "
                        f"нужна история за несколько дней"]}
    C, H, L = closes[:n], highs[:n], lows[:n]
    score = 0
    why = []
    e = ema_last(C, ema_n)
    if C[-1] > e:
        score += 1
        why.append(f"выше EMA{ema_n}")
    else:
        score -= 1
        why.append(f"ниже EMA{ema_n}")
    a, b, c = n - 9, n - 6, n - 3
    hh = max(H[c:]) > max(H[b:c]) > max(H[a:b])
    hl = min(L[c:]) > min(L[b:c])
    ll = min(L[c:]) < min(L[b:c]) < min(L[a:b])
    lh = max(H[c:]) < max(H[b:c])
    if hh and hl:
        score += 1
        why.append("максимумы и минимумы растут")
    elif ll and lh:
        score -= 1
        why.append("максимумы и минимумы падают")
    else:
        why.append("структура смешанная")
    mom = (C[-1] / C[-6] - 1) * 100 if C[-6] else 0
    if mom > 0.3:
        score += 1
        why.append(f"импульс {mom:+.1f}%")
    elif mom < -0.3:
        score -= 1
        why.append(f"импульс {mom:+.1f}%")
    else:
        why.append(f"импульс {mom:+.1f}% — вялый")
    return {"score": score, "label": LABELS.get(score, "NEUTRAL"),
            "enough": True, "why": why}


def aggregate(bars: list, minutes: int) -> list:
    """
    Склейка минуток в бары по `minutes`, СКВОЗНАЯ через дни.

    Корзина определяется парой (дата, минута дня // minutes) — поэтому бары
    разных дней не сливаются, но и не теряются. Прежняя версия сбрасывала
    буфер по условию на минуту дня и на десятиминутках не срабатывала вовсе.

    bars: [[ts, open, high, low, close, volume], ...], ts вида "YYYY-MM-DD HH:MM:SS"
    """
    out = {}
    order = []
    for r in bars:
        ts = r[0]
        m = int(ts[11:13]) * 60 + int(ts[14:16])
        key = (ts[:10], m // minutes)
        if key not in out:
            out[key] = [ts, r[1], r[2], r[3], r[4], r[5]]
            order.append(key)
        else:
            z = out[key]
            z[2] = max(z[2], r[2])
            z[3] = min(z[3], r[3])
            z[4] = r[4]
            z[5] += r[5]
    return [out[k] for k in order]


def multi_timeframe(minute_bars: list, daily_bars: list = None,
                    timeframes=(15, 60)) -> dict:
    """
    Тренд по нескольким таймфреймам из ОДНОГО набора минуток.

    minute_bars должны покрывать несколько дней — иначе часовой таймфрейм
    получит десять бар и честно ответит «нет данных». Это и есть та проверка,
    отсутствие которой сделало треть таблицы пустой.
    """
    out = {}
    for tf in timeframes:
        agg = aggregate(minute_bars, tf)
        days = len({x[0][:10] for x in agg})
        r = trend_score([x[4] for x in agg], [x[2] for x in agg], [x[3] for x in agg])
        r["bars"] = len(agg)
        r["days"] = days
        need = DAYS_NEEDED.get(tf)
        if not r["enough"] and need:
            r["why"].append(f"для {tf}м нужно ~{need} дн. истории, есть {days}")
        out[f"{tf}m"] = r
    if daily_bars:
        r = trend_score([x[4] for x in daily_bars], [x[2] for x in daily_bars],
                        [x[3] for x in daily_bars])
        r["bars"] = len(daily_bars)
        out["1d"] = r
    return out


def agreement(tf_result: dict) -> str:
    """
    Согласие таймфреймов. Считаются только те, где данных ХВАТИЛО — иначе
    «нет данных» молча превращалось бы в NEUTRAL и портило вывод.
    """
    scores = [v["score"] for v in tf_result.values()
              if v.get("enough") and v.get("score") is not None]
    if len(scores) < 2:
        return "недостаточно таймфреймов"
    if all(s > 0 for s in scores):
        return "ВСЕ ВВЕРХ"
    if all(s < 0 for s in scores):
        return "ВСЕ ВНИЗ"
    return "расходятся"
