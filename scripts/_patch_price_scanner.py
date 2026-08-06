"""
Сканер цены: возраст события, непрерывность ряда и дата в ключе бара.

Три дефекта, найденные разбором 06.08.

1. ПРОВЕРКИ ВОЗРАСТА НЕТ ВОВСЕ. `detect_step` берёт последний закрытый бар
   и не спрашивает, когда тот закрылся. Встанет поток в 12:00 — в 18:00 сканер
   всё ещё будет писать «ход бара в 3.4 раза больше обычного». Событие — это
   заявление про «сейчас», и без возраста оно никем не проверено.

2. НОЧЬ СКЛЕЕНА С УТРОМ. Ходы считаются между соседями ПО СПИСКУ, а между
   23:49 и 06:50 в списке нет ничего. Ночной разрыв приходил как резкое
   ускорение, и той же болезнью болели «три бара стояли», откат и смена
   структуры. «Подряд» — это про время, а не про соседство в списке.

3. НАСТОЯЩИЙ БАР ИСЧЕЗАЛ ИЗ СЕРЕДИНЫ. Ключ бара — минута суток, делённая
   на шаг, без даты. В 10:42 на шаге 30 ключ равен 21 — и вчерашний бар
   10:30–10:59 с тем же ключом объявлялся формирующимся и выбрасывался.

ПРАВИЛО ПРИМЕНЕНИЯ. Якорь обязан найтись РОВНО ОДИН раз. Иначе — ОТКАЗ, и
файл не трогается вовсе: частично применённый патч хуже неприменённого.
Повторный запуск безопасен: у каждой правки есть метка «уже сделано».
"""
import sys
from pathlib import Path

PRICE = Path("src/analysis/price_events.py")
TF = Path("src/analysis/timeframes.py")

HELPERS = '''

MSK_SHIFT_H = 3        # контейнер живёт по UTC, ключи баров московские
MAX_AGE_STEPS = 2      # столько баров своего шага событию позволено прожить
MAX_HOLE_MIN = 15      # дыра длиннее — разрыв сессии, а не тихие минуты


def _now_msk() -> datetime:
    """Московское время без зоны: метки баров тоже без зоны и московские."""
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(hours=MSK_SHIFT_H))


def _as_dt(ts):
    try:
        return datetime(int(ts[0:4]), int(ts[5:7]), int(ts[8:10]),
                        int(ts[11:13]), int(ts[14:16]))
    except (TypeError, ValueError, IndexError):
        return None


def _age_min(ts, step: int = 1, at=None):
    """
    Сколько минут прошло с ЗАКРЫТИЯ бара.

    Метка бара — его ПЕРВАЯ минута, поэтому к ней прибавляется шаг. Иначе
    только что закрывшийся получасовой бар выглядел бы получасовой стариной.

    Отрицательный возраст допустим: метки биржи бывают впереди часов.
    """
    dt = _as_dt(ts)
    if dt is None:
        return None
    end = dt + timedelta(minutes=max(1, int(step or 1)))
    return round(((at or _now_msk()) - end).total_seconds() / 60.0, 1)


def _last_run(closed: list, step: int, max_hole_min: int) -> list:
    """
    Оставить последний отрезок баров, идущих подряд ВО ВРЕМЕНИ.

    Почему здесь, а не в каждом детекторе: разрыв одинаково портит и ходы, и
    структуру, и откат, и пробои. Одно место лечения вместо четырёх.

    Пятнадцать минут выбраны так, чтобы вечерний перерыв 18:40–18:59 рвал
    отрезок, а тихие минуты неликвида — нет.
    """
    if len(closed) < 2:
        return closed
    i = len(closed) - 1
    while i > 0:
        k1 = closed[i].get("key")
        k0 = closed[i - 1].get("key")
        if not isinstance(k1, int) or not isinstance(k0, int):
            break
        if (k1 - k0 - 1) * max(1, int(step or 1)) > max_hole_min:
            break
        i -= 1
    return closed[i:]
'''

STALE = '''def stale(minutes: dict, at=None, p=None) -> int:
    """
    Сколько бумаг молчат не потому, что тихо, а потому, что данные протухли.

    Пустая таблица имеет несколько разных причин, и они обязаны различаться.
    Без этого числа молчание сканера неотличимо от исправной тишины.
    """
    p = {**DEFAULTS, **(p or {})}
    n = 0
    for rows in (minutes or {}).values():
        rows = list(rows or ())
        if not rows:
            continue
        age = _age_min(rows[-1].get("ts"), 1, at)
        if age is None or age > p["max_age"]:
            n += 1
    return n


'''

ABS_MINUTE = '''def _abs_minute(ts: str) -> Optional[int]:
    """
    Минута от начала эпохи, а не минута суток.

    Ключ бара считался как минута суток, делённая на шаг. Из-за этого вчерашние
    10:30 и сегодняшние 10:30 были ОДНИМ ключом: вчерашний бар помечался
    формирующимся и выбрасывался из середины истории, а ночь склеивалась с утром.

    Границы баров остаются там же, где были: день делится на шаг нацело
    (1440 % шаг == 0 для 1, 3, 5, 15 и 30).
    """
    mm = _minute_of_day(ts)
    if mm is None:
        return None
    try:
        base = date(int(ts[0:4]), int(ts[5:7]), int(ts[8:10])).toordinal()
    except (TypeError, ValueError, IndexError):
        return mm
    return base * 1440 + mm


'''

