"""Фикстура теста нормы — на настоящие деньги. Идемпотентен."""

TST = "tests/test_volume_events.py"

OLD = '''    rows = steady(12, vol=100) + [bar(12, 10000), bar(13, 10000)]
    got = detect_step(rows, 1, lot=1)
    assert got and got[0]["base_rub"] == round(100 * 100.0)
    assert got[0]["times"] >= 50'''

NEW = '''    # НОРМА ЗДЕСЬ ДОЛЖНА БЫТЬ НАСТОЯЩЕЙ. Раньше было 100 лотов × 100 ₽ =
    # 10 000 ₽ — ниже порога шума (20 000 ₽), то есть тест проверял своё
    # утверждение на бумаге, которой почти не торгуют. Проверяется
    # исключение измеряемого бара из своей же нормы, а не поведение
    # на шуме — для шума есть отдельные тесты.
    rows = steady(12, vol=1000) + [bar(12, 100000), bar(13, 100000)]
    got = detect_step(rows, 1, lot=1)
    assert got and got[0]["base_rub"] == round(1000 * 100.0)
    assert got[0]["base_thin"] is False, "норма 100 тыс ₽ — это не шум"
    assert got[0]["times"] >= 50'''

with open(TST, encoding="utf-8") as f:
    text = f.read()

if NEW in text:
    print(" = уже применено")
else:
    n = text.count(OLD)
    if n != 1:
        raise SystemExit(f"НЕ ПРИМЕНЕНО: якорь найден {n} раз в {TST}")
    with open(TST, "w", encoding="utf-8") as f:
        f.write(text.replace(OLD, NEW, 1))
    print(" + фикстура починена")
