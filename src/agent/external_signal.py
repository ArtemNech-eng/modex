"""Приём сценария от ВНЕШНЕГО аналитика в тот же конвейер.

ЗАЧЕМ. Прогноз в системе создавался только внутренним путём Claude. Пока
бюджет API не пополнен, роль аналитика выполняет внешняя модель, и её сценарии
обязаны попадать в ТОТ ЖЕ журнал: с планом, размером от Risk Engine, оценкой по
исходу и сравнением с решением человека. Иначе это мнение в переписке, а не
сделка — неизмеримое и потому бесполезное для системы.

КЛЮЧЕВОЕ ТРЕБОВАНИЕ К ЧЕСТНОСТИ. Внешний сценарий проходит те же проверки, что
внутренний: полнота плана, лимиты риска, стоп против спреда, глубина стакана.
Никакого обходного пути — иначе завтра нельзя будет сравнить внешнего аналитика
с API на равных, и любое «у меня лучше» окажется артефактом поблажек.

Поле `analyst` — ось для будущего сравнения: hyperagent / claude-api / human.
Когда API включат обратно, оба источника будут в одной таблице, и вопрос «кто
точнее» станет арифметикой, а не спором.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

ALLOWED_DIRECTIONS = ("up", "down")
ALLOWED_ANALYSTS = ("hyperagent", "claude-api", "human", "other")


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):   # NaN / inf
        return None
    return f


def validate_scenario(payload: dict) -> tuple:
    """Проверить сценарий. Возвращает (errors: list, normalized: dict).

    Чистая функция — покрывается тестами. Пустой список ошибок означает, что
    сценарий пригоден к передаче в Risk Engine.

    Требования не формальность:
      • entry/stop/target обязательны все три — иначе intraday_outcome не сможет
        оценить сделку, `realized_r` не посчитается, и сигнал не попадёт ни в
        метрику в R, ни в виртуальный счёт;
      • confidence строго больше нуля — при нуле запись не считается
        направленным прогнозом в accuracy_stats и выпадет из измерения;
      • reason и invalidation обязательны, потому что сценарий без «почему» и без
        «что его отменяет» невозможно разобрать в пост-мортеме. Это же требование
        стоит в архитектуре: аналитик отвечает на пять вопросов, включая «что
        может пойти не так».
    """
    errors: list = []
    p = payload or {}

    ticker = str(p.get("ticker") or "").strip().upper()
    if not ticker:
        errors.append("ticker обязателен")
    else:
        try:
            from config.settings import MOEX_TICKERS
            if ticker not in MOEX_TICKERS:
                errors.append(f"тикер {ticker} не отслеживается — оценка исхода "
                              "будет невозможна")
        except Exception:                     # noqa: BLE001 — офлайн-тесты
            pass

    direction = str(p.get("direction") or "").strip().lower()
    if direction in ("long", "buy"):
        direction = "up"
    elif direction in ("short", "sell"):
        direction = "down"
    if direction not in ALLOWED_DIRECTIONS:
        errors.append(f"direction должен быть up или down, получено {p.get('direction')!r}")

    entry, stop, target = _num(p.get("entry")), _num(p.get("stop")), _num(p.get("target"))
    for name, val in (("entry", entry), ("stop", stop), ("target", target)):
        if val is None or val <= 0:
            errors.append(f"{name} обязателен и должен быть положительным числом")

    # Геометрия плана. Risk Engine проверяет сторону СТОПА, но не цели — а цель
    # по неверную сторону делает сделку бессмысленной ещё до расчёта размера.
    if entry and stop and target and direction in ALLOWED_DIRECTIONS:
        if direction == "up":
            if stop >= entry:
                errors.append(f"long: стоп {stop} должен быть ниже входа {entry}")
            if target <= entry:
                errors.append(f"long: цель {target} должна быть выше входа {entry}")
        else:
            if stop <= entry:
                errors.append(f"short: стоп {stop} должен быть выше входа {entry}")
            if target >= entry:
                errors.append(f"short: цель {target} должна быть ниже входа {entry}")

    conf = _num(p.get("confidence"))
    if conf is None or not (0 < conf <= 1):
        errors.append("confidence обязателен, строго больше 0 и не больше 1 "
                      "(при нуле запись выпадет из измерения точности)")

    reason = str(p.get("reason") or "").strip()
    if len(reason) < 10:
        errors.append("reason обязателен: сценарий без обоснования нельзя разобрать")
    invalidation = str(p.get("invalidation") or "").strip()
    if len(invalidation) < 5:
        errors.append("invalidation обязателен: нужно условие, отменяющее сценарий")

    analyst = str(p.get("analyst") or "hyperagent").strip().lower()
    if analyst not in ALLOWED_ANALYSTS:
        errors.append(f"analyst должен быть одним из {ALLOWED_ANALYSTS}")

    rr = None
    if entry and stop and target and entry != stop:
        rr = round(abs(target - entry) / abs(entry - stop), 2)

    normalized = {
        "ticker": ticker, "direction": direction,
        "entry": entry, "stop": stop, "target": target,
        "confidence": conf, "rr": rr,
        "reason": reason, "invalidation": invalidation,
        "analyst": analyst,
        "regime": (str(p.get("regime")).strip().lower()
                   if p.get("regime") else None),
        "mode": (str(p.get("mode")).strip().lower() if p.get("mode") else None),
        "horizon_hours": p.get("horizon_hours"),
    }
    return errors, normalized


def _default_horizon_hours(msk_now: Optional[datetime] = None) -> int:
    """Горизонт до конца той сессии, в которую сделку МОЖНО исполнить.

    Прежняя логика целилась в 08:00 следующего дня. Для сигнала, выданного днём,
    это верно: остаток сессии плюс вечерняя укладываются в окно. Но для сигнала,
    выданного ВЕЧЕРОМ после закрытия, окно истекало в 08:00 — то есть РАНЬШЕ, чем
    в 10:00 откроется сессия, в которую его вообще можно торговать. Сценарий
    гарантированно истекал неисполнимым, а оценщик не видел ни одной свечи
    нужной сессии.

    Правило теперь по фазе суток МСК:
      • до 10:00      — торгуем сегодняшнюю сессию, окно до 23:50 сегодня;
      • 10:00-18:40   — сессия идёт, окно до 08:00 следующего дня (как было);
      • после 18:40   — торгуем СЛЕДУЮЩУЮ сессию, окно до 23:50 следующего дня.
    """
    now = msk_now or (datetime.now(timezone.utc) + timedelta(hours=3))
    mod = now.hour * 60 + now.minute
    try:
        from config import settings as S
        e_close = getattr(S, "SESSION_EVENING_CLOSE", 23 * 60 + 50)
        low_liq = getattr(S, "SESSION_LOW_LIQUIDITY_AFTER", 22 * 60)
    except Exception:                                  # noqa: BLE001
        e_close, low_liq = 23 * 60 + 50, 22 * 60

    def _at(day_shift, minute):
        h, m = divmod(minute, 60)
        return (now + timedelta(days=day_shift)).replace(
            hour=h, minute=m, second=0, microsecond=0)

    # ЗАПАС считаем по отсечке ликвидности, а НЕ по формальному закрытию.
    # После low_liq новые входы не открываются, поэтому час между 22:00 и 23:50
    # торгуемым временем не является. Раньше здесь стояло формальное закрытие, и
    # сценарий, выданный в 21:00, получал горизонт 3 часа при одном часе реально
    # доступного времени — заведомо неисполнимо.
    min_runway_h = 2
    runway_h = (low_liq - mod) / 60.0
    if runway_h >= min_runway_h:
        due = _at(0, e_close)          # успеваем в сегодняшний торговый день
    else:
        due = _at(1, e_close)          # переносим на следующий
    return max(1, int((due - now).total_seconds() // 3600) + 1)


async def submit(payload: dict) -> dict:
    """Принять сценарий: валидация → Risk Engine → журнал.

    Возвращает результат с размером позиции либо причину отказа. Прогноз
    сохраняется ТОЛЬКО если движок риска одобрил сделку: сценарий без размера
    неисполним, а запись без размера не двигает виртуальный счёт.
    """
    from src import db
    from src.risk import engine as risk

    errors, s = validate_scenario(payload)
    if errors:
        return {"status": "rejected", "stage": "validation", "errors": errors}

    ticker = s["ticker"]

    # Не плодим второй сигнал по тикеру, пока открытый не отработал — то же
    # правило, что во внутреннем пути.
    try:
        if await db.has_open_signal(ticker):
            await db.add_signal_attempt({
                "stage": "external", "ticker": ticker, "reason": "already_open",
                "verdict": "skip", "confidence": s["confidence"],
                "entry": s["entry"], "stop": s["stop"], "target": s["target"],
                "note": f"аналитик {s['analyst']}: по тикеру уже открыт сигнал"})
            return {"status": "rejected", "stage": "already_open",
                    "errors": [f"по {ticker} уже есть открытый сигнал"]}
    except Exception as e:                    # noqa: BLE001
        logger.debug("external_signal: проверка открытого сигнала: %s", e)

    # Ликвидность из живого стакана — те же две проверки, что для внутреннего пути.
    spread_pct = depth = None
    try:
        from src.collector.tinkoff_client import TinkoffClient
        snap = await TinkoffClient().get_full_snapshot(ticker)
        spread_pct, depth = risk.liquidity_from_orderbook(
            (snap or {}).get("orderbook"))
    except Exception as e:                    # noqa: BLE001
        logger.debug("external_signal: стакан недоступен для %s: %s", ticker, e)

    # На каких данных построен сценарий: реалтайм Tinkoff или MOEX ISS с
    # задержкой ~15 минут. Флаг существовал внутри интрадей-контекста и до записи
    # сигнала не доходил — аналитик работал на запоздавших данных, не зная об
    # этом. Для дневной структуры 15 минут терпимы, для интрадей-сетапа это всё.
    data_source = None
    data_delayed = None
    try:
        from src.agent import intraday_analyst as ia
        ictx = await ia.build_intraday_context(ticker)
        if ictx:
            data_source = ictx.get("source")
            data_delayed = ictx.get("delayed")
    except Exception as e:                    # noqa: BLE001
        logger.debug("external_signal: интрадей-контекст для %s: %s", ticker, e)

    cfg = risk.load_config()
    state = await risk.load_state(cfg)
    decision = risk.evaluate_trade(s["entry"], s["stop"], s["direction"],
                                   state, cfg, spread_pct=spread_pct,
                                   depth_near_mid=depth)
    if not decision.approved:
        await db.add_signal_attempt({
            "stage": "external", "ticker": ticker, "reason": decision.reason,
            "verdict": "veto", "confidence": s["confidence"], "rr": s["rr"],
            "entry": s["entry"], "stop": s["stop"], "target": s["target"],
            "note": f"аналитик {s['analyst']}: {decision.detail}"[:400]})
        return {"status": "rejected", "stage": "risk",
                "reason": decision.reason, "detail": decision.detail,
                "position": decision.as_dict()}

    # Цена на момент сигнала нужна оценщику: realized_return считается от неё.
    price_at = None
    try:
        from src.analysis import technical as ta
        tech = await ta.analyze_ticker(ticker)
        price_at = getattr(tech, "price", None)
    except Exception as e:                    # noqa: BLE001
        logger.debug("external_signal: цена для %s недоступна: %s", ticker, e)
    if not price_at:
        # Осознанный компромисс: без рыночной цены берём вход как отсчёт и
        # помечаем это в снимке, чтобы при разборе было видно происхождение.
        price_at = s["entry"]

    horizon = s["horizon_hours"] or _default_horizon_hours()
    context = {
        "direction": s["direction"],
        "confidence": s["confidence"],
        "signal_time_msk": (datetime.now(timezone.utc) + timedelta(hours=3)
                            ).strftime("%Y-%m-%d %H:%M МСК"),
        "decision_by": s["analyst"],          # ось сравнения аналитиков
        "analyst": s["analyst"],
        "mode": s["mode"],
        "regime_claude": s["regime"],
        "reason": s["reason"],
        "invalidation": s["invalidation"],
        "narrative": s["reason"],
        "price_at_source": ("market" if price_at != s["entry"] else "entry_fallback"),
        "stop_pct": round(abs(s["entry"] - s["stop"]) / s["entry"], 4),
        "risk_shares": decision.shares,
        "risk_rub": round(decision.risk_rub, 2),
        "risk_pct_of_account": round(decision.risk_pct_of_account, 4),
        "risk_notional_rub": round(decision.notional_rub, 2),
        "risk_binding": decision.binding_constraint,
        "spread_pct_at_signal": decision.spread_pct,
        "depth_near_mid_at_signal": decision.depth_near_mid,
        # Происхождение данных — в журнал. Позже позволит замерить, отличается ли
        # результативность сценариев на реалтайме и на задержке: ещё одна ось
        # сравнения, которую без этого поля построить нельзя.
        "data_source": data_source,
        "data_delayed": data_delayed,
    }

    pred_id = await db.add_prediction({
        "ticker": ticker,
        "horizon_hours": horizon,
        "confidence": s["confidence"],
        "combined_score": (s["confidence"] if s["direction"] == "up"
                           else -s["confidence"]),
        "direction": s["direction"],
        "price_at": price_at,
        "regime": s["regime"],
        "entry": s["entry"], "stop": s["stop"], "target": s["target"],
        "rr_planned": s["rr"],
        "context": context,
    })

    await db.add_signal_attempt({
        "stage": "external", "ticker": ticker, "reason": "saved",
        "verdict": s["direction"], "final": s["direction"], "saved": True,
        "prediction_id": pred_id, "confidence": s["confidence"], "rr": s["rr"],
        "entry": s["entry"], "stop": s["stop"], "target": s["target"],
        "regime": s["regime"], "mode": s["mode"],
        "note": f"аналитик {s['analyst']}: {s['reason']}"[:400]})

    return {"status": "saved", "prediction_id": pred_id,
            "ticker": ticker, "direction": s["direction"],
            "entry": s["entry"], "stop": s["stop"], "target": s["target"],
            "rr_planned": s["rr"], "confidence": s["confidence"],
            "horizon_hours": horizon, "price_at": price_at,
            "analyst": s["analyst"],
            "data_source": data_source, "data_delayed": data_delayed,
            "position": decision.as_dict()}
