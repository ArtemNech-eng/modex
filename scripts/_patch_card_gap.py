"""
Патч: диагностика отсутствующего профиля объёма по времени суток.

УРОК ДВУХ ПРОГОНОВ: соседний тест читает РОВНО 2500 символов после
`async def _volume_profiles` и ищет в них sleep(3600). Добавленные строки
вытолкнули sleep за окно, и тест упал не потому, что код плохой, а
потому, что ослеп. Поэтому вся длинная фраза живёт в чистой функции,
в main.py остаются две строки, а окно проверяется здесь же.

Идемпотентен.
"""

SRC = "src/analysis/volume_events.py"
TST = "tests/test_volume_events.py"
MAIN = "main.py"
WINDOW = 2500


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
        raise SystemExit(f"НЕ ПРИМЕНЕНО: {what} — {want!r} найдено {len(hits)} раз")
    return hits[0]


# ---------------------------------------------------------------- main.py
main = read(MAIN)
if "profile_gap" in main:
    print(" = main.py уже пропатчен")
else:
    imp = "import day_profile, MIN_DAYS"
    if main.count(imp) != 1:
        raise SystemExit(f"НЕ ПРИМЕНЕНО: импорт найден {main.count(imp)} раз")
    main = main.replace(
        imp, "import day_profile, profile_gap, profile_note, MIN_DAYS", 1)

    lines = main.split("\n")

    # 1) сбор причин рядом со счётчиком успехов
    i = only(lines, "built = 0", "сброс счётчика")
    lines.insert(i + 1, indent_of(lines[i]) + "gaps = []")

    # 2) профиль не вышел — запомнить ПОЧЕМУ
    i = only(lines, "built += 1", "инкремент счётчика")
    inner = indent_of(lines[i])
    outer = inner[:-4]
    lines[i + 1:i + 1] = [
        outer + "else:",
        outer + "    gaps.append(profile_gap(per_day, min_days=MIN_DAYS))",
    ]

    # 3) в лог — числа вместо «дней пока мало»
    s = only(lines, "if built:", "начало отчёта")
    end = None
    for j in range(s, min(s + 14, len(lines))):
        if "скользящей" in lines[j]:
            end = j
            break
    if end is None:
        raise SystemExit("НЕ ПРИМЕНЕНО: не нашёл конец отчёта в лог")
    ind = indent_of(lines[s])
    lines[s:end + 1] = [
        ind + "if built:",
        ind + '    logger.info(f"Норма объёма по времени суток: {built} бумаг")',
        ind + "else:",
        ind + "    stream.profile_note = profile_note(gaps)",
        ind + '    logger.info("Норма объёма по времени суток %s",',
        ind + "                stream.profile_note)",
    ]

    main = "\n".join(lines)

    # ГЛАВНАЯ ПРОВЕРКА: соседний тест читает только 2500 символов
    at = main.index("async def _volume_profiles")
    if "3600" not in main[at:at + WINDOW]:
        raise SystemExit(f"НЕ ПРИМЕНЕНО: функция раздулась, sleep(3600) вышел "
                         f"за окно в {WINDOW} символов — соседний тест ослепнет")

    write(MAIN, main)
    print(" + main.py: причина теперь в числах, окно цело")

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


def profile_note(gaps) -> str:
    """
    Одна фраза для человека из собранных profile_gap чисел.

    Живёт ЗДЕСЬ, а не в main.py, по двум причинам: её можно проверить
    тестом без запуска конвейера, и фоновая функция в main.py остаётся
    короткой — соседний тест смотрит на неё окном в 2500 символов, и
    раздувшаяся функция его ослепляет. Так упали два прогона 04.08.
    """
    if not gaps:
        return "не построена: минутной истории в базе нет вовсе"
    g = max(gaps, key=lambda x: x.get("usable_days", 0))
    return ("не построена: лучшая бумага имеет {u} торговых дней из {n} "
            "нужных; в базе {d} дней (выходных {w}, коротких {s}, пустых "
            "{e}), день считается от {b} баров — сканер работает по "
            "скользящей").format(
        u=g.get("usable_days", 0), n=g.get("need_days", MIN_DAYS),
        d=g.get("days_in_db", 0), w=g.get("weekend_days", 0),
        s=g.get("short_days", 0), e=g.get("empty_days", 0),
        b=g.get("min_bars_day", MIN_BARS_DAY))
'''

src = read(SRC)
if "def profile_gap(" in src:
    print(" = profile_gap уже есть")
else:
    write(SRC, src.rstrip("\n") + "\n" + FUNC)
    print(" + profile_gap и profile_note дописаны")

# ------------------------------------------------ tests/test_volume_events.py
TESTS = '''

from src.analysis.volume_events import (profile_gap, profile_note,  # noqa: E402
                                        MIN_BARS_DAY)


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


def test_the_note_carries_the_numbers_not_just_words():
    """«Дней пока мало» звучит одинаково и при поломке строителя."""
    rows = {"2026-08-03": day_rows("2026-08-03", 100),
            "2026-08-01": day_rows("2026-08-01", 100)}
    note = profile_note([profile_gap(rows)])
    assert "1 торговых дней из %d" % MIN_DAYS in note
    assert "выходных 1" in note
    assert str(MIN_BARS_DAY) in note, "порог дня назван"


def test_the_note_says_when_there_is_nothing_at_all():
    assert "нет вовсе" in profile_note([])


def test_the_builder_asks_for_the_reason_and_stays_short():
    """
    Соседний тест читает РОВНО 2500 символов после начала функции.
    04.08 два прогона упали именно потому, что добавленные строки
    вытолкнули sleep(3600) за это окно и тест ОСЛЕП, а не нашёл баг.
    """
    m = (ROOT / "main.py").read_text(encoding="utf-8")
    i = m.index("async def _volume_profiles")
    body = m[i:i + 2500]
    assert "profile_gap" in body, "строитель обязан спрашивать причину"
    assert "profile_note" in body, "и класть её в лог числами"
    assert "3600" in body, "и оставаться коротким: раз в час видно в окне"
'''

tst = read(TST)
if "def test_profile_gap_agrees_with_the_real_filter_at_the_boundary(" in tst:
    print(" = тесты уже есть")
else:
    write(TST, tst.rstrip("\n") + "\n" + TESTS)
    print(" + тесты дописаны")
