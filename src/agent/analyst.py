"""
MOODEX — AI-агент (аналитик)

Схема работы:
  1. rubert размечает каждое сообщение из Telegram/Пульса → агрегатор строит
     индексы настроения по тикерам (sentiment_index, avg_signal, топ-сообщения)
  2. Технический анализ (MOEX ISS: тренд, RSI, MACD, геополитика)
  3. Claude получает всё это и принимает финальное решение (buy/sell/hold)
     с обоснованием на русском языке
  4. Прогноз сохраняется в БД для последующего бэктеста и обучения

⚠️ Не является инвестиционной рекомендацией.
"""
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.analysis import technical as ta
from src.analysis import geopolitics as geo
from src.analysis.macro import get_macro_context
from src.analysis.fundamentals import get_fundamentals
from src.agent import predictor as pred
from src.agent.claude_agent import ClaudeAgent
from src.agent.context_builder import (
    build_ticker_context, build_price_context,
    build_memory_context, build_news_context, build_multiframe_context,
    build_lessons_context, build_knowledge_context, build_orderbook_context,
    build_levels_context, build_structure_context,
)
from src.agent.chart_generator import generate_chart_b64
from src.collector.tinkoff_client import TinkoffClient
from src import db

_tinkoff = TinkoffClient()

logger = logging.getLogger(__name__)

DISCLAIMER = "Не является инвестиционной рекомендацией. Торговля сопряжена с риском."

# Один экземпляр Claude на весь модуль
_claude = ClaudeAgent()


def _recommendation(direction: str, confidence: float) -> str:
    if direction == "up":
        return "Покупать 🟢" if confidence >= 0.5 else "Накапливать 🟢"
    if direction == "down":
        return "Продавать 🔴" if confidence >= 0.5 else "Сокращать 🔴"
    return "Держать / нейтрально ⚪"


def _quality_veto(direction: str, claude_result: dict, intraday_ctx,
                  c_entry, mod_msk: int, regime_stats: dict):
    """
    Вето-слой КАЧЕСТВА интрадей-сигнала (усиления №1/№2/№4). Возвращает причину
    отклонения (str) или None. НИЧЕГО не создаёт и НЕ трогает стоп 1% и решение
    Claude — только отсекает слабые входы (сигнал → «наблюдать», не сохраняется).

      №1 ОКНО      — не входим в первые N мин сессии (шум) и без сформированного ORB;
      №2 АНТИ-ЧЕЙЗ — вход обязан быть у структуры (≤ K×ATR до уровня), не «в поле»;
      №4 РЕЖИМ+HTF — планка конфлюенса выше против тренда/HTF и для сливающих режимов.
    """
    from config import settings as S

    levels = (intraday_ctx or {}).get("levels") or {}

    # ── №1 ОКНО ТОРГОВЛИ ──────────────────────────────────────────────────────
    # Открытие ТОЙ сессии, в которой мы сейчас. Раньше здесь было зашито 10:00,
    # поэтому фильтр «первые N минут шума» не действовал на утреннее открытие в
    # 07:00 и на вечернее в 19:00 — шум открытия этих сессий не отсекался вовсе.
    from src.analysis.intraday import session_open_minute
    sess_open = session_open_minute(mod_msk)
    first_min = getattr(S, "FILTER_NO_ENTRY_FIRST_MIN", 15)
    if sess_open is not None and sess_open <= mod_msk < sess_open + first_min:
        h, m = divmod(sess_open, 60)
        return (f"первые {first_min} мин сессии (открытие {h:02d}:{m:02d}) — "
                "шум открытия, ждём")
    if getattr(S, "FILTER_REQUIRE_ORB", True) and levels.get("or_high") is None:
        return "диапазон открытия (ORB) ещё не сформирован"

    # ── №2 АНТИ-ЧЕЙЗ (зависит от mode) ────────────────────────────────────────
    mode = (claude_result.get("mode") or "pullback").lower()
    atr = (intraday_ctx or {}).get("atr")
    if c_entry and atr and atr > 0:
        if mode == "momentum":
            # Моментум: вход у уровня НЕ требуется (входим по продолжению тренда),
            # но НЕ на исходе — потолок растяжения от VWAP (анти-пик).
            vwap = levels.get("vwap")
            ext = getattr(S, "MOMENTUM_EXT_ATR", 2.0)
            if vwap and abs(c_entry - vwap) > ext * atr:
                return (f"моментум: цена растянута {abs(c_entry - vwap) / atr:.1f}×ATR от VWAP "
                        f"(> {ext}×ATR) — поздно, ждём")
        else:
            # Pullback: вход обязан быть у структуры, не «в поле».
            ref = [levels.get(k) for k in
                   ("vwap", "or_high", "or_low", "session_high", "session_low",
                    "spike_high", "spike_low")]
            ref = [x for x in ref if x]
            if ref:
                nearest = min(abs(c_entry - x) for x in ref)
                max_atr = getattr(S, "FILTER_ENTRY_MAX_ATR", 0.5)
                if nearest > max_atr * atr:
                    return (f"вход далеко от уровня ({nearest / atr:.1f}×ATR > "
                            f"{max_atr}×ATR) — это чейз, ждём откат")

    # ── №4 РЕЖИМ + HTF + КОНФЛЮЕНС ────────────────────────────────────────────
    try:
        conf = int(claude_result.get("confluence_score") or 0)
    except (TypeError, ValueError):
        conf = 0
    htf = (claude_result.get("htf_bias") or "neutral").lower()
    regime = (claude_result.get("regime") or "unclear").lower()
    against_htf = ((htf == "long" and direction == "down") or
                   (htf == "short" and direction == "up"))

    required = getattr(S, "FILTER_CONFLUENCE_MIN", 3)
    if regime == "range":                       # фейд границ = контр-тренд
        required = max(required, getattr(S, "FILTER_CONFLUENCE_COUNTERTREND", 4))
    if against_htf:                             # против старшего фона
        required = max(required, getattr(S, "FILTER_CONFLUENCE_AGAINST_HTF", 5))

    # Гейт по статистике режима: если режим на выборке сливает — планку вверх.
    min_trades = getattr(S, "FILTER_REGIME_MIN_TRADES", 20)
    for row in (regime_stats or {}).get("by_regime", []):
        if row.get("regime") == regime and (row.get("trades") or 0) >= min_trades:
            avg_r = row.get("avg_r")
            if avg_r is not None and avg_r < 0:
                required = max(required, getattr(S, "FILTER_CONFLUENCE_AGAINST_HTF", 5))
            break

    if conf < required:
        return f"конфлюенс {conf} < порога {required} (режим {regime}/HTF {htf})"

    return None


