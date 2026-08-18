"""
Расписание сессий: один источник на весь проект.

Проверяется не «правильные ли часы у биржи», а свойство кода: одна и та же
минута обязана получать одну фазу везде. Расхождение было настоящим —
основная сессия в day_slice кончалась в 18:39, в настройках в 18:50, а в
метке свечи 09:50-09:59 записывалось как основная сессия.

Запуск: python3 tests/test_sessions_source.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.analysis import day_slice as ds  # noqa: E402
from src.analysis import sessions  # noqa: E402


def hm(h, m=0):
    return h * 60 + m


# ───────────────────────────── фазы ──────────────────────────────────────────

def test_три_сессии_существуют():
    assert sessions.phase(hm(8)) == "morning"
    assert sessions.phase(hm(14)) == "main"
    assert sessions.phase(hm(21)) == "evening"


def test_утро_начинается_в_семь():
    assert sessions.phase(hm(7)) == "morning"
    assert sessions.phase(hm(9, 49)) == "morning"


def test_аукцион_открытия_не_основная_сессия():
    """ГЛАВНОЕ. 09:50-09:59 — аукцион, а метка свечи звала его main."""
    for m in (50, 55, 59):
        assert sessions.phase(hm(9, m)) == "pre", m


def test_утренний_аукцион_не_утренняя_сессия():
    """Бар 06:59 существует (MAGN 18.08, 2234 лота), но это не сессия."""
    assert sessions.phase(hm(6, 59)) == "pre"
    assert sessions.phase(hm(6, 30)) == "closed"


def test_перерыв_не_основная_сессия():
    assert sessions.phase(hm(18, 49)) == "main"
    assert sessions.phase(hm(18, 55)) == "break"


def test_вечер_и_ночь_разведены():
    assert sessions.phase(hm(19)) == "evening"
    assert sessions.phase(hm(23, 49)) == "evening"
    assert sessions.phase(hm(23, 55)) == "closed"
    assert sessions.phase(hm(2)) == "closed"


def test_выходные_закрыты_в_любой_час():
    """Tinkoff в субботу отдаёт дилерские сделки при закрытой бирже."""
    for wd in (5, 6):
        for t in (hm(8), hm(12, 34), hm(21)):
            assert sessions.phase(t, wd) == "closed", (wd, t)


def test_будни_не_сломаны_проверкой_дня():
    for wd in range(5):
        assert sessions.phase(hm(8), wd) == "morning", wd
        assert sessions.phase(hm(12, 34), wd) == "main", wd
        assert sessions.phase(hm(21), wd) == "evening", wd


def test_вход_разрешён_только_в_непрерывных_торгах():
    assert sessions.is_trading(hm(8)) is True
    assert sessions.is_trading(hm(9, 55)) is False
    assert sessions.is_trading(hm(18, 55)) is False


def test_открытие_своей_сессии():
    assert sessions.session_open(hm(7, 5)) == hm(7)
    assert sessions.session_open(hm(10, 5)) == hm(10)
    assert sessions.session_open(hm(19, 5)) == hm(19)
    assert sessions.session_open(hm(9, 55)) is None


def test_фаза_по_ключу_минуты():
    assert sessions.phase_of_ts("2026-08-18T09:55") == "pre"
    assert sessions.phase_of_ts("2026-08-18T14:00") == "main"
    assert sessions.phase_of_ts("мусор") == "closed"


# ─────────────────── согласованность с day_slice ─────────────────────────────

def test_day_slice_берёт_границы_из_одного_места():
    assert ds.SESSIONS == sessions.windows_inclusive_ru()


def test_каждая_минута_суток_совпадает_в_двух_модулях():
    """Ради этого теста файл и написан: расхождение было молчаливым."""
    ru = {"morning": "утро", "main": "основная", "evening": "вечер"}
    for mm in range(24 * 60):
        want = ru.get(sessions.phase(mm))
        assert ds.session_of(mm) == want, mm


def test_основная_сессия_длиной_530_минут():
    """10:00-18:49 включительно. Было 520: одиннадцать минут выпадали."""
    exp = ds.expected_minutes("2026-08-06", __import__("datetime").datetime(2026, 8, 7, 11, 0))
    assert exp["основная"] == 530
    assert exp["вечер"] == 290
    assert exp["утро"] == 170


# ─────────────────── границы читаются из окружения ───────────────────────────

def test_границы_настраиваемые_а_не_зашитые():
    """Биржа меняет часы; это не должно требовать правки кода."""
    import importlib
    os.environ["SESSION_EVENING_CLOSE"] = str(hm(23, 30))
    try:
        mod = importlib.reload(sessions)
        assert mod.phase(hm(23, 40)) == "closed"
    finally:
        del os.environ["SESSION_EVENING_CLOSE"]
        importlib.reload(sessions)
        importlib.reload(ds)


def test_мусор_в_переменной_не_ломает_расписание():
    import importlib
    os.environ["SESSION_MAIN_OPEN"] = "десять утра"
    try:
        mod = importlib.reload(sessions)
        assert mod.MAIN_OPEN == hm(10)
    finally:
        del os.environ["SESSION_MAIN_OPEN"]
        importlib.reload(sessions)
        importlib.reload(ds)


if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ПАДАЕТ {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ОШИБКА {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed} из {len(tests)} пройдено")
    sys.exit(1 if failed else 0)
