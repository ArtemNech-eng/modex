"""
Суита не должна зависеть от ПОРЯДКА.

12 тестов падали, будучи исправными: `asyncio.run()` в одном файле обнулял
текущую петлю, и всё, что дальше звало `asyncio.get_event_loop()`, получало
RuntimeError. Кто упадёт — решал алфавит имён файлов.

Я весь день писал в отчётах «12 падений прежние». Это был не диагноз, а
отговорка: постоянно красная суита приучает не смотреть на красное, и новое
падение становится неотличимо от старого.
"""
import asyncio
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


async def _nothing():
    return 1


def test_a_neighbour_calls_asyncio_run():
    """
    ПЕРВАЯ ПОЛОВИНА ПРОВЕРКИ. Именно так делает test_outcome_window, и на 3.9
    asyncio.run после себя ОБНУЛЯЕТ текущую петлю.

    Внутри одного теста это законно: кто позвал asyncio.run, тот и распоряжается
    петлёй. Болезнь начиналась у СОСЕДА — см. следующий тест.
    """
    assert asyncio.run(_nothing()) == 1


def test_the_next_test_still_has_a_working_loop():
    """
    ВТОРАЯ ПОЛОВИНА, И В НЕЙ ВЕСЬ СМЫСЛ. Этот тест идёт сразу после соседа с
    asyncio.run — до conftest ровно здесь и падало «нет текущей петли».

    Порядок внутри файла у pytest сверху вниз, так что пара стоит рядом
    намеренно: разнести их значило бы потерять проверку.
    """
    loop = asyncio.get_event_loop()
    assert loop is not None and not loop.is_closed()
    assert loop.run_until_complete(_nothing()) == 1


def test_conftest_gives_every_test_its_own_loop():
    src = (ROOT / "tests/conftest.py").read_text()
    assert "autouse=True" in src, "правило обязано быть общим, а не по вспоминанию"
    assert "new_event_loop" in src and "set_event_loop" in src
    assert "loop.close" in src, "петля закрывается, иначе течёт"


def test_no_test_file_leaves_a_loop_behind_by_hand():
    """
    Ручное new_event_loop без возврата прежней петли — та же болезнь. Если
    появится снова, пусть тест назовёт файл.
    """
    bad = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        src = f.read_text()
        if "new_event_loop" not in src:
            continue
        # Создаёт петлю сам — обязан и вернуть прежнюю.
        if "set_event_loop(prev" not in src and "set_event_loop(None" not in src:
            bad.append(f.name)
    assert not bad, "создают петлю и не возвращают прежнюю: " + ", ".join(bad)
