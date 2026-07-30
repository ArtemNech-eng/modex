"""
Окно интрадей-запроса обязано накрывать всю сессию.

Ошибка, ради которой написан файл: в fetch_intraday стояло hours=8, в
построителе контекста hours=9. Пока сессия короткая, окно её накрывает, и
всё выглядит рабочим. После ~15:00 МСК начало дня уходит за край окна, а
min/max по нему продолжают называться «максимум дня» и «минимум дня».

30.07.2026 в 23:30 по FLOT окно начиналось в 15:25. Диапазон дня вышел
77.61–78.92 при настоящем 77.27–79.75; максимум стоял в 10:00. По этому
«максимуму» был выдан сигнал на пробой вверх — вход на уровне, который рынок
прошёл и отверг тринадцатью часами ранее. Сплошная проверка 48 бумаг: у 46
хотя бы один экстремум дня оказался вне окна.

Тест проверяет ровно одно свойство: окно достаёт до утреннего аукциона в
любой момент торгового дня.
"""
from datetime import datetime, timedelta

import pytest

from src.agent.intraday_analyst import SESSION_START_MSK, _session_hours_msk

# 06:50 — первая свеча утреннего аукциона, 23:50 — конец вечерней сессии.
AUCTION_H, AUCTION_M = 6, 50
SESSION_END_H = 23


def _msk(h, m=0):
    return datetime(2026, 7, 30, h, m)


def _window_start(now):
    """Момент, с которого начнётся окно при таком «сейчас»."""
    return now - timedelta(hours=_session_hours_msk(now))


@pytest.mark.parametrize("h,m", [
    (6, 50), (7, 0), (7, 30), (9, 55), (10, 0), (10, 30), (12, 0),
    (14, 0), (15, 25), (16, 0), (18, 50), (19, 0), (21, 0), (22, 0),
    (23, 30), (23, 49),
])
def test_window_reaches_morning_auction(h, m):
    """В любой момент сессии окно обязано доставать до 06:50 того же дня."""
    now = _msk(h, m)
    start = _window_start(now)
    auction = _msk(AUCTION_H, AUCTION_M)
    assert start <= auction, (
        f"в {h:02d}:{m:02d} МСК окно начинается в {start:%H:%M} и теряет начало дня; "
        f"именно так максимум FLOT в 10:00 пропал из диапазона в 23:30"
    )


def test_the_exact_flot_case():
    """Регрессия на конкретный случай: 23:30 МСК, окно 8 часов начиналось в 15:25."""
    now = _msk(23, 30)
    assert _session_hours_msk(now) > 8, "восемь часов — ровно та константа, что ломала уровни"
    assert _window_start(now) <= _msk(AUCTION_H, AUCTION_M)


def test_window_does_not_grow_without_bound():
    """Окно не должно тянуть лишние сутки: это чужой день в min/max."""
    for h in range(SESSION_START_MSK[0], SESSION_END_H + 1):
        assert _session_hours_msk(_msk(h, 0)) <= 24


def test_before_auction_looks_at_previous_session():
    """До открытия смотреть не на что: окно должно уйти во вчерашний хвост."""
    now = _msk(5, 30)
    start = _window_start(now)
    assert start < now
    assert start.day == now.day - 1, "перед аукционом окно обязано захватить вчерашнюю сессию"


def test_window_covers_at_least_elapsed_session():
    """Окно не короче уже прошедшей части сессии — иначе часть дня не видна."""
    for h, m in [(8, 0), (11, 0), (13, 30), (17, 0), (20, 0), (23, 0)]:
        now = _msk(h, m)
        elapsed = (now - _msk(*SESSION_START_MSK)).total_seconds() / 3600.0
        assert _session_hours_msk(now) >= elapsed


def test_default_is_computed_not_constant():
    """fetch_intraday должен вычислять окно, а не брать фиксированное число."""
    import inspect

    from src.agent import intraday_analyst as ia

    sig = inspect.signature(ia.fetch_intraday)
    assert sig.parameters["hours"].default is None, (
        "hours обязан быть None, чтобы окно считалось от начала сессии"
    )
    src = inspect.getsource(ia.fetch_intraday)
    assert "_session_hours_msk()" in src


def test_context_builder_uses_the_same_window():
    """
    Второе место с константой (hours=9) тоже должно считать окно.

    Читаем файл текстом, а не импортом: context_builder тянет src/db.py, а
    в песочнице агента нет sqlalchemy — импорт упал бы не по делу.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/agent/context_builder.py").read_text()
    assert "hours=9" not in src, "константа 9 часов вернулась в построитель контекста"
    assert "_session_hours_msk()" in src
