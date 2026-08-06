"""
Читатель свечей должен отдавать рубли.

ЗАЧЕМ. Колонки lot и turnover_rub в таблице candle_minute есть и
заполняются: сверка с минутными свечами ISS за 06.08 дала медиану
ISS/наш = 1.0 на одиннадцати общих минутах, объёмы совпали до штуки.
Но candle_series собирал выходной словарь руками из девяти полей, и
новые колонки в него не попадали. Снаружи рублей просто не было.

ПРО НОЛЬ. lot = 0 в базе означает "не знаем", а не "лот нулевой".
Наружу такой ноль отдавать нельзя: потребитель умножит на него и
получит тихий ноль вместо честного пропуска. Поэтому отдаём None.

Скрипт идемпотентный: если правка уже в файле, он это печатает и
ничего не трогает. Если якорь встречается не один раз, скрипт падает,
а не гадает.
"""
from pathlib import Path

P = Path("src/db.py")
text = P.read_text(encoding="utf-8")
applied = 0
skipped = 0


def swap(old: str, new: str, name: str) -> None:
    global text, applied, skipped
    if new in text:
        print(f"{name}: уже было")
        skipped += 1
        return
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"{name}: якорь встречается {n} раз, ожидалась 1")
    text = text.replace(old, new)
    applied += 1
    print(f"{name}: применено")


# 1. candle_series — проекция колонок в словарь
swap(
    '    plain = [{"ts": r.ts, "session": r.session, "open": r.open, "high": r.high,\n'
    '              "low": r.low, "close": r.close, "volume": r.volume,\n'
    '              "volume_buy": r.volume_buy, "volume_sell": r.volume_sell}\n'
    '             for r in rows]',
    '    plain = [{"ts": r.ts, "session": r.session, "open": r.open, "high": r.high,\n'
    '              "low": r.low, "close": r.close, "volume": r.volume,\n'
    '              "volume_buy": r.volume_buy, "volume_sell": r.volume_sell,\n'
    '              "lot": r.lot, "turnover_rub": r.turnover_rub}\n'
    '             for r in rows]',
    "проекция колонок",
)

# 2. aggregate_candles — накопление рублей и лотности по бару
swap(
    '        if k not in buckets:\n'
    '            buckets[k] = {"ts": ts, "o": r.get("open") or 0.0,\n'
    '                          "h": r.get("high") or 0.0, "l": r.get("low") or 0.0,\n'
    '                          "c": r.get("close") or 0.0, "v": 0, "vb": 0, "vs": 0,\n'
    '                          "session": r.get("session") or "main"}\n'
    '            order.append(k)',
    '        if k not in buckets:\n'
    '            buckets[k] = {"ts": ts, "o": r.get("open") or 0.0,\n'
    '                          "h": r.get("high") or 0.0, "l": r.get("low") or 0.0,\n'
    '                          "c": r.get("close") or 0.0, "v": 0, "vb": 0, "vs": 0,\n'
    '                          "rub": 0.0, "lot": 0,\n'
    '                          "session": r.get("session") or "main"}\n'
    '            order.append(k)',
    "копилка бара",
)

swap(
    '        b["vb"] += r.get("volume_buy") or 0\n'
    '        b["vs"] += r.get("volume_sell") or 0\n'
    '    out = []',
    '        b["vb"] += r.get("volume_buy") or 0\n'
    '        b["vs"] += r.get("volume_sell") or 0\n'
    '        # Рубли складываем по той же причине, что и объём: минуты внутри\n'
    '        # бара не пересекаются. Лотность одна на бумагу, берём максимум —\n'
    '        # ноль означает "не знаем" и не должен вытеснять known-значение.\n'
    '        b["rub"] += float(r.get("turnover_rub") or 0.0)\n'
    '        lot_row = int(r.get("lot") or 0)\n'
    '        if lot_row > b["lot"]:\n'
    '            b["lot"] = lot_row\n'
    '    out = []',
    "накопление рублей",
)

# 3. aggregate_candles — выдача
swap(
    '            "ts": b["ts"], "ticker": ticker.upper(), "session": b["session"],\n'
    '            "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],\n'
    '            "volume": b["v"], "volume_buy": b["vb"], "volume_sell": b["vs"],\n'
    '            "buy_ratio": round(b["vb"] / tot, 4) if tot else None,',
    '            "ts": b["ts"], "ticker": ticker.upper(), "session": b["session"],\n'
    '            "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"],\n'
    '            "volume": b["v"], "volume_buy": b["vb"], "volume_sell": b["vs"],\n'
    '            "turnover_rub": round(b["rub"], 2) if b["rub"] else None,\n'
    '            "lot": b["lot"] or None,\n'
    '            "buy_ratio": round(b["vb"] / tot, 4) if tot else None,',
    "выдача бара",
)

if applied:
    P.write_text(text, encoding="utf-8")
print(f"итог: применено {applied}, уже было {skipped}")
