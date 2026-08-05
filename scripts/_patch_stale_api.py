"""
Вывести счётчик устаревших баров в ответ /api/volume-scan.

ПОЧЕМУ ПАТЧ, А НЕ ПРАВКА ФАЙЛА. src/api/main.py — 167 КБ и три тысячи
строк. Целиком его передавать нельзя — любое усечение по дороге стоило бы
обезглавливания API. Правка идёт по короткому уникальному якорю.

ОТСТУПЫ НЕ ЗАШИТЫ В ЯКОРЬ. Прошлые отказы были именно на пробелах:
якорь с жёстким отступом ломается от одного переноса строки. Отступ
вычисляется из самого текста и повторяется в вставляемой строке.

КАЖДЫЙ ЯКОРЬ ОБЯЗАН ВСТРЕТИТЬСЯ РОВНО ОДИН РАЗ. Два совпадения — это не
«поправим оба», а признак того, что я не понимаю файл. Тогда ОТКАЗ.

ИДЕМПОТЕНТНОСТЬ. Повторный запуск на уже исправленном файле ничего не
дублирует и считается успехом: аппликатор может быть запущен второй раз
по чужой причине.

ВЫХОД 0 ВСЕГДА. О провале должен сообщать ТЕСТ, а не скрипт: тест
смотрит на результат, а скрипт — только на свою попытку.
"""
import os
import re

API = "src/api/main.py"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def patch_import(src):
    """stale рядом с warming_up: те же семь букв, тот же модуль."""
    if re.search(r"warming_up,\s*stale", src):
        return src, "уже было"
    pat = re.compile(r"warming_up, FLOOR_RUB\)")
    found = pat.findall(src)
    if len(found) != 1:
        return src, f"ОТКАЗ — якорь найден {len(found)} раз(а)"
    return pat.sub("warming_up, stale, FLOOR_RUB)", src, count=1), "применено"


def patch_field(src):
    """Третья причина пустоты встаёт рядом с двумя прежними."""
    if '"stale": stale(mins)' in src:
        return src, "уже было"
    pat = re.compile(r"([ \t]*)\"warming_up\": warming_up\(mins\),")
    found = pat.findall(src)
    if len(found) != 1:
        return src, f"ОТКАЗ — якорь найден {len(found)} раз(а)"

    def repl(m):
        pad = m.group(1)
        return (
            f"{pad}\"warming_up\": warming_up(mins),\n"
            f"{pad}# НОЧНАЯ ТИШИНА ОБЯЗАНА НАЗЫВАТЬ СЕБЯ. Событие больше не\n"
            f"{pad}# выставляется на устаревшем баре, и это верно, но снаружи\n"
            f"{pad}# такое молчание неотличимо от спокойного рынка. Три причины\n"
            f"{pad}# пустой таблицы различимы: below_floor, warming_up, stale.\n"
            f"{pad}\"stale\": stale(mins),"
        )

    return pat.sub(repl, src, count=1), "применено"


def main():
    if not os.path.exists(API):
        print(f"ОТКАЗ — нет файла {API}")
        print("итог: применено 0, отказов 1")
        return
    src = _read(API)
    done = 0
    refused = 0
    for label, fn in (("stale в импорте маршрута", patch_import),
                      ("счётчик устаревших в ответе", patch_field)):
        src, note = fn(src)
        print(f"{label}: {note}")
        if note.startswith("ОТКАЗ"):
            refused += 1
        else:
            done += 1
    if refused == 0:
        _write(API, src)
    else:
        print("файл НЕ тронут: частичная правка хуже, чем никакой")
    print(f"итог: применено {done}, отказов {refused}")
    if refused == 0:
        try:
            os.remove(__file__)
            print("Скрипт удалил себя.")
        except OSError as exc:                               # noqa: BLE001
            print(f"удалить себя не вышло: {exc}")


main()
