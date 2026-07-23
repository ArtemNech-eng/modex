"""
MOODEX — Исследование связи «настроение ↔ цена»

Не навязывает правило, а ИЗУЧАЕТ на истории: как дневное настроение толпы
связано с ПОСЛЕДУЮЩИМ движением цены (через 1/5/10 дней). Из найденной
закономерности уже строится стратегия (моментум либо контртренд).

Чистые функции — тестируются без сети.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Коэффициент корреляции Пирсона."""
    n = len(xs)
    if n < 5:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx ** 0.5 * vy ** 0.5)


def forward_samples(sentiment_rows: list[dict], dates: list[str], closes: list[float],
                    horizon: int) -> list[tuple]:
    """
    Сопоставить настроение с будущей доходностью через `horizon` торговых дней.
    Возвращает список (sentiment_index, avg_signal, fwd_return_pct).
    """
    idx_by_date = {}
    for i, d in enumerate(dates):
        idx_by_date[(d or "")[:10]] = i

    out = []
    for row in sentiment_rows:
        i = idx_by_date.get(row["date"][:10])
        if i is None:
            continue
        j = i + horizon
        if j >= len(closes) or closes[i] == 0:
            continue
        fwd = (closes[j] / closes[i] - 1) * 100
        out.append((row["sentiment_index"], row.get("avg_signal", 0.0), fwd))
    return out


def summarize(samples: list[tuple]) -> dict:
    """Корреляция + разбивка по корзинам настроения → средняя будущая доходность."""
    n = len(samples)
    if n < 10:
        return {"n": n, "corr": None, "buckets": [], "note": "мало данных"}

    sent = [s[0] for s in samples]
    fwd = [s[2] for s in samples]
    corr = pearson(sent, fwd)

    bins = [(0, 20, "Сильный медвежий (0–20)"), (20, 40, "Медвежий (20–40)"),
            (40, 60, "Нейтральный (40–60)"), (60, 80, "Бычий (60–80)"),
            (80, 101, "Сильный бычий (80–100)")]
    buckets = []
    for lo, hi, label in bins:
        grp = [f for (si, _, f) in samples if lo <= si < hi]
        if not grp:
            buckets.append({"label": label, "count": 0, "avg_fwd_pct": None, "pct_up": None})
            continue
        buckets.append({
            "label": label,
            "count": len(grp),
            "avg_fwd_pct": round(sum(grp) / len(grp), 2),
            "pct_up": round(sum(1 for f in grp if f > 0) / len(grp) * 100, 1),
        })
    return {"n": n, "corr": round(corr, 3) if corr is not None else None, "buckets": buckets}


def interpret(corr: Optional[float]) -> str:
    if corr is None:
        return "Недостаточно данных для вывода."
    if corr > 0.1:
        return ("Прямая связь: чем выше настроение, тем выше будущая доходность — "
                "МОМЕНТУМ (толпа права краткосрочно). Стратегия: покупать на позитиве, шортить на негативе.")
    if corr < -0.1:
        return ("Обратная связь: высокое настроение → цена скорее падает — "
                "КОНТРТРЕНД (толпа как противоположный индикатор). Стратегия: шортить эйфорию, покупать панику.")
    return ("Связь слабая (|corr|<0.1): настроение само по себе почти не предсказывает "
            "движение на этом горизонте. Возможно, работает только в комбинации с техникой.")
