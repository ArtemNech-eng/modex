"""
Охрана состава дозаливки.

Смысл тестов один: норма объёма должна строиться по ТЕМ же бумагам,
которые смотрит сканер. 05.08 расхождение было 48 против 80, и треть
доски молча жила на скользящей норме, которая при затяжном росте
объёма гасит всплеск.

Тесты читают исходник, а не импортируют скрипт: у него на верхнем
уровне сетевые и базовые зависимости, а проверяем мы решение о составе,
а не работу с сетью.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "backfill_iss_minutes.py")


def _src():
    with open(SCRIPT, encoding="utf-8") as f:
        return f.read()


def _wanted_body(src):
    """Тело tickers_wanted — только оно решает, кого заливать."""
    i = src.index("def tickers_wanted")
    j = src.index("async def fetch_day", i)
    return src[i:j]


def test_default_source_is_the_traded_universe():
    body = _wanted_body(_src())
    assert "cached_universe" in body


def test_config_list_is_only_a_fallback():
    """MOEX_TICKERS может остаться — но после попытки взять оборот, не вместо."""
    body = _wanted_body(_src())
    assert body.index("cached_universe") < body.rindex("return static")


def test_a_broken_universe_does_not_leave_the_backfill_empty():
    body = _wanted_body(_src())
    assert "except Exception" in body
    assert body.count("return static") >= 3


def test_env_list_still_wins():
    """TICKERS= — единственный способ пробы на трёх бумагах."""
    body = _wanted_body(_src())
    assert body.index('os.getenv("TICKERS")') < body.index("cached_universe")


def test_config_tickers_are_not_dropped_when_they_leave_the_top_of_turnover():
    """Историю уже залитых бумаг терять из-за смены лидеров дня незачем."""
    body = _wanted_body(_src())
    assert "got + extra" in body
    assert "if t not in set(got)" in body


def test_the_old_behaviour_stays_reachable_for_comparison():
    body = _wanted_body(_src())
    assert "BACKFILL_UNIVERSE" in body


def test_the_choice_of_universe_is_printed():
    """Состав, о котором нельзя узнать из вывода, снова создаст тихую дыру."""
    body = _wanted_body(_src())
    assert body.count("print(") >= 4
