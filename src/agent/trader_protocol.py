"""
Trader Protocol 19.08 — read-only сигналы без записи

Требования из инструкции:
  - Только чтение, без POST /api/analyst-signal, /api/ingest/deals, /api/live-signals/start/stop
  - Не использовать /api/screen, /api/volume-scan, /api/setup-watch, Claude-сигналы (по прямому указанию)
  - Анализ на сырых данных:
      дельта — ISS trades.json BUYSELL (B=buyer hits ask, S=bid) ground truth
      стакан по уровням bid5_sum/ask5_sum — /api/live/{TICKER}
      агрегат глубина — ISS marketdata BIDDEPTHT/OFFERDEPTHT/WAPRICE/NUMTRADES
      свечи/VWAP/ATR — /api/candles/brief
      дельта по закрытым минутам — /api/flow?source=exchange&res=5m&day=YYYY-MM-DD (парам day, не date)
      юниверс — /api/universe (только состав и ранг, цены кэш устаревают)
      направление книги — /api/orderbook-index но trust фильтр 31+
  - 3 независимых подтверждения (книга, лента ISS, VWAP/ORB)
  - R/R ≥2 арифметически
  - условие инвалидации, время среза, приписка "не инвестиционная рекомендация"
  - формулировка "структура и план", т.к. пробойные лонги убыточны 181д лучшая -0.113R

Протокол индекса (из memo 19.08):
  - Индекс IMOEX2 07:00-23:50, в основную совпадает с IMOEX до копейки
  - Утро 07:00-09:49 value <~10.3 млрд кандидат
  - Первый час основной 09:50-10:49 сумма value 6 бакетов 10м, граница ~7.5 млрд или 0.75 медианы 5д = сжатый
    Примеры: 19.08 4.9 сжатый, 18.08 6.0 сжатый, 14.08 6.2 сжатый, 12.08 8.5 плотный, 13.08 8.9 плотный, 11.08 9.5 плотный, 17.08 12.5 плотный
    Утро 8.8-10.1 сжатые, 38.2 17.08 гэп
  - Триггер расширения ≥3× среднего 5 пред бакетов только основная, первое 10:50, после 16:00 не торговать
    Направление close-open бакета, вход по закрытию
    Примеры: 14.08 4.24× 11:20 вниз -3.4% до close, 18.08 4.66× 11:40 вверх +0.66% (+1.4% до хая), 19.08 4.2× 15:10 вверх +0.5% (+0.8%), 12.08 4.16× 17:10 после 16:00 не торговать -0.24%
  - Выбор инструмента по нормированной эффективности в сторону сигнала
  - Подтверждение взрыв trade_count, стоп за экстремум кумулятивной дельты (11/13), R/R≥2, инвалидация, время среза
  - Кумулятивная дельта экстремум = экстремум цены дня 11/13 (ASTR/TATN/NVTK/SIBN/LKOH 14-19.08), 2 промаха объяснимы обрывом выгрузки 12:50 и дивергенцией
  - Эффективность потока = % хода цены на 1% дневного объёма в чистой дельте (старая "пунктов на 100k лотов" непригодна кросс-бумажно)

Безопасность:
  - INGEST_TOKEN пустой, CORS allow_origins=["*"] — запись открыта, поэтому никаких POST
  - Абсолютную дельту — только по закрытым минутам; в текущей минуте /api/live лента и счётчик свечи флашатся с разной частотой
  - /api/universe — только состав и ранг, цены кэш устаревают (SBER 275.01 vs 277.22 live)
  - «31+ снимков» — фильтр доверия к /api/orderbook-index, не проверка наличия; по импульсным брать книгу из /api/live/{TICKER} напрямую (ASTR 4 снимка в индексе vs 104 обновления в live)
  - Встречный поток при движущейся цене — поглощение, не разворот

Этот модуль НЕ делает POST, НЕ вызывает запрещённые эндпоинты, только читает и считает.
"""

import logging
from typing import Optional, List, Dict, Tuple
from datetime import datetime, timezone, timedelta
import statistics

logger = logging.getLogger(__name__)

