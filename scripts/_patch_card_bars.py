"""
Одноразовый патч: карточка принимает бар живого потока.

Зачем. В src/collector/stream.py минутная запись складывается так:

    bar = {"ts": ts, "close": close, "open": ..., "high": ..., "low": ...,
           "volume": ...}

а card._series читает короткие ключи o/h/l/c/v. Самое неприятное в этом
то, что при подключении к продакшну ошибка НЕ вызвала бы падения:
_f(None) даёт нули, и карточка вернула бы внешне правдоподобный JSON
с нулевым ATR, нулевым VWAP и отклонениями в сотни процентов.
Именно такая ошибка — молчаливая, а не шумная — держалась в коде месяц
в виде таблицы FIGI, где 22 бумаги получали цены чужих инструментов.

Почему правим карточку, а не поток. Формат записи в stream.py совпадает
с тем, что ждёт timeframes.bars, и переименование сломало бы сборку
в 5/15/30 минут и оба работающих сканера ради одного нового читателя.
Дешевле и безопаснее научить нового читателя обоим видам записи.

Скрипт идемпотентен: повторный запуск ничего не меняет.
"""

import sys

CARD = "src/analysis/card.py"
TEST = "tests/test_card.py"

done = []
skipped = []


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ─── 1. нормализация бара в card.py ────────────────────────────

OLD = '''def _series(bars: list) -> tuple:
    """Разложить бары в параллельные списки: так их ждёт intraday."""
    o = [_f(b.get("o")) for b in bars]'''

NEW = '''def _bar(b: dict) -> dict:
    """
    Привести минутную запись к коротким ключам o/h/l/c/v.

    В системе живут ДВА вида минутной записи, и это не неряшливость:

      • короткий — {"ts", "o", "h", "l", "c", "v"};
      • длинный — {"ts", "open", "high", "low", "close", "volume"},
        именно он лежит в CURRENT.minutes и его ждёт timeframes.bars.

    Переименовать длинный вид в потоке значило бы тронуть сборку баров
    в 5/15/30 минут и два работающих сканера ради одного читателя.

    Почему не просто b.get("o") or b.get("open"): при отсутствии ключа
    получились бы нули, и карточка отдала бы правдоподобный ответ с
    нулевым ATR вместо отказа. Молчаливо неверное число опаснее
    пустого поля: по пустому полю никто не торгует.
    """
    if not isinstance(b, dict):
        return {}
    if "c" in b or "o" in b:
        return b
    return {"ts": b.get("ts"), "o": b.get("open"), "h": b.get("high"),
            "l": b.get("low"), "c": b.get("close"), "v": b.get("volume")}


def _series(bars: list) -> tuple:
    """Разложить бары в параллельные списки: так их ждёт intraday."""
    bars = [_bar(b) for b in (bars or [])]
    o = [_f(b.get("o")) for b in bars]'''

text = read(CARD)
if "def _bar(" in text:
    skipped.append("card.py: _bar уже есть")
elif OLD in text:
    write(CARD, text.replace(OLD, NEW, 1))
    done.append("card.py: добавлена нормализация бара")
else:
    print("НЕ ПРИМЕНЕНО: card.py — не найдено тело _series", file=sys.stderr)
    sys.exit(1)


# ─── 2. тест на живой формат ───────────────────────────────

TEST_CODE = '''

def test_bars_of_the_live_stream_are_understood():
    """
    Запись из CURRENT.minutes должна давать тот же ответ, что короткая.

    Стережёт расхождение, найденное 04.08: поток кладёт open/high/low/
    close/volume, а карточка читала o/h/l/c/v. На продакшне это не упало бы,
    а тихо отдало нулевой ATR и нулевой VWAP.
    """
    short = _bars(30)
    live = [{"ts": b["ts"], "open": b["o"], "high": b["h"], "low": b["l"],
             "close": b["c"], "volume": b["v"]} for b in short]

    a = card.build("SBER", bars=short, now_sec=1000,
                   minute_of_day=12 * 60, weekday=1)
    b = card.build("SBER", bars=live, now_sec=1000,
                   minute_of_day=12 * 60, weekday=1)

    assert a["price"] == b["price"]
    assert a["geometry"]["atr"] == b["geometry"]["atr"]
    assert a["structure"] == b["structure"]
    # И главное: числа не нулевые, иначе равенство ничего не значит.
    assert b["geometry"]["atr"] and b["geometry"]["atr"] > 0
    assert b["price"]["vwap"] and b["price"]["vwap"] > 0
'''

text = read(TEST)
if "test_bars_of_the_live_stream_are_understood" in text:
    skipped.append("test_card.py: тест уже есть")
else:
    write(TEST, text.rstrip("\n") + "\n" + TEST_CODE)
    done.append("test_card.py: добавлен тест на живой формат")


# ─── 3. предупреждение, а не ошибка ──────────────────────────
# Если где-то ещё бар читается напрямую, мимо _series — об этом надо
# знать, но это не повод ронять сборку.
suspicious = [ln for ln in read(CARD).splitlines()
              if ('.get("c")' in ln or '.get("v")' in ln or '.get("h")' in ln)
              and "_series" not in ln and "def _bar" not in ln]
if suspicious:
    print("ПРЕДУПРЕЖДЕНИЕ: бар читается напрямую в строках:")
    for ln in suspicious:
        print("   ", ln.strip())

print("ПРИМЕНЕНО:")
for d in done:
    print(" +", d)
for s in skipped:
    print(" =", s)
