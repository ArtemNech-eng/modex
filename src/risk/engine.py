"""Risk Engine: размер позиции и жёсткие лимиты.

ПРИНЦИП. Claude предлагает сценарий, движок решает размер и право на сделку.
Ни одно число здесь не может быть переопределено промптом или ответом модели —
это отдельный модуль с приоритетом над решением агента. Причина простая:
модель ошибается, и стоимость её ошибки должна быть ограничена конструкцией
системы, а не её доброй волей.

ПОЧЕМУ ДВА ОГРАНИЧИТЕЛЯ, А НЕ ОДИН. Риск в процентах от счёта сам по себе НЕ
ограничивает экспозицию. На реальном примере (вход 315.20, стоп 313.90 —
риск 0.41% от цены) при риске 1.33% от счёта размер выходит 322% счёта, то есть
требует плеча 3.2x. Поэтому лимит риска и лимит экспозиции работают независимо,
и связывает тот, который строже. При узких интрадей-стопах это почти всегда
экспозиция, и фактический риск оказывается НИЖЕ целевого — так и должно быть.

ЧЕГО ЗДЕСЬ СОЗНАТЕЛЬНО НЕТ. Лимит на сектор и округление до лота требуют
данных, которых в проекте нет: справочника секторов и лотности MOEX. Движок не
делает вид, что защищает по этим правилам — он возвращает признак
`sector_limit_active=False` и сообщает, что лотность принята за 1. Ложная
защита опаснее отсутствующей, потому что на неё полагаются.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_STATE_KEY = "risk_state"

# Коды причин — в стиле db.ATTEMPT_REASONS, чтобы попадали в журнал попыток
# и было видно, какое именно ограничение съедает сделки.
REASONS = {
    "risk_ok":              "риск одобрен",
    "risk_no_levels":       "нет входа или стопа — размер не считается",
    "risk_stop_wrong_side": "стоп по неверную сторону от входа",
    "risk_zero_size":       "размер вышел нулевым (стоп слишком далеко или лимит мал)",
    "risk_daily_loss":      "достигнут дневной лимит убытка",
    "risk_weekly_loss":     "достигнут недельный лимит убытка",
    "risk_max_trades":      "достигнут дневной лимит числа сделок",
    "risk_max_positions":   "достигнут лимит одновременных позиций",
    "risk_sector_limit":    "лимит позиций в одном секторе",
    "risk_kill_switch":     "kill switch: просадка от пика",
    "risk_exposure_full":   "суммарная экспозиция исчерпана",
    "risk_spread_too_wide": "стоп уже спреда — выбьет одним спредом, а не движением",
    "risk_book_too_thin":   "стакан не переварит позицию даже минимального размера",
}


# ─────────────────────────────── конфиг ──────────────────────────────────────

@dataclass(frozen=True)
class RiskConfig:
    """Все числа риска в одном месте. Меняются только здесь или через .env."""

    account_rub: float = 200_000.0
    # Фаза обучения: пока ожидание в R не измерено, размер ставки должен быть
    # минимальным из достаточных для набора статистики. Симуляция на 20 000
    # прогонов: при нулевом эйдже 0.25% даёт медиану −0.3% за год и НУЛЕВОЙ шанс
    # потерять половину счёта, а 1.33% — медианную просадку 38% и 6.6% шанс
    # потери половины. Асимметрия и есть причина выбора.
    risk_per_trade_pct: float = 0.25
    max_position_pct: float = 25.0        # экспозиция на одну позицию
    max_total_exposure_pct: float = 50.0  # суммарная экспозиция
    daily_loss_limit_r: float = 3.0
    weekly_loss_limit_r: float = 6.0
    max_trades_per_day: int = 3
    max_open_positions: int = 2
    max_per_sector: int = 1
    kill_switch_dd_pct: float = 5.0       # просадка от пика капитала

    # ── ЛИКВИДНОСТЬ ──────────────────────────────────────────────────────────
    # Тонкие бумаги дают самые крупные проценты (15% за день на малом капе
    # бывает, на Сбере нет), поэтому вселенную сужать не нужно. Но тонкий стакан
    # ломает арифметику риска: движок считает риск как shares × (вход − стоп),
    # предполагая выход ПО стопу. На тонком рынке выход происходит хуже, и
    # заявленные 0.25% превращаются в 1%+. Поэтому не исключаем бумагу, а режем
    # размер до того, что стакан реально переварит.
    min_stop_to_spread: float = 3.0   # стоп должен быть не уже 3 спредов
    max_depth_fraction: float = 10.0  # позиция ≤ 10% лотов у середины стакана

    @property
    def risk_rub(self) -> float:
        return self.account_rub * self.risk_per_trade_pct / 100.0

    @property
    def max_position_rub(self) -> float:
        return self.account_rub * self.max_position_pct / 100.0

    @property
    def max_total_exposure_rub(self) -> float:
        return self.account_rub * self.max_total_exposure_pct / 100.0


def load_config() -> RiskConfig:
    """Конфиг из окружения с безопасными значениями по умолчанию."""
    def _f(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, default))
        except (TypeError, ValueError):
            logger.warning("RiskEngine: %s не число, беру %s", name, default)
            return float(default)

    def _i(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, default))
        except (TypeError, ValueError):
            logger.warning("RiskEngine: %s не число, беру %s", name, default)
            return int(default)

    return RiskConfig(
        account_rub=_f("RISK_ACCOUNT_RUB", 200_000.0),
        risk_per_trade_pct=_f("RISK_PER_TRADE_PCT", 0.25),
        max_position_pct=_f("RISK_MAX_POSITION_PCT", 25.0),
        max_total_exposure_pct=_f("RISK_MAX_TOTAL_EXPOSURE_PCT", 50.0),
        daily_loss_limit_r=_f("RISK_DAILY_LOSS_R", 3.0),
        weekly_loss_limit_r=_f("RISK_WEEKLY_LOSS_R", 6.0),
        max_trades_per_day=_i("RISK_MAX_TRADES_DAY", 3),
        max_open_positions=_i("RISK_MAX_OPEN_POSITIONS", 2),
        max_per_sector=_i("RISK_MAX_PER_SECTOR", 1),
        kill_switch_dd_pct=_f("RISK_KILL_SWITCH_DD_PCT", 5.0),
    )


# ──────────────────────────── состояние дня ──────────────────────────────────

@dataclass
class RiskState:
    """Текущее состояние риска. Считается из фактов, не из предположений.

    `realized_r_today` и `realized_r_week` — сумма фактического R по закрытым
    сделкам. В советническом режиме (исполняет человек) это R по закрытым
    сценариям, посчитанный тем же оценщиком, что и для исполненных: иначе
    сравнение «принято против отклонено» будет смещённым.
    """

    realized_r_today: float = 0.0
    realized_r_week: float = 0.0
    trades_today: int = 0
    open_positions: int = 0
    open_exposure_rub: float = 0.0
    equity_peak_rub: Optional[float] = None
    equity_now_rub: Optional[float] = None
    open_sectors: list = field(default_factory=list)

    @property
    def drawdown_pct(self) -> Optional[float]:
        if not self.equity_peak_rub or not self.equity_now_rub:
            return None
        if self.equity_peak_rub <= 0:
            return None
        return max(0.0, (self.equity_peak_rub - self.equity_now_rub)
                   / self.equity_peak_rub * 100.0)


# ──────────────────────────── решение движка ─────────────────────────────────

@dataclass
class RiskDecision:
    approved: bool
    reason: str                      # код из REASONS
    detail: str = ""
    shares: int = 0
    notional_rub: float = 0.0
    risk_rub: float = 0.0
    risk_pct_of_account: float = 0.0
    binding_constraint: str = ""     # risk | exposure | total_exposure | lot
    r_per_share: float = 0.0
    lot_size: int = 1
    sector_limit_active: bool = False
    # Ликвидность: активна только при наличии данных стакана. Если данных нет,
    # флаг False — движок не делает вид, что проверил тонкий рынок.
    liquidity_active: bool = False
    spread_pct: Optional[float] = None
    depth_near_mid: Optional[int] = None
    stop_to_spread: Optional[float] = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["reason_label"] = REASONS.get(self.reason, self.reason)
        return d


# ──────────────────────────── размер позиции ─────────────────────────────────

def size_position(entry: Optional[float], stop: Optional[float],
                  direction: str, cfg: RiskConfig,
                  lot_size: int = 1,
                  available_exposure_rub: Optional[float] = None,
                  spread_pct: Optional[float] = None,
                  depth_near_mid: Optional[int] = None) -> RiskDecision:
    """Чистая функция: сколько акций брать. Без БД и без сети — легко тестируется.

    Возвращает НАИМЕНЬШИЙ размер из допустимых по риску, экспозиции и глубине
    стакана. Округление лотов — всегда ВНИЗ: превысить целевой риск нельзя.

    ЛИКВИДНОСТЬ. `spread_pct` и `depth_near_mid` приходят из снимка стакана
    (tinkoff_client: spread в %, глубина в ЛОТАХ в пределах ±0.5% от середины).
    Если их нет, проверки не выполняются и `liquidity_active=False` — движок не
    делает вид, что проверил тонкий рынок.

    Единицы: глубина в лотах, размер в акциях, поэтому сравнение идёт через
    lot_size. Пока справочника лотности MOEX нет и lot_size=1, одна акция
    считается одним лотом. Когда лотность появится, обе стороны должны
    использовать её — иначе глубина будет завышена в lot_size раз.
    """
    if not entry or not stop or entry <= 0 or stop <= 0:
        return RiskDecision(False, "risk_no_levels",
                            "нужны и вход, и стоп: без стопа риск не определён",
                            lot_size=lot_size)

    d = (direction or "").lower()
    if d in ("up", "long", "buy") and stop >= entry:
        return RiskDecision(False, "risk_stop_wrong_side",
                            f"long, но стоп {stop} не ниже входа {entry}",
                            lot_size=lot_size)
    if d in ("down", "short", "sell") and stop <= entry:
        return RiskDecision(False, "risk_stop_wrong_side",
                            f"short, но стоп {stop} не выше входа {entry}",
                            lot_size=lot_size)

    r_per_share = abs(entry - stop)
    if r_per_share <= 0:
        return RiskDecision(False, "risk_stop_wrong_side",
                            "вход и стоп совпадают", lot_size=lot_size)

    liquidity_active = spread_pct is not None or depth_near_mid is not None
    stop_pct = r_per_share / entry * 100.0
    stop_to_spread = (round(stop_pct / spread_pct, 2)
                      if spread_pct else None)

    # ── ЛИКВИДНОСТЬ, проверка 1: стоп против спреда ──────────────────────────
    # Стоп в 0.4% при спреде 0.3% выбьет одним спредом, без всякого движения
    # цены. Это не вопрос размера — такой стоп неисполним при любом объёме,
    # поэтому отказ, а не уменьшение.
    if spread_pct and stop_pct < cfg.min_stop_to_spread * spread_pct:
        return RiskDecision(
            False, "risk_spread_too_wide",
            f"стоп {stop_pct:.2f}% против спреда {spread_pct:.2f}% "
            f"(нужно ≥ {cfg.min_stop_to_spread}× спреда): выбьет спредом, "
            "а не движением",
            r_per_share=r_per_share, lot_size=lot_size,
            liquidity_active=True, spread_pct=spread_pct,
            depth_near_mid=depth_near_mid, stop_to_spread=stop_to_spread)

    # 1) по риску
    shares_by_risk = cfg.risk_rub / r_per_share
    # 2) по экспозиции на одну позицию
    shares_by_position = cfg.max_position_rub / entry
    # 3) по остатку суммарной экспозиции
    room = (cfg.max_total_exposure_rub if available_exposure_rub is None
            else available_exposure_rub)
    if room <= 0:
        return RiskDecision(False, "risk_exposure_full",
                            "суммарная экспозиция уже исчерпана",
                            r_per_share=r_per_share, lot_size=lot_size,
                            liquidity_active=liquidity_active,
                            spread_pct=spread_pct, depth_near_mid=depth_near_mid)
    shares_by_total = room / entry

    candidates = [(shares_by_risk, "risk"),
                  (shares_by_position, "exposure"),
                  (shares_by_total, "total_exposure")]

    # ── ЛИКВИДНОСТЬ, проверка 2: глубина стакана ─────────────────────────────
    # Позиция не должна быть значимой частью книги: иначе вы сами и есть стакан,
    # и войдёте с проскальзыванием, а выйдете ещё хуже. Здесь именно УМЕНЬШАЕМ
    # размер, а не отказываем — так тонкая бумага остаётся доступной, просто
    # меньшим объёмом.
    if depth_near_mid is not None:
        shares_by_depth = depth_near_mid * (cfg.max_depth_fraction / 100.0) * max(1, lot_size)
        candidates.append((shares_by_depth, "depth"))

    raw = min(c[0] for c in candidates)
    binding = min(candidates, key=lambda t: t[0])[1]

    lot = max(1, int(lot_size))
    lots = int(raw // lot)          # только вниз
    shares = lots * lot
    if shares <= 0:
        # Различаем «стакан слишком тонкий» и «стоп слишком далеко»: это разные
        # проблемы, и в журнале они должны выглядеть по-разному, иначе непонятно,
        # что калибровать — порог глубины или уровни сделки.
        if binding == "depth":
            return RiskDecision(
                False, "risk_book_too_thin",
                f"у середины стакана {depth_near_mid} лотов, допустимая доля "
                f"{cfg.max_depth_fraction:.0f}% не даёт даже одного лота",
                r_per_share=r_per_share, lot_size=lot,
                binding_constraint="depth", liquidity_active=True,
                spread_pct=spread_pct, depth_near_mid=depth_near_mid,
                stop_to_spread=stop_to_spread)
        return RiskDecision(
            False, "risk_zero_size",
            f"на {cfg.risk_rub:.0f}₽ риска при {r_per_share:.2f}₽/акцию "
            f"и лоте {lot} не набирается ни одного лота",
            r_per_share=r_per_share, lot_size=lot,
            binding_constraint="lot" if raw >= 1 else binding,
            liquidity_active=liquidity_active, spread_pct=spread_pct,
            depth_near_mid=depth_near_mid, stop_to_spread=stop_to_spread)

    notional = shares * entry
    risk_rub = shares * r_per_share
    return RiskDecision(
        True, "risk_ok",
        f"{shares} шт, риск {risk_rub:.0f}₽, экспозиция {notional:,.0f}₽; "
        f"ограничивает: {binding}",
        shares=shares, notional_rub=notional, risk_rub=risk_rub,
        risk_pct_of_account=risk_rub / cfg.account_rub * 100.0,
        binding_constraint=binding, r_per_share=r_per_share, lot_size=lot,
        liquidity_active=liquidity_active, spread_pct=spread_pct,
        depth_near_mid=depth_near_mid, stop_to_spread=stop_to_spread)


# ──────────────────────────── жёсткие лимиты ─────────────────────────────────

def check_limits(state: RiskState, cfg: RiskConfig,
                 sector: Optional[str] = None,
                 sector_map_available: bool = False) -> RiskDecision:
    """Проверка запретов ДО расчёта размера. Порядок — от самого опасного."""
    dd = state.drawdown_pct
    if dd is not None and dd >= cfg.kill_switch_dd_pct:
        return RiskDecision(False, "risk_kill_switch",
                            f"просадка {dd:.1f}% >= {cfg.kill_switch_dd_pct}% "
                            "— торговля остановлена до ручного разбора")

    if state.realized_r_today <= -abs(cfg.daily_loss_limit_r):
        return RiskDecision(False, "risk_daily_loss",
                            f"дневной убыток {state.realized_r_today:+.2f}R "
                            f"достиг лимита −{cfg.daily_loss_limit_r}R")

    if state.realized_r_week <= -abs(cfg.weekly_loss_limit_r):
        return RiskDecision(False, "risk_weekly_loss",
                            f"недельный убыток {state.realized_r_week:+.2f}R "
                            f"достиг лимита −{cfg.weekly_loss_limit_r}R")

    if state.trades_today >= cfg.max_trades_per_day:
        return RiskDecision(False, "risk_max_trades",
                            f"сделок сегодня {state.trades_today} из "
                            f"{cfg.max_trades_per_day}")

    if state.open_positions >= cfg.max_open_positions:
        return RiskDecision(False, "risk_max_positions",
                            f"открытых позиций {state.open_positions} из "
                            f"{cfg.max_open_positions}")

    # Лимит по сектору работает ТОЛЬКО при наличии справочника секторов.
    # Без него честно сообщаем, что правило неактивно, вместо имитации защиты.
    if sector_map_available and sector:
        same = sum(1 for s in state.open_sectors if s == sector)
        if same >= cfg.max_per_sector:
            return RiskDecision(False, "risk_sector_limit",
                                f"в секторе {sector} уже {same} позиций")

    return RiskDecision(True, "risk_ok", "лимиты не нарушены",
                        sector_limit_active=bool(sector_map_available))


def evaluate_trade(entry: Optional[float], stop: Optional[float],
                   direction: str, state: RiskState,
                   cfg: Optional[RiskConfig] = None,
                   lot_size: int = 1,
                   sector: Optional[str] = None,
                   sector_map_available: bool = False,
                   spread_pct: Optional[float] = None,
                   depth_near_mid: Optional[int] = None) -> RiskDecision:
    """Единая точка входа: сначала запреты, потом размер с учётом стакана."""
    cfg = cfg or load_config()
    gate = check_limits(state, cfg, sector, sector_map_available)
    if not gate.approved:
        return gate

    room = max(0.0, cfg.max_total_exposure_rub - max(0.0, state.open_exposure_rub))
    out = size_position(entry, stop, direction, cfg, lot_size, room,
                        spread_pct=spread_pct, depth_near_mid=depth_near_mid)
    out.sector_limit_active = bool(sector_map_available)
    return out


def liquidity_from_orderbook(ob: Optional[dict]) -> tuple:
    """Достать (spread_pct, depth_near_mid) из снимка стакана tinkoff_client.

    Отдельная функция, потому что источник может отсутствовать или устареть, и
    решение «проверять ликвидность или честно сказать, что не проверяли» должно
    приниматься в одном месте.
    """
    if not isinstance(ob, dict):
        return None, None
    sp = ob.get("spread_pct")
    dp = ob.get("depth_near_mid")
    try:
        sp = float(sp) if sp is not None else None
    except (TypeError, ValueError):
        sp = None
    try:
        dp = int(dp) if dp is not None else None
    except (TypeError, ValueError):
        dp = None
    # Спред 0 бывает при неполном снимке — не считаем это идеальной ликвидностью.
    if sp is not None and sp <= 0:
        sp = None
    return sp, dp


# ──────────────────────── состояние из БД (МСК-сутки) ────────────────────────

def _msk_day() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")


def _msk_week() -> str:
    d = datetime.now(timezone.utc) + timedelta(hours=3)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _msk(dt: datetime) -> datetime:
    """Момент в МСК. Сутки и неделя риска считаются по бирже, а не по UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc) + timedelta(hours=3)