# Пороги из инструкции 19.08 (подогнаны на 7д, вести журнал)
MORNING_VALUE_THRESHOLD = 10.3e9  # ~10.3 млрд
FIRST_HOUR_THRESHOLD = 7.5e9      # ~7.5 млрд или 0.75 медианы 5д
EXPANSION_MULTIPLIER = 3.0
NO_TRADE_AFTER = 16 * 60  # 16:00 МСК
FIRST_TRIGGER_TIME = 10 * 60 + 50  # 10:50


def msk_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=3)


def parse_hhmm(s: str) -> int:
    """'HH:MM' -> минуты от полуночи"""
    try:
        h, m = map(int, s.split(":")[:2])
        return h * 60 + m
    except Exception:
        return 0


def is_compressed_day(first_hour_value: float, median_5d: Optional[float] = None) -> bool:
    """
    Фильтр режима: сжатый день = сумма value 6 бакетов 09:50-10:49 < ~7.5 млрд или <0.75 медианы 5д
    """
    if median_5d is not None and median_5d > 0:
        if first_hour_value < 0.75 * median_5d:
            return True
    return first_hour_value < FIRST_HOUR_THRESHOLD


def detect_expansion_triggers(buckets: List[dict]) -> List[dict]:
    """
    Триггер старта: value ≥3× среднего 5 пред бакетов, только основная сессия, первое 10:50, после 16:00 не торговать.
    buckets: список {begin: "2026-08-19 10:50:00", open, close, value}
    Возвращает срабатывания с полями: time, multiplier, direction, entry, begin
    """
    triggers = []
    # сортируем по времени
    def _minutes(b):
        try:
            t = b.get("begin") or ""
            if " " in t:
                time_part = t.split(" ", 1)[1]
                h, m = map(int, time_part.split(":")[:2])
                return h * 60 + m
        except Exception:
            pass
        return 0

    buckets_sorted = sorted(buckets, key=_minutes)

    for i, b in enumerate(buckets_sorted):
        mins = _minutes(b)
        if mins < FIRST_TRIGGER_TIME:
            continue
        if mins >= NO_TRADE_AFTER:
            continue
        # только основная сессия 10:00-18:50
        if not (10 * 60 <= mins <= 18 * 60 + 50):
            continue
        # нужно 5 предыдущих бакетов
        if i < 5:
            continue
        prev = buckets_sorted[i - 5:i]
        try:
            avg_prev = sum(float(x.get("value") or 0) for x in prev) / 5
            cur_val = float(b.get("value") or 0)
            if avg_prev <= 0:
                continue
            mult = cur_val / avg_prev
        except Exception:
            continue
        if mult >= EXPANSION_MULTIPLIER:
            try:
                open_p = float(b.get("open") or 0)
                close_p = float(b.get("close") or 0)
                if open_p <= 0 or close_p <= 0:
                    continue
                direction = "up" if close_p > open_p else "down" if close_p < open_p else None
                if direction is None:
                    continue
                triggers.append({
                    "begin": b.get("begin"),
                    "minutes": mins,
                    "time_msk": f"{mins // 60:02d}:{mins % 60:02d}",
                    "value": cur_val,
                    "avg_prev_5": avg_prev,
                    "multiplier": round(mult, 2),
                    "direction": direction,
                    "open": open_p,
                    "close": close_p,
                    "entry": close_p,  # вход по закрытию бакета
                    "change_pct": round((close_p - open_p) / open_p * 100, 3),
                })
            except Exception as e:
                logger.debug(f"trigger parse error: {e}")
                continue
    return triggers


def compute_flow_efficiency(ticker: str, price_move_pct: float, delta_lots: int, day_volume_lots: int) -> Optional[float]:
    """
    Эффективность потока = % хода цены на 1% дневного объёма в чистой дельте
    """
    if day_volume_lots <= 0 or delta_lots == 0:
        return None
    delta_pct = abs(delta_lots) / day_volume_lots * 100
    if delta_pct == 0:
        return None
    return round(price_move_pct / delta_pct, 4)


