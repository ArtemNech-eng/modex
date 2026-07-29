"""Тесты журнала решений (Claude против человека).

Запуск: python3 tests/test_decision_journal.py

Проверяется считалка сравнения. Логика «кто прав — модель или человек» должна
быть покрыта тестами, иначе отчёт будет уверенно показывать неверные цифры, а
доверять ему будут именно потому, что он выглядит точно.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_bucket_stats():
    """Берём функцию из настоящего src/db.py, не копию: тест должен проверять
    тот код, который поедет в прод. Импортировать модуль целиком нельзя —
    он тянет SQLAlchemy, которая для этой проверки не нужна."""
    src = open(os.path.join(ROOT, "src", "db.py"), encoding="utf-8").read()
    m = re.search(r"def decision_bucket_stats\(rows: list\) -> dict:.*?\n(?=\n\nasync def)",
                  src, re.S)
    assert m, "decision_bucket_stats не найдена в src/db.py"
    ns: dict = {}
    exec(m.group(0), ns)
    return ns["decision_bucket_stats"]


bucket = _load_bucket_stats()


class P:
    """Минимальный двойник Prediction для проверки арифметики."""
    def __init__(self, correct=None, realized_r=None, realized_return=None,
                 human_decision=None, direction="up", confidence=0.6):
        self.correct = correct
        self.realized_r = realized_r
        self.realized_return = realized_return
        self.human_decision = human_decision
        self.direction = direction
        self.confidence = confidence


def test_empty_bucket_returns_nulls_not_zeros():
    """Пустая подвыборка — это None, а не 0. Ноль читался бы как «мерили и
    получили ноль», хотя не мерили вовсе."""
    b = bucket([])
    assert b["n"] == 0
    assert b["hit_rate"] is None
    assert b["expectancy_r"] is None
    assert b["r_sample"] == 0


def test_hit_rate_counts_all_rows():
    rows = [P(correct=True), P(correct=True), P(correct=False), P(correct=False)]
    assert bucket(rows)["hit_rate"] == 0.5


def test_expectancy_uses_only_rows_with_r():
    """Ожидание считается по строкам с посчитанным R, но hit_rate — по всем."""
    rows = [P(correct=True, realized_r=2.0), P(correct=False, realized_r=-1.0),
            P(correct=True)]  # без R
    b = bucket(rows)
    assert b["n"] == 3
    assert b["r_sample"] == 2
    assert b["expectancy_r"] == 0.5          # (2.0 - 1.0) / 2
    assert b["hit_rate"] == round(2 / 3, 3)  # по всем трём


def test_expectancy_none_when_no_r_at_all():
    """Пока realized_r не заполняется (r_sample = 0), ожидание НЕ выдумывается."""
    b = bucket([P(correct=True), P(correct=False)])
    assert b["expectancy_r"] is None and b["r_sample"] == 0
    assert b["hit_rate"] == 0.5


def test_human_edge_positive_when_veto_helps():
    """Человек отклонил убыточные — ожидание по принятым выше, чем по всем."""
    acc = [P(correct=True, realized_r=2.0, human_decision="accept"),
           P(correct=True, realized_r=1.0, human_decision="accept")]
    rej = [P(correct=False, realized_r=-1.0, human_decision="reject"),
           P(correct=False, realized_r=-1.0, human_decision="reject")]
    all_r = bucket(acc + rej)["expectancy_r"]
    edge = round(bucket(acc)["expectancy_r"] - all_r, 3)
    assert all_r == 0.25 and bucket(acc)["expectancy_r"] == 1.5
    assert edge > 0, "вето отсекло убытки — вклад человека должен быть положительным"


def test_human_edge_negative_when_veto_hurts():
    """Человек отклонил прибыльные — вклад отрицательный. Это тот случай,
    который система обязана уметь показать владельцу про него самого."""
    acc = [P(correct=False, realized_r=-1.0, human_decision="accept")]
    rej = [P(correct=True, realized_r=3.0, human_decision="reject")]
    all_r = bucket(acc + rej)["expectancy_r"]
    edge = round(bucket(acc)["expectancy_r"] - all_r, 3)
    assert all_r == 1.0 and edge < 0


def test_rejected_and_accepted_measured_identically():
    """Одинаковые исходы дают одинаковую статистику независимо от решения —
    оценщик не должен знать о решении человека."""
    a = [P(correct=True, realized_r=1.5, human_decision="accept")]
    r = [P(correct=True, realized_r=1.5, human_decision="reject")]
    ba, br = bucket(a), bucket(r)
    assert ba["hit_rate"] == br["hit_rate"]
    assert ba["expectancy_r"] == br["expectancy_r"]


def test_decision_validation_and_lock_present_in_source():
    """set_human_decision обязана валидировать значение и запрещать правку
    после оценки: иначе можно «переголосовать» задним числом."""
    src = open(os.path.join(ROOT, "src", "db.py"), encoding="utf-8").read()
    m = re.search(r"async def set_human_decision.*?\n(?=\n\nasync def)", src, re.S)
    assert m, "set_human_decision не найдена"
    body = m.group(0)
    assert "HUMAN_DECISIONS" in body
    assert "уже оценён" in body
    assert "pred.correct is not None" in body


def test_undecided_share_is_reported():
    """Доля нерешённых обязана выводиться: нерешённые — не случайная
    подвыборка, и без этого числа сравнение читать нельзя."""
    src = open(os.path.join(ROOT, "src", "db.py"), encoding="utf-8").read()
    m = re.search(r"async def decision_stats.*?\n(?=\n\n# ─── Key-value)", src, re.S)
    body = m.group(0)
    assert "undecided" in body and "decided_share" in body


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