def _veto_code(reason) -> str:
    """
    Человеческую причину вето → КОД для агрегации в журнале попыток.
    Коды живут в db.ATTEMPT_REASONS: по ним видно, какой именно фильтр съедает
    сигналы, и какие пороги FILTER_* стоит калибровать.
    """
    r = (reason or "").lower()
    if not r:
        return "veto_other"
    if "рынок закрыт" in r or "пауза" in r:
        return "veto_session_closed"
    if "конец сессии" in r:
        return "veto_session_end"
    if "мин сессии" in r or "orb" in r or "диапазон открытия" in r:
        return "veto_window"
    if "чейз" in r or "растянут" in r or "далеко от уровня" in r:
        return "veto_chase"
    if "конфлюенс" in r:
        return "veto_confluence"
    if "неполный план" in r:
        return "no_plan"
    # Risk Engine возвращает готовые коды (risk_daily_loss и т.д.) — пропускаем
    # их как есть, чтобы в журнале было видно, КАКОЕ ограничение съело сделку,
    # а не безликое veto_other.
    if "риск-движок" in r:
        for code in ("risk_kill_switch", "risk_daily_loss", "risk_weekly_loss",
                     "risk_max_trades", "risk_max_positions", "risk_sector_limit",
                     "risk_exposure_full", "risk_zero_size", "risk_no_levels",
                     "risk_stop_wrong_side", "risk_spread_too_wide",
                     "risk_book_too_thin"):
            if code in r:
                return code
        return "risk_other"
    return "veto_other"


async def _load_weights() -> list[float]:
    raw = await db.get_setting(pred.WEIGHTS_KEY)
    return pred.weights_from_json(raw) if raw else pred.DEFAULT_WEIGHTS


