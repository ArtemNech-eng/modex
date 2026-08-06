"""
Тесты опросчика индекса.

Проверяется не «работает ли в хороший день», а поведение при обрыве
связи и мусорном ответе — именно оно портит данные.
"""
from datetime import datetime, timedelta

from src.collector.index_poll import NAMES, minute_key, poll, to_row

LIVE = {"marketdata": {
    "columns": ["SECID", "BOARDID", "LASTVALUE", "OPENVALUE", "CURRENTVALUE",
                "LASTCHANGEPRC", "VALTODAY", "SYSTIME", "HIGH", "LOW",
                "TRADEDATE"],
    "data": [["IMOEX", "SNDX", 2301.65, 2295.11, 2273.74, -1.21,
              35661767928.0, "2026-08-06 13:34:00", 2316.85, 2270.1,
              "2026-08-06"]]}}

AT = datetime(2026, 8, 6, 13, 34, 14)


def _fetch_ok(url):
    return LIVE


def test_строка_готова_для_базы():
    r = to_row(LIVE, at=AT)
    assert r["ts"] == "2026-08-06T13:34"
    assert r["name"] == "IMOEX"
    assert r["value"] == 2273.74
    assert r["change_pct"] == -1.21
    assert r["prev_close"] == 2301.65
    assert r["valtoday_rub"] == 35661767928.0


def test_метка_биржи_переехала_в_exch_ts_а_не_в_ts():
    #  В разборе метка биржи лежит в ts, а в базе ts — это ячейка минуты.
    #  Перепутав их, мы бы молча писали пустую метку биржи.
    r = to_row(LIVE, at=AT)
    assert r["exch_ts"] == "2026-08-06 13:34:00"
    assert r["ts"] != r["exch_ts"]


def test_минута_по_нашим_часам_возраст_по_бирже():
    #  Спросили в 13:37, а значение снято биржей в 13:34.
    r = to_row(LIVE, at=datetime(2026, 8, 6, 13, 37, 0))
    assert r["ts"] == "2026-08-06T13:37"
    assert r["age_sec"] == 180


def test_формат_минуты_тот_же_что_у_свечей():
    #  Разойдётся формат — фон не сойдётся с бумагами по минутам.
    assert minute_key(datetime(2026, 8, 6, 9, 5)) == "2026-08-06T09:05"


def test_застрявшее_значение_пишется_с_пометкой_а_не_выбрасывается():
    #  Дырка в ряду неотличима от обрыва связи, пометка — отличима.
    late = datetime(2026, 8, 6, 14, 34, 0)
    r = to_row(LIVE, at=late)
    assert r["stale"] is True
    assert r["value"] == 2273.74
    assert r["age_sec"] == 3600


def test_свежее_значение_без_пометки():
    assert "stale" not in to_row(LIVE, at=AT)


def test_мусорный_ответ_это_ничего_а_не_нулевая_строка():
    assert to_row({}, at=AT) is None
    assert to_row(None, at=AT) is None


def test_обрыв_связи_это_missing_а_не_ноль_изменения():
    def broken(url):
        raise OSError("сеть упала")

    out = poll(broken, names=["IMOEX"], at=AT)
    assert out["rows"] == []
    assert out["missing"] == ["IMOEX"]


def test_одно_имя_не_роняет_остальные():
    def half(url):
        if "MOEXOG" in url:
            raise TimeoutError("таймаут")
        return LIVE

    out = poll(half, names=["IMOEX", "MOEXOG"], at=AT)
    assert [r["name"] for r in out["rows"]] == ["IMOEX"]
    assert out["missing"] == ["MOEXOG"]


def test_все_строки_одного_опроса_в_одной_минуте():
    #  Иначе фон и отрасли разъедутся по соседним ячейкам.
    out = poll(_fetch_ok, names=["IMOEX", "MOEXOG"], at=AT)
    assert {r["ts"] for r in out["rows"]} == {out["ts"]}


def test_имя_берётся_из_ответа_а_не_из_запроса():
    #  Спросили одно, пришло другое — пишется то, что пришло.
    out = poll(_fetch_ok, names=["MOEXOG"], at=AT)
    assert out["rows"][0]["name"] == "IMOEX"


def test_пустое_имя_пропускается_а_не_спрашивается():
    asked = []

    def spy(url):
        asked.append(url)
        return LIVE

    out = poll(spy, names=["", None, "IMOEX"], at=AT)
    assert len(asked) == 1
    assert len(out["rows"]) == 1


def test_главный_фон_это_imoex():
    assert "IMOEX" in NAMES


def test_адрес_спрашивается_по_имени():
    asked = []

    def spy(url):
        asked.append(url)
        return LIVE

    poll(spy, names=["moexog"], at=AT)
    assert "MOEXOG.json" in asked[0]


def test_без_часов_опрос_всё_равно_даёт_минуту():
    out = poll(_fetch_ok, names=["IMOEX"])
    assert len(out["ts"]) == len("2026-08-06T13:34")
