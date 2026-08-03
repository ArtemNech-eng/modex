"""
Транзиентный отказ на старте не должен убивать сбор до следующего деплоя.

03.08 в 15:57 после деплоя поток не поднялся и не поднялся сам. Прод стоял без
данных пятнадцать минут, пока я не посмотрел на выдачу. В коде был выход без
повтора:

    if not figis:
        logger.error("Стрим: ни одной бумаги не разрешено, выхожу")
        return

FIGI разрешается ТОЛЬКО через Tinkoff API, 80 вызовов при старте, и на выкате их
легко придушить лимитом: старый контейнер ещё держит соединение, новый резолвит.

А health в это время отвечал «включён, но ещё не поднялся или упал при старте» —
фраза, под которую подходит и нормальный прогрев, и падение, и молчаливый выход.
По ней было не понять, ждать или бить тревогу.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_figi_resolution_retries_instead_of_giving_up():
    src = (ROOT / "main.py").read_text()
    assert "выхожу" not in src, "молчаливый выход убран"
    assert "for attempt in range(1, 6)" in src, "резолв повторяется"
    assert "return await stream_pipeline()" in src, "конвейер перезапускает себя"


def test_start_error_is_visible_from_outside():
    """
    Причина обязана быть в /api/stream/health, а не только в логе контейнера:
    доступа к логам у агента нет, и различать «ждать» от «сломано» надо снаружи.
    """
    st = (ROOT / "src/collector/stream.py").read_text()
    assert "START_ERROR" in st, "переменная объявлена в модуле стрима"
    api = (ROOT / "src/api/main.py").read_text()
    assert "start_error" in api, "поле отдаётся наружу"
    assert "НЕИСПРАВНОСТЬ" in api, "неисправность названа неисправностью"


def test_three_states_are_distinguishable():
    """
    Выключен флагом · поднимается · неисправность — три разных ответа. Одна фраза
    на все три и была причиной того, что прод стоял пятнадцать минут незамеченным.
    """
    api = (ROOT / "src/api/main.py").read_text()
    # Привязка к МАРШРУТУ ЗДОРОВЬЯ, а не к первому вхождению «if CURRENT is None»:
    # оно встречается и в карточке бумаги, и первая версия теста проверяла её.
    i = api.index('@app.get("/api/stream/health"')
    j = api.find("\n@app.", i + 10)
    body = api[i:j if j > 0 else len(api)]
    assert "выключен флагом" in body
    assert "поднимается" in body
    assert "НЕИСПРАВНОСТЬ" in body
    assert body.count("reason =") >= 3, "три отдельных ответа, а не два"


def test_card_route_distinguishes_too():
    """Карточка бумаги отвечала так же расплывчато — та же правка нужна и там."""
    api = (ROOT / "src/api/main.py").read_text()
    assert 'else "стрим ещё не поднялся"' not in api, "расплывчатая фраза убрана"
    assert "стрим поднимается, резолв FIGI" in api
