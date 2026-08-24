#!/usr/bin/env python3
"""
Read-only сигналы по протоколу 19.08

Только чтение, без POST /api/analyst-signal, /api/ingest/deals, /api/live-signals/start/stop
Без /api/screen, /api/volume-scan, /api/setup-watch, Claude-сигналов (запрещены)

Источники:
  дельта — ISS trades.json BUYSELL B=buyer hits ask / S=bid ground truth
  стакан — /api/live/{TICKER} напрямую (не orderbook-index trust 31+)
  агрегат — ISS marketdata BIDDEPTHT/OFFERDEPTHT/WAPRICE/NUMTRADES
  свечи/VWAP/ATR — /api/candles/brief
  дельта закрытых минут — /api/flow?source=exchange&res=5m&day=YYYY-MM-DD
  юниверс — /api/universe только состав и ранг
  индекс — IMOEX2 candles interval=10 07:00-23:50

3 подтверждения: книга, лента ISS, VWAP/ORB
R/R≥2, инвалидация, время среза, "не инвестиционная рекомендация", "структура и план"

Запуск:
  python scripts/generate_readonly_signals.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

try:
    from src.collector.iss_trades import (
        fetch_marketdata,
        fetch_trades_delta,
        fetch_index_candles,
        compute_index_buckets,
    )
    from src.agent.trader_protocol import (
        detect_expansion_triggers,
        is_compressed_day,
        generate_readonly_signal,
        format_signal_text,
        FIRST_HOUR_THRESHOLD,
        MORNING_VALUE_THRESHOLD,
        example_signals_from_memory,
    )
except Exception as e:
    print(f"Import failed: {e}")
    sys.exit(1)


def msk_today():
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")


def try_fetch_index():
    """Попытка получить IMOEX2 10m за 7 дней"""
    print("\n=== Индекс IMOEX2 10m ===")
    try:
        candles = fetch_index_candles(index="IMOEX2", interval=10)
        if not candles:
            print("ISS недоступен в этом окружении (TLS/блокировка) — использую примеры из memo")
            return None
        print(f"Получено {len(candles)} свечей IMOEX2")
        buckets_info = compute_index_buckets(candles)
        print(f"Утро 07:00-09:49 value: {buckets_info['morning_value']/1e9:.2f} млрд")
        print(f"Первый час 09:50-10:49 value: {buckets_info['first_hour_value']/1e9:.2f} млрд (порог сжатия {FIRST_HOUR_THRESHOLD/1e9} млрд)")
        compressed = is_compressed_day(buckets_info['first_hour_value'])
        print(f"Режим: {'сжатый' if compressed else 'плотный'}")
        triggers = detect_expansion_triggers(buckets_info['buckets'])
        print(f"Триггеры расширения ≥3×: {len(triggers)}")
        for t in triggers:
            print(f"  {t['time_msk']} {t['multiplier']}× {t['direction']} {t['change_pct']}% entry {t['close']}")
        return buckets_info, triggers
    except Exception as e:
        print(f"Ошибка индекса: {e}")
        return None


def try_fetch_ticker_delta(tickers):
    """Попытка получить дельту по списку тикеров из ISS"""
    print("\n=== Дельта ISS trades.json (ground truth) ===")
    results = []
    for tk in tickers[:10]:  # ограничим 10 для скорости
        try:
            md = fetch_marketdata(tk)
            delta = fetch_trades_delta(tk, limit=100)
            print(f"{tk}: NUMTRADES {md.get('NUMTRADES')} | buy {delta.get('buy_pct')}% delta {delta.get('delta')} tc {delta.get('trade_count')} vwap {delta.get('vwap')}")
            results.append({"ticker": tk, "delta": delta, "marketdata": md})
        except Exception as e:
            print(f"{tk}: ошибка {e}")
    return results


def demo_signals():
    """Демо сигналы из памяти + генерация read-only по протоколу"""
    print("\n=== Примеры из memo 19.08 (когда ISS недоступен) ===")
    for ex in example_signals_from_memory():
        print(ex)

    print("\n=== Генерация read-only сигнала (демо, без живых данных) ===")
    # Демо: NVTK как в примере 14.08
    # Вход по закрытию бакета расширения, стоп за экстремум кумулятивной дельты
    # 14.08 NVTK max +49097 10:15 VWAP 990.83 хай 992.6 стоп туда риск 1.2%

    # Пример 1: шорт NVTK 14.08
    sig1 = generate_readonly_signal(
        ticker="NVTK",
        direction="down",
        entry=979.28,  # VWAP 979.28 из примера
        atr=12.0,
        cum_delta_extreme_price=992.6,  # хай дня, экстремум кумулятивной дельты
        vwap=990.83,
        trade_count_explosion={"prev_avg": 300, "current": 1830},
        book_data={"bid5_sum": 1000, "ask5_sum": 3000, "best_bid": 978.0, "best_ask": 980.0},
        iss_delta={"buy_pct": 20, "delta": -20120, "trade_count": 1830},
    )
    print("\n" + format_signal_text(sig1))

    # Пример 2: лонг NVTK 18.08
    sig2 = generate_readonly_signal(
        ticker="NVTK",
        direction="up",
        entry=1000.0,
        atr=15.0,
        cum_delta_extreme_price=980.0,
        vwap=995.0,
        trade_count_explosion={"prev_avg": 400, "current": 1800},
        book_data={"bid5_sum": 3000, "ask5_sum": 1000, "best_bid": 999.0, "best_ask": 1001.0},
        iss_delta={"buy_pct": 70, "delta": 15000, "trade_count": 1500},
    )
    print("\n" + format_signal_text(sig2))

    print("\n=== Проверка R/R и подтверждений ===")
    for sig in (sig1, sig2):
        print(f"{sig['ticker']} {sig['direction']} R/R {sig['rr']} ok={sig['rr_ok']} conf {sig['confirmations']['count']}/3 all={sig['confirmations']['all_present']}")
        assert sig['rr_ok'], "R/R должен быть ≥2"
        assert sig['disclaimer'] == "не инвестиционная рекомендация"
        assert sig['structure_and_plan'] is True


def main():
    print(f"Время МСК: {(datetime.now(timezone.utc)+timedelta(hours=3)).strftime('%Y-%m-%d %H:%M:%S')} — протокол 19.08")
    print("Только чтение, без POST, без запрещённых эндпоинтов")

    # 1. Индекс
    idx_data = try_fetch_index()

    # 2. Тикеры из конфига (первые 10)
    try:
        from config.settings import MOEX_TICKERS
        tickers = list(MOEX_TICKERS.keys())
    except Exception:
        tickers = ["SBER", "GAZP", "LKOH", "NVTK", "TATN", "SIBN", "ASTR", "CBOM", "YDEX", "SMLT"]

    # 3. Дельта (попытается, но в sandbox скорее всего не получится)
    deltas = try_fetch_ticker_delta(tickers)

    # 4. Демо сигналы
    demo_signals()

    print("\n=== Итог ===")
    print("Код исправлен:")
    print("  - src/db.py flow_candle_check теперь abs(diff)/candle>0.05 ловит и потерю 60.6% (пример ours 13926 vs candle 35351)")
    print("  - src/agent/analyst_brief.py flow помечен DEPRECATED 19.08 UNRELIABLE, ground truth ISS trades.json")
    print("  - src/agent/context_builder.py абсорбция отключена (False) т.к. buy_pct инвертирован")
    print("  - src/collector/tinkoff_client.py _classify_flow DEPRECATED, добавлен _classify_flow_fixed с инверсией для сверки")
    print("  - src/collector/iss_trades.py новый модуль ground truth BUYSELL B/S")
    print("  - src/agent/trader_protocol.py новый протокол IMOEX2 07:00-23:50, сжатый/плотный, триггер 3×, R/R≥2, 3 подтверждения, инвалидация, время среза, disclaimer")
    print("")
    print("Для живого прогона на проде где ISS доступен:")
    print("  python scripts/generate_readonly_signals.py — получит IMOEX2 10m и дельту ISS")
    print("  Сигналы read-only, без POST, формулировка 'структура и план', т.к. пробойные лонги убыточны 181д лучшая -0.113R")


if __name__ == "__main__":
    main()
