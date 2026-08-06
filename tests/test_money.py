"""
Тесты перевода лотов в рубли.

Главное, что здесь проверяется, — не арифметика (она тривиальна), а поведение
при НЕПОЛНЫХ данных. Именно там рождаются числа, которым верят зря.
"""
from src.analysis.money import bar_price, candle_turnover_rub, turnover_rub


def test_лотность_неизвестна_возвращается_none_а_не_ноль():
    #  Самое важное правило модуля.
    assert turnover_rub(1000, None, 276.5) is None
    assert turnover_rub(1000, 0, 276.5) is None


def test_лотность_не_подменяется_единицей():
    #  Если бы подставлялась 1, то без лотности ответ совпал бы с лотностью 1.
    assert turnover_rub(1000, 1, 100.0) == 100000.0
    assert turnover_rub(1000, None, 100.0) != 100000.0


def test_бумаги_сравнимы_только_в_рублях():
    #  Тот самый случай FEES против SBER: равные лоты, несравнимые деньги.
    fees = turnover_rub(1000, 10000, 0.09)
    sber = turnover_rub(1000, 10, 276.5)
    assert fees == 900000.0
    assert sber == 2765000.0
    assert sber > fees


def test_нулевой_объём_это_ноль_рублей_а_не_пробел():
    assert turnover_rub(0, 10, 276.5) == 0.0


def test_без_цены_считать_нечего():
    assert turnover_rub(1000, 10, 0) is None
    assert turnover_rub(1000, 10, None) is None
    assert turnover_rub(1000, 10, -5) is None


def test_отрицательный_объём_это_порча_данных():
    assert turnover_rub(-10, 10, 276.5) is None


def test_цена_бара_средняя_по_трём_а_не_close():
    assert bar_price(10.0, 12.0, 9.0, 12.0) == 11.0


def test_цена_бара_игнорирует_нули():
    #  В недозаполненной свече low может быть нулём — среднее с ним втрое ниже.
    assert bar_price(0, 12.0, 0, 12.0) == 12.0


def test_цена_бара_падает_на_open_если_больше_ничего_нет():
    assert bar_price(7.5, 0, 0, 0) == 7.5


def test_цена_бара_пустой_свечи_это_none():
    assert bar_price(0, 0, 0, 0) is None
    assert bar_price() is None


def test_свеча_целиком():
    row = {"open": 100.0, "high": 102.0, "low": 99.0, "close": 102.0,
           "volume": 50}
    #  (102 + 99 + 102) / 3 = 101, × 50 лотов × 10 акций = 50500
    assert candle_turnover_rub(row, 10) == 50500.0


def test_свеча_без_лотности_не_считается():
    row = {"open": 100.0, "high": 102.0, "low": 99.0, "close": 102.0,
           "volume": 50}
    assert candle_turnover_rub(row, None) is None


def test_свеча_пустая_не_падает():
    assert candle_turnover_rub({}, 10) is None
    assert candle_turnover_rub(None, 10) is None
