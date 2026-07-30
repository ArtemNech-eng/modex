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
import os
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


# Окно причинности новости относительно свечи выноса (минуты). Новость может
# заметно опережать движение (рынок переваривает) и может слегка отставать: у RSS
# есть задержка публикации, а лента иногда двигается раньше заголовка.
try:
    from config.settings import (NEWS_BEFORE_SPIKE_MIN as _NEWS_BEFORE_SPIKE_MIN,
                                 NEWS_AFTER_SPIKE_MIN as _NEWS_AFTER_SPIKE_MIN)
except Exception:                                   # noqa: BLE001
    _NEWS_BEFORE_SPIKE_MIN, _NEWS_AFTER_SPIKE_MIN = 30.0, 10.0


def _to_dt(ts):
    """Отметка времени -> datetime в UTC. None, если не разобрать."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_intraday_context(candles: dict, minute_of_day: int,
                             msg_zscore: Optional[float] = None,
                             has_fresh_news: bool = False,
                             news_ts: Optional[list] = None,
                             delayed: bool = False,
                             source: Optional[str] = None,
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
    vel = iv.velocity(c, v)   # скорость цены/объёма (ROC): движение ускоряется?
    # даты обязательны: без них диапазон открытия считался бы по первым свечам
    # окна загрузки, то есть по утренней сессии вместо текущей
    orr = iv.opening_range(h, l, bars=opening_range_bars,
                           dates=candles.get("dates"))
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

    # ПРИЧИННОСТЬ НОВОСТИ. Раньше has_fresh_news не передавался ниоткуда и всегда
    # был False, то есть настоящие новости до детектора не доходили вовсе. Но
    # просто «была ли новость за последний час» — плохой признак: заголовок,
    # вышедший через сорок минут ПОСЛЕ выноса, не объясняет вынос.
    #
    # Считаем новость объясняющей, если она опубликована в окне вокруг свечи
    # выноса: заметно раньше (рынок переваривает) или чуть позже (лента успела
    # опередить заголовок — у RSS есть задержка публикации).
    news_near = None
    if news_ts and spike_idx is not None:
        spike_dt = _to_dt((candles.get("dates") or [None] * len(c))[spike_idx])
        if spike_dt is not None:
            best = None
            for t in news_ts:
                dt = _to_dt(t)
                if dt is None:
                    continue
                lag = (spike_dt - dt).total_seconds() / 60.0   # >0 — новость раньше
                if -_NEWS_AFTER_SPIKE_MIN <= lag <= _NEWS_BEFORE_SPIKE_MIN:
                    if best is None or abs(lag) < abs(best):
                        best = round(lag, 1)
            news_near = best
    has_news_effective = bool(has_fresh_news or news_near is not None)
    event = iv.classify_event(spike_found, msg_zscore, has_news_effective)

    setup, plan, observe, note = "none", None, False, ""

    # Режимные запреты на новый вход.
    # УТРЕННЯЯ сессия (07:00-09:50) раньше попадала в фазу closed и входы в ней
    # не открывались вообще — три часа реальных торгов пропадали. Теперь это
    # отдельная фаза, и разрешение настраивается.
    try:
        from config import settings as _S
        _allow_morning = getattr(_S, "SESSION_ALLOW_MORNING_ENTRY", True)
        _low_liq_after = getattr(_S, "SESSION_LOW_LIQUIDITY_AFTER", 22 * 60)
    except Exception:                       # noqa: BLE001
        _allow_morning, _low_liq_after = True, 22 * 60

    if phase in ("pre", "break", "closed"):
        observe, note = True, f"фаза сессии: {phase} — вход не открываем"
    elif phase == "morning" and not _allow_morning:
        observe, note = True, "утренняя сессия — входы отключены настройкой"
    elif minute_of_day >= _low_liq_after:
        # Полагаться на часы нельзя, но поздний вечер систематически тоньше:
        # фактическую торгуемость всё равно проверяет гейт ликвидности по спреду
        # и глубине стакана, а это отсечка по расписанию.
        # ВНИМАНИЕ: не h/m — h выше это список high, затирание ломало max(h) ниже
        _hh, _mm = divmod(_low_liq_after, 60)
        observe, note = True, (f"после {_hh:02d}:{_mm:02d} ликвидность падает — "
                               "новых входов не открываем")
    elif last_min:
        observe, note = True, "конец сессии — флэт к закрытию, новых входов нет"

    # 1) Новостной вынос
    if not observe and event["event"] and spike_idx is not None:
        event_high, event_low = h[spike_idx], l[spike_idx]
        # Чем именно момент признан новостным — в текст, а не только в поля.
        # Новостная ветка имеет ПРИОРИТЕТ над пробоем диапазона, поэтому основание
        # должно читаться сразу: иначе непонятно, почему сетап выбран этот.
        if news_near is not None:
            _basis = (f"новость за {news_near:.0f} мин до выноса"
                      if news_near >= 0 else
                      f"новость через {abs(news_near):.0f} мин после выноса")
        elif has_fresh_news:
            _basis = "новость передана извне"
        else:
            _basis = "аномальный объём сообщений"
        if spike_idx >= len(c) - 1:
            # вынос прямо сейчас — разрешения ещё нет, только наблюдаем
            observe, setup, note = (True, "news_observe",
                f"новостной вынос только что ({_basis}) — наблюдаем разрешение")
        else:
            wp = iv.news_whipsaw_plan(event_high, event_low, price, vwap_last, atr)
            if wp["signal"] in ("long", "short"):
                setup, plan, note = ("news_resolution", wp,
                                     f"разрешение новостного выноса ({_basis})")
            else:
                observe, setup, note = (True, "news_observe",
                    f"вынос ({_basis}) — ждём подтверждения разрешения")

    # 2) Пробой диапазона открытия
    #
    # СРОК ГОДНОСТИ. Это техника первого часа: диапазон 10:00-10:30 актуален, пока
    # он свежий. Замер 30.07 в 14:04 — через три с половиной часа после
    # формирования — дал ДЕСЯТЬ шортов при рынке, выросшем на 0.89% (32 бумаги
    # вверх, 15 вниз). Среди них шорт по DIAS, прибавившему 2.54% и стоявшему ВЫШЕ
    # VWAP: «пробой вниз» означал лишь то, что цена ниже утреннего диапазона.
    #
    # СОГЛАСИЕ С VWAP. Пробой вниз при цене выше VWAP — противоречие внутри одного
    # среза: покупатель контролирует день, а сетап предлагает продавать.
    orb_block = None
    if not observe and setup == "none" and orr:
        try:
            from config.settings import ORB_VALID_MIN as _orb_valid
        except Exception:
            _orb_valid = 90
        age_min = None
        if orr.get("or_end_min") is not None:
            age_min = minute_of_day - orr["or_end_min"]
        if age_min is not None and age_min > _orb_valid:
            orb_block = (f"диапазон открытия сформирован {age_min} мин назад "
                         f"(предел {_orb_valid}) — пробой уже не сетап")
        else:
            ob = iv.orb_signal(price, orr["or_high"], orr["or_low"], atr)
            if ob["signal"] in ("long", "short"):
                # согласие с VWAP
                if vwap_last:
                    if ob["signal"] == "short" and price > vwap_last:
                        orb_block = ("пробой вниз при цене выше VWAP — "
                                     "покупатель контролирует день")
                    elif ob["signal"] == "long" and price < vwap_last:
                        orb_block = ("пробой вверх при цене ниже VWAP — "
                                     "продавец контролирует день")
                if orb_block is None:
                    setup, plan, note = "orb", ob, ob["reason"]
            elif ob.get("risk_reward") is not None:
                orb_block = ob["reason"]        # отказ по R/R — тоже причина
    if orb_block and setup == "none":
        note = note or orb_block

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
        "velocity": vel,
        "phase": phase,
        "event": event,
        # Основание, по которому момент назван новостным. Голого флага мало:
        # решение надо уметь проверить постфактум, поэтому отдаём и запас по
        # времени между новостью и выносом, и сколько новостей нашлось.
        # Почему сетап ORB не выдан, если диапазон есть: срок годности, R/R или
        # несогласие с VWAP. Молчаливый отказ не отличить от «нечего торговать».
        "orb_blocked": orb_block,
        "news_lag_min": news_near,
        "news_count": len(news_ts or []),
        "delayed": delayed,
        # Какой источник дал свечи. Флаг задержки был, а ИМЕНИ источника
        # не было — значит нельзя было понять, почему данные запоздали и по
        # каким бумагам это систематически.
        "source": source,
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

def _msk_today():
    """Сегодняшняя торговая дата по Москве."""
    return (datetime.now(timezone.utc) + timedelta(hours=3)).date()


def _last_bar_age_min(dates: list) -> Optional[float]:
    """Возраст последней свечи в минутах по стенным часам. None — не разобрать."""
    if not dates:
        return None
    try:
        dt = datetime.fromisoformat(str(dates[-1]).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


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
        # Только СЕГОДНЯ. Раньше стояло from_date=вчера: ISS отдаёт ответ порциями
        # (~500 строк), поэтому минутный запрос за двое суток обрывался внутри
        # вчерашнего дня и до сегодня не доходил вообще. 30.07 в 10:20 по OZON
        # приходила вся серия за 29.07, помеченная как свежая, и по вчерашней
        # сессии строился сетап против сегодняшней цены.
        raw = await MOEXPriceCollector().get_candles(
            ticker, interval=base_interval, from_date=_msk_today())
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
                                 opening_range_bars: int = 6,
                                 reference_price: Optional[float] = None) -> Optional[dict]:
    """Скачать интрадей-свечи и посчитать контекст/сетап."""
    data = await fetch_intraday(ticker, tf_min=tf_min)
    if not data or not data.get("close"):
        return None
    last_ts = data["dates"][-1] if data.get("dates") else datetime.now(timezone.utc)
    minute = _minute_of_day_msk(last_ts)
    # СВЕЖИЕ НОВОСТИ по бумаге. Раньше has_fresh_news не передавался ниоткуда и
    # всегда был False: вход в детектор существовал, но настоящие новости до него
    # не доходили. Читаем из базы знаний, потому что коллектор и API — разные
    # процессы. Окно берём с запасом: причинность проверяется внутри относительно
    # свечи выноса, а не по факту «была новость за час».
    news_items, news_ts = [], []
    try:
        from src import db as _db
        window = int(_NEWS_BEFORE_SPIKE_MIN + 60)
        news_items = await _db.fresh_news(ticker, since_minutes=window, limit=20)
        news_ts = [n.get("ts") for n in news_items if n.get("ts")]
    except Exception as e:                          # noqa: BLE001
        logger.debug(f"fresh_news {ticker}: {e}")

    ctx = compute_intraday_context(
        data, minute, msg_zscore=msg_zscore, has_fresh_news=has_fresh_news,
        news_ts=news_ts,
        delayed=bool(data.get("_delayed")), source=data.get("_source"),
        opening_range_bars=opening_range_bars)
    if not ctx:
        return ctx
    # Заголовки — чтобы основание решения читалось человеком, а не только кодом.
    if news_items:
        ctx["news_titles"] = [str(n.get("text") or "")[:120] for n in news_items[:3]]
        ctx["news_sources"] = sorted({str(n.get("channel") or n.get("source"))
                                      for n in news_items})[:4]

    # ЗАСЛОН ПО СВЕЖЕСТИ. Источник может молча отдать старую серию: 30.07 по
    # пятнадцати тикерам приходили свечи за 29.07 с пометкой «задержка ~15 мин»,
    # и система строила по ним сетапы против сегодняшней цены. Возраст считаем
    # по стенным часам, а не по данным — данные о своей несвежести не сообщат.
    try:
        from config.settings import INTRADAY_MAX_AGE_MIN as _max_age
    except Exception:
        _max_age = 40
    age = _last_bar_age_min(data.get("dates") or [])
    ctx["age_min"] = None if age is None else round(age, 1)
    ctx["stale"] = False
    if age is not None and age > _max_age:
        ctx["stale"] = True
        ctx["observe"], ctx["setup"], ctx["signal"], ctx["plan"] = True, "none", "observe", None
        h_, m_ = divmod(int(age), 60)
        ctx["note"] = (f"свечи устарели на {h_}ч {m_:02d}мин — сетапы не строим "
                       f"(источник {data.get('_source')})")
        ctx["summary"] = ctx["note"]
        return ctx

    # СВЕРКА ПРИНАДЛЕЖНОСТИ СЕРИИ. Reference — цена из независимого источника
    # (дневные свечи ISS). Если интрадей-серия расходится с ней в разы, значит
    # пришли данные ДРУГОГО инструмента.
    #
    # 30.07 так и было: рукописная таблица FIGI указывала 22 тикера на чужие
    # инструменты, и MAGN получал свечи UPRO (диапазон 1.10-1.12 при цене 20.89),
    # HYDR — свечи FEES (0.052 при цене 0.323). Ни один слой этого не замечал,
    # потому что каждый по отдельности выглядел непротиворечиво.
    if reference_price and reference_price > 0:
        last = (ctx.get("levels") or {}).get("vwap") or ctx.get("price")
        try:
            from config.settings import INTRADAY_PRICE_MISMATCH_PCT as _lim
        except Exception:
            _lim = 15.0
        if last and last > 0:
            diff = abs(last - reference_price) / reference_price * 100
            ctx["price_mismatch_pct"] = round(diff, 2)
            if diff > _lim:
                ctx["mismatch"] = True
                ctx["observe"], ctx["setup"] = True, "none"
                ctx["signal"], ctx["plan"] = "observe", None
                ctx["note"] = (f"интрадей-цена {last} против {reference_price} по дневным "
                               f"данным — расхождение {diff:.0f}%; похоже на свечи другого "
                               f"инструмента, сетапы не строим")
                ctx["summary"] = ctx["note"]
                logger.warning(f"{ticker}: интрадей-серия не сходится с дневной ценой "
                               f"({last} vs {reference_price}, {diff:.0f}%)")
    return ctx


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
                           now_utc: Optional[datetime] = None,
                           horizon_hours: Optional[float] = None) -> Optional[dict]:
    """
    МНОГОНОГОЕ ведение интрадей-сделки по ПУТИ цены (в рамках фикс-риска 1%, без
    расширения начального риска). Оценивается на КАЖДОМ тике; исходы:
      • "stop"      — выбито по начальному стопу −1R (до частичной фиксации);
      • "breakeven" — стоп ушёл в безубыток (+BE_TRIGGER_R достигнут), затем выбит ~0R;
      • "target"    — достигнута цель T1: банкуем PARTIAL_FRAC, остаток трейлим;
      • "session"   — ни цель, ни стоп; окно истекло → выход остатка по close;
      • "pending"   — сделка ещё в игре (частично может быть закрыта) → НЕ финализируем.

    ОКНО ОЦЕНКИ. Если задан `horizon_hours`, путь цены идёт от сигнала до
    истечения горизонта и МОЖЕТ пересекать сессии. Раньше окно жёстко
    ограничивалось МСК-сутками сигнала, из-за чего сценарий, выданный вечером на
    следующую сессию, не мог быть оценён вообще: до конца вечерней сессии цель и
    стоп обычно не трогают, оценщик возвращал pending, и сделка уходила в
    легаси-бэкстоп без расчёта R. Для советнического режима это делало метрику в
    R недостижимой. Без `horizon_hours` поведение прежнее — окно в пределах суток
    сигнала.

    ГЭПЫ. Раз окно пересекает сессии, выход по стопу считается по ХУДШЕЙ из двух
    цен: сам стоп и открытие бара. Если бумага открылась ниже стопа (для лонга),
    реальный выход происходит по открытию, а не по стопу. Иначе овернайт-убытки
    систематически занижались бы — а это ровно тот самый случай, когда система
    льстит себе на деньгах.

    Управление (параметры MGMT_* в settings): при +BE_TRIGGER_R стоп→вход; на T1
    фиксируем PARTIAL_FRAC и остаток ведём чандельер-трейлом (peak − TRAIL_ATR×ATR,
    не ниже BE) до конца окна. Итоговый R — ВЗВЕШЕННЫЙ по ногам выхода.
    Касание — по intrabar high/low; в одном баре стоп имеет приоритет над целью.
    Возвращает dict или None (нет данных → бэкстоп по close в вызывающем коде).
    """
    if not (entry and stop and target) or direction not in ("up", "down"):
        return None
    start = _parse_dt(start_iso)
    if start is None:
        return None
    now = now_utc or datetime.now(timezone.utc)
    # Окно выборки свечей должно покрывать весь горизонт плюс запас на выходные.
    fetch_hours = 48
    deadline = None
    if horizon_hours:
        deadline = start + timedelta(hours=float(horizon_hours))
        age_h = (now - start).total_seconds() / 3600.0
        fetch_hours = int(max(48, min(720, age_h + float(horizon_hours) + 24)))
    data = await fetch_intraday(ticker, tf_min=5, hours=fetch_hours)
    if not data or not data.get("close"):
        return None
    dates = data.get("dates", [])
    highs, lows, closes = data.get("high", []), data.get("low", []), data.get("close", [])
    opens = data.get("open", []) or closes
    sig_date = _msk_date(start)

    # Свечи ПОСЛЕ сигнала: до истечения горизонта (может пересекать сессии) либо,
    # если горизонт не задан, в пределах МСК-суток сигнала — прежнее поведение.
    if deadline is not None:
        path = [i for i, ts in enumerate(dates)
                if (dt := _parse_dt(ts)) is not None and start <= dt <= deadline]
    else:
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
    # Касалась ли цена уровня входа. Пишется как ДАННЫЕ и ничего не блокирует:
    # решение «учитывать ли незаполненную лимитку в деньгах» принимает владелец.
    # Но не записать это — значит потерять информацию навсегда, а по ней потом
    # видно, чего стоила статистика: сделка, которая не открылась, не приносит и
    # не теряет денег, как бы удачно ни сложилось направление.
    entry_touched = False

    for i in path:
        hi = highs[i] if i < len(highs) else closes[i]
        lo = lows[i] if i < len(lows) else closes[i]
        fav = hi if long else lo          # экстремум в нашу сторону в этом баре
        adv = lo if long else hi          # экстремум против нас
        mfe_r = max(mfe_r, r_of(fav))
        # Диапазон бара накрыл уровень входа — заявка исполнилась бы независимо
        # от того, подходила цена сверху (стоп-заявка) или снизу (лимитка).
        if not entry_touched and lo <= entry <= hi:
            entry_touched = True

        # 1) Неблагоприятно: стоп/трейл (тест по стопу, выставленному ПРОШЛЫМИ барами)
        stop_hit = (adv <= cur_stop) if long else (adv >= cur_stop)
        if stop_hit:
            if not partial_done and abs(cur_stop - stop) < 1e-9:
                outcome = "stop"           # начальный −1R
            elif not partial_done and abs(cur_stop - entry) <= 1e-9:
                outcome = "breakeven"      # BE-стоп до частичной фиксации, ~0R
            else:
                outcome = "target"         # T1 уже сняли, остаток вышел по трейлу
            # ГЭП: если бар ОТКРЫЛСЯ хуже стопа, выход происходит по открытию, а
            # не по стопу. Без этого овернайт-убытки занижаются — окно оценки
            # теперь пересекает сессии, поэтому учитывать обязательно.
            op = opens[i] if i < len(opens) else closes[i]
            fill = min(cur_stop, op) if long else max(cur_stop, op)
            legs.append((rem, r_of(fill), fill, "stop" if outcome != "target" else "trail"))
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
        # Не выбито и цель (полностью) не отработала. Финализируем ТОЛЬКО когда
        # окно оценки истекло; иначе сделка ещё в игре → pending.
        # При заданном горизонте окно закрывает он, а не конец суток сигнала:
        # иначе вечерний сценарий на следующую сессию финализировался бы раньше,
        # чем эта сессия вообще начнётся.
        window_over = (now >= deadline) if deadline is not None else _session_over(sig_date, now)
        if not window_over:
            return {"outcome": "pending", "bars": len(path),
                    "mfe_r": round(mfe_r, 3), "partial": partial_done,
                    "entry_touched": entry_touched}
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
        # Данные, а не запрет: касалась ли цена уровня входа за окно оценки.
        # Незаполненная заявка не приносит и не теряет денег, каким бы верным ни
        # оказалось направление, — по этому полю потом видно, чего стоила
        # статистика в рублях.
        "entry_touched": entry_touched,
        "window_hours": float(horizon_hours) if horizon_hours else None,
    }
