"""
Измерительный стенд для проверки блоков стратегии.

Зачем отдельный модуль. До 31.07 каждая проверка писалась заново ad-hoc
скриптом, и это дважды дало неверный результат: один раз я взял бары до
момента выдачи плана и насчитал -2.47R вместо +1.36R, другой раз посчитал
89 зависимых снимков как 89 наблюдений и получил t=8.97 на двенадцати
событиях. Стенд закрывает оба класса ошибок структурно.

Что он гарантирует:
  * причинность — признаки считаются срезом до бара входа, без исключений;
  * события, а не наблюдения — соседние срабатывания одной бумаги в один день
    схлопываются в одно;
  * издержки — 0.05% за круг в долях R, всегда;
  * обязательная батарея проверок: половины по времени, месяцы, концентрация
    по инструментам.
"""
import json
import os
import statistics
from collections import defaultdict

DATA = "/tmp/bench"
COST_PCT = 0.05          # круг, % от цены


def load(ticker: str):
    """Бары одной бумаги: список [ts, o, h, l, c, v]."""
    p = os.path.join(DATA, f"{ticker}.json")
    if not os.path.exists(p):
        return []
    return json.load(open(p))


def tickers():
    out = []
    for f in os.listdir(DATA):
        if f.endswith(".json") and not f.startswith("_"):
            out.append(f[:-5])
    return sorted(out)


def by_day(rows):
    """Бары, разложенные по торговым дням."""
    d = defaultdict(list)
    for r in rows:
        d[r[0][:10]].append(r)
    for k in d:
        d[k].sort(key=lambda x: x[0])
    return d


def minute_of(ts):
    return int(ts[11:13]) * 60 + int(ts[14:16])


def daily_atr(days_map, day_list, upto_idx, n=10):
    """ATR по дневным диапазонам ПРЕДЫДУЩИХ дней. Причинно."""
    vals = []
    for d in day_list[max(0, upto_idx - n):upto_idx]:
        bs = days_map.get(d)
        if bs:
            vals.append(max(x[2] for x in bs) - min(x[3] for x in bs))
    return statistics.mean(vals) if vals else 0.0


def simulate(bars, i, side, stop_dist, exit_idx=None):
    """
    Сделка от бара i: вход по закрытию, стоп на stop_dist, выход по exit_idx
    или по последнему бару. Возвращает R после издержек.
    """
    C = [x[4] for x in bars]
    H = [x[2] for x in bars]
    L = [x[3] for x in bars]
    entry = C[i]
    if stop_dist <= 0:
        return None
    stop = entry - stop_dist if side == "long" else entry + stop_dist
    end = exit_idx if exit_idx is not None else len(C) - 1
    px = None
    for k in range(i + 1, min(end + 1, len(C))):
        if side == "long" and L[k] <= stop:
            px = stop
            break
        if side == "short" and H[k] >= stop:
            px = stop
            break
    if px is None:
        px = C[min(end, len(C) - 1)]
    r = (px - entry) / stop_dist if side == "long" else (entry - px) / stop_dist
    return r - (COST_PCT / 100 * entry) / stop_dist


def dedupe(records):
    """
    Одно событие на пару бумага+день. Соседние срабатывания одной бумаги в
    один день — это одно и то же событие, сколько бы срезов его ни поймало.
    Берём ПЕРВОЕ по времени: именно на нём принимается решение вживую.
    """
    seen = {}
    for r in sorted(records, key=lambda x: (x["tk"], x["day"], x.get("i", 0))):
        key = (r["tk"], r["day"])
        if key not in seen:
            seen[key] = r
    return list(seen.values())


def report(name, records, min_n=40):
    """Батарея проверок. Печатает вердикт и возвращает сводку."""
    recs = dedupe(records)
    n = len(recs)
    if n < min_n:
        print(f"  {name:44} {n:5d} событий — мало данных")
        return None
    v = [x["r"] for x in recs]
    m = statistics.mean(v)
    se = statistics.pstdev(v) / n ** 0.5 if n > 1 else 0
    t = m / se if se else 0
    recs_t = sorted(recs, key=lambda x: x["day"])
    a = [x["r"] for x in recs_t[:n // 2]]
    b = [x["r"] for x in recs_t[n // 2:]]
    halves_ok = len(a) > 5 and len(b) > 5 and min(statistics.mean(a), statistics.mean(b)) > 0
    bym = defaultdict(list)
    for x in recs:
        bym[x["day"][:7]].append(x["r"])
    mon = [(k, statistics.mean(v2)) for k, v2 in bym.items() if len(v2) >= 8]
    mon_pos = sum(1 for _, mm in mon if mm > 0)
    byt = defaultdict(list)
    for x in recs:
        byt[x["tk"]].append(x["r"])
    rank = sorted(byt.items(), key=lambda kv: -sum(kv[1]))
    wo = [x["r"] for x in recs if x["tk"] not in [k for k, _ in rank[:2]]]
    wo_m = statistics.mean(wo) if len(wo) >= min_n // 2 else None
    verdict = "ПРОШЁЛ" if (abs(t) >= 2 and halves_ok and mon and mon_pos >= len(mon) * 2 / 3
                           and wo_m is not None and wo_m > 0) else "не прошёл"
    print(f"  {name:44} {n:5d} соб {m:+7.3f}R  t={t:5.2f}  "
          f"половины {'++' if halves_ok else '+-'}  "
          f"мес {mon_pos}/{len(mon)}  "
          f"без топ-2 {('%+.3f' % wo_m) if wo_m is not None else '  n/a'}  {verdict}")
    return dict(n=n, mean=m, t=t, halves_ok=halves_ok, months=(mon_pos, len(mon)), without_top2=wo_m)
