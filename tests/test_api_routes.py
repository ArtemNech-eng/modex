"""
Маршруты дёргаются целиком, а не по частям.

ЗАЧЕМ ЭТОТ ФАЙЛ. 02.08 маршрут /api/levels отдавал голую пятисотку на проде.
Функция чтения из базы была проверена отдельно и работала. Не работал сам
маршрут: он обращался к CURRENT, который в этом модуле импортируется ЛОКАЛЬНО
внутри каждой функции, — NameError на строке после запроса к базе.

Ни один тест не вызывал маршрут. Проверялись куски, из которых он собран, и все
куски были исправны. Хуже того: не увидев причины, я воспроизвёл локально
ПРАВДОПОДОБНУЮ ошибку («нет таблицы»), принял её за настоящую и построил на ней
целый коммит. Диагноз по совпадению вида ошибки — это догадка, а не измерение.

Здесь маршруты вызываются через TestClient. Проверяется не содержимое ответа —
оно зависит от данных, которых в тестах нет, — а то, что маршрут ОТВЕЧАЕТ и не
падает пятисоткой. Этого достаточно, чтобы поймать NameError, опечатку в имени
поля и необъявленный импорт.
"""
import pytest

pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient           # noqa: E402

# В песочнице Python 3.9, а модуль API использует запись `datetime | None` из
# 3.10 — приложение здесь не импортируется вовсе. Пропуск ЧЕСТНЫЙ и с причиной:
# молча зеленеющий тест хуже отсутствующего. Статическую проверку тех же ошибок,
# работающую на любой версии, см. в test_api_undefined_names.py.
try:
    from src.api.main import app as _app
except Exception as e:                                       # noqa: BLE001
    _app, _why = None, str(e)[:200]
else:
    _why = ""

pytestmark = pytest.mark.skipif(
    _app is None, reason=f"модуль API не импортируется в этой среде: {_why}")


@pytest.fixture(scope="module")
def client():
    with TestClient(_app, raise_server_exceptions=False) as c:
        yield c


# Маршруты, которые обязаны отвечать без данных и без живого стрима.
# 200 — ответ есть; 404 — тикер неизвестен; 400 — параметр не тот.
# Пятисотка недопустима ни в одном случае: она означает необработанное падение.
READ_ROUTES = [
    "/api/levels/SBER",
    "/api/levels/SBER?limit=5",
    "/api/levels/SBER?source=dealer",
    "/api/micro/SBER",
    "/api/book/SBER",
    "/api/candles/SBER",
    "/api/flow/SBER",
    "/api/book-live/SBER",
    "/api/book-live/SBER?light=true",
    "/api/events/SBER",
    "/api/stream/health",
    "/api/stats",
]


@pytest.mark.parametrize("url", READ_ROUTES)
def test_route_answers_without_a_500(client, url):
    """
    Пятисотка означает необработанное исключение. Именно так NameError в
    /api/levels доехал до прода и держался там час.
    """
    r = client.get(url)
    assert r.status_code != 500, f"{url} → 500: {r.text[:300]}"
    assert r.status_code in (200, 400, 404), f"{url} → {r.status_code}"


def test_levels_route_returns_the_expected_shape(client):
    """
    Маршрут обязан отдавать разделение съеденного и снятого — ради него таблица и
    заведена. Пустые данные это не отменяют.
    """
    r = client.get("/api/levels/SBER")
    if r.status_code != 200:
        pytest.skip(f"тикер недоступен в тестовой среде: {r.status_code}")
    d = r.json()
    assert "totals" in d and "rows" in d
    for k in ("traded_lots", "pulled_lots", "added_lots"):
        assert k in d["totals"], k


def test_unknown_ticker_is_404_not_500(client):
    assert client.get("/api/levels/НЕТТАКОГО").status_code == 404


def test_bad_source_is_400_not_500(client):
    assert client.get("/api/levels/SBER?source=garbage").status_code == 400
