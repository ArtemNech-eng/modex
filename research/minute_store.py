"""
Шаг 7, пункты 1-2: сбор минутных данных с гарантиями качества.

ГЛАВНОЕ ОТКРЫТИЕ 31.07 в 22:30, которое отменяет ожидание шести месяцев:
у MOEX ISS ЕСТЬ минутная история, глубиной не меньше трёх лет (проверено
до 2023-06-01), и она ВКЛЮЧАЕТ вечернюю сессию — полный день это ~1009 бар
с 06:50 до 23:40.

ИСПРАВЛЕНИЕ МОЕЙ ОШИБКИ. Ранее в этот же вечер я записал в три модуля, в память
и в файл стратегии утверждение «MOEX ISS вечернюю сессию не отдаёт вообще».
Оно НЕВЕРНО. Проверка в 19:10 дала последний бар 18:54 — но вечерняя сессия
началась в 19:05, то есть баров ещё физически не существовало. В 22:30 ISS
отдал 195 бар с 19:00 по 22:14.

    ПРАВИЛЬНО: ISS публикует вечерние минутные бары с задержкой ~15 минут.
               Для ЖИВОЙ торговли вечером нужен Tinkoff.
               Для ИСТОРИИ достаточно ISS, и вечер в ней есть.

ЧТО ISS ДАЁТ НА МИНУТЕ:
    begin, end, open, high, low, close, volume, value
    value — рублёвый оборот, значит VWAP минуты считается ТОЧНО: value/volume.
    trades_count ISS на свечах НЕ отдаёт — его в схеме нет.

ГАРАНТИИ КАЧЕСТВА (пункт 2 задания):

    дубли          ключ (ticker, begin); повтор отбрасывается, счётчик пишется
    последовательность  бары сортируются по begin, нарушение порядка — в отчёт
    пропуски       ожидаемые минуты внутри торгового окна сравниваются с
                   фактическими; пропуски НЕ заполняются, а фиксируются числом.
                   Заполнять нельзя: пропуск минуты означает отсутствие сделок,
                   и подстановка предыдущей цены создала бы фальшивый объём
    сессии         session=main для минут до 19:00, evening для 19:00 и позже.
                   Утренняя (06:50-09:50) помечается morning отдельно, потому
                   что её ликвидность на порядок ниже основной
    паузы          дискретные аукционы и перерывы выглядят как пропуски и
                   попадают в отчёт как таковые, без придумывания баров
    корпоративные события  сплиты и дробления ISS в свечах НЕ корректирует.
                   Поэтому считается дневной разрыв: если открытие дня
                   отличается от предыдущего закрытия больше чем на 20%,
                   день помечается suspect_gap. Это НЕ автоматическая
                   корректировка — это флаг для ручной проверки
    метка источника  в каждой строке пишется fetched_at — время загрузки

ФОРМАТ ХРАНЕНИЯ: gzip CSV на бумагу-месяц. Возобновляемо: существующий файл
пропускается, поэтому сбор можно останавливать и продолжать.
"""
import csv
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
ISS = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities"
APP = os.getenv("APP", "http://qmo02nhtriftirhz8msvv7pw.176.124.200.67.sslip.io")
STORE = os.getenv("MINUTE_STORE", "/agent/workspace/modex/data/minute")
# Темп подобран опытом 01.08. Всплеск из 40 запросов проходит и на 0.25 с,
# но при ДЛИТЕЛЬНОЙ работе ISS начинает отдавать 403: за ночь вышло 909 отказов
# против 50 успехов. Блокировка временная, через несколько минут снимается.
# Поэтому база медленнее, а отступ на отказе — минуты, а не секунды.
PACE = 0.6
COOLDOWN = 300                   # общая пауза после серии отказов
FAILS_TO_COOLDOWN = 3
COLS = ["ts", "open", "high", "low", "close", "volume", "value", "vwap",
        "session", "fetched_at"]

MORNING_END = 9 * 60 + 50
MAIN_END = 19 * 60


_consec_fail = 0


def _get(url, timeout=60, tries=5):
    """
    Запрос с ДЛИННЫМ отступом на отказ.

    Прежняя версия отступала на 2, 4, 6 секунд и сдавалась. Блокировка ISS
    длится минуты, поэтому все попытки сгорали внутри неё, месяц помечался
    несобранным, и сборщик шёл дальше. За ночь так пропало 909 месяц-файлов
    при 50 успешных.

    Теперь отступ 15, 45, 120, 300 секунд, а после трёх подряд несобранных
    месяцев — общая пауза в пять минут, чтобы дать блокировке сняться.
    """
    global _consec_fail
    backoff = (15, 45, 120, 300)
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "modex-minute"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            _consec_fail = 0
            return d
        except Exception:                                        # noqa: BLE001
            if k == tries - 1:
                _consec_fail += 1
                if _consec_fail >= FAILS_TO_COOLDOWN:
                    sys.stderr.write("  отказов подряд " + str(_consec_fail)
                                     + ", пауза " + str(COOLDOWN) + "с\n")
                    sys.stderr.flush()
                    time.sleep(COOLDOWN)
                    _consec_fail = 0
                raise
            time.sleep(backoff[min(k, len(backoff) - 1)])


def session_of(ts):
    m = int(ts[11:13]) * 60 + int(ts[14:16])
    if m < MORNING_END:
        return "morning"
    if m < MAIN_END:
        return "main"
    return "evening"


