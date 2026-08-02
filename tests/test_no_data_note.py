"""Молчание обязано называть свою причину."""
from src.analysis.intraday import no_data_note


def test_dead_stream_is_named_a_fault():
    assert "неисправность" in no_data_note(False, "main", 0)


def test_closed_market_is_not_a_fault():
    for ph in ("closed", "pre", "break"):
        got = no_data_note(True, ph, 0)
        assert "неисправность" not in got, ph


def test_open_market_without_packets_is_a_fault():
    """
    САМЫЙ ДОРОГОЙ СЛУЧАЙ. В 11:00 буднего дня прежняя фраза «стрим только
    поднялся» скрыла бы остановку сбора.
    """
    got = no_data_note(True, "main", 0)
    assert "неисправность" in got
    assert "открыт" in got


def test_open_market_with_packets_is_just_warmup():
    got = no_data_note(True, "main", 42)
    assert "неисправность" not in got
    assert "набирается" in got


def test_all_four_answers_are_different():
    """Если две причины звучат одинаково, различать нечего."""
    outs = {no_data_note(False, "main", 0), no_data_note(True, "closed", 0),
            no_data_note(True, "main", 0), no_data_note(True, "main", 5)}
    assert len(outs) == 4


def test_morning_and_evening_sessions_count_as_open():
    """Утренняя сессия 07:00-09:50 — рабочая ликвидность, не «закрыто»."""
    for ph in ("morning", "evening"):
        assert "неисправность" in no_data_note(True, ph, 0), ph
