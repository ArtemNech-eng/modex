#!/usr/bin/env python3
"""
Read-only сигналы по протоколам 19.08 + 21.08 «Кто двигает цену»

Только чтение, без POST /api/analyst-signal, /api/ingest/deals, /api/live-signals/start/stop
Без /api/screen, /api/volume-scan, /api/setup-watch, Claude-сигналов (запрещены по memo 21.08)

19.08:
  дельта — ISS trades.json BUYSELL B=buyer hits ask / S=bid ground truth
  стакан — /api/live/{TICKER} напрямую (не orderbook-index trust 31+)
  агрегат — ISS marketdata BIDDEPTHT/OFFERDEPTHT/WAPRICE/NUMTRADES
  свечи/VWAP/ATR — /api/candles/brief
  дельта закрытых минут — /api/flow?source=exchange&res=5m&day=YYYY-MM-DD
  юниверс — /api/universe только состав и ранг
  индекс — IMOEX2 candles interval=10 07:00-23:50
  3 подтверждения: книга, лента ISS, VWAP/ORB — УСТАРЕЛО 21.08

21.08 — главный принцип:
  ⚖️ Лимитная книга не является доказательством. Заявку отменяют бесплатно и именно тогда,
  когда на неё начали ориентироваться. Единственные данные, которые нельзя подделать после факта —
  исполненные сделки с флагом агрессора.

  Отменено как доказательство: bid5_sum/ask5_sum, bid_top_max/ask_top_max,
  BIDDEPTHT/OFFERDEPTHT, /api/orderbook-index (TATN 37.0 PLZL 37.3), imb_min/imb_max
  Допустимо: только для оценки стоимости исполнения — спред/шаг цены

  Доказательство: дельта агрессора ISS BUYSELL, цена к VWAP LAST vs WAPRICE,
  оборот NUMTRADES/VOLTODAY, направление принтов TRADETIME/PRICE

  Порядок получения дельты (обязателен):
    1) NUMTRADES из marketdata securities.json?iss.only=marketdata
    2) start = NUMTRADES-100
    3) trades.json?start=START&limit=100&iss.only=trades&fast_mode=false

  Три вопроса: Q1 перевес <15% шум, Q2 двигает ли поток цену (асимметрия),
  Q3 по каким ценам принты (нисходящие/айсберг/гигант/исчерпание)

  Стоп за провалившийся экстремум агрессора, которого поглотили
  Отмена в сделках: пачка >500 закрытие за уровень + 300+ перестают двигать
  R/R≥2 с комиссией 0.05% в обе стороны

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
        fetch_trades,
        fetch_trades_delta,
        fetch_trades_with_numtrades,
        fetch_index_candles,
        compute_index_buckets,
        analyze_three_questions,
        build_stop_and_invalidation,
        mgnt_reference_example,
        compute_delta,
    )
    from src.agent.trader_protocol import (
        detect_expansion_triggers,
        is_compressed_day,
        generate_readonly_signal,
        generate_signal_from_iss_trades,
        format_signal_text,
        FIRST_HOUR_THRESHOLD,
        example_signals_from_memory,
    )
except Exception as e:
    print(f"Import failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)


def msk_now():
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S МСК")


def try_fetch_index():
    print("\n=== Индекс IMOEX2 10m (протокол 19.08) ===")
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


def try_fetch_ticker_delta_21_08(tickers):
    print("\n=== Дельта ISS trades.json 21.08 ground truth — 3 вопроса ===")
    print("Порядок: NUMTRADES → start=NUMTRADES-100 → trades?limit=100&fast_mode=false")
    print("Плотность: MGNT 100=11мин → запрос каждые 10мин непрерывная дельта; SBER 70875 <1мин точечная")
    results = []
    for tk in tickers[:10]:
        try:
            trades, md = fetch_trades_with_numtrades(tk)
            if not trades:
                print(f"{tk}: нет сделок (ISS недоступен?) NUMTRADES {md.get('NUMTRADES')}")
                continue
            three_q = analyze_three_questions(trades)
            q1 = three_q.get("q1_imbalance") or {}
            q2 = three_q.get("q2_moves_price") or {}
            q3 = three_q.get("q3_print_levels") or {}
            print(f"{tk}: {q1.get('buy_volume')}B/{q1.get('sell_volume')}S delta {q1.get('delta')} перевес {q1.get('imbalance_pct')}% "
                  f"{'шум' if q1.get('is_noise') else 'значимо'} | Q2 контроль={three_q.get('control')} {q2.get('overall_move_pct')}% | "
                  f"Q3 desc={q3.get('descending_sales')} iceberg={q3.get('iceberg_clips')} exhaust={q3.get('exhaustion')} | "
                  f"NUMTRADES {md.get('NUMTRADES')} LAST {md.get('LAST')} WAPRICE {md.get('WAPRICE')} | {three_q.get('sample')}")
            results.append({"ticker": tk, "trades": trades, "marketdata": md, "three_q": three_q})
        except Exception as e:
            print(f"{tk}: ошибка {e}")
            import traceback
            traceback.print_exc()
    return results


def demo_mgnt_reference():
    print("\n=== Эталон MGNT 21.08 14:44-14:56 (из документа) ===")
    ref = mgnt_reference_example()
    for k, v in ref.items():
        if k == "sequence":
            print(f"  {k}:")
            for s in v:
                print(f"    - {s}")
        elif k == "lessons":
            print(f"  {k}:")
            for s in v:
                print(f"    - {s}")
        else:
            print(f"  {k}: {v}")

    # Синтетическая реконструкция сделок MGNT для проверки three_questions
    print("\n--- Реконструкция MGNT 100 сделок для проверки логики ---")
    # 664 buy 1333 sell turnover 1997 delta -669
    # sequence buyer 521 1616->1617, seller 1138 1616->1613 clips 64/64/64/30...
    fake_trades = []
    # buyer part
    for qty in [178, 58, 42, 28, 215]:  # 521
        fake_trades.append({"TRADETIME": "14:53:17", "PRICE": 1616, "QUANTITY": qty, "BUYSELL": "B", "BOARDID": "TQBR"})
    fake_trades.append({"TRADETIME": "14:53:18", "PRICE": 1617, "QUANTITY": 100, "BUYSELL": "B", "BOARDID": "TQBR"})
    # seller part descending
    prices = [1616, 1615.5, 1614.5, 1613.5, 1613, 1613, 1613]
    qtys = [64, 64, 64, 30, 30, 30, 62, 339, 100]  # 1138 ~ но сумма 783 + 521 = 1304, добавим для примера
    # упростим: сделаем 1138
    for i, (p, q) in enumerate(zip(prices + [1613, 1613], [64, 64, 64, 30, 30, 30, 62, 339, 100])):
        fake_trades.append({"TRADETIME": f"14:54:{10+i:02d}", "PRICE": p, "QUANTITY": q, "BUYSELL": "S", "BOARDID": "TQBR"})

    # Дополним до 100 сделок шумом
    while len(fake_trades) < 100:
        fake_trades.append({"TRADETIME": "14:55:00", "PRICE": 1613, "QUANTITY": 5, "BUYSELL": "S", "BOARDID": "TQBR"})

    three_q = analyze_three_questions(fake_trades)
    print(f"Q1: {three_q.get('q1_imbalance')}")
    print(f"Q2: {three_q.get('q2_moves_price')} control={three_q.get('control')}")
    print(f"Q3: {three_q.get('q3_print_levels')}")
    stop_info = build_stop_and_invalidation(three_q, entry_price=1613.03, direction="down")
    print(f"Stop: {stop_info}")


def demo_signals_21_08():
    print("\n=== Генерация read-only сигнала по методу 21.08 (демо) ===")

    # Пример NVTK 14.08 с новыми полями
    fake_trades_nvtk = []
    for _ in range(30):
        fake_trades_nvtk.append({"TRADETIME": "11:20:00", "PRICE": 979, "QUANTITY": 100, "BUYSELL": "S", "BOARDID": "TQBR"})
    for _ in range(10):
        fake_trades_nvtk.append({"TRADETIME": "11:20:01", "PRICE": 978, "QUANTITY": 50, "BUYSELL": "B", "BOARDID": "TQBR"})
    # descending sales
    md_nvtk = {"LAST": 979.28, "WAPRICE": 990.83, "NUMTRADES": 5000}

    sig = generate_signal_from_iss_trades("NVTK", fake_trades_nvtk, md_nvtk, entry_price=979.28)
    print("\n" + format_signal_text(sig))

    # Проверка R/R и доказательств
    if not sig.get("skip"):
        assert sig["rr_ok"], f"R/R должен быть ≥2, получили {sig['rr']}"
        assert sig["disclaimer"] == "не инвестиционная рекомендация"
        assert sig["structure_and_plan"] is True
        assert "book" not in str(sig.get("proofs_only_trades")).lower() or "не доказательство" in str(sig.get("not_proof_book"))
        print("✅ Проверки: R/R≥2, disclaimer, структура и план, книга NOT proof — ок")

    # Второй пример — шум <15% должен скипаться
    fake_noise = []
    for _ in range(50):
        fake_noise.append({"TRADETIME": "11:00:00", "PRICE": 100, "QUANTITY": 10, "BUYSELL": "B", "BOARDID": "TQBR"})
    for _ in range(48):
        fake_noise.append({"TRADETIME": "11:00:01", "PRICE": 100, "QUANTITY": 10, "BUYSELL": "S", "BOARDID": "TQBR"})
    sig_noise = generate_signal_from_iss_trades("SBER", fake_noise, {"LAST": 100, "WAPRICE": 100}, entry_price=100)
    print("\nШум тест (<15%):")
    print(format_signal_text(sig_noise))
    assert sig_noise.get("skip") is True and "шум" in sig_noise.get("reason", "")


def main():
    print(f"Время: {msk_now()} — протоколы 19.08 + 21.08 КТО ДВИГАЕТ ЦЕНУ")
    print("Только чтение, без POST, без запрещённых эндпоинтов")
    print("Книга НЕ доказательство — только для оценки спреда/шага (21.08)")

    idx_data = try_fetch_index()

    try:
        from config.settings import MOEX_TICKERS
        tickers = list(MOEX_TICKERS.keys())
    except Exception:
        tickers = ["SBER", "GAZP", "LKOH", "NVTK", "TATN", "SIBN", "ASTR", "CBOM", "YDEX", "SMLT", "MGNT"]

    deltas = try_fetch_ticker_delta_21_08(tickers)

    demo_mgnt_reference()
    demo_signals_21_08()

    print("\n=== Примеры из memo 19.08 ===")
    for ex in example_signals_from_memory():
        print(ex)

    print("\n=== Итог ===")
    print("Код 21.08:")
    print("  - iss_trades.py: порядок NUMTRADES→start→trades fast_mode:false, analyze_three_questions Q1/Q2/Q3, build_stop_and_invalidation stop за failed extreme, COMMISSION_PCT 0.05%, mgnt_reference_example")
    print("  - trader_protocol.py: книга исключена из доказательств (только спред/шаг), build_signal_structure_21_08 с proofs_only_trades, not_proof_book, three_questions, stop_logic, invalidation в сделках, lag warning, journal_fields, generate_signal_from_iss_trades")
    print("  - R/R≥2 с комиссией 0.05% обе стороны, стоп за провалившийся экстремум поглощённого агрессора, отмена в сделках >500 + 300+ перестают двигать")
    print("  - Правила: исчерпание (крупнейший принт в конце завершение), дивергенция (новый экстремум дельты без нового экстремума цены → входа нет)")
    print("  - Дискреционный вход без индексного триггера 0.84× 1/1 убыток vs с триггером 4+/2-/1 ноль — фильтр объёма важнее ленты")
    print("")
    print("Для живого прогона на проде где ISS доступен:")
    print("  python scripts/generate_readonly_signals.py — получит IMOEX2 10m и дельту ISS с 3 вопросами")
    print("  Сигналы read-only, без POST, формулировка 'структура и план', т.к. пробойные лонги убыточны 181д лучшая -0.113R")


if __name__ == "__main__":
    main()
