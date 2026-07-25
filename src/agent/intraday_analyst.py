"""
MOODEX — Интрадей-аналитик: связывает данные MOEX/Tinkoff с чистой интрадей-
логикой (`src/analysis/intraday.py`) и выдаёт торговый контекст для Claude.

Разделение ответственности (чтобы было тестируемо):
  • aggregate_candles / compute_intraday_context — ЧИСТЫЕ функции (без сети);
  • fetch_intraday / build_intraday_context / realized_price_after — I/O.

Источник данных:
  • если задан TINKOFF_TOKEN — берём РЕАЛТАЙМ-свечи Tinkoff (без задержки);
  • иначе — MOEX ISS (бесплатно, но с задержкой ~15 мин) и помечаем delayed=True.

Не является инвестиционной рекомендацией.
"""
import logging
from datetime import datetime, timezone, timedelta, date
from typing import Optional

from src.analysis import intraday as iv

logger = logging.getLogger(__name__)


# ─── Чистые функции ───────────────────────────────────────────────────────────

def aggregate_candles(c: dict, factor: int) -> dict:
    """
    Схлопнуть свечи в более крупный таймфрейм (напр. 1-мин → 5-мин при factor=5).
    Ожидает dict с параллельными массивами open/high/low/close/volume(/dates).
    Неполная последняя группа тоже агрегируется (частичная свеча).
    """
    if factor <= 1:
        return c
    o, h, l, cl = c.get("open", []), c.get("high", []), c.get("low", []), c.get("close", [])
    v, d = c.get("volume", []), c.get("dates", [])
    n = len(cl)
    out = {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    for i in range(0, n, factor):
        j = min(i + factor, n)
        out["open"].append(o[i] if i < len(o) else cl[i])
        out["high"].append(max(h[i:j]) if h[i:j] else cl[i])
        out["low"].append(min(l[i:j]) if l[i:j] else cl[i])
        out["close"].append(cl[j - 1])
        out["volume"].append(sum(v[i:j]) if v[i:j] else 0)
        out["dates"].append(d[i] if i < len(d) else "")
    return out


def _minute_of_day_msk(iso_or_dt) -> int:
    """Минута дня по МСК (UTC+3) из ISO-строки/datetime. При ошибке — 12:00."""
    try:
        if isinstance(iso_or_dt, datetime):
            dt = iso_or_dt
        else:
            s = str(iso_or_dt).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        msk = dt.astimezone(timezone(timedelta(hours=3)))
        return msk.hour * 60 + msk.minute
    except Exception:
        return 12 * 60


def compute_intraday_context(candles: dict, minute_of_day: int,
                             msg_zscore: Optional[float] = None,
                             has_fresh_news: bool = False,
                             delayed: bool = False,
                             opening_range_bars: int = 6,
                             spike_k: float = 2.5) -> Optional[dict]:
    """
    Собрать интрадей-контекст и выбрать сетап. ЧИСТАЯ функция.

    Логика выбора (приоритет):
      1) новостной вынос → «наблюдение», при подтверждении — вход на разрешении;
      2) пробой диапазона открытия (ORB);
      3) сжатие→расширение (squeeze breakout) как подсказка;
    Плюс режимные ограничения: до открытия/в перерыве/в конце сессии — не входим.
    """
    o = candles.get("open", []); h = candles.get("high", [])
    l = candles.get("low", []);  c = candles.get("close", [])
    v = candles.get("volume", [])
    if len(c) < 5:
        return None

    price = c[-1]
    vw = iv.vwap(h, l, c, v)
    vwap_last = vw[-1] if vw else None
    atr = iv.intraday_atr(h, l, c)
    vol = iv.volatility_state(h, l, c)
    orr = iv.opening_range(h, l, bars=opening_range_bars)
    phase = iv.session_phase(minute_of_day)
    last_min = iv.is_last_minutes(minute_of_day)

    # Ищем последнюю свечу-вынос в недавнем окне (не только самую последнюю):
    # разрешение выноса торгуется на свечах ПОСЛЕ спайка, поэтому нужен его индекс.
    spike_idx = None
    if atr and atr > 0:
        scan = min(len(c), 8)
        for i in range(len(c) - 1, len(c) - 1 - scan, -1):
            if (h[i] - l[i]) / atr >= spike_k:
                spike_idx = i
                break
    spike_found = spike_idx is not None
    event = iv.classify_event(spike_found, msg_zscore, has_fresh_news)

    setup, plan, observe, note = "none", None, False, ""

    # Режимные запреты на новый вход
    if phase in ("pre", "break", "closed"):
        observe, note = True, f"фаза сессии: {phase} — вход не открываем"
    elif last_min:
        observe, note = True, "конец сессии — флэт к закрытию, новых входов нет"

    # 1) Новостной вынос
    if not observe and event["event"] and spike_idx is not None:
        event_high, event_low = h[spike_idx], l[spike_idx]
        if spike_idx >= len(c) - 1:
            # вынос прямо сейчас — разрешения ещё нет, только наблюдаем
            observe, setup, note = True, "news_observe", "новостной вынос только что — наблюдаем разрешение"
        else:
            wp = iv.news_whipsaw_plan(event_high, event_low, price, vwap_last, atr)
            if wp["signal"] in ("long", "short"):
                setup, plan, note = "news_resolution", wp, "разрешение новостного выноса"
            else:
                observe, setup, note = True, "news_observe", "вынос — ждём подтверждения разрешения"

    # 2) Пробой диапазона открытия
    if not observe and setup == "none" and orr:
        ob = iv.orb_signal(price, orr["or_high"], orr["or_low"], atr)
        if ob["signal"] in ("long", "short"):
            setup, plan, note = "orb", ob, ob["reason"]

    # 3) Подсказка по волатильности
    if setup == "none" and vol.get("state") == "expansion":
        note = note or "расширение волатильности из сжатия — следим за импульсом"

    signal = plan["signal"] if plan else ("observe" if observe else "none")

    vwap_rel = None
    if vwap_last:
        vwap_rel = "выше VWAP" if price > vwap_last else "ниже VWAP" if price < vwap_last else "на VWAP"

    summary = _summary_text(price, vwap_last, vwap_rel, atr, vol, phase,
                            setup, plan, observe, note, delayed)

    levels = {
        "vwap": vwap_last,
        "or_high": (orr or {}).get("or_high"),
        "or_low": (orr or {}).get("or_low"),
        "session_high": round(max(h), 6) if h else None,
        "session_low": round(min(l), 6) if l else None,
        "atr": atr,
        "spike_high": round(h[spike_idx], 6) if spike_idx is not None else None,
        "spike_low": round(l[spike_idx], 6) if spike_idx is not None else None,
    }

    return {
        "summary": summary,
        "signal": signal,
        "setup": setup,
        "plan": plan,
        "observe": observe,
        "price": price,
        "vwap": vwap_last,
        "vwap_rel": vwap_rel,
        "atr": atr,
        "volatility_state": vol.get("state"),
        "phase": phase,
        "event": event,
        "delayed": delayed,
        "note": note,
        "levels": levels,
    }


def _summary_text(price, vwap_last, vwap_rel, atr, vol, phase, setup, plan,
                  observe, note, delayed) -> str:
    lines = ["📟 ИНТРАДЕЙ (внутридневной контекст):"]
    if delayed:
        lines.append("  ⚠️ данные MOEX с задержкой ~15 мин (нет реалтайм-фида)")
    lines.append(f"  Цена {price} | {vwap_rel or 'VWAP н/д'} | ATR {atr}")
    lines.append(f"  Волатильность: {vol.get('state')} (ATR-ранг {vol.get('atr_rank')})")
    lines.append(f"  Фаза сессии: {phase}")
    if observe:
        lines.append(f"  Режим: НАБЛЮДЕНИЕ — {note}")
    elif plan:
        lines.append(
            f"  Сетап [{setup}]: {plan['signal'].upper()} @ {plan['entry']}, "
            f"стоп {plan['stop_loss']}, цель {plan['take_profit']}, R/R {plan.get('risk_reward')}"
            + (f" — {note}" if note else "")
        )
    else:
        lines.append(f"  Сетапа нет{(' — ' + note) if note else ''}")
    return "\n".join(lines)


# ─── I/O ──────────────────────────────────────────────────────────────────────

async def fetch_intraday(ticker: str, tf_min: int = 5, hours: int = 8) -> Optional[dict]:
    """
    Интрадей-свечи с пометкой источника/задержки.
    Возвращает dict свечей + ключи "_source" и "_delayed".
    """
    # 1) Реалтайм через Tinkoff (если есть токен)
    try:
        from config.settings import TINKOFF_TOKEN
    except Exception:
        TINKOFF_TOKEN = ""
    if TINKOFF_TOKEN:
        try:
            from src.collector.tinkoff_client import TinkoffClient
            data = await TinkoffClient().get_intraday_candles(ticker, tf_min=tf_min, hours=hours)
            if data and data.get("close"):
                data["_source"], data["_delayed"] = "tinkoff", False
                return data
        except Exception as e:
            logger.debug(f"intraday tinkoff {ticker}: {e}")

    # 2) MOEX ISS (бесплатно, с задержкой ~15 мин). Берём 1-мин и агрегируем до tf.
    try:
        from src.collector.moex_price_collector import MOEXPriceCollector
        base_interval = 1 if tf_min in (1, 5) else (10 if tf_min in (10, 15, 30) else 60)
        raw = await MOEXPriceCollector().get_candles(
            ticker, interval=base_interval,
            from_date=date.today() - timedelta(days=1))
        if not raw:
            return None
        data = {
            "dates": [c.timestamp.isoformat() for c in raw],
            "open": [c.open for c in raw], "high": [c.high for c in raw],
            "low": [c.low for c in raw], "close": [c.close for c in raw],
            "volume": [c.volume for c in raw],
        }
        factor = max(1, tf_min // base_interval)
        data = aggregate_candles(data, factor)
        data["_source"], data["_delayed"] = "moex_iss", True
        return data
    except Exception as e:
        logger.debug(f"intraday moex {ticker}: {e}")
        return None


async def build_intraday_context(ticker: str, tf_min: int = 5,
                                 msg_zscore: Optional[float] = None,
                                 has_fresh_news: bool = False,
                                 opening_range_bars: int = 6) -> Optional[dict]:
    """Скачать интрадей-свечи и посчитать контекст/сетап."""
    data = await fetch_intraday(ticker, tf_min=tf_min)
    if not data or not data.get("close"):
        return None
    last_ts = data["dates"][-1] if data.get("dates") else datetime.now(timezone.utc)
    minute = _minute_of_day_msk(last_ts)
    return compute_intraday_context(
        data, minute, msg_zscore=msg_zscore, has_fresh_news=has_fresh_news,
        delayed=bool(data.get("_delayed")), opening_range_bars=opening_range_bars)


async def realized_price_after(ticker: str, start_iso: str, hours: float) -> Optional[float]:
    """
    Цена (close) примерно через `hours` после старта — для интрадей-оценки
    прогнозов. Берём интрадей-свечи и ищем ближайшую к целевому времени.
    None, если данных нет (тогда оценка откатится на дневной close).
    """
    try:
        start = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    target = start + timedelta(hours=hours)
    data = await fetch_intraday(ticker, tf_min=5, hours=int(hours) + 24)
    if not data or not data.get("close"):
        return None
    best_i, best_dt = None, None
    for i, ts in enumerate(data.get("dates", [])):
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt <= target and (best_dt is None or dt > best_dt):
            best_dt, best_i = dt, i
    if best_i is None:
        return None
    return data["close"][best_i]


def _parse_dt(iso):
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _msk_date(dt: datetime):
    return dt.astimezone(timezone(timedelta(hours=3))).date()


def _session_over(sig_date, now: datetime) -> bool:
    """Сессия МСК-дня сигнала уже закрыта? (наступил след. день ИЛИ ≥23:50 МСК)."""
    now_msk = now.astimezone(timezone(timedelta(hours=3)))
    return (sig_date < now_msk.date()) or (
        sig_date == now_msk.date() and (now_msk.hour * 60 + now_msk.minute) >= (23 * 60 + 50))


async def intraday_outcome(ticker: str, start_iso: str, direction: str,
                           entry: float, stop: float, target: float,
                           now_utc: Optional[datetime] = None) -> Optional[dict]:
    """
    МНОГОНОГОЕ ведение интрадей-сделки по ПУТИ цены (в рамках фикс-риска 1%, без
    расширения начального риска). Оценивается на КАЖДОМ тике; исходы:
      • "stop"      — выбито по начальному стопу −1R (до частичной фиксации);
      • "breakeven" — стоп ушёл в безубыток (+BE_TRIGGER_R достигнут), затем выбит ~0R;
      • "target"    — достигнута цель T1: банкуем PARTIAL_FRAC, остаток трейлим;
      • "session"   — ни цель, ни стоп; сессия закрылась → выход остатка по close;
      • "pending"   — сделка ещё в игре (частично может быть закрыта) → НЕ финализируем.

    Управление (параметры MGMT_* в settings): при +BE_TRIGGER_R стоп→вход; на T1
    фиксируем PARTIAL_FRAC и остаток ведём чандельер-трейлом (peak − TRAIL_ATR×ATR,
    не ниже BE) до конца сессии. Итоговый R — ВЗВЕШЕННЫЙ по ногам выхода.
    Касание — по intrabar high/low; в одном баре стоп имеет приоритет над целью.
    Возвращает dict или None (нет данных → бэкстоп по close в вызывающем коде).
    """
    if not (entry and stop and target) or direction not in ("up", "down"):
        return None
    start = _parse_dt(start_iso)
    if start is None:
        return None
    now = now_utc or datetime.now(timezone.utc)
    data = await fetch_intraday(ticker, tf_min=5, hours=48)
    if not data or not data.get("close"):
        return None
    dates = data.get("dates", [])
    highs, lows, closes = data.get("high", []), data.get("low", []), data.get("close", [])
    sig_date = _msk_date(start)

    # Свечи ПОСЛЕ сигнала, в пределах того же МСК-дня (вся сессия: main+evening).
    path = [i for i, ts in enumerate(dates)
            if (dt := _parse_dt(ts)) is not None and dt >= start and _msk_date(dt) == sig_date]

    R_unit = abs(entry - stop)          # 1R = |вход − стоп| = 1% входа
    if R_unit <= 0:
        return None
    long = direction == "up"

    def r_of(p):                        # реализованный R цены выхода (со знаком)
        return (p - entry) / R_unit if long else (entry - p) / R_unit

    # Параметры управления (с безопасными дефолтами)
    from config import settings as S
    enabled = getattr(S, "MGMT_ENABLED", True)
    be_trig = getattr(S, "MGMT_BE_TRIGGER_R", 1.0)
    part_frac = getattr(S, "MGMT_PARTIAL_FRAC", 0.5) if enabled else 1.0  # выкл → выход всей на T1
    trail_k = getattr(S, "MGMT_TRAIL_ATR", 1.5)
    use_trail = enabled and part_frac < 1.0
    atr = iv.intraday_atr(highs, lows, closes) or R_unit

    # Состояние ведения
    rem = 1.0
    cur_stop = stop           # начинаем с фикс-стопа −1R
    be_done = False
    partial_done = False
    legs: list = []           # (frac, r, price, reason)
    peak = entry              # лучший в нашу сторону экстремум (для трейла)
    mfe_r = 0.0               # max favorable excursion, R
    resolved = False
    outcome = None

    for i in path:
        hi = highs[i] if i < len(highs) else closes[i]
        lo = lows[i] if i < len(lows) else closes[i]
        fav = hi if long else lo          # экстремум в нашу сторону в этом баре
        adv = lo if long else hi          # экстремум против нас
        mfe_r = max(mfe_r, r_of(fav))

        # 1) Неблагоприятно: стоп/трейл (тест по стопу, выставленному ПРОШЛЫМИ барами)
        stop_hit = (adv <= cur_stop) if long else (adv >= cur_stop)
        if stop_hit:
            if not partial_done and abs(cur_stop - stop) < 1e-9:
                outcome = "stop"           # начальный −1R
            elif not partial_done and abs(cur_stop - entry) <= 1e-9:
                outcome = "breakeven"      # BE-стоп до частичной фиксации, ~0R
            else:
                outcome = "target"         # T1 уже сняли, остаток вышел по трейлу
            legs.append((rem, r_of(cur_stop), cur_stop, "stop" if outcome != "target" else "trail"))
            rem = 0.0
            resolved = True
            break

        # 2) Цель T1 (частичная фиксация) — только если ещё не фиксировали
        tgt_hit = (hi >= target) if long else (lo <= target)
        if not partial_done and tgt_hit:
            legs.append((part_frac, r_of(target), target, "target"))
            rem -= part_frac
            partial_done = True
            cur_stop = max(cur_stop, entry) if long else min(cur_stop, entry)  # остаток → BE
            if rem <= 1e-9:                # выходим всей позицией (part_frac=1.0 / выкл mgmt)
                rem = 0.0
                resolved = True
                outcome = "target"
                break
            peak = max(peak, hi) if long else min(peak, lo)
            if use_trail:                 # инициализируем трейл остатка
                trail = (peak - trail_k * atr) if long else (peak + trail_k * atr)
                cur_stop = (max(cur_stop, trail) if long else min(cur_stop, trail))
            continue

        # 3) Триггер безубытка (+BE_TRIGGER_R) — до частичной фиксации
        if enabled and not be_done and not partial_done:
            reached = (hi >= entry + be_trig * R_unit) if long else (lo <= entry - be_trig * R_unit)
            if reached:
                be_done = True
                cur_stop = max(cur_stop, entry) if long else min(cur_stop, entry)

        # 4) Обновляем пик и подтягиваем трейл остатка (для СЛЕДУЮЩЕГО бара)
        peak = max(peak, hi) if long else min(peak, lo)
        if partial_done and use_trail:
            trail = (peak - trail_k * atr) if long else (peak + trail_k * atr)
            trail = max(trail, entry) if long else min(trail, entry)   # не ниже BE
            cur_stop = max(cur_stop, trail) if long else min(cur_stop, trail)

    if not resolved:
        # Не выбито и цель (полностью) не отработала. Финализируем ТОЛЬКО если сессия
        # закрылась; иначе сделка ещё в игре (возможно, частично снята) → pending.
        if not _session_over(sig_date, now):
            return {"outcome": "pending", "bars": len(path),
                    "mfe_r": round(mfe_r, 3), "partial": partial_done}
        if not path:
            return None                    # сессия закрыта, но данных нет → бэкстоп
        last_close = closes[path[-1]]
        legs.append((rem, r_of(last_close), last_close, "session"))
        outcome = "target" if partial_done else "session"
        rem = 0.0

    # Агрегируем ноги: итоговый R взвешенный, цена выхода — средневзвешенная.
    final_r = sum(frac * r for frac, r, _p, _w in legs)
    wsum = sum(frac for frac, _r, _p, _w in legs) or 1.0
    avg_exit = sum(frac * p for frac, _r, p, _w in legs) / wsum
    realized_return = (avg_exit / entry - 1) * 100

    return {
        "outcome": outcome,
        "realized_price": round(avg_exit, 4),
        "realized_return": round(realized_return, 3),
        "realized_r": round(final_r, 3),
        "mfe_r": round(mfe_r, 3),
        "legs": [{"frac": round(f, 3), "r": round(r, 3), "price": round(p, 4), "reason": w}
                 for f, r, p, w in legs],
        "bars": len(path),
        "source": data.get("_source"),
    }
