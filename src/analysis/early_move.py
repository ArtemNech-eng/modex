"""
MOODEX — Early Move Detector (Ранний детектор зарождения движения)

Задача: обнаружить необычное изменение в цене, объёме, стакане или потоке
сделок РАНЬШЕ, чем движение станет очевидным на графике (до закрытия свечи).

НЕ даёт сигналов BUY / SELL.
НЕ прогнозирует направление.
ТОЛЬКО обнаруживает переход состояния:
    было спокойно →
    цена начинает ускоряться + оборот растёт + поток/стакан меняется →
    EARLY_MOVE_UP / EARLY_MOVE_DOWN

Также собирает эмпирическую статистику исходов:
    что происходило с ценой через 1, 3, 5, 15 и 30 минут после каждого события,
    чтобы будущие пороги можно было вывести из данных, а не угадывать.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

logger = logging.getLogger(__name__)


@dataclass
class EarlyMoveEvent:
    """Событие раннего обнаружения движения внутри минуты."""
    ticker: str
    detected_at: str                        # ISO timestamp UTC
    direction: str                          # EARLY_MOVE_UP | EARLY_MOVE_DOWN
    price: float                            # Текущая цена в моменте
    change_10s_pct: float                   # Изменение цены за 10 сек (%)
    change_30s_pct: float                   # Изменение цены за 30 сек (%)
    change_60s_pct: float                   # Изменение цены за 1 мин (%)
    turnover_rub: float                     # Оборот за последние 60 сек (₽)
    turnover_accel: float                   # Ускорение оборота (последние 15с vs предыдущие)
    buy_share: float                        # Доля покупок в потоке сделок [0, 1]
    sell_share: float                       # Доля продаж в потоке сделок [0, 1]
    delta_lots: int                         # Чистая дельта (покупки - продажи) в лотах
    bid_ask_change: str                     # Описание изменения ликвидности в стакане
    nearest_level: Optional[float]          # Ближайший ключевой уровень (сопротивление/поддержка)
    distance_to_level_pct: Optional[float]  # Расстояние до ближайшего уровня (%)
    what_changed: list[str]                 # Что именно изменилось за последние 10-30 сек
    event_id: int = 0
    # Отслеживание исходов через 1, 3, 5, 15, 30 минут
    after_1m_pct: Optional[float] = None
    after_3m_pct: Optional[float] = None
    after_5m_pct: Optional[float] = None
    after_15m_pct: Optional[float] = None
    after_30m_pct: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TickSnapshot:
    """Один снимок состояния тикера в субминутном буфере."""
    def __init__(
        self,
        ts_sec: float,
        price: float,
        turnover_rub: float,
        buy_lots: int,
        sell_lots: int,
        bid_vol: float,
        ask_vol: float,
    ):
        self.ts_sec = ts_sec
        self.price = price
        self.turnover_rub = turnover_rub
        self.buy_lots = buy_lots
        self.sell_lots = sell_lots
        self.bid_vol = bid_vol
        self.ask_vol = ask_vol


class EarlyMoveDetector:
    """
    Детектор раннего зарождения движения в реальном времени.

    Хранит скользящее окно 60 секунд по каждому тикеру и обнаруживает
    переход состояния (ускорение цены + приток оборота + сдвиг баланса).
    """

    def __init__(self, cooldown_sec: float = 180.0):
        self.cooldown_sec = cooldown_sec
        self._buffers: dict[str, deque[TickSnapshot]] = {}
        self._last_fire_ts: dict[str, float] = {}
        self._events: list[EarlyMoveEvent] = []
        self._event_counter = 0

    def clear(self):
        self._buffers.clear()
        self._last_fire_ts.clear()
        self._events.clear()
        self._event_counter = 0

    def add_snapshot(
        self,
        ticker: str,
        price: float,
        turnover_rub: float = 0.0,
        buy_lots: int = 0,
        sell_lots: int = 0,
        bid_vol: float = 0.0,
        ask_vol: float = 0.0,
        ts_sec: Optional[float] = None,
        nearest_level: Optional[float] = None,
    ) -> Optional[EarlyMoveEvent]:
        """
        Добавить текущий снимок тикера и проверить переход состояния.
        Вызывается при каждом пакете из потока (субминутно).
        """
        tk = ticker.upper()
        now = ts_sec or datetime.now(timezone.utc).timestamp()

        # Обновляем исходы для ранее созданных событий по этому тикеру
        self._update_outcomes(tk, now, price)

        if tk not in self._buffers:
            self._buffers[tk] = deque(maxlen=300)
        buf = self._buffers[tk]

        snap = TickSnapshot(
            ts_sec=now,
            price=price,
            turnover_rub=turnover_rub,
            buy_lots=buy_lots,
            sell_lots=sell_lots,
            bid_vol=bid_vol,
            ask_vol=ask_vol,
        )
        buf.append(snap)

        # Очищаем старые снимки старше 65 секунд
        while buf and (now - buf[0].ts_sec) > 65.0:
            buf.popleft()

        if len(buf) < 4:
            return None

        # Проверка защиты от частого повтора (cooldown)
        last_fire = self._last_fire_ts.get(tk, 0.0)
        if (now - last_fire) < self.cooldown_sec:
            return None

        # Оцениваем изменение скорости и состояния
        ev = self._evaluate_transition(tk, buf, now, nearest_level)
        if ev:
            self._event_counter += 1
            ev.event_id = self._event_counter
            self._last_fire_ts[tk] = now
            self._events.append(ev)
            # Храним последние 1000 событий в памяти
            if len(self._events) > 1000:
                self._events.pop(0)
        return ev

    def _get_snapshot_at(self, buf: deque[TickSnapshot], now_sec: float, lag_sec: float) -> Optional[TickSnapshot]:
        """Найти снимок в буфере примерно `lag_sec` секунд назад."""
        target_ts = now_sec - lag_sec
        best: Optional[TickSnapshot] = None
        best_diff = 999.0
        for s in buf:
            diff = abs(s.ts_sec - target_ts)
            if diff < best_diff and diff <= 12.0:
                best = s
                best_diff = diff
        return best

    def _evaluate_transition(
        self,
        ticker: str,
        buf: deque[TickSnapshot],
        now_sec: float,
        nearest_level: Optional[float] = None,
    ) -> Optional[EarlyMoveEvent]:
        curr = buf[-1]
        s10 = self._get_snapshot_at(buf, now_sec, 10.0) or buf[0]
        s30 = self._get_snapshot_at(buf, now_sec, 30.0) or buf[0]
        s60 = self._get_snapshot_at(buf, now_sec, 60.0) or buf[0]

        if curr.price <= 0 or s10.price <= 0 or s30.price <= 0:
            return None

        ch10 = round((curr.price - s10.price) / s10.price * 100.0, 3)
        ch30 = round((curr.price - s30.price) / s30.price * 100.0, 3)
        ch60 = round((curr.price - s60.price) / s60.price * 100.0, 3)

        # Оборот и ускорение (последние 15с против предыдущих 45с)
        s15 = self._get_snapshot_at(buf, now_sec, 15.0) or buf[0]
        recent_turnover = max(0.0, curr.turnover_rub - s15.turnover_rub)
        prev_turnover = max(0.0, s15.turnover_rub - s60.turnover_rub)
        accel = round(recent_turnover / max(prev_turnover / 3.0, 1.0), 2)
        total_turnover = max(0.0, curr.turnover_rub - s60.turnover_rub)

        # Поток сделок (покупки vs продажи за последние 15-30с)
        buy_lots = max(0, curr.buy_lots - s15.buy_lots)
        sell_lots = max(0, curr.sell_lots - s15.sell_lots)
        total_lots = buy_lots + sell_lots
        buy_share = round(buy_lots / total_lots, 3) if total_lots > 0 else 0.5
        sell_share = round(sell_lots / total_lots, 3) if total_lots > 0 else 0.5
        delta_lots = buy_lots - sell_lots

        # Изменение стакана (ликвидность продавцов/покупателей)
        bid_change = curr.bid_vol - s30.bid_vol
        ask_change = curr.ask_vol - s30.ask_vol
        if ask_change < -500 and bid_change > 0:
            book_desc = "оффера быстро проедаются (уменьшение асков, рост бидов)"
        elif bid_change < -500 and ask_change > 0:
            book_desc = "биды быстро проедаются (уменьшение бидов, рост асков)"
        elif curr.ask_vol > curr.bid_vol * 1.5:
            book_desc = "перевес офферов в стакане"
        elif curr.bid_vol > curr.ask_vol * 1.5:
            book_desc = "перевес бидов в стакане"
        else:
            book_desc = "стакан сбалансирован"

        # Ближайший уровень
        dist_pct = None
        if nearest_level and nearest_level > 0:
            dist_pct = round(abs(curr.price - nearest_level) / curr.price * 100.0, 2)

        # ── Критерий зарождения движения (мультифакторный переход состояния) ──
        # Требуем одновременное совпадение минимум 3 из 4 признаков:
        # 1. Ускорение цены (быстрее тихого шума, но ДО закрытия свечи и ДО пробоев в 2%)
        # 2. Ускорение оборота (accel >= 1.5x или приток денег > 5 млн ₽ за минуту)
        # 3. Агрессивный перевес покупок/продаж в ленте (share >= 65%)
        # 4. Сдвиг в книге заявок или близость к уровню

        what_changed: list[str] = []
        direction: Optional[str] = None

        # Проверка EARLY_MOVE_UP
        up_score = 0
        if ch10 >= 0.08 or ch30 >= 0.15:
            up_score += 1
            what_changed.append(f"цена начала ускоряться вверх (+{ch30}% за 30с, +{ch10}% за 10с)")
        if accel >= 1.5 or recent_turnover >= 3_000_000:
            up_score += 1
            what_changed.append(f"оборот ускоряется ({accel}x от нормы, {round(total_turnover/1e6, 2)} млн ₽/мин)")
        if buy_share >= 0.65 and total_lots >= 10:
            up_score += 1
            what_changed.append(f"агрессивные покупки усиливаются (покупки {int(buy_share*100)}%, дельта +{delta_lots} лотов)")
        if "уменьшение асков" in book_desc or "перевес бидов" in book_desc:
            up_score += 1
            what_changed.append(f"стакан меняется: {book_desc}")
        elif dist_pct is not None and dist_pct <= 0.35:
            what_changed.append(f"цена подошла к уровню {nearest_level} (расстояние {dist_pct}%)")

        if up_score >= 3 and ch30 > 0.02:
            direction = "EARLY_MOVE_UP"

        # Проверка EARLY_MOVE_DOWN (если UP не сработал)
        if not direction:
            down_score = 0
            what_changed.clear()
            if ch10 <= -0.08 or ch30 <= -0.15:
                down_score += 1
                what_changed.append(f"цена начала ускоряться вниз ({ch30}% за 30с, {ch10}% за 10с)")
            if accel >= 1.5 or recent_turnover >= 3_000_000:
                down_score += 1
                what_changed.append(f"оборот ускоряется ({accel}x от нормы, {round(total_turnover/1e6, 2)} млн ₽/мин)")
            if sell_share >= 0.65 and total_lots >= 10:
                down_score += 1
                what_changed.append(f"агрессивные продажи усиливаются (продажи {int(sell_share*100)}%, дельта {delta_lots} лотов)")
            if "уменьшение бидов" in book_desc or "перевес офферов" in book_desc:
                down_score += 1
                what_changed.append(f"стакан меняется: {book_desc}")
            elif dist_pct is not None and dist_pct <= 0.35:
                what_changed.append(f"цена подошла к уровню {nearest_level} (расстояние {dist_pct}%)")

            if down_score >= 3 and ch30 < -0.02:
                direction = "EARLY_MOVE_DOWN"

        if not direction:
            return None

        iso_ts = datetime.fromtimestamp(now_sec, tz=timezone.utc).isoformat()
        return EarlyMoveEvent(
            ticker=ticker,
            detected_at=iso_ts,
            direction=direction,
            price=curr.price,
            change_10s_pct=ch10,
            change_30s_pct=ch30,
            change_60s_pct=ch60,
            turnover_rub=round(total_turnover, 2),
            turnover_accel=accel,
            buy_share=buy_share,
            sell_share=sell_share,
            delta_lots=delta_lots,
            bid_ask_change=book_desc,
            nearest_level=nearest_level,
            distance_to_level_pct=dist_pct,
            what_changed=what_changed,
        )

    def _update_outcomes(self, ticker: str, now_sec: float, current_price: float):
        """Обновляет статистику исходов через 1, 3, 5, 15, 30 мин после события."""
        for ev in reversed(self._events):
            if ev.ticker != ticker:
                continue
            try:
                ev_ts = datetime.fromisoformat(ev.detected_at).timestamp()
            except Exception:
                continue
            elapsed = now_sec - ev_ts
            if elapsed < 55.0:
                continue
            ch = round((current_price - ev.price) / ev.price * 100.0, 3)
            if 55.0 <= elapsed <= 90.0 and ev.after_1m_pct is None:
                ev.after_1m_pct = ch
            elif 175.0 <= elapsed <= 240.0 and ev.after_3m_pct is None:
                ev.after_3m_pct = ch
            elif 295.0 <= elapsed <= 380.0 and ev.after_5m_pct is None:
                ev.after_5m_pct = ch
            elif 895.0 <= elapsed <= 1000.0 and ev.after_15m_pct is None:
                ev.after_15m_pct = ch
            elif 1795.0 <= elapsed <= 2000.0 and ev.after_30m_pct is None:
                ev.after_30m_pct = ch

    def get_recent_events(self, ticker: Optional[str] = None, limit: int = 50) -> list[dict]:
        evs = [e.to_dict() for e in self._events if (not ticker or e.ticker == ticker.upper())]
        return evs[-limit:]

    def get_statistics(self) -> dict[str, Any]:
        """
        Эмпирическая статистика: что происходило через 1, 3, 5, 15, 30 минут
        после срабатывания EARLY_MOVE_UP и EARLY_MOVE_DOWN.
        """
        ups = [e for e in self._events if e.direction == "EARLY_MOVE_UP"]
        downs = [e for e in self._events if e.direction == "EARLY_MOVE_DOWN"]

        def _calc_stats(ev_list: list[EarlyMoveEvent], dir_sign: int) -> dict[str, Any]:
            res: dict[str, Any] = {"count": len(ev_list)}
            for horizon_name, attr in [
                ("after_1m", "after_1m_pct"),
                ("after_3m", "after_3m_pct"),
                ("after_5m", "after_5m_pct"),
                ("after_15m", "after_15m_pct"),
                ("after_30m", "after_30m_pct"),
            ]:
                vals = [getattr(e, attr) for e in ev_list if getattr(e, attr) is not None]
                if not vals:
                    res[horizon_name] = {"count": 0, "win_rate": 0.0, "avg_move_pct": 0.0}
                    continue
                # Считаем успех в направлении детектора
                wins = sum(1 for v in vals if (v * dir_sign) > 0.0)
                win_rate = round(wins / len(vals), 3)
                avg_move = round(sum(vals) / len(vals), 3)
                res[horizon_name] = {
                    "count": len(vals),
                    "win_rate": win_rate,
                    "avg_move_pct": avg_move,
                }
            return res

        return {
            "total_events": len(self._events),
            "UP": _calc_stats(ups, +1),
            "DOWN": _calc_stats(downs, -1),
        }


# Глобальный синглтон детектора раннего движения
DETECTOR = EarlyMoveDetector()