async def analyze(ticker: str, aggregator, save: bool = True,
                  stage: str = "deep") -> dict:
    """
    Полный анализ тикера:
      - rubert → агрегатор → индекс настроения + топ-сообщения
      - MOEX → технический анализ
      - Claude → финальное решение и обоснование
    """
    ticker = ticker.upper()

    # ── 1. Настроение (собрано rubert-ом через агрегатор) ─────────────────────
    idx = aggregator.get_ticker_index(ticker)
    if idx:
        sentiment_signal  = idx.avg_signal
        sentiment_block   = idx.to_dict()
        # Топ-сообщения для Claude
        points       = list(aggregator._history.get(ticker, []))[-20:]
        top_messages = [p.text_snippet for p in points if p.text_snippet]
    else:
        sentiment_signal  = None
        sentiment_block   = None
        top_messages      = []

    # ── 2. Технический анализ ────────────────────────────────────────────────
    tech           = await ta.analyze_ticker(ticker)
    technical_score = tech.score if tech else None
    technical_block = tech.to_dict() if tech else None

    # ── 3. Геополитический фон ───────────────────────────────────────────────
    geo_snap  = geo.MONITOR.snapshot()
    geo_score = geo_snap["score"]

    # ── 4. Логистическая модель (fallback если Claude недоступен) ────────────
    weights  = await _load_weights()
    fusion   = pred.fuse(sentiment_signal, technical_score, weights)
    combined = max(-1.0, min(1.0, fusion.combined_score + 0.3 * geo_score))

    if combined > 0.15:
        fallback_direction  = "up"
    elif combined < -0.15:
        fallback_direction  = "down"
    else:
        fallback_direction  = "flat"
    fallback_confidence = abs(combined)

    # ── 5. Claude принимает финальное решение ────────────────────────────────
    claude_result = None
    direction     = fallback_direction
    confidence    = fallback_confidence
    narrative     = None
    intraday_ctx  = None
    # Телеметрия вызова Claude для журнала попыток: живёт отдельно от
    # claude_result, потому что при недоступности claude_result обнуляется, а
    # знать цену вызова и причину отказа нужно именно тогда.
    _call_meta: dict = {}
    _claude_error = None
    _claude_verdict = "unavailable"   # up|down|flat|unavailable|error

    try:
        from config.settings import MOEX_TICKERS
        company = MOEX_TICKERS.get(ticker, ticker)

        # Строим исторический контекст (паттерны настроение → цена)
        hist_ctx = await build_ticker_context(
            ticker=ticker,
            current_sentiment=sentiment_block["sentiment_index"] if sentiment_block else None,
        )

        # Строим ценовой дайджест за 2 года
        price_ctx = await build_price_context(ticker)

        # Параллельно: макро + фундаментал + память + мультитаймфрейм + уроки
        import asyncio
        macro_ctx, fund_ctx, memory_ctx, multiframe_ctx, lessons_ctx, knowledge_ctx, levels_ctx, structure_ctx = await asyncio.gather(
            get_macro_context(),
            get_fundamentals(ticker),
            build_memory_context(ticker),
            build_multiframe_context(ticker),
            build_lessons_context(ticker),
            build_knowledge_context(ticker),
            build_levels_context(ticker),
            build_structure_context(ticker),
            return_exceptions=True,
        )
        macro_ctx      = macro_ctx      if not isinstance(macro_ctx, Exception)      else {}
        fund_ctx       = fund_ctx       if not isinstance(fund_ctx, Exception)       else {}
        memory_ctx     = memory_ctx     if not isinstance(memory_ctx, Exception)     else ""
        multiframe_ctx = multiframe_ctx if not isinstance(multiframe_ctx, Exception) else ""
        lessons_ctx    = lessons_ctx    if not isinstance(lessons_ctx, Exception)    else ""
        knowledge_ctx  = knowledge_ctx  if not isinstance(knowledge_ctx, Exception)  else ""
        levels_ctx     = levels_ctx     if not isinstance(levels_ctx, Exception)     else ""
        structure_ctx  = structure_ctx  if not isinstance(structure_ctx, Exception)  else ""

        # Стакан для Claude как отдельный сигнал: индекс (bid/ask, без потока) +
        # синтез-контекст с трендом/стенами/абсорбцией/ликвидностью из истории БЗ.
        try:
            ob_ctx = await build_orderbook_context(ticker)
        except Exception:
            ob_ctx = ""
        try:
            ob_idx = aggregator.get_orderbook_index(ticker)
        except Exception:
            ob_idx = None
        head = []
        if ob_idx:
            head.append(f"📊 ИНДЕКС СТАКАНА (bid/ask, без потока): "
                        f"{ob_idx['orderbook_index']}/100 — {ob_idx['label']} "
                        f"({ob_idx['snapshot_count']} снимков за час)")
        if ob_ctx:
            head.append(ob_ctx)
        if levels_ctx:
            head.append(levels_ctx)
        if structure_ctx:
            head.append(structure_ctx)
        if head:
            knowledge_ctx = "\n".join(head + ([knowledge_ctx] if knowledge_ctx else []))

        # Интрадей-контекст (VWAP, диапазон открытия, волатильность, вынос) —
        # именно он ведёт внутридневное решение; дневная техника выше остаётся
        # как контекст старшего таймфрейма.
        intraday_ctx = None
        try:
            from config.settings import (
                INTRADAY_MODE, INTRADAY_TF_MIN, INTRADAY_OPENING_RANGE_BARS)
            if INTRADAY_MODE:
                from src.agent import intraday_analyst as ia
                intraday_ctx = await ia.build_intraday_context(
                    ticker, tf_min=INTRADAY_TF_MIN,
                    msg_zscore=(sentiment_block or {}).get("volume_zscore"),
                    opening_range_bars=INTRADAY_OPENING_RANGE_BARS,
                )
        except Exception as e:
            logger.debug(f"intraday context failed for {ticker}: {e}")

        # Tinkoff: стакан + поток сделок + объём
        tinkoff_snap = await _tinkoff.get_full_snapshot(ticker)

        # «Умные деньги»: реальные сделки отслеживаемых трейдеров Пульса
        smart_money_ctx = None
        try:
            from src.api import main as _api
            sm_snap = await _api._smart_money_snapshot()
            from src.collector.pulse_author_collector import PulseAuthorTracker
            smart_money_ctx = PulseAuthorTracker([]).context_for(ticker, sm_snap)
        except Exception as e:
            logger.debug(f"smart-money context failed for {ticker}: {e}")

        # Визуальный разбор графика (Claude Vision) — это ВТОРОЙ вызов Claude на
        # тикер + дорогой input-image, поэтому по умолчанию ВЫКЛЮЧЕН (экономия).
        # Включается флагом CHART_ANALYSIS_ENABLED. Структурных данных и так вагон.
        chart_analysis = None
        try:
            from config.settings import CHART_ANALYSIS_ENABLED
            if CHART_ANALYSIS_ENABLED:
                candles = await ta.fetch_candles(ticker, days=120)
                chart_b64 = await generate_chart_b64(
                    ticker=ticker,
                    closes=candles.get("close", []),
                    highs=candles.get("high", []),
                    lows=candles.get("low", []),
                    opens=candles.get("open", []),
                    dates=candles.get("dates", []),
                    days=120,
                )
                if chart_b64:
                    chart_analysis = await _claude.analyze_chart(
                        ticker=ticker,
                        image_b64=chart_b64,
                        sentiment_index=sentiment_block["sentiment_index"] if sentiment_block else None,
                        extra_context=price_ctx,
                    )
                    logger.info(f"📊 Claude chart → {ticker}: {chart_analysis.get('chart_signal')}")
        except Exception as e:
            logger.warning(f"Chart analysis failed for {ticker}: {e}")

        # Разбор графика (Claude Vision) — ВХОД для Claude, а не отдельный голос:
        chart_ctx = None
        if chart_analysis and chart_analysis.get("chart_signal"):
            chart_ctx = (f"📈 РАЗБОР ГРАФИКА (Vision): {chart_analysis.get('chart_signal')} "
                         f"(уверенность {chart_analysis.get('chart_confidence')}%)")

        claude_result = await _claude.synthesize_ticker(
            ticker=ticker,
            company=company,
            sentiment_index=sentiment_block["sentiment_index"] if sentiment_block else 50.0,
            message_count=sentiment_block["message_count"] if sentiment_block else 0,
            positive_pct=sentiment_block.get("positive_pct", 0) if sentiment_block else 0,
            negative_pct=sentiment_block.get("negative_pct", 0) if sentiment_block else 0,
            top_messages=top_messages,
            price_change_1d=technical_block.get("price_change_1d") if technical_block else None,
            rsi=technical_block.get("rsi") if technical_block else None,
            trend=technical_block.get("regime") if technical_block else None,
            historical_context=hist_ctx.get("summary") if hist_ctx["patterns"] else None,
            price_context=price_ctx,
            tinkoff_context=tinkoff_snap.get("summary") if tinkoff_snap else None,
            macro_context=macro_ctx.get("summary") if macro_ctx else None,
            fundamental_context=fund_ctx.get("summary") if fund_ctx else None,
            memory_context=memory_ctx or None,
            multiframe_context=multiframe_ctx or None,
            smart_money_context=smart_money_ctx,
            lessons_context=lessons_ctx or None,
            intraday_context=(intraday_ctx or {}).get("summary") if intraday_ctx else None,
            knowledge_context=knowledge_ctx or None,
            chart_context=chart_ctx,
            momentum=sentiment_block.get("momentum") if sentiment_block else None,
            momentum_label=sentiment_block.get("momentum_label") if sentiment_block else None,
            source_diversity=sentiment_block.get("source_diversity") if sentiment_block else None,
            volume_zscore=sentiment_block.get("volume_zscore") if sentiment_block else None,
            signal_confidence=sentiment_block.get("confidence") if sentiment_block else None,
        )

        # Сигналы формирует ТОЛЬКО Claude по плейбуку и ТОЛЬКО если реально ответил
        # (claude_result["ok"]). Если Claude недоступен (пустой баланс/ошибка) —
        # СИГНАЛА НЕТ: резервную модель не используем, в обучение ничего не пишем.
        signal_map = {"bullish": "up", "bearish": "down", "neutral": "flat"}
        # Телеметрию забираем ДО обнуления claude_result — иначе цена вызова и
        # причина отказа теряются, и мы опять не знаем, почему нет сигнала.
        _call_meta = dict((claude_result or {}).get("_call") or {})
        _claude_error = (claude_result or {}).get("_error")
        if claude_result and claude_result.get("ok"):
            direction  = signal_map.get(claude_result.get("signal", "neutral"), "flat")
            confidence = round((claude_result.get("confidence", 0) or 0) / 100, 3)
            narrative  = claude_result.get("summary", "")
            _claude_verdict = direction
            # Метрики согласованы С РЕШЕНИЕМ CLAUDE:
            combined   = confidence if direction == "up" else (-confidence if direction == "down" else 0.0)
            logger.info(f"🤖 Claude → {ticker}: {direction} (уверенность {confidence})")
        else:
            # Claude недоступен → СИГНАЛА НЕТ. Сигналы формирует ТОЛЬКО Claude:
            # резервную модель не используем, в обучение ничего не сохраняем.
            claude_result = None
            direction, confidence, combined = "flat", 0.0, 0.0
            narrative = "Claude недоступен — сигнала нет (сигналы формирует только Claude)."
            _claude_verdict = "unavailable"
            logger.warning(f"⚠️ Claude недоступен для {ticker}: сигнала нет"
                           f"{f' ({_claude_error})' if _claude_error else ''}")

    except Exception as e:
        logger.warning(f"Ошибка анализа {ticker} — сигнала нет: {e}")
        claude_result = None
        direction, confidence, combined = "flat", 0.0, 0.0
        narrative = "Ошибка анализа — сигнала нет."
        _claude_verdict = "error"
        _claude_error = _claude_error or str(e)[:300]

    recommendation = _recommendation(direction, confidence)

    # ── План сделки — ИЗ РЕШЕНИЯ CLAUDE (инвалидация-first) ──────────────────────
    # Вход/стоп/цель берём у Claude, а не у rule-движков. Интрадей/техника уже
    # ушли Claude как ВХОДНЫЕ данные в промпт и направление больше не назначают.
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    STOP_PCT = 0.01   # потолок стопа: 1% от входа (pullback — жёстко 1%, momentum — ≤1%)
    mode = ((claude_result or {}).get("mode") or "pullback").lower()
    c_entry = c_stop = c_target = c_rr = None
    claude_trade_plan = None
    if claude_result and direction != "flat":
        up = direction == "up"
        # Вход — от Claude (лимитка), иначе текущая цена. Цель — от Claude из уровней.
        c_entry = (_num(claude_result.get("entry"))
                   or (intraday_ctx.get("price") if intraday_ctx else None)
                   or (tech.price if tech else None))
        c_target = _num(claude_result.get("target"))
        if c_entry:
            cap_stop = round(c_entry * (1 - STOP_PCT), 4) if up else round(c_entry * (1 + STOP_PCT), 4)
            if mode == "momentum":
                # СТРУКТУРНЫЙ стоп от Claude (за базой пробоя), но НЕ шире 1% (потолок)
                # и обязательно на верной стороне входа. Тугой стоп → лучше R:R.
                s = _num(claude_result.get("stop"))
                if s and ((up and s < c_entry) or (not up and s > c_entry)):
                    c_stop = max(s, cap_stop) if up else min(s, cap_stop)
                else:
                    c_stop = cap_stop
            else:
                c_stop = cap_stop   # pullback: фиксированный −1%
            risk = abs(c_entry - c_stop)
            if c_target is not None and risk > 0:
                c_rr = round(abs(c_target - c_entry) / risk, 2)
            _stop_txt = "структурный стоп ≤1%" if mode == "momentum" else "стоп 1%"
            claude_trade_plan = {
                "direction": "long" if up else "short",
                "entry_low": c_entry, "entry_high": c_entry,
                "price": intraday_ctx.get("price") if intraday_ctx else (tech.price if tech else None),
                "stop_loss": c_stop,
                "take_profit_1": c_target, "take_profit_2": None,
                "risk_reward": c_rr, "current_rr": c_rr,
                "entry_status": "enter",
                "mode": mode,
                "entry_note": (claude_result.get("setup") or "") + f" · {_stop_txt}",
                "size": claude_result.get("size"),
                "invalidation": claude_result.get("invalidation"),
                "atr": intraday_ctx.get("atr") if intraday_ctx else None,
                "entry_rule": claude_result.get("setup") or "",
                "exit_rule": (("Моментум: структурный стоп ≤1%; при +1R → безубыток; "
                               "частичка 50% на цели, остаток трейлим (1.5×ATR) до закрытия.")
                              if mode == "momentum" else
                              ("Стоп −1%; при +1R → в безубыток; на цели фиксируем 50%, "
                               "остаток трейлим (1.5×ATR) до закрытия сессии.")),
            }
            reg = claude_result.get("regime")
            if reg and reg != "unclear":
                recommendation = f"{recommendation} · режим: {reg}"

    # ── Гвард безопасности + вето-слой КАЧЕСТВА ─────────────────────────────────
    # Claude решает; этот слой лишь ОТКЛОНЯЕТ вход (→ «наблюдать», не сохраняем).
    # Он НИЧЕГО не создаёт и НЕ трогает стоп 1% — только отсекает небезопасные
    # (рынок закрыт/конец сессии) и СЛАБЫЕ (№1 окно / №2 чейз / №4 режим+HTF) входы.
    result_veto_reason = None
    try:
        from src.analysis import intraday as _iv
        _msk = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3)))
        _mod = _msk.hour * 60 + _msk.minute
        _phase = _iv.session_phase(_mod)
        _near_close = _iv.is_last_minutes(_mod, buffer_min=15)
    except Exception:
        _mod, _phase, _near_close = 12 * 60, "main", False
    # (безопасность) рынок закрыт / пауза / пре-аукцион / конец сессии
    if direction != "flat" and (_phase in ("closed", "break", "pre") or _near_close):
        _why = ("рынок закрыт / пауза" if _phase in ("closed", "break", "pre")
                else "конец сессии — флэт к закрытию")
        direction, confidence, combined = "flat", 0.0, 0.0
        claude_trade_plan = None
        recommendation = f"⚪ Наблюдать — {_why}"
        result_veto_reason = _why
    # (качество №1/№2/№4) отсекаем слабые входы — сигнал остаётся ТОЛЬКО у Claude,
    # фильтр может лишь убрать его, не создать.
    if direction != "flat" and claude_result:
        try:
            _reg_stats = await db.regime_stats()
        except Exception:
            _reg_stats = {"by_regime": []}
        _veto = _quality_veto(direction, claude_result, intraday_ctx, c_entry, _mod, _reg_stats)
        if _veto:
            direction, confidence, combined = "flat", 0.0, 0.0
            claude_trade_plan = None
            recommendation = f"⚪ Наблюдать — фильтр: {_veto}"
            result_veto_reason = _veto
            logger.info(f"🛑 {ticker}: сигнал отклонён фильтром качества — {_veto}")

    # (целостность плана) направленный сигнал без ПОЛНОГО плана — не сигнал.
    # Иначе строка сохранится с target=NULL: intraday_outcome её не оценивает
    # (нужны entry+stop+target), correct остаётся NULL — и тикер блокируется как
    # «открытый сигнал» НАВСЕГДА. Лучше честное «наблюдать» с причиной no_plan.
    if direction != "flat" and claude_result and not (c_entry and c_stop and c_target):
        _miss = [n for n, v in (("вход", c_entry), ("стоп", c_stop), ("цель", c_target))
                 if not v]
        direction, confidence, combined = "flat", 0.0, 0.0
        claude_trade_plan = None
        recommendation = f"⚪ Наблюдать — неполный план: нет {', '.join(_miss)}"
        result_veto_reason = f"неполный план: нет {', '.join(_miss)}"
        logger.info(f"🛑 {ticker}: {result_veto_reason} — сигнал не сохраняем")

    # ── 5б. RISK ENGINE — независимый контур, ПОСЛЕДНЕЕ слово ────────────────
    # Стоит после проверки плана: без входа и стопа размер не определён.
    # Движок решает размер позиции и право на сделку, и его вето ПРИОРИТЕТНЕЕ
    # решения Claude — модель не может его переопределить ни промптом, ни
    # ответом. Сюда же попадают стоп дня и недели, лимит числа сделок и
    # kill switch по просадке: сигнал может быть хорошим, а торговать нельзя.
    risk_decision = None
    if direction != "flat" and c_entry and c_stop:
        try:
            from src.risk import engine as _risk
            _rcfg = _risk.load_config()
            _rstate = await _risk.load_state(_rcfg)
            # Ликвидность из снимка стакана: спред и глубина у середины. Нужны,
            # потому что вселенную мы сознательно НЕ сужаем — тонкие бумаги дают
            # самые крупные проценты. Но тонкий стакан ломает арифметику риска
            # (выход происходит хуже стопа), поэтому размер режется под то, что
            # книга реально переварит, а стоп уже спреда отклоняется совсем.
            _spread, _depth = _risk.liquidity_from_orderbook(
                (tinkoff_snap or {}).get("orderbook"))
            risk_decision = _risk.evaluate_trade(
                float(c_entry), float(c_stop), direction, _rstate, _rcfg,
                spread_pct=_spread, depth_near_mid=_depth)
            if not risk_decision.approved:
                direction, confidence, combined = "flat", 0.0, 0.0
                claude_trade_plan = None
                recommendation = f"⚪ Наблюдать — риск-движок: {risk_decision.detail}"
                result_veto_reason = f"риск-движок: {risk_decision.reason}"
                logger.info(f"🛑 {ticker}: риск-движок отклонил "
                            f"({risk_decision.reason}) — {risk_decision.detail}")
            else:
                logger.info(
                    f"📐 {ticker}: размер {risk_decision.shares} шт, риск "
                    f"{risk_decision.risk_rub:.0f}₽ "
                    f"({risk_decision.risk_pct_of_account:.2f}% счёта), "
                    f"экспозиция {risk_decision.notional_rub:,.0f}₽, "
                    f"ограничивает {risk_decision.binding_constraint}")
        except Exception as e:
            # Движок недоступен — сигнал НЕ пропускаем без размера: сделка без
            # рассчитанного риска неисполнима, а молчаливый пропуск вернул бы
            # нас к «мнению вместо плана».
            logger.warning(f"Risk Engine недоступен для {ticker}: {e}")
            direction, confidence, combined = "flat", 0.0, 0.0
            claude_trade_plan = None
            recommendation = "⚪ Наблюдать — риск-движок недоступен"
            result_veto_reason = "риск-движок: risk_other"
            risk_decision = None

    # ── 6. Обоснование ──────────────────────────────────────────────────────
    reasons: list[str] = []
    if sentiment_block:
        reasons.append(
            f"Настроение толпы: {sentiment_block['sentiment_index']}/100 "
            f"({sentiment_block['label']}), {sentiment_block['message_count']} сообщений."
        )
    else:
        reasons.append("Настроение: недостаточно сообщений за окно.")

    if tech:
        regime_ru = {"range": "боковик", "uptrend": "восходящий тренд",
                     "downtrend": "нисходящий тренд"}.get(tech.regime, tech.regime)
        reasons.append(f"Режим рынка: {regime_ru} (ADX {tech.adx}).")
        reasons.extend(tech.reasons)
    else:
        reasons.append("Технический анализ: нет данных MOEX.")

    if geo_snap["events_analyzed"]:
        reasons.append(
            f"Геополитический фон: {geo_snap['label']} (score {geo_score})."
        )

    if claude_result:
        if claude_result.get("key_insight"):
            reasons.append(f"Claude: {claude_result['key_insight']}")
        if claude_result.get("risk"):
            reasons.append(f"Риск: {claude_result['risk']}")

    if intraday_ctx:
        reasons.append(
            f"Интрадей: {intraday_ctx.get('vwap_rel') or 'VWAP н/д'}, "
            f"волатильность {intraday_ctx.get('volatility_state')}, "
            f"фаза {intraday_ctx.get('phase')}"
            + (f", сетап {intraday_ctx.get('setup')}" if intraday_ctx.get('setup') not in (None, 'none') else "")
        )
        if intraday_ctx.get("delayed"):
            reasons.append("⚠️ Интрадей-данные MOEX с задержкой ~15 мин (нет реалтайм-фида).")

    result = {
        "ticker": ticker,
        "recommendation": recommendation,
        "direction": direction,
        "confidence": confidence,
        "combined_score": round(combined, 3),
        "prob_up": round((combined + 1) / 2, 3),
        "regime": tech.regime if tech else None,
        "strategy": tech.strategy if tech else None,
        "range_position": tech.range_position if tech else None,
        "sentiment": sentiment_block,
        "technical": technical_block,
        "geopolitics": geo_snap,
        "claude": claude_result,
        "chart_analysis": chart_analysis,
        "tinkoff": {
            "orderbook": tinkoff_snap.get("orderbook"),
            "trades":    tinkoff_snap.get("trades"),
        } if tinkoff_snap else None,
        "intraday": {
            "setup": intraday_ctx.get("setup"),
            "signal": intraday_ctx.get("signal"),
            "plan": intraday_ctx.get("plan"),
            "observe": intraday_ctx.get("observe"),
            "vwap": intraday_ctx.get("vwap"),
            "vwap_rel": intraday_ctx.get("vwap_rel"),
            "atr": intraday_ctx.get("atr"),
            "volatility_state": intraday_ctx.get("volatility_state"),
            "phase": intraday_ctx.get("phase"),
            "delayed": intraday_ctx.get("delayed"),
            "note": intraday_ctx.get("note"),
            "levels": intraday_ctx.get("levels"),
        } if intraday_ctx else None,
        "narrative": narrative,
        "reasons": reasons,
        "veto_reason": result_veto_reason,
        "model_weights": [round(w, 3) for w in weights],
        "decision_by": "claude" if claude_result else "fallback_model",
        "disclaimer": DISCLAIMER,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # План сделки на карточке — ИЗ РЕШЕНИЯ CLAUDE (если это вход, не «наблюдать»).
    if claude_trade_plan:
        result["trade_plan"] = claude_trade_plan

    # Размер позиции от Risk Engine — без него план не исполним: «купить SBER»
    # это мнение, «158 акций, риск 205₽» это сделка.
    if risk_decision is not None:
        result["position"] = risk_decision.as_dict()

    # ── 7. Сохраняем прогноз — ТОЛЬКО направленные сигналы Claude (up/down) ──
    # «Наблюдать» / нет-сигнала / не-Claude в обучение НЕ идут: учимся только на
    # реальных сигналах Claude. И не плодим второй сигнал по тикеру, пока
    # открытый не отработал (target/stop/session) — тогда тикер снова свободен.
    is_claude_signal = bool(claude_result) and direction in ("up", "down")
    already_open = False
    if save and is_claude_signal:
        try:
            already_open = await db.has_open_signal(ticker)
        except Exception:
            already_open = False
        if already_open:
            logger.info(f"⏸️ {ticker}: уже есть открытый сигнал — новый не сохраняем")
            result["skipped_open_signal"] = True
    if save and is_claude_signal and not already_open:
        try:
            # Снимок ключевых драйверов на момент прогноза — основа для будущего
            # разбора ошибок (post-mortem): по нему станет ясно, на чём стоял сигнал.
            context_snapshot = {
                "direction": direction,
                "confidence": confidence,
                "signal_time_msk": (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M МСК"),
                "decision_by": "claude",  # сохраняем ТОЛЬКО сигналы Claude
                "mode": mode,             # pullback (фикс −1%) или momentum (структурный ≤1%)
                "stop_pct": (round(abs(c_entry - c_stop) / c_entry, 4)
                             if (c_entry and c_stop) else STOP_PCT),  # фактический риск, %
                # Размер от Risk Engine — чтобы в разборе было видно, каким
                # объёмом сделка была бы исполнена и что ограничивало размер.
                "risk_shares": risk_decision.shares if risk_decision else None,
                "risk_rub": (round(risk_decision.risk_rub, 2)
                             if risk_decision else None),
                "risk_pct_of_account": (round(risk_decision.risk_pct_of_account, 4)
                                        if risk_decision else None),
                "risk_notional_rub": (round(risk_decision.notional_rub, 2)
                                      if risk_decision else None),
                "risk_binding": (risk_decision.binding_constraint
                                 if risk_decision else None),
                # Ликвидность на момент сигнала — нужна, чтобы потом
                # разложить результаты по корзинам ликвидности и
                # ответить данными, работает ли система на тонких бумагах.
                "spread_pct_at_signal": (risk_decision.spread_pct
                                         if risk_decision else None),
                "depth_near_mid_at_signal": (risk_decision.depth_near_mid
                                             if risk_decision else None),
                "sentiment_index": sentiment_block["sentiment_index"] if sentiment_block else None,
                "sentiment_label": sentiment_block.get("label") if sentiment_block else None,
                "sentiment_signal": sentiment_signal,
                "message_count": sentiment_block.get("message_count") if sentiment_block else 0,
                "regime": tech.regime if tech else None,
                "rsi": technical_block.get("rsi") if technical_block else None,
                "technical_score": technical_score,
                "strategy": tech.strategy if tech else None,
                # Параметры детектора режима — сырьё для /api/regime-audit.
                # Без них нельзя проверить, не назвал ли детектор тренд
                # боковиком: порог ADX >= 25 пропускает плавный рост, и
                # стратегия боковика начинает шортить хаи растущего рынка.
                "range_position": getattr(tech, "range_position", None) if tech else None,
                "adx": getattr(tech, "adx", None) if tech else None,
                "regime_claude": (claude_result or {}).get("regime") if claude_result else None,
                "confluence_score": (claude_result or {}).get("confluence_score") if claude_result else None,
                "setup": (claude_result or {}).get("setup") if claude_result else None,
                "geo_score": geo_score,
                "smart_money": smart_money_ctx,
                "chart_signal": (chart_analysis or {}).get("chart_signal") if chart_analysis else None,
                "key_insight": (claude_result or {}).get("key_insight") if claude_result else None,
                "risk": (claude_result or {}).get("risk") if claude_result else None,
                "narrative": narrative,
                "intraday_setup": intraday_ctx.get("setup") if intraday_ctx else None,
                "intraday_vwap_rel": intraday_ctx.get("vwap_rel") if intraday_ctx else None,
                "intraday_volatility": intraday_ctx.get("volatility_state") if intraday_ctx else None,
                "intraday_phase": intraday_ctx.get("phase") if intraday_ctx else None,
                "intraday_delayed": intraday_ctx.get("delayed") if intraday_ctx else None,
            }
            # Горизонт интрадей-сигнала — КОНЕЦ текущей сессии (флэт к закрытию), а
            # ОЦЕНИВАЕМ на СЛЕДУЮЩИЙ день: делаем прогноз «созревающим» к утру след.
            # дня (08:00 МСК), когда путь всей сессии уже закрыт и исход виден.
            try:
                from config.settings import INTRADAY_MODE
            except Exception:
                INTRADAY_MODE = False
            env_h = os.getenv("PREDICTION_HORIZON_HOURS")
            if env_h:
                horizon_h = int(env_h)
            elif INTRADAY_MODE:
                msk_now = datetime.now(timezone.utc) + timedelta(hours=3)
                due_msk = (msk_now + timedelta(days=1)).replace(
                    hour=8, minute=0, second=0, microsecond=0)
                horizon_h = max(1, int((due_msk - msk_now).total_seconds() // 3600) + 1)
            else:
                horizon_h = 24
            pred_id = await db.add_prediction({
                "ticker": ticker,
                "horizon_hours": horizon_h,
                "sentiment_index": sentiment_block["sentiment_index"] if sentiment_block else None,
                "sentiment_signal": sentiment_signal,
                "technical_score": technical_score,
                "combined_score": combined,
                "confidence": confidence,
                "direction": direction,
                "price_at": tech.price if tech else None,
                # momentum → тег trend_momentum (отдельная строка в regime-stats).
                "regime": ("trend_momentum" if mode == "momentum"
                           else ((claude_result or {}).get("regime") if claude_result else None)),
                "confluence_score": (claude_result or {}).get("confluence_score") if claude_result else None,
                "entry": c_entry,        # вход (лимитка Claude / текущая цена)
                "stop": c_stop,          # pullback: фикс −1%; momentum: структурный ≤1%
                "target": c_target,      # цель Claude из уровней
                "rr_planned": c_rr,      # R:R от фактического риска |вход−стоп|
                "context": context_snapshot,
            })
            result["prediction_id"] = pred_id
        except Exception as e:
            logger.warning(f"Не удалось сохранить прогноз {ticker}: {e}")

    # ── 8. ЖУРНАЛ ПОПЫТОК — пишем ВСЕГДА, даже когда сигнала нет ────────────────
    # Раньше «neutral от Claude», вето и недоступность Claude не оставляли следа:
    # в БД были только up/down. Из-за этого на вопрос «почему за день 0 сигналов»
    # ответа не было вообще. Теперь у каждой попытки есть код причины и цена.
    if save:
        _saved_id = result.get("prediction_id")
        if _saved_id:
            _reason = "saved"
        elif already_open:
            _reason = "already_open"
        elif result_veto_reason:
            _reason = _veto_code(result_veto_reason)
        elif _claude_verdict == "flat":
            _reason = "claude_flat"
        elif _claude_verdict == "error":
            _reason = "analysis_error"
        elif _claude_verdict == "unavailable":
            _reason = ("budget" if "budget" in (_claude_error or "").lower()
                       else "claude_unavailable")
        else:
            _reason = "veto_other"
        _note = result_veto_reason or _claude_error or (narrative or "")[:200]
        await db.add_signal_attempt({
            # "deep" — из сканера (воронка), "manual" — открытие карточки руками:
            # разделяем, чтобы ручные клики не искажали статистику сканера.
            "stage": stage,
            "ticker": ticker,
            "phase": _phase,
            "verdict": _claude_verdict,
            "final": direction,
            "saved": bool(_saved_id),
            "prediction_id": _saved_id,
            "reason": _reason,
            "mode": mode,
            "regime": (claude_result or {}).get("regime"),
            "confluence": (claude_result or {}).get("confluence_score"),
            "confidence": confidence,
            "rr": c_rr,
            "entry": c_entry,
            "stop": c_stop,
            "target": c_target,
            "cost_rub": _call_meta.get("cost_rub"),
            "tokens_in": _call_meta.get("tokens_in"),
            "tokens_out": _call_meta.get("tokens_out"),
            "note": _note,
        })

    return result


# ─── Обучение на результатах ──────────────────────────────────────────────────

async def evaluate_due_predictions() -> dict:
    """
    Оценка сигналов. (1) Открытые интрадей Claude-сигналы — на КАЖДОМ тике: исход
    target/stop фиксируем сразу при касании, session — по закрытию сессии, иначе
    оставляем открытым (pending). (2) Легаси/не-Claude — по истечении горизонта.
    """
    evaluated = 0

    # ── 1) ОТКРЫТЫЕ интрадей Claude-сигналы — оцениваем НА КАЖДОМ ТИКЕ ─────────
    # target/stop фиксируем СРАЗУ при касании (не ждём горизонт/след. день);
    # session — по закрытию сессии того же дня; pending — сигнал ещё в игре.
    try:
        from src.agent import intraday_analyst as ia
        open_intra = await db.get_open_intraday_predictions()
    except Exception as e:
        logger.debug(f"open intraday fetch: {e}")
        open_intra = []
    for p in open_intra:
        if not p.created_at:
            continue
        try:
            # Горизонт передаём обязательно: без него окно оценки схлопывается
            # до МСК-суток сигнала, и вечерний сценарий на следующую сессию
            # оценить невозможно — цель и стоп до 23:50 обычно не трогают.
            oc = await ia.intraday_outcome(
                p.ticker, p.created_at.isoformat(), p.direction,
                float(p.entry), float(p.stop), float(p.target),
                horizon_hours=p.horizon_hours)
        except Exception as e:
            logger.debug(f"intraday_outcome {p.ticker}: {e}")
            oc = None
        if not oc or oc.get("outcome") in (None, "pending"):
            continue  # ещё в игре / нет данных — оставляем открытым
        rr = oc.get("realized_r")
        correct = bool(rr is not None and rr > 0)  # прибыльна в R → «верна»
        # Факт касания входа — в снимок контекста, как данные. Направление
        # оценивается как раньше (владелец так решил: верно прочитанный тренд
        # ценен сам по себе), но информация о незаполненной заявке сохраняется.
        try:
            if oc.get("entry_touched") is not None:
                await db.merge_prediction_context(
                    p.id, {"entry_touched": bool(oc.get("entry_touched")),
                           "eval_window_hours": oc.get("window_hours")})
        except Exception as _e:
            logger.debug(f"entry_touched {p.ticker}: {_e}")
        await db.evaluate_prediction(
            p.id, oc["realized_price"], oc["realized_return"], correct,
            outcome=oc["outcome"], realized_r=rr,
            mfe_r=oc.get("mfe_r"), legs=oc.get("legs"))
        logger.info(
            f"📏 {p.ticker} {p.direction}: исход={oc['outcome']} "
            f"R={rr} (MFE {oc.get('mfe_r')}R, {oc['realized_return']:+.2f}%)")
        evaluated += 1

    # ── 2) Легаси/не-Claude прогнозы — по горизонту, оценка по цене (бэкстоп) ──
    due = await db.get_due_predictions()
    for p in due:
        if not p.price_at:
            continue
        realized_price = None
        try:
            from config.settings import INTRADAY_MODE
            if INTRADAY_MODE and p.horizon_hours and p.horizon_hours < 24 and p.created_at:
                from src.agent import intraday_analyst as ia
                realized_price = await ia.realized_price_after(
                    p.ticker, p.created_at.isoformat(), float(p.horizon_hours))
        except Exception:
            realized_price = None
        if realized_price is None:
            try:
                closes = await ta.fetch_closes(p.ticker, days=10)
            except Exception:
                continue
            if not closes:
                continue
            realized_price = closes[-1]
        realized_return = (realized_price / p.price_at - 1) * 100
        actual_up = realized_return > 0
        if p.direction == "up":
            correct = actual_up
        elif p.direction == "down":
            correct = not actual_up
        else:
            correct = abs(realized_return) < 1.0

        await db.evaluate_prediction(p.id, realized_price, realized_return, correct)
        evaluated += 1

    # Разбор ошибок по свежеоценённым (и любым неразобранным) прогнозам
    analyzed = await generate_post_mortems()

    retrained = await retrain()
    return {"evaluated": evaluated, "post_mortems": analyzed, "retrained": retrained}


async def generate_post_mortems(limit: int = 25) -> int:
    """
    Для оценённых, но ещё не разобранных прогнозов сформулировать причину
    успеха/провала и урок, и сохранить их в БД. Возвращает число разборов.

    Это замыкает цикл обучения: выводы копятся в журнале и через
    build_lessons_context() попадают в промпт будущих прогнозов.
    """
    import json
    pending = await db.get_predictions_for_post_mortem(limit=limit)
    done = 0
    for p in pending:
        context = {}
        raw = p.get("context_json")
        if raw:
            try:
                context = json.loads(raw)
            except Exception:
                context = {}
        try:
            pm = await _claude.post_mortem(
                ticker=p["ticker"],
                direction=p.get("direction", "flat"),
                confidence=p.get("confidence", 0.0),
                context=context,
                realized_return=p.get("realized_return"),
                correct=p.get("correct"),
                horizon_hours=p.get("horizon_hours", 24),
            )
            await db.set_post_mortem(
                p["id"], pm.get("cause", ""), pm.get("lesson", ""), pm.get("tags", []))
            done += 1
        except Exception as e:
            logger.warning(f"Не удалось разобрать прогноз id={p.get('id')}: {e}")
    if done:
        logger.info(f"🔎 Разобрано закрытых сигналов (post-mortem): {done}")
    return done


async def retrain() -> bool:
    """Переобучить веса fallback-модели на оценённых прогнозах."""
    rows    = await db.get_evaluated_predictions()
    samples = []
    for r in rows:
        if r.get("realized_return") is None:
            continue
        samples.append({
            "sentiment_signal": r.get("sentiment_signal"),
            "technical_score":  r.get("technical_score"),
            "label": 1 if r["realized_return"] > 0 else 0,
        })

    if len(samples) < 10:
        return False

    new_weights = pred.train_weights(samples)
    await db.set_setting(pred.WEIGHTS_KEY, pred.weights_to_json(new_weights))
    logger.info(f"🧠 Fallback-модель переобучена на {len(samples)} примерах: {new_weights}")
    return True
