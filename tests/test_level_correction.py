"""Тесты правки уровней сигнала до оценки.

Запуск: python3 tests/test_level_correction.py

Оценка считает R-мультипликатор по entry и stop ИЗ ЗАПИСИ прогноза, а не из
снимка контекста. 30.07 сигнал 664 по MTLR был записан с входом 39.00, тогда как
сделка исполнена по 39.40: при стопе 38.85 это разница между R/R 4.8 и 0.58, то
есть между «отличный вход» и «вход не оправдан». Все производные — R, ожидание,
накопленная точность — считались бы по цене, которой не было.

Правка допускается ТОЛЬКО до оценки: менять уровни после расчёта исхода значит
переписывать историю. Прежние значения остаются в контексте как след.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _Pred:
    """Двойник записи прогноза: без sqlalchemy в песочнице."""

    def __init__(self, entry=39.0, stop=38.85, target=39.72, rr=4.8,
                 correct=None, realized=None, ctx=None):
        self.entry, self.stop, self.target = entry, stop, target
        self.rr_planned, self.correct, self.realized_price = rr, correct, realized
        self.context_json = ctx


def _apply(pred, entry=None, stop=None, target=None, note=""):
    """Чистое воспроизведение логики correct_prediction_levels."""
    import json
    from datetime import datetime, timezone
    if pred.correct is not None or pred.realized_price is not None:
        return {"ok": False, "reason": "прогноз уже оценён — уровни не меняем"}
    before = {"entry": pred.entry, "stop": pred.stop, "target": pred.target,
              "rr_planned": pred.rr_planned}
    if entry is not None:
        pred.entry = float(entry)
    if stop is not None:
        pred.stop = float(stop)
    if target is not None:
        pred.target = float(target)
    risk = abs(pred.entry - pred.stop)
    pred.rr_planned = round(abs(pred.target - pred.entry) / risk, 2) if risk else None
    ctx = json.loads(pred.context_json) if pred.context_json else {}
    trail = ctx.get("level_corrections") or []
    trail.append({"at": datetime.now(timezone.utc).isoformat(), "before": before,
                  "after": {"entry": pred.entry, "stop": pred.stop,
                            "target": pred.target, "rr_planned": pred.rr_planned},
                  "note": note or "исполнение отличалось от записанного"})
    ctx["level_corrections"] = trail
    pred.context_json = json.dumps(ctx, ensure_ascii=False)
    return {"ok": True, "before": before, "after": trail[-1]["after"]}


def test_entry_correction_changes_rr():
    """ГЛАВНОЕ: настоящий случай MTLR. Вход 39.00 против 39.40 при стопе 38.85 —
    это R/R 4.8 против 0.58."""
    p = _Pred()
    assert p.rr_planned == 4.8
    r = _apply(p, entry=39.40)
    assert r["ok"] and p.entry == 39.40
    assert p.rr_planned == 0.58, p.rr_planned


def test_correction_keeps_trail():
    """Прежние значения обязаны остаться: аудит должен видеть, что менялось."""
    import json
    p = _Pred()
    _apply(p, entry=39.40, note="исполнение по 39.40")
    ctx = json.loads(p.context_json)
    tr = ctx["level_corrections"]
    assert len(tr) == 1
    assert tr[0]["before"]["entry"] == 39.0 and tr[0]["after"]["entry"] == 39.40
    assert "39.40" in tr[0]["note"]


def test_second_correction_appends_not_overwrites():
    import json
    p = _Pred()
    _apply(p, entry=39.40)
    _apply(p, stop=38.90)
    tr = json.loads(p.context_json)["level_corrections"]
    assert len(tr) == 2
    assert tr[0]["before"]["entry"] == 39.0


def test_refuses_after_evaluation():
    """После оценки уровни не меняем — это переписывание истории."""
    p = _Pred(correct=True)
    assert _apply(p, entry=39.40)["ok"] is False
    p2 = _Pred(realized=39.9)
    assert _apply(p2, entry=39.40)["ok"] is False


def test_stop_correction_recomputes_rr():
    p = _Pred(entry=39.40, stop=39.10, target=40.10, rr=2.33)
    _apply(p, stop=38.85)
    assert p.rr_planned == 1.27, p.rr_planned


def test_zero_risk_gives_no_rr():
    """Стоп на уровне входа — риска нет, R/R не выдумываем."""
    p = _Pred(entry=39.40, stop=39.40, target=40.0, rr=None)
    _apply(p, entry=39.40)
    assert p.rr_planned is None


def test_real_function_exists_with_same_contract():
    """Двойник должен повторять настоящую функцию, а не расходиться с ней."""
    import inspect
    import pathlib
    src = pathlib.Path(ROOT, "src", "db.py").read_text(encoding="utf-8")
    assert "async def correct_prediction_levels(" in src
    assert "прогноз уже оценён" in src
    assert "level_corrections" in src


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
