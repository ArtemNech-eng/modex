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
    """Чистое воспроизведение логики correct_prediction_levels, включая пересчёт
    снимка позиции: рублёвый результат считается по risk_shares/risk_rub из
    контекста, а не по уровням записи."""
    import json
    from datetime import datetime, timezone
    from src.risk.engine import RiskConfig, size_position
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
    pos_before = {k: ctx.get(k) for k in ("risk_shares", "risk_rub", "stop_pct")}
    d = size_position(entry=pred.entry, stop=pred.stop,
                      direction=("up" if getattr(pred, "direction", "up") == "up" else "down"),
                      cfg=RiskConfig(), lot_size=1,
                      spread_pct=ctx.get("spread_pct_at_signal"),
                      depth_near_mid=ctx.get("depth_near_mid_at_signal"))
    pos_after = {"risk_shares": d.shares, "risk_rub": round(d.risk_rub, 2),
                 "stop_pct": round(abs(pred.entry - pred.stop) / pred.entry, 4)}
    ctx.update(pos_after)
    ctx["entry"], ctx["stop"], ctx["target"] = pred.entry, pred.stop, pred.target
    trail = ctx.get("level_corrections") or []
    trail.append({"at": datetime.now(timezone.utc).isoformat(), "before": before,
                  "after": {"entry": pred.entry, "stop": pred.stop,
                            "target": pred.target, "rr_planned": pred.rr_planned},
                  "position_before": pos_before, "position_after": pos_after,
                  "note": note or "исполнение отличалось от записанного"})
    ctx["level_corrections"] = trail
    pred.context_json = json.dumps(ctx, ensure_ascii=False)
    return {"ok": True, "before": before, "after": trail[-1]["after"],
            "position_before": pos_before, "position_after": pos_after}


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



# ───── снимок позиции пересчитывается вместе с уровнями ───────────────────────

def test_position_snapshot_recomputed():
    """ГЛАВНОЕ. Рублёвый результат считается по risk_shares и risk_rub из
    КОНТЕКСТА (_position_from_context), а не по уровням записи. Настоящий случай
    664: снимок был посчитан от входа 39.00 со стопом 38.85 — риск 0.15 на акцию,
    1282 шт, 192₽. При входе 39.40 и стопе 39.10 риск 0.30 на акцию: 1269 шт и
    381₽. Расхождение вдвое, и без пересчёта оно попало бы в ожидание и в отчёт."""
    import json
    p = _Pred(entry=39.0, stop=38.85, target=39.72, rr=4.8,
              ctx=json.dumps({"risk_shares": 1282, "risk_rub": 192.3,
                              "stop_pct": 0.0038,
                              "spread_pct_at_signal": 0.0255,
                              "depth_near_mid_at_signal": 231300}))
    r = _apply(p, entry=39.40, stop=39.10)
    assert r["position_before"]["risk_shares"] == 1282
    assert r["position_after"]["risk_shares"] != 1282, "размер обязан пересчитаться"
    # риск на акцию вырос вдвое -> рублёвый риск вырос
    assert r["position_after"]["risk_rub"] > r["position_before"]["risk_rub"] * 1.5, \
        (r["position_before"]["risk_rub"], r["position_after"]["risk_rub"])


def test_stop_pct_recomputed():
    import json
    p = _Pred(entry=39.0, stop=38.85, ctx=json.dumps({"stop_pct": 0.0038}))
    r = _apply(p, entry=39.40, stop=39.10)
    assert abs(r["position_after"]["stop_pct"] - 0.0076) < 0.0005, r["position_after"]["stop_pct"]


def test_context_levels_stay_in_sync():
    """Контекст держит свою копию уровней. Если поправить только поля записи,
    пост-мортем и аудит прочитают разные цены одного сигнала."""
    import json
    p = _Pred(ctx=json.dumps({"entry": 39.0, "stop": 38.85, "target": 39.72}))
    _apply(p, entry=39.40, stop=39.10, target=39.90)
    ctx = json.loads(p.context_json)
    assert ctx["entry"] == 39.40 and ctx["stop"] == 39.10 and ctx["target"] == 39.90


def test_position_trail_keeps_both_snapshots():
    import json
    p = _Pred(ctx=json.dumps({"risk_shares": 1282, "risk_rub": 192.3}))
    _apply(p, entry=39.40, stop=39.10)
    tr = json.loads(p.context_json)["level_corrections"][0]
    assert tr["position_before"]["risk_shares"] == 1282
    assert tr["position_after"]["risk_shares"] is not None


def test_real_function_recomputes_position():
    import pathlib
    src = pathlib.Path(ROOT, "src", "db.py").read_text(encoding="utf-8")
    assert "position_after" in src and "size_position(" in src
    assert "risk_shares" in src.split("async def correct_prediction_levels")[1][:4000]

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
