"""Тесты аудита детектора режима.

Запуск: python3 tests/test_regime_audit.py

Отчёт, который читают как основание для правки торговой логики, обязан быть
покрыт тестами — и обязан предупреждать о своей выборке. Второе проверяется
здесь наравне с арифметикой: отчёт без предупреждений опаснее отсутствия отчёта,
потому что односторонняя выборка выглядит как доказательство.
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load():
    """Берём функцию из настоящего src/db.py — тест должен проверять код,
    который поедет в прод. Импорт модуля целиком тянет SQLAlchemy."""
    src = open(os.path.join(ROOT, "src", "db.py"), encoding="utf-8").read()
    m = re.search(r"def analyze_regime_audit\(rows: list\) -> dict:.*?\n(?=\n\nasync def regime_audit_rows)",
                  src, re.S)
    assert m, "analyze_regime_audit не найдена в src/db.py"
    buckets = re.search(r"_SCORE_BUCKETS = \(.*?\n\)\n", src, re.S)
    assert buckets, "_SCORE_BUCKETS не найдены"
    ns: dict = {}
    exec(buckets.group(0) + "\n" + m.group(0), ns)
    return ns["analyze_regime_audit"]


audit = _load()
NOW = datetime.now(timezone.utc)


def _row(ret, score=None, regime=None, days_ago=0, rpos=None, adx=None):
    return {"realized_return": ret, "technical_score": score, "regime": regime,
            "range_position": rpos, "adx": adx,
            "created_at": (NOW - timedelta(days=days_ago)).isoformat()}


def test_empty_input_says_so():
    a = audit([])
    assert a["n"] == 0
    assert any("мерить нечего" in c for c in a["caveats"])


def test_drift_is_computed():
    a = audit([_row(2.0), _row(3.0), _row(-1.0), _row(4.0)])
    assert a["n"] == 4
    assert a["drift"]["mean_pct"] == 2.0
    assert a["drift"]["share_up"] == 0.75


def test_range_fade_signature_detects_wrong_shorts():
    """Ключевой замер: шорты от верха диапазона против фактического роста."""
    rows = [_row(2.5, score=-0.6) for _ in range(9)] + [_row(-1.0, score=-0.6)]
    a = audit(rows)
    sig = a["range_fade_signature"]
    assert sig["n"] == 10
    assert sig["share_up"] == 0.9
    assert sig["sigma_from_random"] > 2


def test_fade_signature_absent_without_matching_scores():
    a = audit([_row(1.0, score=0.1), _row(1.0, score=-0.1)])
    assert a["range_fade_signature"] is None


def test_score_buckets_split_correctly():
    rows = [_row(3.0, score=-0.8), _row(2.0, score=-0.6),
            _row(1.0, score=-0.3), _row(2.0, score=0.0)]
    a = audit(rows)
    names = [b["bucket"] for b in a["by_tech_score"]]
    assert len(names) == 4 and len(set(names)) == 4
    strong = [b for b in a["by_tech_score"] if "сильный шорт" in b["bucket"]][0]
    assert strong["n"] == 1 and strong["mean_return_pct"] == 3.0


def test_regime_share_zero_warns_when_label_missing():
    """Если метка режима не записана — отчёт обязан сказать это прямо,
    а не молча показать пустую таблицу."""
    a = audit([_row(2.0, score=-0.6), _row(1.0, score=-0.6)])
    assert a["regime_recorded_share"] == 0.0
    assert a["by_regime"] == []
    assert any("метка режима не записана" in c for c in a["caveats"])


def test_regime_breakdown_when_label_present():
    rows = [_row(2.5, score=-0.6, regime="range") for _ in range(8)]
    rows += [_row(-1.0, score=-0.6, regime="range") for _ in range(2)]
    rows += [_row(1.0, score=0.6, regime="uptrend") for _ in range(5)]
    a = audit(rows)
    assert a["regime_recorded_share"] == 1.0
    by = {g["regime"]: g for g in a["by_regime"]}
    assert by["range"]["n"] == 10 and by["range"]["share_up"] == 0.8
    # боковик с 80% роста — детектор не увидел тренд
    assert by["range"]["looks_mislabelled"] is True
    assert by["uptrend"]["looks_mislabelled"] is False


def test_range_with_balanced_moves_not_flagged():
    """Настоящий боковик (движения в обе стороны) НЕ помечается ошибкой."""
    rows = ([_row(1.0, score=-0.6, regime="range") for _ in range(5)] +
            [_row(-1.0, score=-0.6, regime="range") for _ in range(5)])
    a = audit(rows)
    assert a["by_regime"][0]["looks_mislabelled"] is False


def test_short_window_warns_about_single_regime():
    """Главная защита от неверного прочтения: короткое окно = один режим."""
    a = audit([_row(2.0, score=-0.6, days_ago=i % 4) for i in range(40)])
    assert a["window"]["days"] <= 4
    assert any("ОДИН режим рынка" in c for c in a["caveats"])
    assert any("нисходящий" in c for c in a["caveats"])


def test_long_window_drops_single_regime_warning():
    rows = [_row(1.0, score=-0.6, days_ago=i) for i in range(40)]
    a = audit(rows)
    assert a["window"]["days"] == 40 and a["window"]["weeks"] >= 3
    assert not any("ОДИН режим" in c for c in a["caveats"])


def test_overlap_caveat_is_always_present():
    """Предупреждение о перекрытии горизонтов не должно исчезать никогда:
    сигмы в этом отчёте нельзя читать буквально ни при каком объёме данных."""
    for rows in ([_row(1.0, score=-0.6)],
                 [_row(1.0, score=-0.6, days_ago=i) for i in range(60)]):
        a = audit(rows)
        assert any("эффективная выборка МЕНЬШЕ" in c for c in a["caveats"])


def test_unevaluated_rows_ignored():
    a = audit([_row(None, score=-0.6), _row(2.0, score=-0.6)])
    assert a["n"] == 1


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