PATCHES = [
    {
        "label": "price: импорт времени",
        "path": PRICE,
        "mark": "from datetime import datetime, timedelta, timezone",
        "old": "from statistics import median\nfrom typing import Optional\n",
        "new": ("from datetime import datetime, timedelta, timezone\n"
                "from statistics import median\nfrom typing import Optional\n"),
    },
    {
        "label": "price: часы, возраст и непрерывный отрезок",
        "path": PRICE,
        "mark": "def _age_min(",
        "old": "FALSE_BARS = 3         # столько баров есть у ложного пробоя, чтобы вернуться\n",
        "new": ("FALSE_BARS = 3         # столько баров есть у ложного пробоя, чтобы вернуться\n"
                + HELPERS),
    },
    {
        "label": "price: пороги времени в DEFAULTS",
        "path": PRICE,
        "mark": '"max_age": MAX_AGE_STEPS',
        "old": '            "false_bars": FALSE_BARS, "leg_scale": LEG_SCALE,\n            "pull_bars": PULL_BARS}',
        "new": ('            "false_bars": FALSE_BARS, "leg_scale": LEG_SCALE,\n'
                '            "pull_bars": PULL_BARS,\n'
                '            "max_age": MAX_AGE_STEPS, "max_hole": MAX_HOLE_MIN}'),
    },
    {
        "label": "price: ворота свежести в detect_step",
        "path": PRICE,
        "mark": 'closed = _last_run(closed, step, p["max_hole"])',
        "old": '''def detect_step(rows: list, step: int, tick: float = 0.01,
                levels: Optional[list] = None, p: Optional[dict] = None) -> list:
    """
    События одного шага. `rows` — минутные бары по возрастанию времени.

    Считается ТОЛЬКО по закрытым барам: незакрытый отбрасывается целиком.
    """
    p = {**DEFAULTS, **(p or {})}
    bs = bars(rows, step)
    closed = [b for b in bs if b.get("complete")]
    if len(closed) < NEED:
        return []''',
        "new": '''def detect_step(rows: list, step: int, tick: float = 0.01,
                levels: Optional[list] = None, p: Optional[dict] = None,
                at=None) -> list:
    """
    События одного шага. `rows` — минутные бары по возрастанию времени.

    Считается ТОЛЬКО по закрытым барам: незакрытый отбрасывается целиком.

    И только по последнему НЕПРЕРЫВНОМУ отрезку: дыра длиннее `max_hole`
    минут обрывает историю — ночь не является ходом цены.

    И только если последний бар свежий: событие не переживает бар, который
    описывает. Часы можно передать параметром `at` — иначе правило невозможно
    проверить тестом.
    """
    p = {**DEFAULTS, **(p or {})}
    bs = bars(rows, step)
    closed = [b for b in bs if b.get("complete")]
    closed = _last_run(closed, step, p["max_hole"])
    if len(closed) < NEED:
        return []
    age = _age_min(closed[-1].get("ts"), step, at)
    if age is None or age > p["max_age"] * max(1, int(step or 1)):
        return []''',
    },
    {
        "label": "price: возраст на каждом событии",
        "path": PRICE,
        "mark": 'e["age_min"] = _age_min(',
        "old": '''    flip = _structure_flip(closed, scale * p["leg_scale"] / 3)
    if flip:
        out.append(_ev("direction_changed", flip[1], step, last,
                       was=flip[0], now=flip[2]))
    return out''',
        "new": '''    flip = _structure_flip(closed, scale * p["leg_scale"] / 3)
    if flip:
        out.append(_ev("direction_changed", flip[1], step, last,
                       was=flip[0], now=flip[2]))
    #  Возраст СВОЙ у каждого события, а не общий: ложный пробой датируется
    #  баром возврата, и один на всех возраст был бы ложью.
    for e in out:
        e["age_min"] = _age_min(e.get("ts"), step, at)
    return out''',
    },
    {
        "label": "price: часы через detect",
        "path": PRICE,
        "mark": "detect_step(rows, st, tick=tick, levels=levels, p=p, at=at)",
        "old": '''def detect(rows: list, tick: float = 0.01, levels: Optional[list] = None,
           steps: tuple = STEPS, p: Optional[dict] = None) -> list:
    """Все события бумаги по всем шагам, от старых к новым."""
    out = []
    for st in steps:
        out.extend(detect_step(rows, st, tick=tick, levels=levels, p=p))
    return out''',
        "new": '''def detect(rows: list, tick: float = 0.01, levels: Optional[list] = None,
           steps: tuple = STEPS, p: Optional[dict] = None, at=None) -> list:
    """Все события бумаги по всем шагам, от старых к новым."""
    out = []
    for st in steps:
        out.extend(detect_step(rows, st, tick=tick, levels=levels, p=p, at=at))
    return out''',
    },
    {
        "label": "price: часы в events_for",
        "path": PRICE,
        "mark": "def events_for(rows: list, tick: float = 0.01, steps: tuple = STEPS,\n               p: Optional[dict] = None, at=None)",
        "old": ("def events_for(rows: list, tick: float = 0.01, steps: tuple = STEPS,\n"
                "               p: Optional[dict] = None) -> list:"),
        "new": ("def events_for(rows: list, tick: float = 0.01, steps: tuple = STEPS,\n"
                "               p: Optional[dict] = None, at=None) -> list:"),
    },
    {
        "label": "price: events_for передаёт часы",
        "path": PRICE,
        "mark": "levels=lv, steps=steps, p=p, at=at)",
        "old": "    return detect(rows, tick=tick or 0.01, levels=lv, steps=steps, p=p)",
        "new": "    return detect(rows, tick=tick or 0.01, levels=lv, steps=steps, p=p,\n                  at=at)",
    },
    {
        "label": "price: часы в scan",
        "path": PRICE,
        "mark": "         p: Optional[dict] = None, at=None) -> list:",
        "old": ("def scan(minutes: dict, ticks: Optional[dict] = None,\n"
                "         levels: Optional[dict] = None, steps: tuple = STEPS,\n"
                "         p: Optional[dict] = None) -> list:"),
        "new": ("def scan(minutes: dict, ticks: Optional[dict] = None,\n"
                "         levels: Optional[dict] = None, steps: tuple = STEPS,\n"
                "         p: Optional[dict] = None, at=None) -> list:"),
    },
    {
        "label": "price: scan → detect с часами",
        "path": PRICE,
        "mark": "levels=levels[tk], steps=steps, p=p, at=at)",
        "old": ("            evs = detect(list(rows or ())[-WINDOW:], tick=tick,\n"
                "                         levels=levels[tk], steps=steps, p=p)"),
        "new": ("            evs = detect(list(rows or ())[-WINDOW:], tick=tick,\n"
                "                         levels=levels[tk], steps=steps, p=p,\n"
                "                         at=at)"),
    },
    {
        "label": "price: scan → events_for с часами",
        "path": PRICE,
        "mark": "events_for(rows, tick=tick, steps=steps, p=p, at=at)",
        "old": "            evs = events_for(rows, tick=tick, steps=steps, p=p)",
        "new": "            evs = events_for(rows, tick=tick, steps=steps, p=p, at=at)",
    },
    {
        "label": "price: счётчик протухших",
        "path": PRICE,
        "mark": "def stale(minutes: dict",
        "old": "def rates_by_step(scanned: list, total: int) -> dict:",
        "new": STALE + "def rates_by_step(scanned: list, total: int) -> dict:",
    },
    {
        "label": "timeframes: импорт даты",
        "path": TF,
        "mark": "from datetime import date",
        "old": "from typing import Optional\n",
        "new": "from datetime import date\nfrom typing import Optional\n",
    },
    {
        "label": "timeframes: минута с датой",
        "path": TF,
        "mark": "def _abs_minute(",
        "old": "def _minute_of_day(ts: str) -> Optional[int]:",
        "new": ABS_MINUTE + "def _minute_of_day(ts: str) -> Optional[int]:",
    },
    {
        "label": "timeframes: ключ последнего бара",
        "path": TF,
        "mark": "mm_last = _abs_minute(",
        "old": "    mm_last = _minute_of_day(last_ts)",
        "new": "    mm_last = _abs_minute(last_ts)",
    },
    {
        "label": "timeframes: ключ строки",
        "path": TF,
        "mark": "mm = _abs_minute(r.get(",
        "old": '        mm = _minute_of_day(r.get("ts"))',
        "new": '        mm = _abs_minute(r.get("ts"))',
    },
]


