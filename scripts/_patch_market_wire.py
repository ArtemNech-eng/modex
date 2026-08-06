"""
Врезка хранения фона рынка в фоновый цикл main.py.

Что было: _market_background каждые 30 секунд спрашивал IMOEX и клал его в
stream.imoex — В ПАМЯТЬ. После любого деплоя память пуста, и вопрос «что было
с рынком в 14:32» оставался без ответа.

Что становится: тот же цикл дополнительно пишет строку в market_minute. Память
остаётся как была: от неё зависят карточка и широта рынка.

Два записи в минуту (цикл 30 с) — это нормально: merge ЗАМЕНЯЕТ значение в
ячейке и ОТКАЗЫВАЕТСЯ затирать свежую метку биржи более старой.

ОТКАЗ ВМЕСТО ЧАСТИЧНОГО ПАТЧА. Если якорь не найден или встретился больше
одного раза, файл не трогается вообще: половина правки в запускающемся файле
хуже, чем никакой.
"""
import os
import sys

PATH = "main.py"

EDITS = []

#  1. Импорт. Якорь из ДВУХ строк: первая сама по себе встречается трижды
#     (_lots, _atr_background, _market_background).
EDITS.append((
    "импорт опросчика",
    "        import urllib.request as u, json as j\n"
    "        from datetime import datetime as _dt\n",
    "        import urllib.request as u, json as j\n"
    "        from datetime import datetime as _dt\n"
    "        from src.collector.index_poll import (NAMES as INDEX_NAMES,\n"
    "                                              poll as poll_index)\n",
))

#  2. Сама запись. Якорь — весь цикл целиком: «while True:» в файле много.
OLD_LOOP = (
    "        while True:\n"
    "            try:\n"
    "                got = await asyncio.to_thread(_fetch_index)\n"
    "                if got:\n"
    "                    got[\"fetched_at\"] = datetime.now(timezone.utc).timestamp()\n"
    "                    stream.imoex = got\n"
    "            except Exception as e:                               # noqa: BLE001\n"
    "                logger.debug(f\"IMOEX: {e}\")\n"
    "            await asyncio.sleep(30)\n"
)

NEW_LOOP = (
    "        def _fetch_json(url):\n"
    "            return j.load(u.urlopen(url, timeout=25))\n"
    "\n"
    "        while True:\n"
    "            try:\n"
    "                got = await asyncio.to_thread(_fetch_index)\n"
    "                if got:\n"
    "                    got[\"fetched_at\"] = datetime.now(timezone.utc).timestamp()\n"
    "                    stream.imoex = got\n"
    "            except Exception as e:                               # noqa: BLE001\n"
    "                logger.debug(f\"IMOEX: {e}\")\n"
    "            #  ФОН РЫНКА В БАЗУ, ПОМИНУТНО. Выше значение кладётся в память\n"
    "            #  и живёт до первого деплоя. Без истории фона любое движение\n"
    "            #  бумаги приходится объяснять её собственными причинами,\n"
    "            #  даже когда просто росло всё сразу.\n"
    "            try:\n"
    "                out = await asyncio.to_thread(poll_index, _fetch_json,\n"
    "                                              INDEX_NAMES)\n"
    "                if out[\"rows\"]:\n"
    "                    await db.merge_market_minutes(out[\"rows\"])\n"
    "                if out[\"missing\"]:\n"
    "                    #  Неответ НЕ пишется нулём: ноль изменения — это\n"
    "                    #  утверждение «рынок стоит», а не отсутствие данных.\n"
    "                    logger.debug(\"фон рынка: нет ответа по %s\",\n"
    "                                 \", \".join(out[\"missing\"]))\n"
    "            except Exception as e:                               # noqa: BLE001\n"
    "                logger.debug(f\"фон рынка в базу: {e}\")\n"
    "            await asyncio.sleep(30)\n"
)

EDITS.append(("запись фона в базу", OLD_LOOP, NEW_LOOP))


def main() -> int:
    if not os.path.exists(PATH):
        print(f"ОТКАЗ: нет файла {PATH}")
        return 1
    with open(PATH, encoding="utf-8") as f:
        text = f.read()

    applied = already = refused = 0
    for name, old, new in EDITS:
        if new in text:
            print(f"{name}: уже было")
            already += 1
            continue
        n = text.count(old)
        if n != 1:
            print(f"{name}: ОТКАЗ, якорь встретился {n} раз вместо 1")
            refused += 1
            continue
        text = text.replace(old, new, 1)
        print(f"{name}: применено")
        applied += 1

    if refused:
        print("файл НЕ тронут: частичная врезка хуже, чем никакой")
        print(f"итог: применено 0, уже было {already}, отказов {refused}")
        return 1

    if applied:
        with open(PATH, "w", encoding="utf-8") as f:
            f.write(text)

    print(f"итог: применено {applied}, уже было {already}, отказов 0")
    try:
        os.remove(__file__)
        print("скрипт удалил себя")
    except Exception as e:                                        # noqa: BLE001
        print(f"себя не удалил: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
