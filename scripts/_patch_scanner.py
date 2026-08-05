"""
Патч сканера объёма: свежесть последнего бара и непрерывность ускорения.

Два дефекта, из-за которых сканер мог отдать читателю неправду.

1. НЕТ ПРОВЕРКИ ВРЕМЕНИ. detect_step берёт последний закрытый бар и сравнивает
   его с нормой, не спрашивая, когда этот бар закрылся. Если поток встал в
   14:10, то в 16:00 сканер по-прежнему покажет «оборот в 12 раз выше нормы» с
   меткой 14:09. Метку времени рядом читает человек, а поля берут дашборд,
   сортировка и агент — они увидят событие, которого сейчас нет. Ночью то же
   самое: в 01:00 отдаётся всплеск закрытия вечерней сессии.

2. «ТРИ БАРА ПОДРЯД» НЕ ПРОВЕРЯЕТ, ЧТО ОНИ ПОДРЯД. tail берётся срезом
   vals[-gb-1:], а бары в ряду не обязаны идти минута в минуту: тихие минуты
   пропущены, между вечерней и утренней сессией лежит семь часов. Ряд
   23:47 → 23:49 → 06:52 формально «растёт три бара подряд», а на деле это
   разные дни.

Скрипт идемпотентен: если правка уже стоит, шаг пропускается. Если якорь не
найден ровно один раз — печатается ОТКАЗ и выход с кодом 0: падать должен
тест, а не применитель.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VE = "src/analysis/volume_events.py"

ok: list = []
fail: list = []


def patch(rel: str, old: str, new: str, label: str) -> bool:
    p = ROOT / rel
    src = p.read_text(encoding="utf-8")
    if new in src:
        ok.append(f"{label}: уже стоит")
        return True
    n = src.count(old)
    if n != 1:
        fail.append(f"{label}: ОТКАЗ — якорь найден {n} раз(а)")
        return False
    p.write_text(src.replace(old, new), encoding="utf-8")
    ok.append(f"{label}: применено")
    return True


# --- A. Часы, возраст бара и непрерывность ряда ---------------------------

A_OLD = '''def detect_step(rows: list, step: int, lot: int = 1,
                profile: Optional[dict] = None,
                p: Optional[dict] = None) -> list:'''

A_NEW = '''# СВЕЖЕСТЬ ДАННЫХ. Бар не становится неправдой оттого, что состарился, но
# СОБЫТИЕ становится: «оборот в 12 раз выше нормы» читается как «сейчас», а
# метку времени рядом читает человек и не читает дашборд.
#
# Часы московские. Минутные ключи в базе московские (msk_minute в stream.py), а
# контейнер живёт по UTC: сравнение московского бара с UTC-часами состарило бы
# каждый бар ровно на три часа и выключило бы сканер целиком.
MSK_SHIFT_H = 3

# Предельный возраст меряется в ШАГАХ: у минутки три минуты, у пятиминутки
# пятнадцать. Отрицательный возраст не отбрасывается — это разметка бара
# минутой вперёд, а не данные из будущего.
MAX_AGE_STEPS = 3


def _now_msk():
    return _dt.datetime.utcnow() + _dt.timedelta(hours=MSK_SHIFT_H)


def _as_dt(ts):
    try:
        return _dt.datetime.strptime(str(ts)[:16], "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return None


def _age_min(ts, at=None) -> Optional[float]:
    """Сколько минут назад закрылся бар. `at` — «сейчас», задаётся в тестах."""
    t = _as_dt(ts)
    ref = _as_dt(at) if at else _now_msk()
    if t is None or ref is None:
        return None
    return round((ref - t).total_seconds() / 60.0, 1)


def _contiguous(bs: list, step: int) -> bool:
    """
    Идут ли бары подряд, минута в минуту.

    Срез списка ничего не говорит о времени: между соседними элементами может
    лежать пропущенная тихая минута или целая ночь. Ряд 23:47 → 23:49 → 06:52
    как срез выглядит непрерывным.
    """
    ts = [_as_dt(b.get("ts")) for b in (bs or ())]
    if len(ts) < 2 or any(t is None for t in ts):
        return False
    gap = _dt.timedelta(minutes=step)
    return all(b - a == gap for a, b in zip(ts, ts[1:]))


def detect_step(rows: list, step: int, lot: int = 1,
                profile: Optional[dict] = None,
                p: Optional[dict] = None, at=None) -> list:'''

# --- B. Событие только на свежем баре -------------------------------------

B_OLD = '''    last = closed[-1]
    now = vals[-1]
    if now <= 0:
        return []'''

B_NEW = '''    last = closed[-1]
    now = vals[-1]
    if now <= 0:
        return []
    # СТАРЫЙ БАР — НЕ СОБЫТИЕ. Поток мог встать, бумага могла уйти с торгов, а
    # на дворе может быть ночь после вечерней сессии. Возраст кладётся в само
    # событие: пусть тот, кто берёт поля, видит его наравне с кратностью.
    age = _age_min(last.get("ts"), at)
    if age is None or age > p["max_age"] * step:
        return []'''

# --- C. Ускорение только на непрерывных барах ------------------------------

C_OLD = '''    gb = p["grow_bars"]
    if len(vals) >= gb + 1 and mult >= p["grow_min_mult"]:
        tail = vals[-gb - 1:]'''

C_NEW = '''    gb = p["grow_bars"]
    # Непрерывность обязательна: «подряд» — про время, а не про соседство в
    # списке. Дыра в ряду превращала вечер и следующее утро в один разгон.
    if (len(vals) >= gb + 1 and mult >= p["grow_min_mult"]
            and _contiguous(closed[-gb - 1:], step)):
        tail = vals[-gb - 1:]'''

# --- D и E. Возраст в оба события ------------------------------------------

D_OLD = '''            step, last, rub=round(now), base_rub=round(base),
            times=(None if thin else round(mult, 2)),
            times_vs_thin_base=(round(mult, 2) if thin else None),
            base_source=source, base_thin=thin))'''

D_NEW = '''            step, last, rub=round(now), base_rub=round(base),
            times=(None if thin else round(mult, 2)),
            times_vs_thin_base=(round(mult, 2) if thin else None),
            age_min=age, base_source=source, base_thin=thin))'''

E_OLD = '''                bars_growing=gb,
                series=[round(v) for v in tail], base_source=source,
                base_thin=thin))'''

E_NEW = '''                bars_growing=gb, age_min=age,
                series=[round(v) for v in tail], base_source=source,
                base_thin=thin))'''

# --- F. Порог возраста настраивается через p -------------------------------

F_OLD = '''DEFAULTS = {"look": LOOK, "surge": SURGE, "grow_bars": GROW_BARS,
            "grow": GROW, "grow_min_mult": GROW_MIN_MULT,
            "floor": FLOOR_RUB, "thin": THIN_RUB}'''

F_NEW = '''DEFAULTS = {"look": LOOK, "surge": SURGE, "grow_bars": GROW_BARS,
            "grow": GROW, "grow_min_mult": GROW_MIN_MULT,
            "floor": FLOOR_RUB, "thin": THIN_RUB,
            "max_age": MAX_AGE_STEPS}'''

# --- G. «Сейчас» протягивается через detect и scan --------------------------

G_OLD = '''def detect(rows: list, lot: int = 1, profile: Optional[dict] = None,
           steps: tuple = STEPS, p: Optional[dict] = None) -> list:
    out = []
    for st in steps:
        out.extend(detect_step(rows, st, lot=lot, profile=profile, p=p))
    return out'''

G_NEW = '''def detect(rows: list, lot: int = 1, profile: Optional[dict] = None,
           steps: tuple = STEPS, p: Optional[dict] = None, at=None) -> list:
    out = []
    for st in steps:
        out.extend(detect_step(rows, st, lot=lot, profile=profile, p=p, at=at))
    return out'''

H_OLD = '''def scan(minutes: dict, lots: Optional[dict] = None,
         profiles: Optional[dict] = None, steps: tuple = STEPS,
         p: Optional[dict] = None) -> list:'''

H_NEW = '''def scan(minutes: dict, lots: Optional[dict] = None,
         profiles: Optional[dict] = None, steps: tuple = STEPS,
         p: Optional[dict] = None, at=None) -> list:'''

I_OLD = '''        evs = detect(list(rows or ()), lot=(lots or {}).get(tk) or 1,
                     profile=(profiles or {}).get(tk), steps=steps, p=p)'''

I_NEW = '''        evs = detect(list(rows or ()), lot=(lots or {}).get(tk) or 1,
                     profile=(profiles or {}).get(tk), steps=steps, p=p,
                     at=at)'''

# --- J. Молчание обязано называть причину ----------------------------------

J_OLD = '''def rates(scanned: list, total: int) -> dict:'''

J_NEW = '''def stale(minutes: dict, at=None, p: Optional[dict] = None) -> int:
    """
    Сколько бумаг молчат не потому, что тихо, а потому что данные устарели.

    Без этого числа пустая таблица читается как «на рынке спокойно», хотя
    правда может быть «поток встал полчаса назад». Это тот же урок, что дали
    below_floor и warming_up: молчание обязано называть причину.
    """
    p = {**DEFAULTS, **(p or {})}
    n = 0
    for _tk, rows in (minutes or {}).items():
        cl = [b for b in bars(list(rows or ()), 1) if b.get("complete")]
        if not cl:
            continue
        age = _age_min(cl[-1].get("ts"), at)
        if age is None or age > p["max_age"]:
            n += 1
    return n


def rates(scanned: list, total: int) -> dict:'''


def main() -> None:
    patch(VE, A_OLD, A_NEW, "часы и непрерывность")
    patch(VE, B_OLD, B_NEW, "событие только на свежем баре")
    patch(VE, C_OLD, C_NEW, "ускорение только подряд")
    patch(VE, D_OLD, D_NEW, "возраст во всплеске")
    patch(VE, E_OLD, E_NEW, "возраст в ускорении")
    patch(VE, F_OLD, F_NEW, "порог возраста в DEFAULTS")
    patch(VE, G_OLD, G_NEW, "detect протягивает время")
    patch(VE, H_OLD, H_NEW, "scan принимает время")
    patch(VE, I_OLD, I_NEW, "scan протягивает время")
    patch(VE, J_OLD, J_NEW, "счётчик устаревших")

    for line in ok + fail:
        print(line)
    print(f"итог: применено {len(ok)}, отказов {len(fail)}")
    if not fail:
        Path(__file__).unlink()
        print("Скрипт удалил себя.")


if __name__ == "__main__":
    main()
