"""
Тесты сборки дня.

ВРЕМЯ ВЕЗДЕ ПЕРЕДАЁТСЯ ЯВНО. Привязка к часам реального мира уже
однажды сделала набор красным ночью — повторять не будем.
"""
from datetime import datetime

from src.analysis import day_slice as ds

DAY = "2026-08-06"
AT = datetime(2026, 8, 6, 14, 32)          # середина основной сессии
AT_NEXT = datetime(2026, 8, 7, 11, 0)      # следующий день


def c(ts, **kw):
    row = {"ts": ts, "ticker": "SBER"}
    row.update(kw)
    return row


def test_минута_дня_из_ключа():
    assert ds.minute_of_day("2026-08-06T14:32") == 14 * 60 + 32


def test_сессия_по_минуте():
    assert ds.session_of(10 * 60) == "основная"
    assert ds.session_of(7 * 60) == "утро"
    assert ds.session_of(20 * 60) == "вечер"


def test_ночью_сессии_нет():
    assert ds.session_of(3 * 60) is None


def test_медиана_не_падает_на_пустоте():
    assert ds.median([]) is None


def test_медиана_устойчива_к_выбросу():
    assert ds.median([10, 11, 12, 1000]) == 11.5


def test_сегодняшняя_сессия_обрезана_текущим_временем():
    exp = ds.expected_minutes(DAY, AT)
    assert exp["основная"] == (14 * 60 + 32) - 10 * 60 + 1
    assert exp["вечер"] == 0


def test_прошедший_день_считается_целиком():
    exp = ds.expected_minutes(DAY, AT_NEXT)
    assert exp["основная"] == 520
    assert exp["вечер"] == 290


def test_будущий_день_пуст():
    assert ds.expected_minutes("2026-08-10", AT)["основная"] == 0


def test_дубли_не_считаются_дважды():
    rows = [c("2026-08-06T10:00"), c("2026-08-06T10:00"), c("2026-08-06T10:01")]
    comp = ds.completeness(rows, DAY, AT)
    assert comp["основная"]["есть"] == 2


def test_пустая_сессия_не_считается_дырявой():
    comp = ds.completeness([], DAY, AT)
    assert comp["вечер"]["доля"] is None
    assert "вечер" not in ds.thin_sessions(comp)


def test_дырявая_сессия_называется_по_имени():
    rows = [c(f"2026-08-06T10:{m:02d}") for m in range(10)]
    comp = ds.completeness(rows, DAY, AT)
    assert "основная" in ds.thin_sessions(comp)


def test_возраст_считается_от_конца_минуты():
    assert ds.age_sec("2026-08-06T14:30", AT) == 60


def test_текущая_минута_не_считается_протухшей():
    assert ds.age_sec("2026-08-06T14:32", AT) == 0


def test_штамп_впереди_часов_не_даёт_минуса():
    assert ds.age_sec("2026-08-06T15:00", AT) == 0


def test_норма_не_строится_на_двух_днях():
    hist = [c("2026-08-04T14:32", turnover_rub=10),
            c("2026-08-05T14:32", turnover_rub=20)]
    value, days = ds.minute_norm(hist, 14 * 60 + 32)
    assert value is None and days == 2


def test_норма_берётся_только_за_свою_минуту():
    hist = [c("2026-08-03T14:32", turnover_rub=10),
            c("2026-08-04T14:32", turnover_rub=20),
            c("2026-08-05T14:32", turnover_rub=30),
            c("2026-08-05T11:00", turnover_rub=99999)]
    value, days = ds.minute_norm(hist, 14 * 60 + 32)
    assert value == 20 and days == 3


def test_нули_в_историю_нормы_не_идут():
    hist = [c("2026-08-03T14:32", turnover_rub=0),
            c("2026-08-04T14:32", turnover_rub=0),
            c("2026-08-05T14:32", turnover_rub=30)]
    value, days = ds.minute_norm(hist, 14 * 60 + 32)
    assert days == 1 and value is None


def test_пустой_блок_попадает_в_missing():
    res = ds.assemble("SBER", DAY, {"свечи": []}, at=AT)
    assert any("свечи" in m for m in res["missing"])


def test_непосчитанные_рубли_не_молчат():
    rows = [c("2026-08-06T14:31", turnover_rub=0)]
    res = ds.assemble("SBER", DAY, {"свечи": rows}, at=AT)
    assert any("рублях не посчитан" in m for m in res["missing"])


def test_отношение_к_норме_считается_когда_есть_история():
    rows = [c("2026-08-06T14:31", turnover_rub=60)]
    hist = [c("2026-08-03T14:31", turnover_rub=10),
            c("2026-08-04T14:31", turnover_rub=20),
            c("2026-08-05T14:31", turnover_rub=30)]
    res = ds.assemble("SBER", DAY, {"свечи": rows}, history=hist, at=AT)
    assert res["норма"]["оборот_руб_обычно"] == 20
    assert res["норма"]["оборот_к_норме"] == 3.0


def test_без_фона_рынка_срез_говорит_об_этом():
    res = ds.assemble("SBER", DAY, {"свечи": [c("2026-08-06T14:31", turnover_rub=5)]},
                      at=AT)
    assert res["рынок"] is None
    assert any("фон рынка" in m for m in res["missing"])


def test_в_срезе_нет_вердиктных_полей():
    res = ds.assemble("SBER", DAY, {"свечи": [c("2026-08-06T14:31", turnover_rub=5)]},
                      at=AT)
    запрет = ("signal", "сигнал", "direction", "направление", "вердикт",
              "entry", "вход", "event", "событие")
    assert not [k for k in res if k in запрет]