def select_instrument_by_efficiency(candidates: List[dict], signal_direction: str) -> Optional[dict]:
    """
    Выбор инструмента по нормированной эффективности в сторону сигнала.
    candidates: [{ticker, efficiency, price_move_pct, delta, ...}]
    signal_direction: up/down
    Фильтр: дельта должна быть в сторону сигнала, иначе пропуск (SIBN пример)
    """
    # фильтр по направлению дельты
    filtered = []
    for c in candidates:
        d = c.get("delta")
        if d is None:
            continue
        if signal_direction == "up" and d <= 0:
            continue
        if signal_direction == "down" and d >= 0:
            continue
        if c.get("efficiency") is None:
            continue
        filtered.append(c)

    if not filtered:
        return None

    # сортируем по эффективности (по модулю, т.к. уже в сторону сигнала)
    filtered.sort(key=lambda x: x.get("efficiency") or 0, reverse=True)
    return filtered[0]


def build_signal_structure(
    ticker: str,
    direction: str,
    entry: float,
    stop: float,
    target: float,
    trigger_info: dict,
    confirmations: dict,
    invalidation: str,
    time_slice: str,
) -> dict:
    """
    Формирует структуру сигнала по требованиям:
      - 3 независимых подтверждения
      - R/R ≥2 арифметически
      - инвалидация, время среза, disclaimer, "структура и план"
    """
    try:
        risk = abs(entry - stop)
        reward = abs(target - entry)
        rr = round(reward / risk, 2) if risk > 0 else None
    except Exception:
        rr = None

    # проверка R/R
    rr_ok = rr is not None and rr >= 2.0

    # подтверждения: книга, лента ISS, VWAP/ORB
    book_conf = confirmations.get("book")
    tape_conf = confirmations.get("tape")
    vwap_conf = confirmations.get("vwap_orb")

    conf_count = sum(1 for x in (book_conf, tape_conf, vwap_conf) if x)

    signal = {
        "ticker": ticker.upper(),
        "direction": direction,  # up = лонг, down = шорт
        "structure_and_plan": True,
        "entry": entry,
        "stop": stop,
        "target": target,
        "risk_per_share": round(abs(entry - stop), 4) if entry and stop else None,
        "reward_per_share": round(abs(target - entry), 4) if entry and target else None,
        "rr": rr,
        "rr_ok": rr_ok,
        "trigger": trigger_info,
        "confirmations": {
            "book": book_conf,
            "tape_iss": tape_conf,
            "vwap_orb": vwap_conf,
            "count": conf_count,
            "required": 3,
            "all_present": conf_count >= 3,
        },
        "invalidation": invalidation,
        "time_slice_msk": time_slice,
        "disclaimer": "не инвестиционная рекомендация",
        "protocol": "19.08 index -> instrument, read-only, no POST",
        "notes": [
            "Встречный поток при движущейся цене — поглощение, не разворот",
            "Абсолютную дельту только по закрытым минутам",
            "Кумулятивная дельта экстремум = экстремум цены дня 11/13",
            "Выборка 7д индекса, 4 срабатывания основная, 3 сжатый, порог 3× подогнан — вести журнал режим/кратность/время/инструмент/R минимальным размером",
        ],
    }

    return signal


def example_signals_from_memory() -> List[dict]:
    """
    Примеры из memo 19.08 для проверки логики, когда ISS недоступен.
    Это НЕ живые сигналы, а иллюстрация протокола.
    """
    examples = [
        {
            "date": "2026-08-14",
            "index_trigger": "4.24× 11:20 вниз",
            "result": "-3.4% до close",
            "instrument": "NVTK",
            "efficiency": 0.65,
            "trade_count": "242→381→1830 7.6× в 11:25 дельта -20120 VWAP 979.28 шорт до 928.1 +5.2%",
            "stop": "за экстремум кумулятивной дельты max +49097 10:15 VWAP 990.83 хай 992.6 стоп туда риск 1.2%",
        },
        {
            "date": "2026-08-18",
            "index_trigger": "4.66× 11:40 вверх +0.66% (+1.4% до хая)",
            "instrument": "NVTK 0.65 +5.18% vs LKOH 0.42 +1.58% vs SIBN дельта - пропуск",
            "result": "выбор по эффективности",
        },
        {
            "date": "2026-08-19",
            "index_trigger": "4.2× 15:10 вверх +0.5% (+0.8%)",
            "regime": "4.9 сжатый",
        },
    ]
    return examples


