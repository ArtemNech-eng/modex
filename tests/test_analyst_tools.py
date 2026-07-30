"""Тесты инструментов аналитика: полная сводка и память.

Запуск: python3 tests/test_analyst_tools.py

Владелец решил 30.07, что сигналы даёт агент, а не механические правила: «система
так не может как ты или клод и никогда не сможет, нет смысла её учить, лучше тебя
усилить нужными данными и чтобы ты самообучался». Основание у решения фактическое —
за день моё суждение по MTLR дало +3 893₽, а около 8000 наблюдений механических
правил в четырёх механиках не дали ни одной работающей конфигурации.

Отсюда два инструмента:
  • analyst_brief — ВСЁ по бумаге одним вызовом. Нужен потому, что данные лежали в
    разных местах, и я построил оценку на цене и средних, не посмотрев стакан,
    поток и новости — при том что весь день чинил именно эти источники.
  • analyst_memory — правила, купленные ошибками, и послужной список. Обучение без
    памяти невозможно: каждая сессия начиналась бы с чистого листа.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.agent.analyst_memory import ANALYST_RULES   # noqa: E402


# ───────────────────── память аналитика ──────────────────────────────────────

def test_rules_exist_and_are_specific():
    """Правило без причины и цены забывается. Каждое куплено ошибкой."""
    assert len(ANALYST_RULES) >= 8
    for r in ANALYST_RULES:
        assert r.get("rule") and len(r["rule"]) > 15, r
        assert r.get("why") and len(r["why"]) > 40, r["rule"]
        assert r.get("cost"), r["rule"]


def test_stop_distance_rule_present():
    """Самая дорогая ошибка дня: стоп в 0.24 ATR выбило шумом, а сделка дала бы +10R."""
    txt = " ".join(r["rule"] + r["why"] for r in ANALYST_RULES)
    assert "0.5" in txt and "ATR" in txt
    assert "39.10" in txt or "0.24" in txt


def test_absorption_rule_present():
    """Дважды за день прочитал встречный поток как разворот и оба раза ошибся."""
    txt = " ".join(r["rule"] for r in ANALYST_RULES)
    assert "поглощение" in txt.lower()


def test_lookahead_rule_present():
    """Фильтр по признаку, известному только к закрытию, обнуляет бэктест."""
    txt = " ".join(r["rule"] + r["why"] for r in ANALYST_RULES)
    assert "закрыти" in txt and ("+0.496" in txt or "заглядыван" in txt)


def test_field_name_rule_present():
    """Четыре раза за день запросил не те имена полей."""
    txt = " ".join(r["rule"] for r in ANALYST_RULES)
    assert "имена полей" in txt or "bid_ask_ratio" in txt


def test_validation_rule_present():
    """Одно наблюдение не правило: трижды за день убил собственные находки."""
    txt = " ".join(r["rule"] + r["why"] for r in ANALYST_RULES)
    assert "по половинам" in txt or "инструментам" in txt
    assert "+0.264" in txt or "-0.113" in txt


# ───────────────────── полная сводка ─────────────────────────────────────────

def test_brief_covers_all_sources():
    """Сводка обязана покрывать ВСЕ источники: пропуск стакана и новостей и был
    исходной претензией владельца."""
    import pathlib
    src = pathlib.Path(ROOT, "src", "agent", "analyst_brief.py").read_text(encoding="utf-8")
    for key in ('"daily"', '"intraday"', '"orderbook"', '"flow"', '"news"',
                '"market_state"', '"data_quality"'):
        assert key in src, key


def test_brief_reports_gaps_loudly():
    """Молчаливое отсутствие данных весь день было источником неверных выводов."""
    import pathlib
    src = pathlib.Path(ROOT, "src", "agent", "analyst_brief.py").read_text(encoding="utf-8")
    assert '"gaps"' in src and 'out["ready"]' in src
    assert src.count('out["gaps"].append') >= 6


def test_brief_names_spread_correctly():
    """bid_ask_ratio это отношение ОБЪЁМОВ, а не спред: путаница стоила ложного
    отказа риск-контура при ошибке в 66 раз."""
    import pathlib
    src = pathlib.Path(ROOT, "src", "agent", "analyst_brief.py").read_text(encoding="utf-8")
    assert "bid_ask_VOLUME_ratio" in src and '"spread_pct"' in src
    assert "НЕ спред" in src


def test_brief_warns_about_legacy_regime():
    """Прежний regime ненадёжен — сводка обязана это говорить, иначе им пользуются
    по инерции."""
    import pathlib
    src = pathlib.Path(ROOT, "src", "agent", "analyst_brief.py").read_text(encoding="utf-8")
    assert "regime_legacy_warning" in src and "SMLT" in src


def test_brief_flags_stale_and_mismatch():
    import pathlib
    src = pathlib.Path(ROOT, "src", "agent", "analyst_brief.py").read_text(encoding="utf-8")
    assert '"stale"' in src and '"mismatch"' in src and '"age_min"' in src


def test_endpoints_registered():
    import pathlib
    src = pathlib.Path(ROOT, "src", "api", "main.py").read_text(encoding="utf-8")
    assert "/api/analyst-brief/{ticker}" in src
    assert "/api/analyst-memory" in src


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
