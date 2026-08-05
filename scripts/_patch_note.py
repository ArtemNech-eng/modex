"""
Надпись о норме по времени суток обязана описывать ФАКТ (версия 2).

05.08 в проде одновременно: profiles_ready 44, vol_profiles 44 — и рядом
«не построена: лучшая бумага имеет 2 торговых дней из 10 нужных».

Причина не в тексте, а в ветвлении: stream.profile_note присваивался
только в ветке else, то есть когда не построено ни одной бумаги. Как
только дозаливка ISS дала первые 44 профиля, надпись замерла в
состоянии «до» и начала врать.

Что правится:
  src/analysis/volume_events.py — profile_note учится говорить «построена по N
                                  бумагам» и сколько ждать остальным;
  main.py                       — надпись пишется ВСЕГДА, без ветвления.

Оба якоря сверены с живыми файлами в main перед отправкой, байт в байт.

Скрипт идемпотентен, всегда выходит нулём (об отказе кричит тест, а не
лог сборки) и удаляет себя при успехе.
"""
import io
import os
import sys

VE = "src/analysis/volume_events.py"
MAIN = "main.py"

VE_SIG_OLD = "def profile_note(gaps) -> str:"
VE_SIG_NEW = "def profile_note(gaps, built: int = 0, total: int = 0) -> str:"

VE_BODY_OLD = (
    '    if not gaps:\n'
    '        return "не построена: минутной истории в базе нет вовсе"\n'
    '    g = max(gaps, key=lambda x: x.get("usable_days", 0))\n'
)

VE_BODY_NEW = (
    '    # ФАКТ, А НЕ ТОЛЬКО ОТСУТСТВИЕ. 05.08 в проде рядом стояли\n'
    '    # profiles_ready 44 и «не построена: лучшая бумага имеет 2 дней».\n'
    '    # Надпись писалась только в ветке «не построено ничего» и после\n'
    '    # дозаливки осталась висеть прошлым состоянием. Диагностика,\n'
    '    # противоречащая тому, что описывает, хуже её отсутствия.\n'
    '    if built and not gaps:\n'
    '        return ("построена по всем {b} бумагам: норма берётся по этой же "\n'
    '                "минуте прошлых дней").format(b=built)\n'
    '    if built:\n'
    '        g = max(gaps, key=lambda x: x.get("usable_days", 0))\n'
    '        return ("построена по {b} бумагам из {t}; у остальных {r} истории "\n'
    '                "мало (лучшая имеет {u} торговых дней из {n}) — там "\n'
    '                "работает скользящая").format(\n'
    '            b=built, t=(total or built + len(gaps)), r=len(gaps),\n'
    '            u=g.get("usable_days", 0), n=g.get("need_days", MIN_DAYS))\n'
    '    if not gaps:\n'
    '        return "не построена: минутной истории в базе нет вовсе"\n'
    '    g = max(gaps, key=lambda x: x.get("usable_days", 0))\n'
)

MAIN_OLD = (
    '                if built:\n'
    '                    logger.info(f"Норма объёма по времени суток: {built} бумаг")\n'
    '                else:\n'
    '                    stream.profile_note = profile_note(gaps)\n'
    '                    logger.info("Норма объёма по времени суток %s",\n'
    '                                stream.profile_note)\n'
)

MAIN_NEW = (
    '                # Надпись пишется ВСЕГДА. Раньше она присваивалась только\n'
    '                # когда не построено ни одной бумаги, и после дозаливки\n'
    '                # ISS осталась висеть прошлым состоянием: в ответе API\n'
    '                # одновременно стояли profiles_ready 44 и «не построена».\n'
    '                stream.profile_note = profile_note(gaps, built=built,\n'
    '                                                   total=len(tickers))\n'
    '                logger.info("Норма объёма по времени суток: %s",\n'
    '                            stream.profile_note)\n'
)

PAIRS = [
    (VE, "подпись profile_note", VE_SIG_OLD, VE_SIG_NEW),
    (VE, "ветки «построена» в profile_note", VE_BODY_OLD, VE_BODY_NEW),
    (MAIN, "безусловная запись надписи", MAIN_OLD, MAIN_NEW),
]


def main():
    print("патч надписи о норме объёма, версия 2")
    print("python " + sys.version.split()[0] + ", cwd " + os.getcwd())
    done, already, missed = [], [], []
    per_file = {}
    for path, name, old, new in PAIRS:
        if not os.path.exists(path):
            missed.append(name + " (нет файла " + path + ")")
            continue
        src = per_file.get(path)
        if src is None:
            src = io.open(path, encoding="utf-8").read()
        if new in src:
            already.append(name)
        elif old in src:
            src = src.replace(old, new, 1)
            done.append(name)
        else:
            missed.append(name)
        per_file[path] = src

    if not missed:
        for path, src in per_file.items():
            io.open(path, "w", encoding="utf-8").write(src)

    print("ПРИМЕНЕНО:")
    for n in done:
        print(" + " + n)
    for n in already:
        print(" = " + n + " (уже было)")
    if missed:
        print("НЕ НАЙДЕН ЯКОРЬ (файлы НЕ тронуты):")
        for n in missed:
            print(" ! " + n)
        print("Отказ увидит tests/test_profile_note.py — он упадёт.")
        return

    try:
        os.remove(os.path.abspath(__file__))
        print("Скрипт удалил себя.")
    except OSError as e:
        print("Себя удалить не смог: " + str(e))


if __name__ == "__main__":
    main()