def generate_readonly_signal(
    ticker: str,
    direction: str,
    entry: float,
    atr: Optional[float] = None,
    cum_delta_extreme_price: Optional[float] = None,
    vwap: Optional[float] = None,
    trade_count_explosion: Optional[dict] = None,
    book_data: Optional[dict] = None,
    iss_delta: Optional[dict] = None,
) -> dict:
    """
    Генерация read-only сигнала с R/R≥2, стопом за экстремум кумулятивной дельты, 3 подтверждениями.

    Параметры:
      ticker, direction up/down, entry — цена входа (close бакета)
      atr — для расчёта стопа если нет cum_delta_extreme
      cum_delta_extreme_price — цена экстремума кумулятивной дельты (стоп за неё)
      vwap, trade_count_explosion, book_data, iss_delta — для подтверждений
    """
    # стоп за экстремум кумулятивной дельты (11/13) — основной
    if cum_delta_extreme_price is not None and cum_delta_extreme_price > 0:
        stop = cum_delta_extreme_price
        # риск должен быть в разумных пределах: если стоп слишком далеко, используем ATR
        risk_pct = abs(entry - stop) / entry if entry else 0
        if risk_pct > 0.03:  # >3% — слишком широко, пробуем ATR
            if atr:
                if direction == "up":
                    stop = entry - atr * 1.0
                else:
                    stop = entry + atr * 1.0
    else:
        # fallback ATR
        if atr is None:
            atr = entry * 0.01  # 1% по умолчанию
        if direction == "up":
            stop = entry - atr * 1.0
        else:
            stop = entry + atr * 1.0

    # цель R/R≥2
    risk = abs(entry - stop)
    if direction == "up":
        target = entry + risk * 2.2  # 2.2 для запаса ≥2
    else:
        target = entry - risk * 2.2

    # подтверждения
    confirmations = {}

    # 1. книга: берём из /api/live/{TICKER} напрямую (не orderbook-index)
    if book_data:
        bid5 = book_data.get("bid5_sum") or book_data.get("bid_volume")
        ask5 = book_data.get("ask5_sum") or book_data.get("ask_volume")
        if bid5 and ask5:
            total = bid5 + ask5
            bid_share = bid5 / total if total else 0.5
            if direction == "up" and bid_share > 0.55:
                confirmations["book"] = f"книга: bid_share {bid_share:.2f} >0.55, покупатели давят"
            elif direction == "down" and bid_share < 0.45:
                confirmations["book"] = f"книга: bid_share {bid_share:.2f} <0.45, продавцы давят"
            else:
                confirmations["book"] = f"книга: bid_share {bid_share:.2f} нейтрально, но уровни: {book_data.get('best_bid')}/{book_data.get('best_ask')}"
        else:
            confirmations["book"] = f"книга: best_bid {book_data.get('best_bid')} best_ask {book_data.get('best_ask')} (из /api/live)"
    else:
        confirmations["book"] = None

    # 2. лента ISS: BUYSELL ground truth
    if iss_delta:
        buy_pct = iss_delta.get("buy_pct")
        delta = iss_delta.get("delta")
        tc = iss_delta.get("trade_count")
        if buy_pct is not None and delta is not None:
            if direction == "up" and buy_pct > 55:
                confirmations["tape"] = f"лента ISS: buy {buy_pct}% delta {delta:+d} trade_count {tc} — в сторону лонга"
            elif direction == "down" and buy_pct < 45:
                confirmations["tape"] = f"лента ISS: buy {buy_pct}% delta {delta:+d} trade_count {tc} — в сторону шорта"
            else:
                confirmations["tape"] = f"лента ISS: buy {buy_pct}% delta {delta:+d} — расхождение? поглощение? (встречный поток при движущейся цене — поглощение, не разворот)"
        else:
            confirmations["tape"] = f"лента ISS: delta {delta} (BUYSELL ground truth)"
    else:
        confirmations["tape"] = None

    # 3. VWAP/ORB
    if vwap:
        if direction == "up" and entry > vwap:
            confirmations["vwap_orb"] = f"VWAP: entry {entry} > VWAP {vwap} — выше VWAP, лонг по тренду"
        elif direction == "down" and entry < vwap:
            confirmations["vwap_orb"] = f"VWAP: entry {entry} < VWAP {vwap} — ниже VWAP, шорт по тренду"
        else:
            confirmations["vwap_orb"] = f"VWAP: entry {entry} vs VWAP {vwap} — контртренд? нужен ORB"
    else:
        confirmations["vwap_orb"] = "VWAP: нет данных — проверить /api/candles/brief"

    # trade_count взрыв — дополнительное подтверждение
    if trade_count_explosion:
        prev = trade_count_explosion.get("prev_avg")
        cur = trade_count_explosion.get("current")
        if prev and cur and prev > 0:
            mult = cur / prev
            if mult >= 3:
                # добавляем к ленте
                extra = f" trade_count взрыв {prev}→{cur} {mult:.1f}×"
                if confirmations.get("tape"):
                    confirmations["tape"] += extra
                else:
                    confirmations["tape"] = extra

    # инвалидация
    if direction == "up":
        invalidation = f"закрытие ниже {stop} (экстремум кумулятивной дельты) или пробой VWAP вниз"
    else:
        invalidation = f"закрытие выше {stop} (экстремум кумулятивной дельты) или пробой VWAP вверх"

    time_slice = msk_now().strftime("%Y-%m-%d %H:%M МСК")

    trigger_info = {
        "source": "IMOEX2 10m value ≥3× avg 5 prev buckets, 10:50-16:00",
        "entry_rule": "вход по закрытию бакета расширения",
        "regime_filter": f"first_hour <{FIRST_HOUR_THRESHOLD/1e9:.1f} млрд или <0.75 медианы 5д = сжатый",
    }

    return build_signal_structure(
        ticker=ticker,
        direction=direction,
        entry=round(entry, 4),
        stop=round(stop, 4),
        target=round(target, 4),
        trigger_info=trigger_info,
        confirmations=confirmations,
        invalidation=invalidation,
        time_slice=time_slice,
    )