async def compute_paper_account(cfg: Optional[RiskConfig] = None) -> dict:
    """Виртуальный счёт: пересчёт с нуля по закрытым ПРИНЯТЫМ сделкам.

    ПОЧЕМУ ПЕРЕСЧЁТ, А НЕ ИНКРЕМЕНТ. Инкрементальное обновление состояния
    требует помнить, какие сделки уже учтены, иначе повторный проход оценщика
    удвоит убыток и ложно сработает kill switch. Полный пересчёт идемпотентен по
    построению и самовосстанавливается после любого сбоя. Сделок сотни, счёт
    мгновенный — экономить тут нечего, а цена ошибки высокая.

    ПОЧЕМУ ТОЛЬКО ПРИНЯТЫЕ. В советническом режиме деньгами двигают только те
    сделки, которые человек взял. Отклонённый убыточный сигнал не должен ни
    уменьшать виртуальный счёт, ни съедать дневной лимит убытка: вы его не
    брали. При этом ОЦЕНИВАЮТСЯ все сценарии — это нужно для измерения качества
    сигналов и сравнения человека с моделью, но к счёту отношения не имеет.

    P&L в рублях = realized_r × risk_rub, где risk_rub — фактический риск
    позиции, посчитанный Risk Engine на момент сигнала. У старых сделок его нет;
    такие считаются в статистике сделок, но счёт не двигают, и их число
    возвращается отдельно, чтобы расхождение не выглядело загадкой.
    """
    cfg = cfg or load_config()
    today, week = _msk_day(), _msk_week()
    try:
        from src import db
        rows = await db.accepted_closed_trades()
    except Exception as e:                       # noqa: BLE001
        logger.warning("RiskEngine: не удалось собрать виртуальный счёт: %s", e)
        out = accumulate_paper([], cfg.account_rub, today, week)
        out["error"] = str(e)[:200]
        return out
    return accumulate_paper(rows, cfg.account_rub, today, week)


