"""
Надпись о норме по времени суток обязана совпадать с ФАКТОМ.

05.08 в проде одновременно стояло: profiles_ready 44, vol_profiles 44 — и
рядом «не построена: лучшая бумага имеет 2 торговых дней из 10 нужных».
Оба числа из одного цикла, и одно из утверждений ложь.

Причина: в main.py надпись присваивалась ТОЛЬКО в ветке «не построено
ничего», поэтому после дозаливки ISS она осталась висеть прошлым
состоянием. Ошибка не в тексте, а в том, что текст не переписывался.

Поэтому тестов здесь два вида: на саму фразу и на то, что main.py пишет её
безусловно. Второй смотрит на исходник как на текст — конвейер в тесте не
поднять, а именно ветвление в конвейере и было причиной.
"""
import io
import os

from src.analysis.volume_events import MIN_DAYS, profile_note

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")
WINDOW = 2500


def gap(usable: int = 2, days: int = 4) -> dict:
    """Строка profile_gap в том виде, в каком её отдаёт настоящая функция."""
    return {"usable_days": usable, "need_days": MIN_DAYS, "days_in_db": days,
            "weekend_days": 2, "short_days": 0, "empty_days": 0,
            "min_bars_day": 200, "missing_days": max(0, MIN_DAYS - usable),
            "ready": usable >= MIN_DAYS, "days": []}


def _main_src() -> str:
    return io.open(MAIN, encoding="utf-8").read()


def test_nothing_built_keeps_the_old_explanation():
    s = profile_note([gap()])
    assert s.startswith("не построена")
    assert "2 торговых дней из 10" in s


def test_no_history_at_all_says_so():
    assert "истории в базе нет вовсе" in profile_note([])


def test_all_built_never_claims_it_is_missing():
    s = profile_note([], built=44, total=44)
    assert "не построена" not in s
    assert "44" in s


def test_partly_built_names_both_sides():
    s = profile_note([gap(), gap(usable=5)], built=44, total=80)
    assert "не построена" not in s
    assert "44" in s and "80" in s
    # Берётся ЛУЧШАЯ из недостроенных: она показывает, сколько ждать.
    assert "5 торговых дней из 10" in s


def test_total_is_derived_when_not_given():
    assert "из 4" in profile_note([gap()], built=3)


def test_the_note_is_written_even_when_profiles_exist():
    assert "stream.profile_note = profile_note(gaps, built=built" in _main_src()


def test_the_note_is_not_hidden_behind_the_empty_branch():
    src = _main_src()
    i = src.find("async def _volume_profiles")
    assert i > 0, "фоновая сборка норм переименована — тест ослеп"
    window = src[i:i + WINDOW]
    # Считаются ПРИСВАИВАНИЯ, а не упоминания. Первая версия этого теста
    # требовала ровно одного упоминания `stream.profile_note` и была КРАСНОЙ
    # на верном патче: ту же строку законно передают в logger, и это второе
    # упоминание. Тест обязан запрещать второе место записи, а не чтение.
    assert window.count("stream.profile_note = ") == 1, \
        "надпись присваивается не в одном месте — ветвление вернулось"
    assert "if built:" not in window, \
        "надпись снова зависит от того, построено ли хоть что-то"