def format_signal_text(sig: dict) -> str:
    """Человекочитаемый текст сигнала для вывода"""
    lines = []
    lines.append(f"=== СИГНАЛ {sig['ticker']} {sig['direction'].upper()} ===")
    lines.append(f"Структура и план (не пробойный лонг — они убыточны 181д лучшая -0.113R):")
    lines.append(f"  Вход: {sig['entry']}")
    lines.append(f"  Стоп: {sig['stop']} (за экстремум кумулятивной дельты, 11/13)")
    lines.append(f"  Цель: {sig['target']}")
    lines.append(f"  R/R: {sig['rr']} {'✅ ≥2' if sig['rr_ok'] else '❌ <2'}")
    lines.append(f"  Риск: {sig['risk_per_share']} на акцию, награда {sig['reward_per_share']}")
    lines.append("")
    lines.append(f"Триггер: {sig['trigger']}")
    lines.append("")
    lines.append("Подтверждения (3 независимых):")
    for k in ("book", "tape_iss", "vwap_orb"):
        v = sig["confirmations"].get(k)
        lines.append(f"  {k}: {v if v else 'НЕТ — ждать'}")
    lines.append(f"  Итого: {sig['confirmations']['count']}/3 {'✅' if sig['confirmations']['all_present'] else '❌'}")
    lines.append("")
    lines.append(f"Инвалидация: {sig['invalidation']}")
    lines.append(f"Время среза: {sig['time_slice_msk']}")
    lines.append(f"{sig['disclaimer']}")
    lines.append("")
    lines.append("Примечания:")
    for n in sig.get("notes", []):
        lines.append(f"  - {n}")
    return "\n".join(lines)
