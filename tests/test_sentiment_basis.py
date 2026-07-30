"""Тесты честности индекса настроения.

Запуск: python3 tests/test_sentiment_basis.py

30.07 в проде volume_zscore держался в диапазоне 1.91-2.45 у ВСЕХ 26 тикеров, и
18 из 26 стояли выше порога новостного события 2.0 — при том что текстовых
сообщений не поступало вовсе (Telegram 0 событий в час, Пульс заблокирован,
RSS 21 событие). Две причины:

  1) в счёт шли снимки стакана: 2157 в час против 21 новости, то есть мерился
     темп опроса Tinkoff, а не поток сообщений;
  2) база пополнялась при КАЖДОМ обращении к индексу, а счётчик после
     перезапуска монотонно растёт по мере набора 60-минутного окна. База
     [1,2,3,4,5] при счётчике 6 даёт ровно z=1.90.

Последствие торговое: classify_event объявлял «аномальный объём сообщений», а
новостная ветка имеет приоритет над пробоем диапазона открытия — ложная
аномалия перехватывала выбор сетапа.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.aggregator.aggregator import SentimentAggregator, ORDERBOOK_CHANNEL  # noqa: E402


def _agg():
    return SentimentAggregator()


def _add(a, ticker="TEST", n=1, channel="markettwits", signal=0.5, minutes_ago=1):
    now = datetime.now(timezone.utc)
    for i in range(n):
        a.add_point(ticker=ticker, signal=signal,
                    label="positive" if signal > 0 else "negative",
                    score=0.8, channel=channel, text="текст",
                    timestamp=now - timedelta(minutes=minutes_ago))


# ──────────────────── основа расчёта видна потребителю ───────────────────────

def test_orderbook_only_is_labeled_orderbook():
    """ГЛАВНОЕ: индекс из снимков стакана обязан честно называться стаканом,
    а не выдаваться за настроение чатов."""
    a = _agg()
    _add(a, n=6, channel=ORDERBOOK_CHANNEL)
    d = a.get_ticker_index("TEST").to_dict()
    assert d["basis"] == "orderbook"
    assert d["orderbook_count"] == 6 and d["text_count"] == 0


def test_text_only_is_labeled_text():
    a = _agg()
    _add(a, n=6)
    d = a.get_ticker_index("TEST").to_dict()
    assert d["basis"] == "text" and d["text_count"] == 6


def test_mixed_is_labeled_mixed():
    a = _agg()
    _add(a, n=4)
    _add(a, n=4, channel=ORDERBOOK_CHANNEL)
    d = a.get_ticker_index("TEST").to_dict()
    assert d["basis"] == "mixed"
    assert d["text_count"] == 4 and d["orderbook_count"] == 4


# ──────────────────── z-score не считает снимки стакана ──────────────────────

def test_orderbook_snapshots_never_produce_message_anomaly():
    """Снимки стакана идут постоянным темпом; z-score по ним объявлял аномалию
    сообщений на 18 тикерах одновременно."""
    a = _agg()
    for _ in range(30):
        _add(a, n=1, channel=ORDERBOOK_CHANNEL)
        idx = a.get_ticker_index("TEST")
        if idx:
            assert idx.volume_zscore == 0.0, idx.volume_zscore


def test_monotonic_warmup_is_not_an_anomaly():
    """База [1,2,3,4,5] при счётчике 6 давала ровно 1.90. Монотонный рост —
    это набор окна, а не новость."""
    a = _agg()
    for n in range(1, 12):
        _add(a, n=1)
        idx = a.get_ticker_index("TEST")
        if idx:
            assert idx.volume_zscore == 0.0, (n, idx.volume_zscore)


def test_real_text_spike_is_detected():
    """Настоящий всплеск сообщений на неровной базе обязан дать аномалию —
    иначе заслон убил бы полезный сигнал вместе с ложным."""
    a = _agg()
    # неровная база нужной длины: чередуем, чтобы не сработал заслон монотонности
    for i in range(25):
        a._baseline_counts["TEST"].append((3, 1, 4, 2, 3)[i % 5])
    _add(a, n=25)                     # резкий всплеск
    idx = a.get_ticker_index("TEST")
    assert idx.volume_zscore >= 2.0, idx.volume_zscore


def test_baseline_must_be_full_before_trusting():
    """Недобранная база молчит, а не выдумывает."""
    a = _agg()
    a._baseline_counts["TEST"].extend([1, 5, 2, 6])   # меньше BASELINE_MIN_SAMPLES
    _add(a, n=6)
    assert a.get_ticker_index("TEST").volume_zscore == 0.0



def test_baseline_sampled_on_schedule_not_per_request():
    """База на 168 значений задумывалась как неделя часовых замеров, но
    пополнялась при каждом обращении к индексу — горизонт задавался трафиком."""
    a = _agg()
    _add(a, n=6)
    for _ in range(50):               # пятьдесят запросов подряд
        a.get_ticker_index("TEST")
    assert len(a._baseline_counts["TEST"]) == 1, \
        f"замеров должно быть 1, а не {len(a._baseline_counts['TEST'])}"


if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ПАДАЕТ {name}: {e}")
        except Exception as e:                      # noqa: BLE001
            failed += 1
            print(f"  ОШИБКА {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed} из {len(tests)} пройдено")
    sys.exit(1 if failed else 0)
