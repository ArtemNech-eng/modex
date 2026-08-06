"""
Врезка лотности в запись минутных свечей (main.py, функция _flush).

Зачем. src/db.py:merge_candle_minutes считает оборот в рублях сам, но только
если в строке есть lot >= 1:

    lot = int(r.get("lot") or 0)
    if lot >= 1:
        rub = candle_turnover_rub(r, lot)

Стрим лотность в строку не клал. Значит в candle_minute у всех строк lot = 0 и
turnover_rub = 0, а сравнивать бумаги между собой можно ТОЛЬКО в рублях:
«1000 лотов» у SBER и у UGLD отличаются на три порядка.

Скрипт идемпотентен: если врезка уже стоит, он говорит «уже было» и ничего не
трогает. Если якорь встретился не один раз — отказ целиком, потому что
частичная врезка хуже, чем никакой.
"""
import os

PATH = "main.py"

OLD = ("        for tk, rows in candle.items():\n"
       "            n = await db.merge_candle_minutes(tk, rows)\n")

NEW = ("        for tk, rows in candle.items():\n"
       "            #  ЛОТНОСТЬ ЕДЕТ ВМЕСТЕ СО СВЕЧОЙ. Биржа отдаёт объём в\n"
       "            #  ЛОТАХ, а лот у бумаг разный: SBER 1, GAZP 10, UGLD 1000.\n"
       "            #  Без лотности merge_candle_minutes оставляет lot = 0 и\n"
       "            #  turnover_rub = 0 — то есть «рубли НЕ посчитаны», и\n"
       "            #  сравнить две бумаги по обороту нечем.\n"
       "            #\n"
       "            #  Неизвестная лотность НЕ подменяется единицей: единица —\n"
       "            #  это тоже утверждение о бумаге, и для GAZP оно ложное.\n"
       "            #  Нет лотности — остаётся честный ноль.\n"
       "            lot = (stream.lots or {}).get(tk)\n"
       "            if lot:\n"
       "                rows = [{**r, \"lot\": int(lot)} for r in rows]\n"
       "            n = await db.merge_candle_minutes(tk, rows)\n")


def main() -> int:
    if not os.path.exists(PATH):
        print(f"ОТКАЗ, файла нет: {PATH}")
        return 1
    text = open(PATH, encoding="utf-8").read()

    if NEW in text:
        print("лотность в свечах: уже было")
        print("итог: применено 0, уже было 1, отказов 0")
        return 0

    n = text.count(OLD)
    if n != 1:
        print(f"ОТКАЗ, якорь встретился {n} раз вместо 1")
        print("файл НЕ тронут: частичная врезка хуже, чем никакой")
        return 1

    open(PATH, "w", encoding="utf-8").write(text.replace(OLD, NEW))
    print("лотность в свечах: применено")
    print("итог: применено 1, уже было 0, отказов 0")
    os.remove(__file__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
