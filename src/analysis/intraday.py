"""
MOODEX — Внутридневная (интрадей) аналитика и торговля волатильностью.

Чистые функции без сети и внешних зависимостей — их легко тестировать.
Рассчитаны на минутные / 5-минутные свечи ОДНОЙ торговой сессии MOEX.

Здесь собрано ядро интрадей-логики:
  • vwap / intraday_atr / candle_anatomy — базовые интрадей-метрики;
  • opening_range + orb_signal — диапазон открытия и пробой (ORB);
  • volatility_state — сжатие → расширение (squeeze breakout);
  • detect_spike + news_whipsaw_plan — новостной вынос и вход «на разрешении»;
  • session_phase / is_last_minutes — фазы сессии MOEX (основная/вечерняя).

Все функции возвращают простые словари/числа, чтобы их удобно было отдавать в
контекст Claude и сохранять в журнал. Не является инвестиционной рекомендацией.
"""
from typing import Optional

# ─── Базовые интрадей-метрики ─────────────────────────────────────────────────

def typical_prices(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """Типичная цена (H+L+C)/3 по каждой свече."""
    n = min(len(highs), len(lows), len(closes))
    return [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]


def vwap(highs: list[float], lows: list[float], closes: list[float],
         volumes: list[float]) -> list[float]:
    """
    Кумулятивный VWAP по свечам сессии (сбрасывается каждый торговый день —
    подавай свечи ОДНОЙ сессии). vwap[i] — средневзвешенная по объёму цена до i.
    Если объёмов нет (нули) — откатывается на типичную цену.
    """
    tp = typical_prices(highs, lows, closes)
    out: list[float] = []
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(len(tp)):
        v = volumes[i] if i < len(volumes) and volumes[i] else 0.0
        cum_pv += tp[i] * v
        cum_v += v
        out.append(round(cum_pv / cum_v, 6) if cum_v > 0 else round(tp[i], 6))
    return out


def true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    """Истинный диапазон по каждой свече (с оглядкой на предыдущий close)."""
    n = min(len(highs), len(lows), len(closes))
    trs: list[float] = []
    for i in range(n):
        if i == 0:
            trs.append(highs[i] - lows[i])
        else:
            trs.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
    return trs


def intraday_atr(highs: list[float], lows: list[float], closes: list[float],
                 period: int = 14) -> Optional[float]:
    """ATR как простое среднее истинных диапазонов за последние `period` свечей."""
    trs = true_ranges(highs, lows, closes)
    if not trs:
        return None
    window = trs[-period:]
    return round(sum(window) / len(window), 6)


def candle_anatomy(o: float, h: float, l: float, c: float) -> dict:
    """
    Анатомия свечи: тело, тени, доля тела в диапазоне.
    Помогает распознать разворотные/индесижн-свечи (длинные тени, малое тело).
    """
    rng = h - l
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    body_pct = (body / rng) if rng > 0 else 0.0
    return {
        "range": round(rng, 6),
        "body": round(body, 6),
        "upper_wick": round(upper, 6),
        "lower_wick": round(lower, 6),
        "body_pct": round(body_pct, 4),
        "direction": "up" if c > o else "down" if c < o else "flat",
        # индесижн: маленькое тело и заметные тени с обеих сторон
        "indecision": body_pct < 0.35 and upper > body and lower > body,
    }


# ─── Диапазон открытия и его пробой (ORB) ─────────────────────────────────────

def _minute_of_day(ts) -> Optional[int]:
    """Минута дня по МСК из отметки свечи. None, если отметку не разобрать."""
    from datetime import datetime, timedelta, timezone as _tz
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    msk = dt.astimezone(_tz(timedelta(hours=3)))
    return msk.hour * 60 + msk.minute


def _msk_date(ts):
    """Календарная дата по МСК из отметки свечи. None, если не разобрать."""
    from datetime import datetime, timedelta, timezone as _tz
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return dt.astimezone(_tz(timedelta(hours=3))).date()


def opening_range(highs: list[float], lows: list[float], bars: int = 6,
                  dates: Optional[list] = None,
                  assume_scoped: bool = False) -> Optional[dict]:
    """
    Диапазон открытия ТЕКУЩЕЙ сессии: хай/лоу первых `bars` её свечей.
    Для 5-мин свечей bars=6 ≈ первые 30 минут.

    ВАЖНО, почему нужны даты. Раньше брались просто highs[:bars] — первые свечи
    ПРИСЛАННОГО ОКНА. Окно интрадей-загрузки 8 часов, поэтому в 10:05 МСК первыми
    свечами оказывались 06:55-07:25 — открытие УТРЕННЕЙ сессии. К основной сессии
    цена почти всегда уходила за этот трёхчасовой давности диапазон, и «пробой»
    срабатывал сразу почти на каждой ликвидной бумаге: 30.07 в 10:07 сразу десять
    тикеров получили одинаковый сетап orb с интересом 0.90-1.00.

    Без дат возвращаем None: молча считать по чужой сессии хуже, чем не считать.
    Пока с открытия сессии не набралось `bars` свечей — диапазона ещё нет,
    и это тоже None (в первые 30 минут ORB не существует).

    assume_scoped=True — вызывающий ЗАЯВЛЯЕТ, что серия уже принадлежит одной
    сессии, и тогда даты не нужны. Флаг явный именно потому, что молчаливое
    допущение «серия уже нарезана» и было причиной ложных пробоев.
    """
    if assume_scoped:
        n = min(len(highs), len(lows))
        if bars <= 0 or n < bars:
            return None
        return {"or_high": round(max(highs[:bars]), 6),
                "or_low": round(min(lows[:bars]), 6), "bars": bars}
    n = min(len(highs), len(lows))
    if bars <= 0 or n < bars:
        return None
    if not dates or len(dates) < n:
        return None

    mins = [_minute_of_day(d) for d in dates[:n]]
    days = [_msk_date(d) for d in dates[:n]]
    if mins[-1] is None or days[-1] is None:
        return None
    s_open = session_open_minute(mins[-1])
    if s_open is None:
        # Вне торгов (аукцион/перерыв/закрыто) — берём сессию последней свечи,
        # которая была торговой, иначе диапазон не определён.
        for m in reversed(mins):
            if m is not None and session_open_minute(m) is not None:
                s_open = session_open_minute(m)
                break
    if s_open is None:
        return None

    # Свечи текущей сессии: та же КАЛЕНДАРНАЯ дата и минута дня не раньше открытия.
    # Дата обязательна: сравнение по одной минуте дня подхватывало бары ВЧЕРАШНЕЙ
    # сессии, если источник прислал окно длиннее суток, и «диапазоном открытия»
    # становилось вчерашнее открытие.
    today = days[-1]
    idx = [i for i, m in enumerate(mins)
           if m is not None and m >= s_open and days[i] == today]
    if len(idx) < bars:
        return None
    first = idx[:bars]
    return {
        "or_high": round(max(highs[i] for i in first), 6),
        "or_low": round(min(lows[i] for i in first), 6),
        "bars": bars,
        "session_open_min": s_open,
        "session_bars": len(idx),
        # Минута дня, когда диапазон СФОРМИРОВАЛСЯ. Нужна, чтобы у сетапа был срок
        # годности: пробой диапазона открытия — техника первого часа, а в 14:04
        # «пробой» диапазона 10:00-10:30 это уже просто «рынок ниже утра».
        "or_end_min": mins[first[-1]],
    }


def orb_signal(price: float, or_high: float, or_low: float, atr: float,
               target_mult: float = 1.5, min_rr: Optional[float] = None) -> dict:
    """
    Пробой диапазона открытия. Стоп — за противоположную границу диапазона,
    цель — на target_mult·ATR от входа. R/R считаем честно.
    """
    if atr is None or atr <= 0:
        return {"signal": "none", "reason": "нет ATR"}
    if min_rr is None:
        try:
            from config.settings import SETUP_MIN_RR as _m
            min_rr = _m
        except Exception:
            min_rr = 1.5
    if price > or_high:
        entry, stop = price, or_low
        target = round(price + target_mult * atr, 6)
        risk = entry - stop
        return _plan("long", entry, stop, target, risk,
                     "пробой диапазона открытия вверх", min_rr=min_rr)
    if price < or_low:
        entry, stop = price, or_high
        target = round(price - target_mult * atr, 6)
        risk = stop - entry
        return _plan("short", entry, stop, target, risk,
                     "пробой диапазона открытия вниз", min_rr=min_rr)
    return {"signal": "none", "reason": "цена внутри диапазона открытия"}


def consolidation_breakout(highs: list[float], lows: list[float], closes: list[float],
                           volumes: list[float], atr: Optional[float],
                           vwap: Optional[float], window: int = 6,
                           max_width_atr: float = 1.2, vol_mult: float = 1.5,
                           max_width_atr_short: Optional[float] = None,
                           target_r: float = 2.0, max_risk_pct: float = 3.0,
                           min_rr: Optional[float] = None,
                           min_width_day_share: float = 0.15,
                           allow: tuple = ("long", "short")) -> dict:
    """Пробой внутридневной консолидации по тренду.

    ЗАЧЕМ. Единственной техникой входа был пробой диапазона открытия, а он живёт
    только первые 90 минут. 30.07 Мечел прошёл 7.1% от минимума дня, и в момент
    правильного входа (13:25, пробой сжатия на объёме 4x) ни один сетап системы не
    срабатывал: диапазон открытия просрочен, новостей нет, а дневная техника
    рекомендовала ШОРТ у верхней границы коридора. Этот сетап закрывает дырку и
    работает весь день, потому что консолидация по определению свежая.

    УСТРОЙСТВО. Стоп — под минимум консолидации, поэтому чем уже сжатие, тем
    меньше риск. Цель задана В РИСКЕ (entry + target_r * risk), а не в ATR: тогда
    R/R равен target_r по построению, и остаётся один содержательный вопрос —
    достигается ли эта цель на практике. С целью в ATR планируемый R/R
    структурно занижался: у победившего входа по Мечелу он выходил 1.05 при
    фактическом ходе 4.03R.

    ПРОВЕРКА. 12 ликвидных бумаг, 10-мин свечи, 18 торговых дней июля 2026,
    издержки 0.05% на круг. Ожидание положительно во ВСЕХ проверенных
    конфигурациях, но устойчиво только при узком сжатии:

        ширина <=2.0 ATR: 365 входов, +0.065R; по половинам -0.098R и +0.176R —
                          знак меняется, ненадёжно;
        ширина <=1.5 ATR: 149 входов, +0.077R; первая половина после издержек
                          уходит в минус;
        ширина <=1.2 ATR:  55 входов, +0.264R; по половинам +0.381R и +0.220R,
                          после издержек +0.291R и +0.149R.

    РАЗБИВКА ПО СЕССИЯМ (6 месяцев, 181 день) — главное, что нашлось. Утренняя
    сессия 07:00-09:50 ведёт себя ПРОТИВОПОЛОЖНО основной:

        утро,     лонг  (шир 1.2):  68 входов, -0.402R
        утро,     ШОРТ  (шир 1.5): 275 входов, +0.302R -> +0.210R после издержек
        основная, лонг  (шир 1.2): 242 входа,  +0.076R -> -0.074R
        основная, шорт  (шир 1.2): 316 входов, -0.089R
        вечер, обе стороны: отрицательно

    Утренний шорт проверен отдельно и держится: половины выборки дали +0.200R и
    +0.204R (почти одинаково), 6 месяцев из 7 положительны, 10 бумаг из 12
    положительны. КЛЮЧЕВОЕ: лучший месяц — январь (+0.600R), когда рынок РОС, то
    есть это не эффект падающего рынка.

    Объяснение механизмом: утренняя сессия тонкая, за ночь накапливаются новости и
    гэпы. Пробой вверх в тонкой ликвидности не находит продолжения — покупателя за
    ним нет; пробой вниз идёт дальше, потому что заявок на покупку меньше.

    Поэтому стороны разрешаются по фазам через allow: параметр говорит, какие
    направления вообще рассматривать в текущей фазе.

    Поэтому по умолчанию 1.2. ЧЕГО ПРОВЕРКА НЕ ДОКАЗЫВАЕТ: выборка 55 входов, один
    режим рынка (июль 2026, рынок рос), 12 бумаг из 48, тест на 10-мин свечах, а
    вживую сетап смотрит 5-мин. Это измеряемая гипотеза, а не установленное
    преимущество — исходы надо накапливать.
    """
    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n < window + 2 or not atr or atr <= 0:
        return {"signal": "none", "reason": "мало данных или нет ATR"}
    price = closes[n - 1]
    seg = slice(n - 1 - window, n - 1)          # бары ДО пробойного
    c_high, c_low = max(highs[seg]), min(lows[seg])
    width_atr = (c_high - c_low) / atr
    # Порог ширины РАЗНЫЙ для сторон. У утреннего шорта устойчивое преимущество на
    # пороге 1.5 (275 входов, половины выборки +0.200R и +0.204R), у лонга лучший
    # из плохих вариантов — 1.2. Держать один порог значило бы либо потерять
    # проверенный шорт, либо впустить непроверенный лонг.
    up_pre = price > c_high
    lim = (max_width_atr if up_pre
           else (max_width_atr_short if max_width_atr_short else max_width_atr))
    if width_atr > lim:
        return {"signal": "none",
                "reason": (f"диапазон {width_atr:.2f}xATR шире предела "
                           f"{lim} — это не сжатие, а коридор"),
                "width_atr": round(width_atr, 2)}

    # ВТОРАЯ мера ширины: доля ДНЕВНОГО диапазона.
    #
    # Одного сравнения с ATR пятиминутки мало. 31.07 сканер выдал сигнал по SBER:
    # сжатие 274.08-274.70, то есть 0.62 руб при недельном ходе бумаги
    # 267.54-277.65 (10.11 руб). Формально 1.53xATR — проходит. По сути это три
    # спокойные минуты внутри широкого боковика, и пробить такое можно двадцать
    # раз за день в обе стороны. Сигнал был выдан владельцу и оказался мусорным:
    # цена вернулась под уровень через пять минут.
    #
    # ПРОВЕРКА порога (шорт-пробой, 12 бумаг, 181 день, издержки 0.05%):
    #     оставляем (>=15%): 2344 входа, -0.074R
    #     отсекаем  (<15%):  3078 входов, -0.203R
    #     разница 0.129R, t=4.17, положительна в 6 месяцах из 7.
    # Фильтр НЕ делает основную сессию прибыльной — он надёжно отделяет заведомо
    # плохие входы от просто плохих. По утреннему шорту выборки на 10-мин свечах
    # не хватило (33 входа), там эффект не измерен.
    day_range = max(highs[:n]) - min(lows[:n])
    width_share = (c_high - c_low) / day_range if day_range > 0 else 0.0
    if width_share < min_width_day_share:
        return {"signal": "none",
                "reason": (f"сжатие {width_share:.0%} дневного диапазона — это "
                           f"пауза между сделками, а не сжатие"),
                "width_atr": round(width_atr, 2),
                "width_day_share": round(width_share, 3)}
    up = price > c_high
    down = price < c_low
    if not (up or down):
        return {"signal": "none", "reason": "цена внутри консолидации",
                "width_atr": round(width_atr, 2)}
    if up and "long" not in allow:
        return {"signal": "none",
                "reason": f"пробой вверх, но лонги в этой фазе выключены ({allow})",
                "width_atr": round(width_atr, 2)}
    if down and "short" not in allow:
        return {"signal": "none",
                "reason": f"пробой вниз, но шорты в этой фазе выключены ({allow})",
                "width_atr": round(width_atr, 2)}

    vols = [v for v in volumes[seg] if v]
    avg_v = sum(vols) / len(vols) if vols else 0
    last_v = volumes[n - 1] or 0
    if not avg_v or last_v < vol_mult * avg_v:
        return {"signal": "none",
                "reason": (f"объём пробоя {last_v / avg_v:.2f}x при требуемых "
                           f"{vol_mult}x — выход без участия" if avg_v
                           else "нет данных по объёму"),
                "width_atr": round(width_atr, 2)}

    # Согласие с VWAP: пробой вверх при цене ниже средней дня — это отскок внутри
    # снижения, а не продолжение. Для шорта зеркально.
    if vwap and up and price < vwap:
        return {"signal": "none",
                "reason": "пробой вверх при цене ниже VWAP — продавец контролирует день"}
    if vwap and down and price > vwap:
        return {"signal": "none",
                "reason": "пробой вниз при цене выше VWAP — покупатель контролирует день"}

    side = "long" if up else "short"
    entry = price
    stop = c_low if up else c_high
    risk = (entry - stop) if up else (stop - entry)
    if risk <= 0:
        return {"signal": "none", "reason": "стоп не с той стороны от входа"}
    if risk / entry * 100 > max_risk_pct:
        return {"signal": "none",
                "reason": f"риск {risk / entry * 100:.2f}% выше предела {max_risk_pct}%"}
    target = round(entry + target_r * risk, 6) if up else round(entry - target_r * risk, 6)
    plan = _plan(side, entry, stop, target, risk,
                 (f"пробой сжатия {c_low}-{c_high} ({width_atr:.2f}xATR) "
                  f"{'вверх' if up else 'вниз'} на объёме {last_v / avg_v:.1f}x"),
                 min_rr=min_rr)
    if plan.get("signal") in ("long", "short"):
        plan["width_atr"] = round(width_atr, 2)
        plan["vol_x"] = round(last_v / avg_v, 2)
        plan["consolidation"] = {"low": round(c_low, 6), "high": round(c_high, 6),
                                 "bars": window}
    return plan


# ─── Сжатие → расширение волатильности (squeeze breakout) ─────────────────────

def volatility_state(highs: list[float], lows: list[float], closes: list[float],
                     period: int = 14, lookback: int = 50,
                     squeeze_pct: float = 0.25, expansion_pct: float = 0.80) -> dict:
    """
    Состояние волатильности через перцентиль текущего ATR за `lookback` свечей.
    - squeeze  (сжатие)  — ATR у минимумов (перцентиль <= squeeze_pct);
    - expansion (расширение) — ATR у максимумов (перцентиль >= expansion_pct);
    - normal — между.
    Сжатие часто предшествует импульсу → торгуем по факту расширения.
    """
    trs = true_ranges(highs, lows, closes)
    if len(trs) < max(period + 1, 5):
        return {"state": "unknown", "atr": None, "atr_rank": None}

    # серия ATR (скользящее среднее TR), затем перцентиль последнего значения
    atr_series = []
    for i in range(period, len(trs) + 1):
        window = trs[i - period:i]
        atr_series.append(sum(window) / len(window))
    atr_series = atr_series[-lookback:]
    cur = atr_series[-1]
    below = sum(1 for x in atr_series if x <= cur)
    rank = below / len(atr_series)

    state = "squeeze" if rank <= squeeze_pct else "expansion" if rank >= expansion_pct else "normal"
    return {"state": state, "atr": round(cur, 6), "atr_rank": round(rank, 3)}


# ─── Новостной вынос: детектор спайка + вход «на разрешении» ───────────────────

def detect_spike(opens: list[float], highs: list[float], lows: list[float],
                 closes: list[float], atr: Optional[float] = None,
                 k: float = 2.5) -> dict:
    """
    Детектор всплеска волатильности на последней свече: её диапазон против ATR.
    spike=True, если диапазон > k·ATR. reversal=True — свеча-индесижн (вынос
    в обе стороны с длинными тенями), типичная для новостного прострела.
    """
    if not highs or not lows or not closes or not opens:
        return {"spike": False, "range_ratio": None, "reversal": False}
    if atr is None:
        atr = intraday_atr(highs, lows, closes)
    if not atr or atr <= 0:
        return {"spike": False, "range_ratio": None, "reversal": False}

    anat = candle_anatomy(opens[-1], highs[-1], lows[-1], closes[-1])
    ratio = anat["range"] / atr
    return {
        "spike": ratio >= k,
        "range_ratio": round(ratio, 2),
        "reversal": anat["indecision"],
        "anatomy": anat,
    }


def classify_event(spike: bool, msg_zscore: Optional[float] = None,
                   has_fresh_news: bool = False) -> dict:
    """
    Классифицировать момент как «новостное событие»:
    спайк волатильности + аномальный объём сообщений и/или свежая новость.
    """
    msg_anomaly = (msg_zscore is not None and msg_zscore >= 2.0)
    is_event = bool(spike and (msg_anomaly or has_fresh_news))
    signals = []
    if spike:
        signals.append("спайк волатильности")
    if msg_anomaly:
        signals.append("аномальный объём сообщений")
    if has_fresh_news:
        signals.append("свежая новость")
    return {"event": is_event, "signals": signals}


def news_whipsaw_plan(event_high: float, event_low: float, price: float,
                      vwap_last: Optional[float], atr: Optional[float],
                      confirm_buffer_atr: float = 0.1) -> dict:
    """
    Вход «на разрешении» новостного выноса. Не ловим сам прострел: ждём выхода
    и удержания за границу пост-новостного диапазона (event_high/event_low),
    подтверждённого положением относительно VWAP.

    long  — price пробил event_high и держится выше VWAP;
    short — price пробил event_low и держится ниже VWAP;
    иначе — ждём (wait).
    Стоп — за противоположную границу выноса; цель — измеренное движение
    (высота выноса) от точки входа.
    """
    if event_high is None or event_low is None or event_high <= event_low:
        return {"signal": "wait", "reason": "нет валидного диапазона выноса"}
    buf = (atr or 0.0) * confirm_buffer_atr
    height = event_high - event_low

    above = price > event_high + buf and (vwap_last is None or price >= vwap_last)
    below = price < event_low - buf and (vwap_last is None or price <= vwap_last)

    if above:
        entry, stop = price, event_low
        target = round(price + height, 6)
        return _plan("long", entry, stop, target, entry - stop,
                     "разрешение выноса вверх (пробой+удержание над VWAP)")
    if below:
        entry, stop = price, event_high
        target = round(price - height, 6)
        return _plan("short", entry, stop, target, stop - entry,
                     "разрешение выноса вниз (пробой+удержание под VWAP)")
    return {"signal": "wait", "reason": "разрешение не подтверждено — наблюдаем"}


# ─── Фазы торговой сессии MOEX (время МСК) ────────────────────────────────────

def _sched() -> dict:
    """Границы сессий из конфига с безопасными значениями по умолчанию."""
    try:
        from config import settings as S
        return {
            "m_open": getattr(S, "SESSION_MORNING_OPEN", 7 * 60),
            "m_close": getattr(S, "SESSION_MORNING_CLOSE", 9 * 60 + 50),
            "open": getattr(S, "SESSION_MAIN_OPEN", 10 * 60),
            "close": getattr(S, "SESSION_MAIN_CLOSE", 18 * 60 + 50),
            "e_open": getattr(S, "SESSION_EVENING_OPEN", 19 * 60),
            "e_close": getattr(S, "SESSION_EVENING_CLOSE", 23 * 60 + 50),
        }
    except Exception:                       # noqa: BLE001 — офлайн-тесты
        return {"m_open": 7 * 60, "m_close": 9 * 60 + 50, "open": 10 * 60,
                "close": 18 * 60 + 50, "e_open": 19 * 60, "e_close": 23 * 60 + 50}


def session_phase(minute_of_day: int, weekday: Optional[int] = None) -> str:
    """
    Фаза сессии MOEX по минуте дня (МСК). Границы берутся из конфига.

    Фазы: morning (07:00-09:50), pre (аукцион открытия), main (10:00-18:50),
    break, evening (19:00-23:50), closed.

    УТРЕННЯЯ СЕССИЯ раньше отсутствовала: 07:00-09:50 попадало в "closed", и
    почти три часа реальных торгов система считала нерабочим временем — входы в
    это время не открывались вообще, хотя ликвидность там рабочая.

    ДЕНЬ НЕДЕЛИ. Функция знала только время суток и в СУББОТУ 01.08 в 12:34
    отвечала "main". От неё зависят шесть мест, включая сторож сетапов —
    выходной считался обычным торговым днём. Совпало это с другой находкой того
    же дня: Tinkoff в выходные отдаёт ДИЛЕРСКИЕ сделки (внутренний рынок
    брокера) при закрытой бирже, а ISS в этот день не дал ни одного минутного
    бара против 500 в пятницу. Вместе получалось, что на котировках брокера в
    субботу можно было выдать сигнал «основной сессии».

    weekday: 0-6 как у datetime.weekday(). None — проверки нет, прежнее
    поведение; так вызывают там, где даты под рукой нет.

    Праздники в будни этим НЕ покрываются: для них нужен торговый календарь
    биржи, которого у нас пока нет.
    """
    if weekday is not None and weekday >= 5:
        return "closed"
    d = _sched()
    if d["m_open"] <= minute_of_day < d["m_close"]:
        return "morning"
    if d["m_close"] <= minute_of_day < d["open"]:
        return "pre"
    if d["open"] <= minute_of_day < d["close"]:
        return "main"
    if d["close"] <= minute_of_day < d["e_open"]:
        return "break"
    if d["e_open"] <= minute_of_day < d["e_close"]:
        return "evening"
    return "closed"


def session_open_minute(minute_of_day: int) -> Optional[int]:
    """Минута открытия ТОЙ сессии, в которой мы находимся. Нужна фильтру «первые
    N минут шума»: он был жёстко привязан к 10:00 и на утреннее открытие в 07:00
    не действовал вообще."""
    d = _sched()
    if d["m_open"] <= minute_of_day < d["m_close"]:
        return d["m_open"]
    if d["open"] <= minute_of_day < d["close"]:
        return d["open"]
    if d["e_open"] <= minute_of_day < d["e_close"]:
        return d["e_open"]
    return None



def trading_day_progress(minute_of_day: int) -> float:
    """Доля ТОРГУЕМЫХ минут дня, которая уже прошла (0..1).

    Считается по фактическому расписанию: утро + основная + вечерняя, БЕЗ
    перерывов и аукционов. Нужна, чтобы сравнивать ТЕМП объёма, а не абсолют:
    незавершённый день против полных дней всегда выглядит пустым.
    """
    d = _sched()
    spans = [(d["m_open"], d["m_close"]), (d["open"], d["close"]),
             (d["e_open"], d["e_close"])]
    total = sum(max(0, b - a) for a, b in spans)
    if total <= 0:
        return 1.0
    done = sum(max(0, min(minute_of_day, b) - a) for a, b in spans)
    return max(0.0, min(1.0, done / total))

def is_last_minutes(minute_of_day: int, buffer_min: int = 10) -> bool:
    """
    True, если до конца текущей сессии осталось <= buffer_min минут — в это
    время новые интрадей-входы обычно не открываем (флэт к закрытию).
    """
    d = _sched()
    # Утренняя сессия тоже имеет конец: перед ним новые входы не открываем.
    for end in (d["m_close"], d["close"], d["e_close"]):
        if 0 <= end - minute_of_day <= buffer_min:
            return True
    return False


def velocity(closes: list, volumes: list, k: int = 6) -> dict:
    """
    Скорость цены и объёма (ROC) за последние k баров — «движение ускоряется?».
    price_roc — % изменения цены за k баров; vol_ratio — объём последних k баров
    относительно предыдущих k (>1 = приток объёма/ускорение). Чистая функция.
    """
    out = {"price_roc": None, "vol_ratio": None}
    if closes and len(closes) >= k + 1 and closes[-k - 1]:
        out["price_roc"] = round((closes[-1] / closes[-k - 1] - 1) * 100, 3)
    if volumes and len(volumes) >= 2 * k:
        recent = volumes[-k:]
        base = volumes[-2 * k:-k]
        ra = sum(recent) / len(recent) if recent else 0
        ba = sum(base) / len(base) if base else 0
        out["vol_ratio"] = round(ra / ba, 2) if ba else None
    return out


# ─── helpers ──────────────────────────────────────────────────────────────────

def _plan(signal: str, entry: float, stop: float, target: float,
          risk: float, reason: str, min_rr: Optional[float] = None) -> dict:
    """Собрать план входа. Отказывает, если геометрия не оправдывает риск.

    R/R считался и раньше, но НИКТО на него не смотрел, поэтому план выдавался
    при любой геометрии. Замер 30.07 в 14:04: из 36 сетапов ORB у 35 R/R был ниже
    1.0, медиана около 0.27 — стоп на границе утреннего диапазона стоял в 1-5% от
    цены, а цель 1.5*ATR была в разы меньше. Худшие: MGNT 0.12, SMLT 0.22 при
    риске 5.1%. Такой план формально корректен и практически безнадёжен.
    """
    reward = abs(target - entry)
    rr = round(reward / risk, 2) if risk and risk > 0 else None
    if min_rr is None:
        # Порога нет по умолчанию НАМЕРЕННО. У новостного плана стоп стоит за всей
        # свечой выноса (около 2.5*ATR), а цель 1.5*ATR, поэтому единый порог
        # обнулил бы новостную ветку целиком — не потому, что она плоха, а потому
        # что у неё неверно задана цель. Это отдельная задача: стоп должен стоять
        # чуть за уровнем пробоя, а не за всем диапазоном события.
        return {
            "signal": signal, "entry": round(entry, 6),
            "stop_loss": round(stop, 6), "take_profit": round(target, 6),
            "risk_reward": rr, "reason": reason,
        }
    if rr is None:
        return {"signal": "none", "reason": "риск не определён"}
    if rr < min_rr:
        return {"signal": "none",
                "reason": f"R/R {rr} ниже минимума {min_rr} — вход не оправдан",
                "risk_reward": rr}
    return {
        "signal": signal,
        "entry": round(entry, 6),
        "stop_loss": round(stop, 6),
        "take_profit": round(target, 6),
        "risk_reward": rr,
        "reason": reason,
    }


# ─── Настроение из стакана и потока сделок («реальные деньги») ─────────────────

def orderbook_sentiment(bid_ask_ratio: Optional[float],
                        buy_pct: Optional[float] = None) -> dict:
    """
    Перевести перекос стакана (bid/ask) и поток сделок (доля покупок) в сигнал
    настроения [-1..1]. Это «настроение реальных денег» — можно подавать в
    агрегатор наравне с настроением из чатов.

    bid_ask_ratio: суммарный объём заявок bid / ask (>1 — доминируют покупатели).
    buy_pct:       доля агрессивных покупок в потоке сделок (0..100), необязательно.
    """
    import math
    parts = []
    if bid_ask_ratio and bid_ask_ratio > 0:
        parts.append(math.tanh(math.log(bid_ask_ratio)))  # r=1→0, растёт/падает симметрично
    if buy_pct is not None:
        parts.append(max(-1.0, min(1.0, (buy_pct - 50.0) / 50.0)))
    if not parts:
        return {"signal": 0.0, "label": "neutral", "score": 0.0}
    signal = max(-1.0, min(1.0, sum(parts) / len(parts)))
    label = "positive" if signal > 0.15 else "negative" if signal < -0.15 else "neutral"
    return {"signal": round(signal, 3), "label": label, "score": round(abs(signal), 3)}


# ─── Профиль объёма и VWAP-полосы (интрадей-уровни) ───────────────────────────

def volume_profile(highs: list[float], lows: list[float], closes: list[float],
                   volumes: list[float], bins: int = 24) -> Optional[dict]:
    """
    Профиль объёма (объём по ценам) по свечам сессии. Объём каждой свечи
    распределяется по ценовым корзинам её диапазона [low, high]. Возвращает:
      poc     — цена корзины с макс. объёмом (Point of Control, «магнит»);
      val/vah — границы зоны стоимости (~70% объёма вокруг POC);
      nodes   — топ высокообъёмных ценовых узлов (уровни притяжения/отбоя).
    Это настоящие торговые уровни: где реально наторговали больше всего.
    """
    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n < 5:
        return None
    lo = min(lows[:n])
    hi = max(highs[:n])
    if hi <= lo or bins < 2:
        return None
    width = (hi - lo) / bins
    buckets = [0.0] * bins

    def _idx(price: float) -> int:
        return min(max(int((price - lo) / width), 0), bins - 1)

    for i in range(n):
        v = volumes[i] or 0
        if v <= 0:
            continue
        b_lo, b_hi = _idx(lows[i]), _idx(highs[i])
        share = v / (b_hi - b_lo + 1)
        for b in range(b_lo, b_hi + 1):
            buckets[b] += share

    total = sum(buckets)
    if total <= 0:
        return None

    def _price(b: int) -> float:
        return round(lo + (b + 0.5) * width, 4)

    poc_b = max(range(bins), key=lambda b: buckets[b])
    # Зона стоимости: расширяемся от POC в сторону большего объёма, пока не 70%
    covered = buckets[poc_b]
    left, right = poc_b - 1, poc_b + 1
    lo_b = hi_b = poc_b
    while covered < 0.7 * total and (left >= 0 or right < bins):
        tl = buckets[left] if left >= 0 else -1.0
        tr = buckets[right] if right < bins else -1.0
        if tr >= tl:
            covered += max(tr, 0.0); hi_b = right; right += 1
        else:
            covered += max(tl, 0.0); lo_b = left; left -= 1

    order = sorted(range(bins), key=lambda b: buckets[b], reverse=True)
    return {
        "poc": _price(poc_b),
        "val": _price(lo_b),
        "vah": _price(hi_b),
        "nodes": [_price(b) for b in order[:3]],
    }


def vwap_bands(highs: list[float], lows: list[float], closes: list[float],
               volumes: list[float], k: float = 1.0) -> Optional[dict]:
    """
    Сессионный VWAP + полосы ±k·σ (σ — объёмно-взвешенное стандартное отклонение
    типичной цены от VWAP). Ориентир «дорого/дёшево» относительно средневзвешенной.
    """
    import math
    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n < 5:
        return None
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    vsum = sum((volumes[i] or 0) for i in range(n))
    if vsum > 0:
        v = sum(tp[i] * (volumes[i] or 0) for i in range(n)) / vsum
        var = sum((volumes[i] or 0) * (tp[i] - v) ** 2 for i in range(n)) / vsum
    else:
        v = sum(tp) / n
        var = sum((x - v) ** 2 for x in tp) / n
    sigma = math.sqrt(var) if var > 0 else 0.0
    return {
        "vwap": round(v, 4),
        "upper": round(v + k * sigma, 4),
        "lower": round(v - k * sigma, 4),
        "sigma": round(sigma, 4),
    }
