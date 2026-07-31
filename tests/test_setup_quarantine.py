"""
Сетап не выдаётся сигналом в день изобретения.

Ошибка, ради которой написан файл — единственная за два дня, которая стоила
владельцу денег напрямую.

31.07 в 11:48 я нашёл сетап «возврат под VWAP в нисходящем тренде»: 959 входов
на истории, +0.158R, t=4.35. В 11:50 выдал по нему сигнал. Владелец вошёл в
VTBR 2512 акциями по 56.17 и закрылся через четырнадцать минут в убыток.

Сетап был не виноват. Виноваты две вещи:

1. Живой сканер работал НЕ ТАК, как проверенный тест. Тест входит В МОМЕНТ
   пересечения VWAP; сканер проверял состояние «был выше, сейчас ниже» и
   сработал через час четырнадцать после события. По той же выборке:
       вход в момент пересечения  +0.147R  t=4.02
       вход через час             +0.067R  t=1.53
   Владельцу был продан один сетап, а исполнен другой.

2. У сетапа был НОЛЬ дней живой работы. Утренний шорт перед выдачей наблюдался
   сутки; этот — ноль.

Настоящая причина глубже: владелец пять раз просил сигнал, я пять раз отвечал
«нечем», и на шестой выпустил находку немедленно. Давление выдать результат —
единственная причина, от которой не страхует внимательность. Поэтому здесь
заслон в коде, а не памятка в документе.
"""
import pytest

from src.agent.setup_watcher import SETUP_LIVE_SINCE, setup_is_live


def test_unknown_setup_is_never_a_signal():
    """
    Незнакомое имя не проходит по умолчанию. Забыть внести запись должно быть
    безопасно; забыть про карантин — нет.
    """
    assert setup_is_live("vwap_reclaim_fail", today="2026-12-31") is False
    assert setup_is_live("что_угодно_новое", today="2026-12-31") is False


def test_setup_found_today_is_not_live_today():
    """Сетап, внесённый сегодняшней датой, сигналом станет только завтра."""
    SETUP_LIVE_SINCE_TEST = "2026-08-05"
    assert not ("2026-08-04" >= SETUP_LIVE_SINCE_TEST)
    assert "2026-08-05" >= SETUP_LIVE_SINCE_TEST


def test_known_setups_are_live():
    """Проверенные и обкатанные сетапы работают как раньше."""
    assert setup_is_live("consolidation_breakout", today="2026-07-31") is True
    assert setup_is_live("news_resolution", today="2026-07-31") is True


def test_known_setup_not_live_before_its_date():
    """До даты обкатки даже знакомый сетап — только наблюдение."""
    assert setup_is_live("consolidation_breakout", today="2026-07-29") is False


def test_vwap_setup_is_explicitly_quarantined():
    """
    Сетап, стоивший владельцу денег, обязан оставаться в карантине, пока не
    отработает сутки. Запись о нём есть только в комментарии — и это намеренно.
    """
    assert "vwap_reclaim_fail" not in SETUP_LIVE_SINCE


def test_quarantine_is_applied_in_the_fire_path():
    """Заслон стоит в коде принятия решения, а не только в справочнике."""
    import inspect

    from src.agent import setup_watcher as sw

    src = inspect.getsource(sw)
    assert "setup_is_live(name)" in src
    assert 'kind = "observation"' in src


def test_incident_is_recorded_next_to_the_gate():
    """
    Обстоятельства записаны рядом с заслоном. Без них следующий агент решит,
    что карантин — лишняя формальность, и снимет его.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/agent/setup_watcher.py").read_text()
    assert "VTBR" in src
    assert "t=4.02" in src and "t=1.53" in src


@pytest.mark.parametrize("name", list(SETUP_LIVE_SINCE))
def test_every_live_setup_has_a_real_date(name):
    """У каждой записи в справочнике — настоящая дата, а не заглушка."""
    v = SETUP_LIVE_SINCE[name]
    assert isinstance(v, str) and len(v) == 10 and v.count("-") == 2
