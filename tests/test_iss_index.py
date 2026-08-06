"""
Тесты разбора индекса из ISS.

Образец взят не из головы, а из живого ответа 06.08.2026 13:34.
"""
from datetime import datetime

from src.collector.iss_index import (STALE_SEC, age_sec, index_url,
                                     parse_index, parse_systime)

#  Живой ответ ISS, сокращён по колонкам, но порядок сохранён.
LIVE = {"marketdata": {
    "columns": ["SECID", "BOARDID", "LASTVALUE", "OPENVALUE", "CURRENTVALUE",
                "LASTCHANGE", "LASTCHANGETOOPENPRC", "UPDATETIME",
                "LASTCHANGEPRC", "VALTODAY", "SYSTIME", "HIGH", "LOW",
                "TRADEDATE"],
    "data": [["IMOEX", "SNDX", 2301.65, 2295.11, 2273.74, -27.91, -0.93,
              "13:34:00", -1.21, 35661767928.0, "2026-08-06 13:34:00",
              2316.85, 2270.1, "2026-08-06"]]}}

AT = datetime(2026, 8, 6, 13, 34, 14)


def test_живой_ответ_разбирается():
    r = parse_index(LIVE, at=AT)
    assert r["name"] == "IMOEX"
    assert r["value"] == 2273.74
    assert r["change_pct"] == -1.21
    assert r["high"] == 2316.85 and r["low"] == 2270.1
    assert r["day"] == "2026-08-06"


def test_возраст_считается_от_метки_биржи():
    #  Замерено на живых данных: 13:34:00 при часах 13:34:14.
    assert parse_index(LIVE, at=AT)["age_sec"] == 14


def test_возраст_не_от_времени_запроса():
    #  Закрытая биржа: спросили только что, а значению двое суток.
    #  Именно так ошиблись 02.08 — показывалось «11 секунд».
    late = datetime(2026, 8, 8, 13, 34, 0)
    r = parse_index(LIVE, at=late)
    assert r["age_sec"] == 2 * 24 * 3600
    assert r["stale"] is True


def test_свежее_значение_не_помечается_несвежим():
    assert "stale" not in parse_index(LIVE, at=AT)


def test_граница_свежести_это_число_в_коде():
    assert STALE_SEC == 180
    late = datetime(2026, 8, 6, 13, 34, 0 + STALE_SEC + 1)
    assert parse_index(LIVE, at=late).get("stale") is True


def test_часы_разошлись_на_секунды_это_не_ошибка():
    #  Метка биржи чуть впереди наших часов — это самое свежее, что есть.
    early = datetime(2026, 8, 6, 13, 33, 50)
    assert parse_index(LIVE, at=early)["age_sec"] == 0


def test_метка_из_будущего_всё_же_подозрительна():
    #  Час вперёд — это уже не расхождение часов, а сломанный часовой пояс.
    assert age_sec("2026-08-06 13:34:00", at=datetime(2026, 8, 6, 12, 34)) < 0


def test_нет_текущего_значения_значит_нет_ответа():
    #  Самая опасная подмена: LASTVALUE — закрытие ПРОШЛОГО дня.
    bad = {"marketdata": {"columns": ["SECID", "CURRENTVALUE", "LASTVALUE",
                                      "SYSTIME"],
                          "data": [["IMOEX", None, 2301.65,
                                    "2026-08-06 13:34:00"]]}}
    assert parse_index(bad, at=AT) is None


def test_нулевое_значение_индекса_это_мусор():
    bad = {"marketdata": {"columns": ["SECID", "CURRENTVALUE", "SYSTIME"],
                          "data": [["IMOEX", 0, "2026-08-06 13:34:00"]]}}
    assert parse_index(bad, at=AT) is None


def test_колонки_читаются_по_имени_а_не_по_номеру():
    #  Тот же ответ с переставленными колонками должен дать то же число.
    swapped = {"marketdata": {
        "columns": ["SYSTIME", "CURRENTVALUE", "SECID", "LASTCHANGEPRC"],
        "data": [["2026-08-06 13:34:00", 2273.74, "IMOEX", -1.21]]}}
    r = parse_index(swapped, at=AT)
    assert r["value"] == 2273.74 and r["change_pct"] == -1.21


def test_пустой_и_сломанный_ответ_не_падают():
    assert parse_index(None) is None
    assert parse_index({}) is None
    assert parse_index({"marketdata": {}}) is None
    assert parse_index({"marketdata": {"columns": ["A"], "data": []}}) is None
    assert parse_index("не json") is None


def test_строка_короче_колонок_это_отказ_а_не_сдвиг():
    #  При сдвиге данные пришли бы чужие, но правдоподобные.
    bad = {"marketdata": {"columns": ["SECID", "CURRENTVALUE", "SYSTIME"],
                          "data": [["IMOEX", 2273.74]]}}
    assert parse_index(bad, at=AT) is None


def test_метка_времени_мусорная_возраст_неизвестен():
    assert parse_systime("вчера") is None
    assert parse_systime(None) is None
    assert age_sec("вчера") is None
    ok = {"marketdata": {"columns": ["SECID", "CURRENTVALUE", "SYSTIME"],
                         "data": [["IMOEX", 2273.74, "вчера"]]}}
    r = parse_index(ok, at=AT)
    #  Значение есть, возраст неизвестен — и не притворяется нулём.
    assert r["value"] == 2273.74 and r["age_sec"] is None
    assert "stale" not in r


def test_отсутствующее_поле_отсутствует_а_не_равно_нулю():
    thin = {"marketdata": {"columns": ["SECID", "CURRENTVALUE", "SYSTIME"],
                           "data": [["IMOEX", 2273.74, "2026-08-06 13:34:00"]]}}
    r = parse_index(thin, at=AT)
    assert "change_pct" not in r
    assert "high" not in r and "valtoday_rub" not in r


def test_адрес_без_токена_и_с_нужным_именем():
    u = index_url("imoex")
    assert "IMOEX.json" in u and "iss.only=marketdata" in u
    assert "token" not in u.lower()
    assert "MOEXOG.json" in index_url("MOEXOG")
