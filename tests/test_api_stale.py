"""
Третья причина пустой таблицы должна быть видна снаружи.

Пустой список событий читается как «на рынке спокойно», и это чтение бывает
ложным тремя разными способами: всё выброшено полом по обороту, бумаги ещё
не набрали своих баров, или — с этого дня — данные просто устарели. Первые
две причины уже имели своё число (below_floor, warming_up), третья жила только
в ядре и наружу не попадала.

Читается ИСХОДНИК, а не импортируется модуль: src/api/main.py тянет за собой
базу, сеть и конфиг, а проверяется здесь факт наличия поля, а не работа
веб-сервера. Тот же приём, что в test_api_profile_note.py.
"""
import re
from pathlib import Path

API = Path("src/api/main.py")
CORE = Path("src/analysis/volume_events.py")


def _api():
    return API.read_text(encoding="utf-8")


def _volume_scan_route():
    """Тело ровно одного маршрута: от его декоратора до следующего."""
    src = _api()
    start = src.index('@app.get("/api/volume-scan"')
    nxt = src.find("@app.get(", start + 10)
    return src[start:nxt if nxt > 0 else len(src)]


def test_the_core_knows_how_to_count_stale_tickers():
    """Счётчик должен существовать в ядре, иначе выводить нечего."""
    assert re.search(r"^def stale\(", CORE.read_text(encoding="utf-8"), re.M)


def test_the_route_imports_the_counter():
    assert re.search(r"warming_up,\s*stale", _volume_scan_route())


def test_the_answer_carries_the_stale_count():
    assert '"stale": stale(mins)' in _volume_scan_route()


def test_all_three_reasons_for_an_empty_table_are_reported_together():
    """
    Порознь любое из трёх чисел вводит в заблуждение: без stale ночная
    тишина выглядит как спокойствие, без below_floor — как пустой рынок.
    """
    body = _volume_scan_route()
    for field in ('"below_floor"', '"warming_up"', '"stale"'):
        assert field in body, field


def test_the_counter_is_not_pasted_twice():
    """Патч идемпотентен: два одинаковых ключа в словаре — тихая беда."""
    assert _api().count('"stale": stale(mins)') == 1


def test_the_field_sits_in_the_volume_scan_and_not_somewhere_else():
    """Соседние маршруты не должны были пострадать от правки по якорю."""
    assert '"stale": stale(mins)' in _volume_scan_route()


def test_the_reason_is_explained_in_words_next_to_the_number():
    """Число без объяснения через месяц будет загадкой для меня же."""
    body = _volume_scan_route()
    assert "устаревшем баре" in body or "устарели" in body
