"""
Список бумаг строится по факту, а не переписывается руками.

Ошибка, ради которой написан файл. До 31.07 сорок восемь тикеров были зашиты
в config/settings.py константой. Владелец прислал скриншот «Взлёты дня», и
десять из пятнадцати лидеров роста оказались вне системы:

    MVID   +8.29%   оборот 390 млн   ← лидер дня, в системе НЕТ
    SGZH   -4.21%   оборот 637 млн   ← лидер падения, в системе НЕТ
    SPBE   +2.31%   оборот 416 млн
    RAGR   +2.00%   оборот 436 млн
    CNRU   +3.68%   оборот 101 млн

В это время я докладывал владельцу, что «лидер дня SMLT +4.92%». Проверка
показала шестнадцать потерянных ликвидных акций.

Рукописный список стареет МОЛЧА: бумага набирает обороты, а система её не
видит и никак об этом не сообщает. Поэтому список строится из оборота, а
расхождение с прежним выводится наружу.
"""
import pytest

from src.analysis.universe import (MIN_TURNOVER_RUB, SHARE_TYPES,
                                   build_universe, diff_against)


def test_only_shares_no_funds():
    """
    Включаем только акции. Биржевые фонды дают огромный оборот от
    маркетмейкера и забьют весь список: 31.07 из двадцати девяти
    «пропущенных ликвидных бумаг» шестнадцать были фондами.
    """
    assert SHARE_TYPES == {"1", "2"}
    assert "J" not in SHARE_TYPES        # ETF
    assert "9" not in SHARE_TYPES        # ПИФ
    assert "A" not in SHARE_TYPES
    assert "B" not in SHARE_TYPES
    assert "D" not in SHARE_TYPES        # депозитарные расписки


def test_preferred_shares_included():
    """
    Префы включены намеренно: SBERP и MTLRP ходят не так, как обыкновенные.
    31.07 MTLRP дал +3.04% отдельным движением от MTLR.
    """
    assert "2" in SHARE_TYPES


def test_threshold_matches_position_size():
    """
    Порог оборота привязан к размеру позиции владельца (150-250 тыс ₽),
    а не взят круглым числом от балды.
    """
    assert MIN_TURNOVER_RUB == 100_000_000


def test_fallback_when_exchange_unreachable(monkeypatch):
    """
    Биржа упала — отдаём запасной список, а не пустоту. Пустой список
    останавливает весь сканер, вчерашний состав хуже свежего, но лучше нуля.
    """
    import src.analysis.universe as u

    def boom(*a, **kw):
        raise OSError("сеть недоступна")

    monkeypatch.setattr(u, "_fetch", boom)
    out = u.build_universe(fallback=["SBER", "GAZP"])
    assert out["source"] == "fallback"
    assert out["tickers"] == ["SBER", "GAZP"]


def test_empty_result_also_falls_back(monkeypatch):
    """Биржа ответила, но подходящих бумаг ноль — тоже запасной список."""
    import src.analysis.universe as u

    monkeypatch.setattr(u, "_fetch", lambda *a, **kw: [])
    out = u.build_universe(fallback=["SBER"])
    assert out["source"] == "fallback"
    assert out["tickers"] == ["SBER"]


def test_sorted_by_turnover(monkeypatch):
    """Список отсортирован по обороту: сверху то, что реально торгуется."""
    import src.analysis.universe as u

    rows = [
        {"ticker": "AAA", "sectype": "1", "name": "A", "turnover": 2e8,
         "price": 10, "change_pct": 1, "lot": 1},
        {"ticker": "BBB", "sectype": "1", "name": "B", "turnover": 9e8,
         "price": 10, "change_pct": 1, "lot": 1},
        {"ticker": "FUND", "sectype": "J", "name": "ETF", "turnover": 5e9,
         "price": 10, "change_pct": 0, "lot": 1},
        {"ticker": "TINY", "sectype": "1", "name": "T", "turnover": 1e6,
         "price": 10, "change_pct": 0, "lot": 1},
    ]
    monkeypatch.setattr(u, "_fetch", lambda *a, **kw: rows)
    out = u.build_universe()
    assert out["tickers"] == ["BBB", "AAA"], "фонд и неликвид обязаны отсеяться"


def test_max_n_caps_the_list(monkeypatch):
    """Ограничение сверху есть: 500 бумаг сканер не потянет по лимитам API."""
    import src.analysis.universe as u

    rows = [{"ticker": f"T{k}", "sectype": "1", "name": f"n{k}",
             "turnover": 1e9 - k, "price": 10, "change_pct": 0, "lot": 1}
            for k in range(200)]
    monkeypatch.setattr(u, "_fetch", lambda *a, **kw: rows)
    assert len(u.build_universe(max_n=30)["tickers"]) == 30


def test_diff_surfaces_what_static_list_loses(monkeypatch):
    """
    Расхождение обязано быть видно наружу. Молчаливое устаревание списка —
    и есть та ошибка, ради которой всё это написано.
    """
    import src.analysis.universe as u

    rows = [
        {"ticker": "SBER", "sectype": "1", "name": "Сбербанк", "turnover": 4e9,
         "price": 275, "change_pct": 0.7, "lot": 1},
        {"ticker": "MVID", "sectype": "1", "name": "М.видео", "turnover": 3.9e8,
         "price": 50, "change_pct": 8.2, "lot": 1},
    ]
    monkeypatch.setattr(u, "_fetch", lambda *a, **kw: rows)
    d = u.diff_against(["SBER", "УСТАРЕЛ"])
    assert [m["ticker"] for m in d["missing"]] == ["MVID"]
    assert d["missing"][0]["turnover_mln"] == 390
    assert d["stale"] == ["УСТАРЕЛ"]


def test_incident_recorded_in_module():
    """Обстоятельства записаны рядом с кодом, иначе список снова зашьют руками."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/analysis/universe.py").read_text()
    assert "MVID" in src and "SGZH" in src
    assert "8.29" in src