def accumulate_paper(rows: list, start_rub: float,
                     today: str, week: str) -> dict:
    """Чистая арифметика виртуального счёта. Вынесена из БД-функции, чтобы
    покрывалась тестами: ошибка в подсчёте капитала дороже любой другой, потому
    что на него смотрит kill switch.

    Ожидает список словарей с realized_r, risk_rub и evaluated_at, уже
    упорядоченный по времени закрытия.
    """
    out = {
        "equity_rub": start_rub, "equity_peak_rub": start_rub,
        "start_rub": start_rub, "pnl_rub": 0.0, "pnl_pct": 0.0,
        "drawdown_pct": 0.0, "trades_total": 0, "trades_today": 0,
        "realized_r_total": 0.0, "realized_r_today": 0.0, "realized_r_week": 0.0,
        "wins": 0, "losses": 0, "skipped_no_size": 0,
        "day": today, "week": week,
    }
    equity = peak = float(start_rub)
    for r in rows or []:
        rr = r.get("realized_r")
        risk_rub = r.get("risk_rub")
        out["trades_total"] += 1
        if rr is None:
            continue
        out["realized_r_total"] += rr
        out["wins" if rr > 0 else "losses"] += 1
        closed = r.get("evaluated_at")
        if closed is not None:
            c = _msk(closed)
            if c.strftime("%Y-%m-%d") == today:
                out["trades_today"] += 1
                out["realized_r_today"] += rr
            y, w, _ = c.isocalendar()
            if f"{y}-W{w:02d}" == week:
                out["realized_r_week"] += rr
        if not risk_rub:
            # Сделка без рассчитанного размера (легаси) двигать счёт не может:
            # R без риска в рублях не переводится в деньги. Считаем её в
            # статистике и показываем счётчик, чтобы расхождение было объяснимо.
            out["skipped_no_size"] += 1
            continue
        equity += rr * risk_rub
        peak = max(peak, equity)

    out["equity_rub"] = round(equity, 2)
    out["equity_peak_rub"] = round(peak, 2)
    out["pnl_rub"] = round(equity - start_rub, 2)
    out["pnl_pct"] = round((equity / start_rub - 1) * 100, 3) if start_rub else 0.0
    out["drawdown_pct"] = (round(max(0.0, (peak - equity) / peak * 100), 3)
                           if peak > 0 else 0.0)
    for k in ("realized_r_total", "realized_r_today", "realized_r_week"):
        out[k] = round(out[k], 3)
    return out


