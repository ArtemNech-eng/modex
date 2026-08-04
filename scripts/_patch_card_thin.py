"""Патч: у тонкой базы пропадает само поле times. Идемпотентен."""

SRC = "src/analysis/volume_events.py"
TST = "tests/test_volume_events.py"

EDITS = [
    # ─── 1. ВСПЛЕСК: кратности нет, если знаменатель шум ──────────────
    (SRC,
     '''        out.append(_ev(
            "volume_surge", why,
            step, last, rub=round(now), base_rub=round(base),
            times=round(mult, 2), base_source=source, base_thin=thin))''',
     '''        # КРАТНОСТЬ ПРОПАДАЕТ ЦЕЛИКОМ, А НЕ ОГОВАРИВАЕТСЯ. Текст говорил
        # «кратность считать не по чему», а рядом лежало times:30.0 — и 04.08
        # на экране было ASTR ×30.0 на обороте 11 223 ₽. Кто берёт поля, а
        # не читает прозу — дашборд, сортировка, агент — видел только
        # число. Оговорка в соседнем поле — не защита.
        #
        # Число не выбрашивается, а ПЕРЕИМЕНОВывается: ×1510 при норме
        # 856 ₽ — сам по себе признак того, что бумага проснулась из ничего,
        # и скрывать его совсем значило бы потерять признак.
        out.append(_ev(
            "volume_surge", why,
            step, last, rub=round(now), base_rub=round(base),
            times=(None if thin else round(mult, 2)),
            times_vs_thin_base=(round(mult, 2) if thin else None),
            base_source=source, base_thin=thin))'''),

    # ─── 2. УСКОРЕНИЕ: то же самое ───────────────────────────────
    (SRC,
     "                times=round(mult, 2), bars_growing=gb,",
     "                times=(None if thin else round(mult, 2)),\n"
     "                times_vs_thin_base=(round(mult, 2) if thin else None),\n"
     "                bars_growing=gb,"),

    # ─── 3. СКАНЕР: бумага без кратности не встаёт наверх ───────────
    (SRC,
     '''            top = max(e.get("times", 0) for e in evs)
            out.append({"ticker": tk, "events": evs, "count": len(evs),
                        "max_times": round(top, 2),
                        "kinds": sorted({e["kind"] for e in evs})})
    out.sort(key=lambda x: (-x["max_times"], x["ticker"]))''',
     '''            # СОРТИРОВКА ТОЛЬКО ПО НАСТОЯЩЕЙ КРАТНОСТИ. Раньше верх доски
            # занимали бумаги с шумом в знаменателе: ASTR ×30.0, RASP ×28.77 —
            # выше всего, что было на рынке на самом деле.
            got = [e["times"] for e in evs if e.get("times") is not None]
            out.append({"ticker": tk, "events": evs, "count": len(evs),
                        "max_times": round(max(got), 2) if got else None,
                        "no_multiple": not got,
                        "kinds": sorted({e["kind"] for e in evs})})
    out.sort(key=lambda x: (-(x["max_times"] or 0), x["ticker"]))'''),

    # ─── 4. Старый тест абсурда читает новое имя ────────────────────
    (TST,
     '''    assert got[0]["base_thin"] is True
    assert got[0]["times"] > 100
    assert "раза выше нормы" not in got[0]["why"]''',
     '''    assert got[0]["base_thin"] is True
    assert "times" not in got[0], "кратности к шуму не существует"
    assert got[0]["times_vs_thin_base"] > 100, "но абсурд виден под другим именем"
    assert "раза выше нормы" not in got[0]["why"]'''),

    # ─── 5. Фикстура порядка: у SMALL была тонкая норма 9 000 ₽ ────────
    (TST,
     '''    small = steady(12, vol=100, close=90.0) + [bar(12, 5000, 90.0),
                                               bar(13, 5000, 90.0)]''',
     '''    # У SMALL теперь норма 90 тыс ₽, а не 9 тыс: проверяется порядок
    # сортировки, а не поведение на шуме — иначе тест держал бы сразу
    # две разные вещи и сломался бы от любой из них.
    small = steady(12, vol=1000, close=90.0) + [bar(12, 10000, 90.0),
                                                bar(13, 10000, 90.0)]'''),
]

