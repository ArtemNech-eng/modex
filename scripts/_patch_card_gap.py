"""Патч: диагностика отсутствующего профиля объёма. Идемпотентен."""

SRC = "src/analysis/volume_events.py"
TST = "tests/test_volume_events.py"
MAIN = "main.py"

FUNC = '''

def profile_gap(rows_by_day, min_days: int = MIN_DAYS) -> dict:
    """
    ПОЧЕМУ профиля нет — числами, а не словами «дней пока мало».

    04.08 в проде: vol_profiles:0 при 240 минутных барах в памяти. По логу
    нельзя было отличить две разные вещи: история ещё копится (стрим
    поднялся 01.08, а 01–02.08 — выходные, то есть торговый день в базе
    всего один) — или строитель молча сломан. Первое требует подождать,
    второе — починить, и путать их дороже всего.

    Критерии те же, что у day_profile: выходные не считаются, день короче
    MIN_BARS_DAY баров не считается. Согласие с настоящим фильтром
    проверяется тестом ровно на границе: диагностика, которая расходится
    с тем, что проверяет, хуже отсутствия диагностики.
    """
    from datetime import datetime as _dt
    weekend = short = empty = 0
    usable = []
    for day, rows in (rows_by_day or {}).items():
        n = len(rows or [])
        try:
            wd = _dt.strptime(str(day)[:10], "%Y-%m-%d").weekday()
        except Exception:                                        # noqa: BLE001
            empty += 1        # ключ дня непонятен — день не считается
            continue
        if wd >= 5:
            weekend += 1
        elif n == 0:
            empty += 1
        elif n < MIN_BARS_DAY:
            short += 1
        else:
            usable.append(str(day)[:10])
    missing = max(0, min_days - len(usable))
    return {"days_in_db": len(rows_by_day or {}),
            "usable_days": len(usable),
            "need_days": min_days,
            "missing_days": missing,
            "weekend_days": weekend,
            "short_days": short,
            "empty_days": empty,
            "min_bars_day": MIN_BARS_DAY,
            "ready": missing == 0,
            "days": sorted(usable)}
'''

TESTS = '''

from src.analysis.volume_events import profile_gap, MIN_BARS_DAY  # noqa: E402


def test_profile_gap_agrees_with_the_real_filter_at_the_boundary():
    """
    ГЛАВНОЕ. Диагностика, которая расходится с настоящим фильтром,
    хуже, чем её отсутствие: она будет говорить «готово» при пустом
    профиле. Проверяется совпадение РОВНО на границе.
    """
    days = weekdays(MIN_DAYS)
    rows = {d: day_rows(d, 100) for d in days}
    g = profile_gap(rows)
    assert g["usable_days"] == MIN_DAYS
    assert g["ready"] is True and g["missing_days"] == 0
    assert day_profile(rows, lot=1), "профиль строится ровно тогда же"

    fewer = {d: day_rows(d, 100) for d in days[:-1]}
    g2 = profile_gap(fewer)
    assert g2["ready"] is False and g2["missing_days"] == 1
    assert not day_profile(fewer, lot=1), "и не строится ровно тогда же"


def test_profile_gap_names_weekends_and_short_days():
    """Именно это случилось в проде: стрим поднялся в субботу."""
    rows = {"2026-08-03": day_rows("2026-08-03", 100),          # понедельник
            "2026-08-04": day_rows("2026-08-04", 100),          # вторник
            "2026-08-05": day_rows("2026-08-05", 100),          # среда
            "2026-08-01": day_rows("2026-08-01", 100),          # суббота
            "2026-08-02": day_rows("2026-08-02", 100),          # воскресенье
            "2026-07-31": day_rows("2026-07-31", 100, minutes=50)}   # короткий
    g = profile_gap(rows)
    assert g["days_in_db"] == 6
    assert g["weekend_days"] == 2, "выходные названы отдельно"
    assert g["short_days"] == 1, "короткий день назван отдельно"
    assert g["usable_days"] == 3
    assert g["missing_days"] == MIN_DAYS - 3
    assert g["min_bars_day"] == MIN_BARS_DAY, "порог виден тому, кто читает"
    assert not day_profile(rows, lot=1)


def test_profile_gap_says_when_there_is_nothing_at_all():
    g = profile_gap({})
    assert g["days_in_db"] == 0 and g["usable_days"] == 0
    assert g["ready"] is False and g["missing_days"] == MIN_DAYS


def test_unparseable_day_is_not_counted_as_a_trading_day():
    g = profile_gap({"не-дата": day_rows("2026-08-03", 100)})
    assert g["usable_days"] == 0 and g["empty_days"] == 1


def test_the_background_builder_reports_the_gap_in_numbers():
    """Лог обязан называть числа, а не «дней пока мало»."""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "profile_gap" in src, "строитель обязан спрашивать причину"
    assert "торговых дней из" in src, "и называть её числами"
'''

EDITS = [
    (MAIN,
     "        from src.analysis.volume_events import day_profile, MIN_DAYS",
     "        from src.analysis.volume_events import (day_profile, profile_gap,\n"
     "                                                MIN_DAYS)"),
    (MAIN,
     '''                        for i in range(1, 31)]        # ПРОШЛЫЕ дни, без сегодня
                built = 0''',
     '''                        for i in range(1, 31)]        # ПРОШЛЫЕ дни, без сегодня
                built = 0
                gaps = []'''),
    (MAIN,
     '''                    if prof:
                        stream.vol_profiles[tk] = prof
                        built += 1
                if built:
                    logger.info(f"Норма объёма по времени суток: {built} бумаг")
                else:
                    logger.info("Норма объёма по времени суток: дней пока мало, "
                                "сканер работает по скользящей")''',
     '''                    if prof:
                        stream.vol_profiles[tk] = prof
                        built += 1
                    else:
                        # ПРИЧИНА ЧИСЛАМИ. «Дней пока мало» звучит одинаково
                        # и когда история копится, и когда строитель сломан.
                        gaps.append(profile_gap(per_day, min_days=MIN_DAYS))
                if built:
                    logger.info(f"Норма объёма по времени суток: {built} бумаг")
                elif gaps:
                    g = max(gaps, key=lambda x: x["usable_days"])
                    stream.profile_gap = g
                    logger.info(
                        "Норма объёма по времени суток не построена: лучшая "
                        "бумага имеет %d торговых дней из %d нужных; в базе "
                        "%d дней (выходных %d, коротких %d, пустых %d), "
                        "день считается от %d баров. Сканер работает по "
                        "скользящей.",
                        g["usable_days"], MIN_DAYS, g["days_in_db"],
                        g["weekend_days"], g["short_days"], g["empty_days"],
                        g["min_bars_day"])
                else:
                    logger.info("Норма объёма по времени суток: минутной "
                                "истории в базе нет вовсе — профилировать нечего")'''),
]


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
        fails.append(f"{path}: якорь найден {n} раз — {old.strip()[:70]}")
        continue
    write(path, text.replace(old, new, 1))
    print(" + правка:", path)

if fails:
    raise SystemExit("НЕ ПРИМЕНЕНО:\n" + "\n".join(fails))

src = read(SRC)
if "def profile_gap(" in src:
    print(" = функция уже есть")
else:
    write(SRC, src.rstrip("\n") + "\n" + FUNC)
    print(" + profile_gap дописан")

tst = read(TST)
if "def test_profile_gap_agrees_with_the_real_filter_at_the_boundary(" in tst:
    print(" = тесты уже есть")
else:
    write(TST, tst.rstrip("\n") + "\n" + TESTS)
    print(" + тесты дописаны")