async def load_state(cfg: Optional[RiskConfig] = None) -> RiskState:
    """Собрать состояние риска из БД. Пишется по образцу бюджет-гарда.

    Дневной и недельный R берутся по ЗАКРЫТЫМ прогнозам с заполненным
    realized_r. Пока уровни не заполняются и r_sample = 0, суммы будут
    нулевыми — это честный ноль, а не выдуманное значение.
    """
    cfg = cfg or load_config()
    state = RiskState()
    # Значения по умолчанию выставляем ДО обращения к БД: иначе при её
    # недоступности пик капитала остался бы None, drawdown_pct вернул бы None,
    # и kill switch молча стал бы неактивным. Защита не должна зависеть от того,
    # повезло ли с импортом.
    state.equity_peak_rub = cfg.account_rub
    state.equity_now_rub = cfg.account_rub

    # Накопительные лимиты (дневной, недельный, число сделок, kill switch) до
    # этого были ИНЕРТНЫ: состояние читалось, но никто его не заполнял, поэтому
    # realized_r_today всегда был 0, просадка 0, и ни один из них не мог
    # сработать. Тесты это не поймали, потому что подставляли состояние напрямую.
    # Теперь состояние выводится из виртуального счёта по закрытым принятым
    # сделкам — тем же пересчётом, что и paper trading.
    try:
        acc = await compute_paper_account(cfg)
    except Exception as e:                        # noqa: BLE001
        logger.warning("RiskEngine: состояние не собралось, лимиты неактивны: %s", e)
        return state

    state.equity_now_rub = float(acc.get("equity_rub") or cfg.account_rub)
    state.equity_peak_rub = float(acc.get("equity_peak_rub") or cfg.account_rub)
    state.realized_r_today = float(acc.get("realized_r_today") or 0.0)
    state.realized_r_week = float(acc.get("realized_r_week") or 0.0)
    state.trades_today = int(acc.get("trades_today") or 0)

    # Открытые позиции и связанная экспозиция — только по ПРИНЯТЫМ сценариям:
    # держим мы лишь то, что взяли, значит и лимиты концентрации считаются по ним.
    try:
        from src import db
        open_rows = await db.accepted_open_trades()
        state.open_positions = len(open_rows)
        state.open_exposure_rub = sum(
            float(r.get("notional_rub") or 0.0) for r in open_rows)
    except Exception as e:                        # noqa: BLE001
        logger.debug("RiskEngine: открытые позиции не прочитались: %s", e)
    return state


async def save_state(state: RiskState) -> None:
    try:
        from src import db
        payload = {
            "date": _msk_day(),
            "trades_today": state.trades_today,
            "realized_r_today": round(state.realized_r_today, 4),
            "realized_r_week": round(state.realized_r_week, 4),
            "equity_peak_rub": state.equity_peak_rub,
            "equity_now_rub": state.equity_now_rub,
        }
        await db.set_setting(_STATE_KEY, json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.warning("RiskEngine: не удалось сохранить состояние: %s", e)
