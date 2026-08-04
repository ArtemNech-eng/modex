"""
Патч: диагностика отсутствующего профиля объёма по времени суток.

Идемпотентен. Правки ищут строки по СОДЕРЖАНИЮ, а не по точным пробелам:
прошлый прогон умер именно на выравнивании комментария в якоре.
"""

SRC = "src/analysis/volume_events.py"
TST = "tests/test_volume_events.py"
MAIN = "main.py"


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, t):
    with open(p, "w", encoding="utf-8") as f:
        f.write(t)


def indent_of(line):
    return line[: len(line) - len(line.lstrip())]


def only(lines, want, what):
    hits = [i for i, l in enumerate(lines) if l.strip() == want]
    if len(hits) != 1:
        near = "\n".join(f"{i}: {lines[i]!r}" for i in hits[:5])
        raise SystemExit(f"НЕ ПРИМЕНЕНО: {what} — строка {want!r} найдена "
                         f"{len(hits)} раз\n{near}")
    return hits[0]


# ---------------------------------------------------------------- main.py
main = read(MAIN)
if "profile_gap" in main:
    print(" = main.py уже пропатчен")
else:
    imp = "import day_profile, MIN_DAYS"
    if main.count(imp) != 1:
        raise SystemExit(f"НЕ ПРИМЕНЕНО: импорт найден {main.count(imp)} раз")
    main = main.replace(imp, "import day_profile, profile_gap, MIN_DAYS", 1)

    lines = main.split("\n")

    # 1) счётчик причин рядом со счётчиком успехов
    i = only(lines, "built = 0", "сброс счётчика")
    ind = indent_of(lines[i])
    lines.insert(i + 1, ind + "gaps = []")

    # 2) когда профиль не вышел — спросить ПОЧЕМУ
    i = only(lines, "built += 1", "инкремент счётчика")
    inner = indent_of(lines[i])          # тело if prof:
    outer = inner[:-4]                   # сам if prof:
    lines[i + 1:i + 1] = [
        outer + "else:",
        outer + "    # ПРИЧИНА ЧИСЛАМИ. «Дней пока мало» звучит одинаково",
        outer + "    # и когда история копится, и когда строитель сломан,",
        outer + "    # а это требует разного: подождать или починить.",
        outer + "    gaps.append(profile_gap(per_day, min_days=MIN_DAYS))",
    ]

    # 3) отчёт в лог: числа вместо «дней пока мало»
    s = only(lines, "if built:", "начало отчёта")
    end = None
    for j in range(s, min(s + 14, len(lines))):
        if "скользящей" in lines[j]:
            end = j
            break
    if end is None:
        dump = "\n".join(f"{k}: {lines[k]!r}" for k in range(s, min(s + 14, len(lines))))
        raise SystemExit("НЕ ПРИМЕНЕНО: не нашёл конец отчёта\n" + dump)
    ind = indent_of(lines[s])
    lines[s:end + 1] = [
        ind + "if built:",
        ind + '    logger.info(f"Норма объёма по времени суток: {built} бумаг")',
        ind + "elif gaps:",
        ind + '    g = max(gaps, key=lambda x: x["usable_days"])',
        ind + "    stream.profile_gap = g",
        ind + "    logger.info(",
        ind + '        "Норма объёма по времени суток не построена: лучшая "',
        ind + '        "бумага имеет %d торговых дней из %d нужных; в базе "',
        ind + '        "%d дней (выходных %d, коротких %d, пустых %d), день "',
        ind + '        "считается от %d баров. Сканер работает по скользящей.",',
        ind + '        g["usable_days"], MIN_DAYS, g["days_in_db"],',
        ind + '        g["weekend_days"], g["short_days"], g["empty_days"],',
        ind + '        g["min_bars_day"])',
        ind + "else:",
        ind + '    logger.info("Норма объёма по времени суток: минутной истории "',
        ind + '                "в базе нет вовсе — профилировать нечего")',
    ]

    write(MAIN, "\n".join(lines))
    print(" + main.py: причина теперь в числах")

# ------------------------------------------------- src/analysis/volume_events.py
FUNC = '''

def profile_gap(rows_by_day, min_days: int = MIN_DAYS) -> dict:
    """
    ПОЧЕМУ профиля нет — числами, а не словами «дней пока мало».

    04.08 в проде: vol_profiles:0 при 240 минутных барах в памяти. По логу
    нельзя было отличить две разные вещи: история ещё копится (стрим
    поднялся 01.08, а 01–02.08 — выходные, то есть торговый день в базе
    всего один) — или строитель молча сломан. Первое требует подождать,
    второе — починить, и путать их дорого.

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

src = read(SRC)
if "def profile_gap(" in src:
    print(" = profile_gap уже есть")
else:
    write(SRC, src.rstrip("\n") + "\n" + FUNC)
    print(" + profile_gap дописан")

# ------------------------------------------------ tests/test_volume_events.py
TESTS = '''

from src.analysis.volume_events import profile_gap, MIN_BARS_DAY  # noqa: E402


def test_profile_gap_agrees_with_the_real_filter_at_the_boundary():
    """
    ГЛАВНОЕ. Диагностика, расходящаяся с настоящим фильтром, хуже
    её отсутствия: она скажет «готово» при пустом профиле.
    Проверяется совпадение РОВНО на границе.
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
    rows = {"2026-08-03": day_rows("2026-08-03", 100),            # понедельник
            "2026-08-04": day_rows("2026-08-04", 100),            # вторник
            "2026-08-05": day_rows("2026-08-05", 100),            # среда
            "2026-08-01": day_rows("2026-08-01", 100),            # суббота
            "2026-08-02": day_rows("2026-08-02", 100),            # воскресенье
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

tst = read(TST)
if "def test_profile_gap_agrees_with_the_real_filter_at_the_boundary(" in tst:
    print(" = тесты уже есть")
else:
    write(TST, tst.rstrip("\n") + "\n" + TESTS)
    print(" + тесты дописаны")
