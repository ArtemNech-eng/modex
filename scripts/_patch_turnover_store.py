"""
Разовый патч: оборот минутной свечи в РУБЛЯХ, считаемый при записи.

Почему патчем из скрипта, а не пушим файл целиком: src/db.py — 3.2 тысячи
строк. Целиком такой файл не вычитывается без обрезания, а перезаписать по памяти
то, чего не видел целиком, — верный способ потерять кусок кода.

ЧТО ДЕЛАЕТСЯ И ПОЧЕМУ

Сейчас во всех минутных таблицах объём лежит в ЛОТАХ, а лот у разных бумаг
отличается на четыре порядка (FEES 10000 акций, SBER 10). Срез для вердикта
сравнивает бумаги между собой — значит лоты там бессмысленны.

Считать рубли на ЧТЕНИИ нельзя: лотность берётся из справочника ISS по сети, и
если справочник не ответил, весь исторический ряд молча пересчитался бы по
лотности 1. В момент записи лотность известна точно или не известна вовсе, и
это фиксируется навсегда.

Лоты НЕ заменяются рублями, а соседствуют с ними: volume_buy и volume_sell
приходят от биржи именно в лотах, и сверка с биржей возможна только в
исходных единицах.

Колонка lot хранится рядом с turnover_rub не для красоты. lot = 0 означает «рубли
НЕ посчитаны» и отличается от честного нулевого оборота. Без этого различия в
срез попали бы нули, неотличимые от тишины на рынке.

Старые строки не пересчитываются: у них останется lot = 0, то есть «не знаем».
Это честнее, чем задним числом придумать им оборот по сегодняшней лотности.

Скрипт идемпотентен и удаляет себя после успешного применения.
"""
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "db.py"

#  Каждая замена — (имя, старое, новое). Признак «уже применено» — ЦЕЛИКОМ
#  новый блок, а не короткая строчка из него. На прошлом цикле короткая метка
#  совпала с результатом соседнего патча, и патч счёл себя применённым.
EDITS = []

#  1. Импорт арифметики. Модуль money ничего не импортирует, цикла не будет.
EDITS.append((
    "импорт money",
    "from sqlalchemy import String, Integer, Float, Boolean, DateTime, Text, select, delete",
    "from sqlalchemy import String, Integer, Float, Boolean, DateTime, Text, select, delete\n"
    "\n"
    "#  Чистая арифметика лотов в рубли. Вынесена отдельно, чтобы её можно было\n"
    "#  проверять тестами без базы и без SQLAlchemy.\n"
    "from src.analysis.money import candle_turnover_rub",
))

#  2. Колонки в модели.
EDITS.append((
    "колонки CandleMinute",
    '    volume: Mapped[int] = mapped_column(Integer, default=0)          # лоты\n'
    '    volume_buy: Mapped[int] = mapped_column(Integer, default=0)\n'
    '    volume_sell: Mapped[int] = mapped_column(Integer, default=0)\n',
    '    volume: Mapped[int] = mapped_column(Integer, default=0)          # лоты\n'
    '    volume_buy: Mapped[int] = mapped_column(Integer, default=0)\n'
    '    volume_sell: Mapped[int] = mapped_column(Integer, default=0)\n'
    '    #  Рубли рядом с лотами, а не вместо них: сравнивать бумаги можно только\n'
    '    #  в рублях, а сверяться с биржей — только в лотах.\n'
    '    #\n'
    '    #  lot = 0 читается как «рубли НЕ посчитаны». Это НЕ то же самое, что\n'
    '    #  turnover_rub = 0 при lot >= 1, где ноль — факт о рынке. Смешать их значит\n'
    '    #  выдать собственный пробел за тишину на бирже.\n'
    '    lot: Mapped[int] = mapped_column(Integer, default=0)             # 0 = не знаем\n'
    '    turnover_rub: Mapped[float] = mapped_column(Float, default=0.0)  # оценка\n',
))

#  3. Миграция: таблица на живом сервере уже существует, create_all колонки не
#  допишет. Якорь взят вместе со строкой predictions: голое "_ADDED_COLUMNS = {"
#  есть также внутри имени _PREDICTION_ADDED_COLUMNS и совпадает дважды.
EDITS.append((
    "миграция candle_minute",
    '_ADDED_COLUMNS = {\n'
    '    "predictions": _PREDICTION_ADDED_COLUMNS,\n',
    '_ADDED_COLUMNS = {\n'
    '    "predictions": _PREDICTION_ADDED_COLUMNS,\n'
    '    #  Оборот в рублях добавлен, когда таблица уже жила на сервере, —\n'
    '    #  только миграция, create_all тут бессилен.\n'
    '    "candle_minute": {"lot": "INTEGER DEFAULT 0",\n'
    '                      "turnover_rub": "DOUBLE PRECISION DEFAULT 0"},\n',
))

#  4. Новая строка свечи.
EDITS.append((
    "создание строки",
    '                        volume=0, volume_buy=0, volume_sell=0, updates=0)',
    '                        volume=0, volume_buy=0, volume_sell=0, updates=0,\n'
    '                        lot=0, turnover_rub=0.0)',
))

#  5. Сам подсчёт. Стоит рядом с ЗАМЕНОЙ объёма и тоже заменяет, а не
#  прибавляет: объём в свече накопительный, значит и рубли накопительные.
EDITS.append((
    "подсчёт рублей",
    '                row.volume_sell = int(r.get("volume_sell") or 0)\n',
    '                row.volume_sell = int(r.get("volume_sell") or 0)\n'
    '                #  Рубли тоже ЗАМЕНЯЮТСЯ, а не прибавляются: они считаются от\n'
    '                #  накопительного объёма текущей версии свечи.\n'
    '                #\n'
    '                #  Если лотность не пришла, СТАРОЕ значение НЕ затирается. Справочник\n'
    '                #  ISS может отвалиться на одном цикле и вернуться на следующем;\n'
    '                #  обнулять из-за этого уже посчитанный оборот нельзя.\n'
    '                lot = int(r.get("lot") or 0)\n'
    '                if lot >= 1:\n'
    '                    rub = candle_turnover_rub(r, lot)\n'
    '                    if rub is not None:\n'
    '                        row.lot = lot\n'
    '                        row.turnover_rub = rub\n',
))


def main() -> int:
    if not TARGET.exists():
        print(f"НЕТ ФАЙЛА: {TARGET}")
        return 1
    text = TARGET.read_text(encoding="utf-8")
    done = skip = fail = 0
    for name, old, new in EDITS:
        if new in text:
            print(f"turnover: {name}: уже было")
            skip += 1
            continue
        cnt = text.count(old)
        if cnt != 1:
            print(f"turnover: {name}: ОТКАЗ, якорь встретился {cnt} раз вместо 1")
            fail += 1
            continue
        text = text.replace(old, new, 1)
        print(f"turnover: {name}: применено")
        done += 1

    if fail:
        print(f"итог: применено {done}, уже было {skip}, отказов {fail}")
        print("файл НЕ тронут: частичный патч базы хуже, чем никакого")
        return 1

    if done:
        TARGET.write_text(text, encoding="utf-8")

    print(f"итог: применено {done}, уже было {skip}, отказов {fail}")
    try:
        os.remove(__file__)
        print("скрипт удалил себя")
    except OSError as e:
        print(f"себя не удалил: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
