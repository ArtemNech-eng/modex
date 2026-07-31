"""
Старший таймфрейм требует истории за несколько дней, иначе честный отказ.

Ошибка, ради которой написан файл. 31.07 я выдал владельцу таблицу трендов по
25 бумагам, где у ВСЕХ двадцати пяти 60-минутный тренд был NEUTRAL. Причина:
часовые бары брались внутри одного дня, а их там десять (10:00-19:00). Для
EMA20 плюс проверка структуры нужно 29 бар. Функция возвращала ноль, а
печаталось NEUTRAL — треть таблицы была молча пустой и выглядела осмысленной.

Ту же ошибку я перед этим допустил в режимной проверке, не исправил и повторил
через два часа. Поэтому здесь проверяется именно ОТКАЗ: недостаток данных
обязан быть виден, а не превращаться в NEUTRAL.
"""
import pytest

from src.analysis.trend import (DAYS_NEEDED, MIN_BARS, aggregate, agreement,
                                multi_timeframe, trend_score)


def _series(n, start=100.0, step=0.5, up=True):
    C, H, L = [], [], []
    p = start
    for _ in range(n):
        p += step if up else -step
        C.append(p)
        H.append(p + 0.2)
        L.append(p - 0.2)
    return C, H, L


def _minutes(days, per_day=540, start_hour=10):
    """Минутные бары за `days` дней подряд."""
    out = []
    p = 100.0
    for d in range(days):
        day = f"2026-07-{d + 1:02d}"
        for k in range(per_day):
            m = start_hour * 60 + k
            ts = f"{day} {m // 60:02d}:{m % 60:02d}:00"
            p += 0.01
            out.append([ts, p, p + 0.05, p - 0.05, p, 100])
    return out


def test_not_enough_bars_is_an_explicit_refusal():
    """Мало бар — «НЕТ ДАННЫХ» с причиной, а НЕ ноль под видом NEUTRAL."""
    C, H, L = _series(10)
    r = trend_score(C, H, L)
    assert r["enough"] is False
    assert r["score"] is None
    assert r["label"] == "НЕТ ДАННЫХ", "молчаливый NEUTRAL — это и была ошибка"
    assert "нужно" in " ".join(r["why"])


def test_enough_bars_gives_a_direction():
    C, H, L = _series(MIN_BARS + 5)
    r = trend_score(C, H, L)
    assert r["enough"] is True
    assert r["score"] > 0 and "LONG" in r["label"]


def test_downtrend_detected():
    C, H, L = _series(MIN_BARS + 5, start=200.0, up=False)
    r = trend_score(C, H, L)
    assert r["score"] < 0 and "SHORT" in r["label"]


def test_hourly_inside_one_day_is_refused():
    """
    Ровно тот случай, что сломал таблицу: один день минуток -> у часового
    таймфрейма десять бар -> обязан быть отказ, а не NEUTRAL.
    """
    out = multi_timeframe(_minutes(1), timeframes=(15, 60))
    assert out["60m"]["enough"] is False
    assert out["60m"]["days"] == 1
    assert any("нужно" in w for w in out["60m"]["why"])
    # 15-минутный при 540 минутах в дне набирает 36 бар — ему одного дня хватает
    assert out["15m"]["enough"] is True


def test_hourly_across_several_days_works():
    """Три дня истории — часовому таймфрейму хватает."""
    out = multi_timeframe(_minutes(4), timeframes=(60,))
    assert out["60m"]["enough"] is True
    assert out["60m"]["days"] == 4
    assert out["60m"]["bars"] >= MIN_BARS


def test_aggregate_does_not_merge_different_days():
    """
    Корзина — это (дата, номер интервала). Без даты бары одного часа разных
    дней слились бы в один, и «тренд» считался бы по перемешанной истории.
    """
    bars = _minutes(3, per_day=120)
    agg = aggregate(bars, 60)
    days = {x[0][:10] for x in agg}
    assert len(days) == 3
    assert len(agg) == 3 * 2, "по два часовых бара на каждый из трёх дней"


def test_aggregate_works_on_coarse_input():
    """
    Прежняя склейка сбрасывала буфер по условию на минуту дня и на
    десятиминутных барах не срабатывала ВООБЩЕ — получался один бар на день.
    """
    bars = []
    for k in range(6):
        m = 600 + k * 10
        bars.append([f"2026-07-01 {m//60:02d}:{m%60:02d}:00", 10, 11, 9, 10, 5])
    agg = aggregate(bars, 60)
    assert len(agg) == 1
    assert agg[0][2] == 11 and agg[0][3] == 9 and agg[0][5] == 30


def test_agreement_ignores_insufficient_timeframes():
    """
    Согласие считается только по таймфреймам с данными. Иначе «нет данных»
    попадало бы в расчёт как NEUTRAL и ломало вывод.
    """
    tf = {"15m": {"score": 2, "enough": True},
          "60m": {"score": None, "enough": False},
          "1d": {"score": 1, "enough": True}}
    assert agreement(tf) == "ВСЕ ВВЕРХ"
    tf["1d"]["score"] = -1
    assert agreement(tf) == "расходятся"
    tf2 = {"15m": {"score": 2, "enough": True},
           "60m": {"score": None, "enough": False}}
    assert agreement(tf2) == "недостаточно таймфреймов"


def test_days_needed_table_is_honest():
    """Часовому нужно не меньше трёх дней — это и есть суть исправления."""
    assert DAYS_NEEDED[60] >= 3
    assert DAYS_NEEDED[15] == 1


def test_incident_recorded_in_module():
    """Обстоятельства рядом с кодом, иначе ошибку повторят третий раз."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/analysis/trend.py").read_text()
    assert "NEUTRAL" in src and "29" in src
    assert "десять" in src, "число часовых бар в дне должно быть названо"