NEW_TESTS = '''

def test_thin_base_has_no_multiple_field_at_all():
    """
    04.08 на живом экране: ASTR times:30.0 при base_rub:11223, RASP
    times:28.77 при base_rub:8945. Текст честно говорил «кратность считать
    не по чему», а поле рядом говорило обратное. Побеждает поле: его
    читают программы.
    """
    rows = [bar(i, 35, close=100.0) for i in range(12)]       # норма 3 500 ₽
    rows += [bar(12, 8936, close=100.0), bar(13, 8936, close=100.0)]
    got = [e for e in detect_step(rows, 1, lot=1) if e["kind"] == "volume_surge"]
    assert got
    e = got[0]
    assert e["base_thin"] is True
    assert "times" not in e, "поля кратности нет вовсе"
    assert e["times_vs_thin_base"] > 200, "число сохранилось под другим именем"
    assert e["rub"] > 0 and e["base_rub"] > 0, "сами деньги на месте"


def test_accelerating_on_thin_base_also_loses_the_multiple():
    """Разгон из ничего — такая же неправда, что и всплеск из ничего."""
    rows = [bar(i, 35, close=100.0) for i in range(10)]       # норма 3 500 ₽
    rows += [bar(10, 3000, 100.0), bar(11, 5000, 100.0), bar(12, 9000, 100.0)]
    rows.append(bar(13, 9000, 100.0))
    got = [e for e in detect_step(rows, 1, lot=1)
           if e["kind"] == "volume_accelerating"]
    if got:
        assert "times" not in got[0]
        assert got[0]["times_vs_thin_base"] > 0


def test_thin_ticker_is_not_ranked_above_real_money():
    """
    ГЛАВНОЕ ПОСЛЕДСТВИЕ. Сортировка шла по times, и верх доски занимал
    шум: ASTR ×30 на 11 тыс ₽ стоял выше всего настоящего.
    """
    real = steady(12, vol=1000, close=100.0)                 # норма 100 тыс ₽
    real += [bar(12, 5000, 100.0), bar(13, 5000, 100.0)]     # 500 тыс ₽, ×5
    thin = [bar(i, 35, close=100.0) for i in range(12)]      # норма 3 500 ₽
    thin += [bar(12, 8936, close=100.0), bar(13, 8936, close=100.0)]
    got = scan({"REAL": real, "THIN": thin}, lots={"REAL": 1, "THIN": 1})
    assert [x["ticker"] for x in got][0] == "REAL", "шум не возглавляет доску"
    row = [x for x in got if x["ticker"] == "THIN"][0]
    assert row["max_times"] is None, "кратности нет и у строки целиком"
    assert row["no_multiple"] is True, "и это названо явно, а не нулём"
    assert row["events"], "само событие осталось: деньги пришли"


def test_normal_money_keeps_the_multiple_field():
    real = steady(12, vol=1000, close=100.0)
    real += [bar(12, 5000, 100.0), bar(13, 5000, 100.0)]
    got = scan({"REAL": real}, lots={"REAL": 1})
    assert got[0]["max_times"] >= 3
    assert got[0]["no_multiple"] is False
'''


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, t):
    with open(p, "w", encoding="utf-8") as f:
        f.write(t)


fails = []
for path, old, new in EDITS:
    text = read(path)
    if new in text:
        print(" = уже применено:", path)
        continue
    n = text.count(old)
    if n != 1:
        fails.append(f"{path}: якорь найден {n} раз — {old.strip()[:60]}")
        continue
    write(path, text.replace(old, new, 1))
    print(" + правка:", path)

if fails:
    raise SystemExit("НЕ ПРИМЕНЕНО:\n" + "\n".join(fails))

test = read(TST)
if "def test_thin_base_has_no_multiple_field_at_all(" in test:
    print(" = тесты уже есть")
else:
    write(TST, test.rstrip("\n") + "\n" + NEW_TESTS)
    print(" + тесты дописаны")