def fetch_range(ticker, frm, till):
    """Все минутные бары за период, со страничной подгрузкой."""
    out, start = [], 0
    while True:
        q = urllib.parse.urlencode({"iss.meta": "off", "interval": 1,
                                    "from": frm, "till": till, "start": start})
        c = _get(f"{ISS}/{ticker}/candles.json?{q}")["candles"]
        i = {k: n for n, k in enumerate(c["columns"])}
        rows = c["data"]
        if not rows:
            break
        for r in rows:
            out.append({
                "ts": r[i["begin"]], "open": r[i["open"]], "high": r[i["high"]],
                "low": r[i["low"]], "close": r[i["close"]],
                "volume": r[i["volume"]], "value": r[i["value"]],
            })
        if len(rows) < 500:
            break
        start += len(rows)
        time.sleep(PACE)
        if start > 60000:
            break
    return out


def quality(rows, ticker):
    """
    Отчёт о качестве. НИЧЕГО НЕ ЧИНИТ — только измеряет и сообщает.
    Молчаливая починка хуже дырки: дырку видно, а подставленный бар нет.
    """
    rep = {"ticker": ticker, "rows": len(rows), "dupes": 0, "out_of_order": 0,
           "gaps": 0, "days": 0, "suspect_gap_days": [], "sessions": {},
           "zero_volume": 0}
    if not rows:
        return rep
    seen = set()
    clean = []
    prev_ts = ""
    for r in rows:
        if r["ts"] in seen:
            rep["dupes"] += 1
            continue
        seen.add(r["ts"])
        if r["ts"] < prev_ts:
            rep["out_of_order"] += 1
        prev_ts = r["ts"]
        clean.append(r)
    clean.sort(key=lambda x: x["ts"])
    byday = defaultdict(list)
    for r in clean:
        byday[r["ts"][:10]].append(r)
    rep["days"] = len(byday)
    sess = defaultdict(int)
    for r in clean:
        sess[session_of(r["ts"])] += 1
        if not r["volume"]:
            rep["zero_volume"] += 1
    rep["sessions"] = dict(sess)
    # пропуски: внутри дня между первой и последней минутой
    for d, bars in byday.items():
        first = int(bars[0]["ts"][11:13]) * 60 + int(bars[0]["ts"][14:16])
        last = int(bars[-1]["ts"][11:13]) * 60 + int(bars[-1]["ts"][14:16])
        expect = last - first + 1
        rep["gaps"] += max(0, expect - len(bars))
    # корпоративные события: разрыв открытия к предыдущему закрытию
    dl = sorted(byday)
    for k in range(1, len(dl)):
        pc = byday[dl[k - 1]][-1]["close"]
        op = byday[dl[k]][0]["open"]
        if pc and op and abs(op / pc - 1) > 0.20:
            rep["suspect_gap_days"].append(
                {"day": dl[k], "prev_close": pc, "open": op,
                 "gap_pct": round((op / pc - 1) * 100, 1)})
    return rep, clean


def save_month(ticker, ym, rows, fetched_at):
    os.makedirs(os.path.join(STORE, ticker), exist_ok=True)
    p = os.path.join(STORE, ticker, f"{ym}.csv.gz")
    with gzip.open(p, "wt", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            vol, val = r["volume"] or 0, r["value"] or 0
            w.writerow({
                "ts": r["ts"], "open": r["open"], "high": r["high"],
                "low": r["low"], "close": r["close"], "volume": vol,
                "value": round(val, 2),
                "vwap": round(val / vol, 6) if vol else "",
                "session": session_of(r["ts"]), "fetched_at": fetched_at,
            })
    return p


def months_back(n):
    now = datetime.now(MSK)
    out = []
    y, m = now.year, now.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def month_bounds(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    frm = f"{ym}-01"
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    till = (datetime(ny, nm, 1) - timedelta(days=1)).strftime("%Y-%m-%d")
    return frm, till


def universe():
    try:
        return _get(f"{APP}/api/universe")["tickers"]
    except Exception:                                            # noqa: BLE001
        from config.settings import MOEX_TICKERS
        return list(MOEX_TICKERS.keys())


def main():
    n_months = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    tk = universe()
    yms = months_back(n_months)
    os.makedirs(STORE, exist_ok=True)
    reports = []
    total_rows = 0
    for ti, t in enumerate(tk, 1):
        for ym in yms:
            p = os.path.join(STORE, t, f"{ym}.csv.gz")
            if os.path.exists(p):
                continue
            frm, till = month_bounds(ym)
            fetched = datetime.now(timezone.utc).isoformat()
            try:
                raw = fetch_range(t, frm, till)
            except Exception as e:                               # noqa: BLE001
                sys.stderr.write(f"{t} {ym}: сбой {str(e)[:60]}\n")
                continue
            if not raw:
                continue
            rep, clean = quality(raw, t)
            rep["month"] = ym
            reports.append(rep)
            save_month(t, ym, clean, fetched)
            total_rows += len(clean)
            sys.stderr.write(
                f"[{ti}/{len(tk)}] {t} {ym}: бар {len(clean)} дней {rep['days']} "
                f"дубли {rep['dupes']} пропуски {rep['gaps']} "
                f"вечер {rep['sessions'].get('evening', 0)}\n")
            sys.stderr.flush()
            time.sleep(PACE)
        with open(os.path.join(STORE, "_quality.json"), "w") as f:
            json.dump(reports, f, ensure_ascii=False)
    print(f"собрано бар: {total_rows}, отчётов: {len(reports)}")


if __name__ == "__main__":
    main()
