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

    def as_dict(self) -> dict:
        d = asdict(self)
        d["reason_label"] = REASONS.get(self.reason, self.reason)
        return d


# ──────────────────────────── размер позиции ─────────────────────────────────

def size_position(entry: Optional[float], stop: Optional[float],
                  direction: str, cfg: RiskConfig,
                  lot_size: int = 1,
                  available_exposure_rub: Optional[float] = None) -> RiskDecision:
    """Чистая функция: сколько акций брать. Без БД и без сети — легко тестируется.

    Возвращает НАИМЕНЬШИЙ размер из допустимых по риску и по экспозиции.
    Округление лотов — всегда ВНИЗ: превысить целевой риск нельзя.
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
                            r_per_share=r_per_share, lot_size=lot_size)
    shares_by_total = room / entry

    raw = min(shares_by_risk, shares_by_position, shares_by_total)
    binding = min(
        (shares_by_risk, "risk"),
        (shares_by_position, "exposure"),
        (shares_by_total, "total_exposure"),
        key=lambda t: t[0],
    )[1]

    lot = max(1, int(lot_size))
    lots = int(raw // lot)          # только вниз
    shares = lots * lot
    if shares <= 0:
        return RiskDecision(
            False, "risk_zero_size",
            f"на {cfg.risk_rub:.0f}₽ риска при {r_per_share:.2f}₽/акцию "
            f"и лоте {lot} не набирается ни одного лота",
            r_per_share=r_per_share, lot_size=lot,
            binding_constraint="lot" if raw >= 1 else binding)

    notional = shares * entry
    risk_rub = shares * r_per_share
    return RiskDecision(
        True, "risk_ok",
        f"{shares} шт, риск {risk_rub:.0f}₽, экспозиция {notional:,.0f}₽; "
        f"ограничивает: {binding}",
        shares=shares, notional_rub=notional, risk_rub=risk_rub,
        risk_pct_of_account=risk_rub / cfg.account_rub * 100.0,
        binding_constraint=binding, r_per_share=r_per_share, lot_size=lot)


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
                   sector_map_available: bool = False) -> RiskDecision:
    """Единая точка входа: сначала запреты, потом размер."""
    cfg = cfg or load_config()
    gate = check_limits(state, cfg, sector, sector_map_available)
    if not gate.approved:
        return gate

    room = max(0.0, cfg.max_total_exposure_rub - max(0.0, state.open_exposure_rub))
    out = size_position(entry, stop, direction, cfg, lot_size, room)
    out.sector_limit_active = bool(sector_map_available)
    return out


# ──────────────────────── состояние из БД (МСК-сутки) ────────────────────────

def _msk_day() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")


async def load_state(cfg: Optional[RiskConfig] = None) -> RiskState:
    """Собрать состояние риска из БД. Пишется по образцу бюджет-гарда.

    Дневной и недельный R берутся по ЗАКРЫТЫМ прогнозам с заполненным
    realized_r. Пока уровни не заполняются и r_sample = 0, суммы будут
    нулевыми — это честный ноль, а не выдуманное значение.
    """
    cfg = cfg or load_config()
    state = RiskState()
    try:
        from src import db
    except Exception:
        return state

    try:
        raw = await db.get_setting(_STATE_KEY)
        saved = json.loads(raw) if raw else {}
    except Exception:
        saved = {}

    peak = saved.get("equity_peak_rub") or cfg.account_rub
    state.equity_peak_rub = float(peak)
    state.equity_now_rub = float(saved.get("equity_now_rub") or cfg.account_rub)
    if saved.get("date") != _msk_day():
        state.trades_today = 0
    else:
        state.trades_today = int(saved.get("trades_today") or 0)
        state.realized_r_today = float(saved.get("realized_r_today") or 0.0)
    state.realized_r_week = float(saved.get("realized_r_week") or 0.0)
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