def main() -> int:
    by_file = {}
    for patch in PATCHES:
        by_file.setdefault(patch["path"], []).append(patch)

    applied = refused = skipped = 0
    for path, patches in by_file.items():
        if not path.exists():
            print(f"ОТКАЗ — нет файла {path}")
            refused += len(patches)
            continue
        text = original = path.read_text(encoding="utf-8")
        local = 0
        ok = True
        for patch in patches:
            if patch["mark"] in text:
                print(f"{patch['label']}: уже было")
                skipped += 1
                continue
            found = text.count(patch["old"])
            if found != 1:
                print(f"{patch['label']}: ОТКАЗ — якорь найден {found} раз(а)")
                refused += 1
                ok = False
                continue
            text = text.replace(patch["old"], patch["new"], 1)
            print(f"{patch['label']}: применено")
            local += 1
        if not ok:
            print(f"файл НЕ тронут: {path} — частичная правка хуже, чем никакой")
            continue
        if text != original:
            path.write_text(text, encoding="utf-8")
        applied += local

    print(f"итог: применено {applied}, уже было {skipped}, отказов {refused}")
    if refused == 0:
        try:
            Path(__file__).unlink()
            print("Скрипт удалил себя.")
        except OSError as exc:                                # noqa: BLE001
            print(f"Скрипт не смог удалить себя: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
