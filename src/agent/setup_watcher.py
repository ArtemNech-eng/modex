"""
Быстрый наблюдатель сетапов — БЕЗ Claude, то есть бесплатно.

ЗАЧЕМ. Сетап пробоя консолидации по Мечелу 30.07 срабатывал в 13:25 (вход 38.35).
Сканер с подтверждением Claude ходит раз в 45 минут, поэтому этот сигнал был бы
увиден в 14:10, когда цена уже 39.16. Фактический вход состоялся в 14:34 по 39.33 и
дал 4 088₽ на 4048 акциях, тогда как вход 13:25 при том же выходе дал бы 8 056₽ —
ровно вдвое. Опоздание сканера стоило половины прибыли.

ПОЧЕМУ ИНТЕРВАЛ 5 МИНУТ, А НЕ ЧАЩЕ. Сетапы считаются по ЗАКРЫТИЮ пятиминутного
бара. Опрашивать чаще, чем раз в бар, бессмысленно: данные не изменятся, а лимиты
Tinkoff израсходуются. Ровно один проход на бар — это и максимальная скорость,
которая имеет смысл, и минимальная нагрузка.

ЧЕГО ЗДЕСЬ НЕТ. Никакого обращения к Claude: наблюдатель только ловит момент и
записывает его. Подтверждение и разбор — отдельный, платный контур. Смысл
разделения в том, что ловить момент нужно быстро и дёшево, а рассуждать можно
медленно и дорого.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Сетапы, которые наблюдатель считает поводом для сигнала. Диапазон открытия сюда
# НЕ входит: он живёт первые 90 минут и уже покрыт основным сканером, а его
# просроченные срабатывания 30.07 дали 36 сетапов, из которых 35 имели R/R < 1.
WATCHED_SETUPS = ("consolidation_breakout", "news_resolution")

# ─── Карантин: сетап не выдаётся владельцу в день изобретения ────────────────
#
# 31.07 я нашёл сетап «возврат под VWAP в нисходящем тренде» в 11:48 (959 входов
# на истории, +0.158R, t=4.35) и в 11:50 выдал владельцу сигнал по VTBR. Он вошёл
# 2512 акциями и закрылся через 14 минут с убытком.
#
# Сетап был не виноват. Виноваты две вещи, обе мои:
#
#   1. Живой сканер работал НЕ ТАК, как проверенный тест. Тест входит В МОМЕНТ
#      пересечения VWAP; сканер проверял состояние «был выше, сейчас ниже» и
#      сработал через час четырнадцать после события. Замер по той же выборке:
#          вход в момент пересечения  +0.147R  t=4.02
#          вход через час             +0.067R  t=1.53
#      То есть владельцу был продан один сетап, а исполнен другой.
#
#   2. У сетапа был НОЛЬ дней живой работы. Утренний шорт перед выдачей
#      наблюдался сутки. Этот — ноль.
#
# Настоящая причина глубже обеих: владелец пять раз просил сигнал, я пять раз
# отвечал «нечем», и на шестой выпустил находку немедленно, потому что было
# неловко снова сказать «нет». Давление выдать результат — единственная причина,
# от которой не страхует код. Поэтому здесь стоит заслон, а не памятка.
#
# ПРАВИЛО: сетап попадает в сигналы только после того, как отработал в режиме
# наблюдения полную сессию и его исходы записаны. Дата — день, СЛЕДУЮЩИЙ за
# первым полным днём наблюдения.
SETUP_LIVE_SINCE = {
    "consolidation_breakout": "2026-07-31",   # наблюдался 30.07, выдан 31.07
    "news_resolution": "2026-07-31",
    # "vwap_reclaim_fail": НЕ ЗАПОЛНЕНО — сетап найден 31.07, живого дня нет.
    # Заполнять только после суток наблюдения с записанными исходами.
}


def setup_is_live(name: str, today: Optional[str] = None) -> bool:
    """
    Можно ли выдавать этот сетап как СИГНАЛ, а не как наблюдение.

    Сетап без записи в SETUP_LIVE_SINCE — всегда только наблюдение, каким бы
    хорошим ни выглядел бэктест. Незнакомое имя не пропускается по умолчанию:
    забыть внести запись должно быть безопасно, а забыть про карантин — нет.
    """
    since = SETUP_LIVE_SINCE.get(name)
    if not since:
        return False
    today = today or (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    return today >= since

_status: dict = {
    "enabled": False, "running": False, "interval_min": 5,
    "last_pass": None, "next_pass": None, "passes": 0,
    "checked": 0, "fired": 0, "errors": 0, "last_error": None,
}
_recent: list = []          # последние срабатывания (для выдачи в API)
_seen: dict = {}            # (тикер, сетап) -> время последнего срабатывания


def status() -> dict:
    out = dict(_status)
    out["recent"] = list(reversed(_recent[-25:]))
    return out


def _cfg():
    try:
        from config.settings import (SETUP_WATCH_INTERVAL_MIN, SETUP_WATCH_DEDUP_MIN,
                                     SETUP_WATCH_PACING_SEC, MOEX_TICKERS)
        return (SETUP_WATCH_INTERVAL_MIN, SETUP_WATCH_DEDUP_MIN,
                SETUP_WATCH_PACING_SEC, list(MOEX_TICKERS.keys()))
    except Exception:                                # noqa: BLE001
        return 5, 30, 0.2, []


def _is_duplicate(ticker: str, setup: str, dedup_min: int) -> bool:
    """Один и тот же сетап по одной бумаге не повторяем в течение окна.

    Без этого пробой, который держится несколько баров, дал бы сигнал на каждом
    проходе, и журнал заполнился бы копиями одного события.
    """
    key = (ticker, setup)
    last = _seen.get(key)
    now = datetime.now(timezone.utc)
    if last and (now - last) < timedelta(minutes=dedup_min):
        return True
    _seen[key] = now
    return False


async def check_ticker(ticker: str) -> Optional[dict]:
    """Проверить одну бумагу. Возвращает срабатывание или None."""
    from src.agent import intraday_analyst as ia
    from src.analysis import technical as ta
    try:
        from config.settings import INTRADAY_TF_MIN, INTRADAY_OPENING_RANGE_BARS
    except Exception:                                # noqa: BLE001
        INTRADAY_TF_MIN, INTRADAY_OPENING_RANGE_BARS = 5, 6

    ref = None
    try:
        tech = await ta.analyze_ticker(ticker)
        ref = tech.price if tech else None
    except Exception:                                # noqa: BLE001
        ref = None

    ctx = await ia.build_intraday_context(
        ticker, tf_min=INTRADAY_TF_MIN,
        opening_range_bars=INTRADAY_OPENING_RANGE_BARS, reference_price=ref)
    if ctx.get("stale") or ctx.get("mismatch"):
        return None                                  # данные не годятся — не сигналим

    # НАБЛЮДЕНИЕ или СИГНАЛ. Сетап пробоя в режиме observe приходит отдельным полем
    # и сделкой не является: проверка на 181 торговом дне показала, что лонговая
    # сторона убыточна во всех конфигурациях после издержек. Записываем, чтобы через
    # месяц решать по живым числам, но не называем сигналом.
    obs = ctx.get("breakout_observation")
    if obs and obs.get("signal") in ("long", "short"):
        plan, kind, name = obs, "observation", "consolidation_breakout"
    elif ctx.get("setup") in WATCHED_SETUPS:
        plan = ctx.get("plan") or {}
        kind, name = "signal", ctx["setup"]
    else:
        return None
    if plan.get("signal") not in ("long", "short"):
        return None
    # Карантин. Сетап без отработанного дня наблюдения понижается до наблюдения,
    # каким бы ни был бэктест. См. SETUP_LIVE_SINCE и историю VTBR 31.07.
    if kind == "signal" and not setup_is_live(name):
        kind = "observation"
    return {
        "ticker": ticker, "setup": name, "kind": kind, "signal": plan["signal"],
        "entry": plan.get("entry"), "stop": plan.get("stop_loss"),
        "target": plan.get("take_profit"), "rr": plan.get("risk_reward"),
        "reason": plan.get("reason") or ctx.get("note"),
        "width_atr": plan.get("width_atr"), "vol_x": plan.get("vol_x"),
        "price": ctx.get("price"), "vwap": ctx.get("vwap"),
        "source": ctx.get("source"), "age_min": ctx.get("age_min"),
        "at": datetime.now(timezone.utc).isoformat(),
    }


async def _record(fire: dict) -> None:
    """Положить срабатывание в базу знаний: событие должно быть проверяемым.

    Наблюдения и сигналы пишутся РАЗНЫМИ видами событий. Иначе через месяц нельзя
    будет отделить «что система предлагала торговать» от «что она просто заметила»,
    и статистика смешает два разных вопроса.
    """
    try:
        from src import db
        kind = ("setup_observation" if fire.get("kind") == "observation" else "setup")
        await db.add_event({
            "source": "setup_watch", "kind": kind, "ticker": fire["ticker"],
            "channel": fire["setup"], "text": fire.get("reason"),
            "payload": fire,
        })
    except Exception as e:                           # noqa: BLE001
        logger.debug(f"setup_watch add_event: {e}")


async def evaluate_observations(after_min: Optional[int] = None) -> dict:
    """Посчитать исход наблюдений, которым уже пора разрешиться.

    Без этого пункт «копить месяц и решить по факту» невыполним: наблюдения лежали бы
    в базе как записи о моменте, но без результата. Считаем так же, как в бэктесте —
    путём по свечам: сначала стоп или сначала цель, при попадании обоих в одну свечу
    считаем стоп (осторожная сторона).

    Идемпотентно: наблюдение с уже посчитанным исходом пропускается.
    """
    from src import db
    from src.agent import intraday_analyst as ia
    if after_min is None:
        try:
            from config.settings import SETUP_OUTCOME_AFTER_MIN as after_min
        except Exception:                            # noqa: BLE001
            after_min = 90

    obs = await db.recent_events(source="setup_watch", kind="setup_observation",
                                since_minutes=7 * 24 * 60, limit=500)
    done = await db.recent_events(source="setup_watch", kind="setup_outcome",
                                  since_minutes=7 * 24 * 60, limit=500)
    seen = set()
    for e in done:
        pl = e.get("payload") or {}
        if isinstance(pl, str):
            try:
                import json as _j
                pl = _j.loads(pl)
            except Exception:
                pl = {}
        if pl.get("ref"):
            seen.add(pl["ref"])

    now = datetime.now(timezone.utc)
    counted = 0
    for e in obs:
        pl = e.get("payload") or {}
        if isinstance(pl, str):
            try:
                import json as _j
                pl = _j.loads(pl)
            except Exception:
                continue
        ref = f"{pl.get('ticker')}:{pl.get('at')}"
        if not pl.get("at") or ref in seen:
            continue
        try:
            born = datetime.fromisoformat(str(pl["at"]).replace("Z", "+00:00"))
        except Exception:
            continue
        if (now - born) < timedelta(minutes=after_min):
            continue                                 # ещё рано, пусть разрешится

        entry, stop, target = pl.get("entry"), pl.get("stop"), pl.get("target")
        if not all(isinstance(x, (int, float)) for x in (entry, stop, target)):
            continue
        data = await ia.fetch_intraday(pl["ticker"], tf_min=5)
        if not data or not data.get("close"):
            continue
        H, L, D = data["high"], data["low"], data["dates"]
        up = pl.get("signal") == "long"
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        res, hit = None, "open"
        for i, ts in enumerate(D):
            try:
                t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                continue
            if t <= born:
                continue
            if (up and L[i] <= stop) or (not up and H[i] >= stop):
                res, hit = -1.0, "stop"
                break
            if (up and H[i] >= target) or (not up and L[i] <= target):
                res, hit = round(abs(target - entry) / risk, 2), "target"
                break
        if res is None:
            last = data["close"][-1]
            res = round(((last - entry) if up else (entry - last)) / risk, 2)
            hit = "open"
        await db.add_event({
            "source": "setup_watch", "kind": "setup_outcome",
            "ticker": pl["ticker"], "channel": pl.get("setup"),
            "text": f"{hit}: {res:+.2f}R",
            "payload": {"ref": ref, "setup": pl.get("setup"), "signal": pl.get("signal"),
                        "r": res, "hit": hit, "entry": entry, "stop": stop,
                        "target": target, "observed_at": pl.get("at"),
                        "evaluated_at": now.isoformat()},
        })
        counted += 1
    return {"evaluated": counted}


async def stats(days: int = 30) -> dict:
    """Живая статистика наблюдений: ради этого они и копятся.

    Бэктест на 181 дне дал лонговой стороне -0.113R после издержек. Живые числа
    нужны, чтобы решение включать сетап сигналом опиралось на факт, а не на историю
    одного режима.
    """
    from src import db
    rows = await db.recent_events(source="setup_watch", kind="setup_outcome",
                                  since_minutes=days * 24 * 60, limit=2000)
    by: dict = {}
    for e in rows:
        pl = e.get("payload") or {}
        if isinstance(pl, str):
            try:
                import json as _j
                pl = _j.loads(pl)
            except Exception:
                continue
        key = f"{pl.get('setup')}/{pl.get('signal')}"
        b = by.setdefault(key, {"n": 0, "sum_r": 0.0, "target": 0, "stop": 0, "open": 0})
        b["n"] += 1
        b["sum_r"] += float(pl.get("r") or 0)
        b[pl.get("hit", "open")] = b.get(pl.get("hit", "open"), 0) + 1
    out = {}
    for k, b in by.items():
        n = b["n"]
        out[k] = {"наблюдений": n,
                  "ожидание_R": round(b["sum_r"] / n, 3) if n else None,
                  "цель": b.get("target", 0), "стоп": b.get("stop", 0),
                  "не_разрешилось": b.get("open", 0),
                  "доля_успеха": (round(b.get("target", 0) /
                                        max(1, b.get("target", 0) + b.get("stop", 0)), 2))}
    return {"окно_дней": days, "по_сетапам": out,
            "оговорка": ("бэктест на 181 дне дал лонговой стороне -0.113R после "
                         "издержек; живые числа нужны для решения по факту")}


async def one_pass() -> list:
    """Один проход по списку наблюдения. Возвращает список срабатываний."""
    interval, dedup_min, pacing, tickers = _cfg()
    fires = []
    _status["checked"] = 0
    for t in tickers:
        try:
            fire = await check_ticker(t)
            _status["checked"] += 1
            if fire and not _is_duplicate(t, fire["setup"], dedup_min):
                fires.append(fire)
                _recent.append(fire)
                await _record(fire)
                logger.info(f"⚡ сетап {fire['setup']} по {t}: вход {fire['entry']} "
                            f"стоп {fire['stop']} цель {fire['target']} R/R {fire['rr']}")
        except Exception as e:                       # noqa: BLE001
            _status["errors"] += 1
            _status["last_error"] = f"{t}: {type(e).__name__}: {str(e)[:80]}"
        await asyncio.sleep(pacing)
    _status["passes"] += 1
    _status["fired"] += len(fires)
    _status["last_pass"] = datetime.now(timezone.utc).isoformat()
    _status["next_pass"] = (datetime.now(timezone.utc)
                            + timedelta(minutes=interval)).isoformat()
    return fires


async def _loop() -> None:
    interval, *_ = _cfg()
    _status["interval_min"] = interval

    # ЗАДЕРЖКА ПЕРЕД ПЕРВЫМ ПРОХОДОМ. Проход — это 48 бумаг и около двух сотен
    # исходящих запросов за 18 секунд. Если запустить его в момент старта
    # контейнера, он конкурирует с healthcheck (curl /api/stats, таймаут 10 сек,
    # три попытки), а также с наполнением снимков рынка. Коллектор в это время и
    # без того поднимает FIGI по 48 бумагам, потому что рукописную таблицу мы
    # убрали. Ждём, пока приложение встанет.
    try:
        from config.settings import SETUP_WATCH_WARMUP_SEC as _warm
    except Exception:                                # noqa: BLE001
        _warm = 45
    if _warm > 0:
        _status["next_pass"] = (datetime.now(timezone.utc)
                                + timedelta(seconds=_warm)).isoformat()
        logger.info(f"⚡ наблюдатель ждёт {_warm} сек до первого прохода, "
                    f"чтобы не мешать старту приложения")
        await asyncio.sleep(_warm)

    while _status["enabled"]:
        try:
            from src.analysis.intraday import session_phase
            msk = datetime.now(timezone.utc) + timedelta(hours=3)
            phase = session_phase(msk.hour * 60 + msk.minute, msk.weekday())
            if phase in ("morning", "main", "evening"):
                await one_pass()
                # Исходы считаем тем же проходом: это тоже бесплатно, а без них
                # «копить месяц и решить по факту» невыполнимо.
                try:
                    await evaluate_observations()
                except Exception as e:               # noqa: BLE001
                    logger.debug(f"оценка наблюдений: {e}")
            else:
                _status["last_pass"] = None
                _status["next_pass"] = (datetime.now(timezone.utc)
                                        + timedelta(minutes=interval)).isoformat()
        except Exception as e:                       # noqa: BLE001
            _status["errors"] += 1
            _status["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            logger.warning(f"setup_watch: {e}")
        await asyncio.sleep(max(60, interval * 60))
    _status["running"] = False


def start(interval_min: Optional[int] = None) -> dict:
    if _status["running"]:
        return status()
    if interval_min:
        _status["interval_min"] = int(interval_min)
    _status["enabled"] = True
    _status["running"] = True
    asyncio.create_task(_loop())
    logger.info(f"⚡ наблюдатель сетапов запущен, интервал "
                f"{_status['interval_min']} мин, БЕЗ Claude")
    return status()


def stop() -> dict:
    _status["enabled"] = False
    return status()
