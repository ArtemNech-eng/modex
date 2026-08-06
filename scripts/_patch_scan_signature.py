"""
Подпись `scan` в price_events.py получает параметр часов `at`.

ПОЧЕМУ ОТДЕЛЬНЫМ ФАЙЛОМ. В прогоне 31083532399 основной патч применил 15
правок из 16, а правку подписи `scan` счёл уже сделанной. Его метка
идемпотентности была строкой с девятью пробелами:

    "         p: Optional[dict] = None, at=None) -> list:"

а правка `events_for`, выполненная раньше, создала строку с пятнадцатью — такую
же, только глубже. Одна оказалась ПОДСТРОКОЙ другой. Тело `scan` уже звал
`at`, подпись — ещё нет, и четыре теста упали с NameError.

НИЧЕГО НЕ СЛОМАЛОСЬ: коммит кода в применялке привязан к нулевому коду
`pytest`, поэтому поломанный файл остался в ранере и в репозиторий не ушёл.
Оба скрипта отработают заново на чистых файлах, в любом порядке.

УРОК НА БУДУЩЕЕ. Метка идемпотентности обязана быть уникальной не только в
исходном файле, но и среди того, что пишут соседние правки. Отступ — не
признак уникальности, потому что поиск идёт по подстроке, а не по строкам.
"""
import sys
from pathlib import Path

PRICE = Path("src/analysis/price_events.py")

OLD = ("def scan(minutes: dict, ticks: Optional[dict] = None,\n"
       "         levels: Optional[dict] = None, steps: tuple = STEPS,\n"
       "         p: Optional[dict] = None) -> list:")

NEW = ("def scan(minutes: dict, ticks: Optional[dict] = None,\n"
       "         levels: Optional[dict] = None, steps: tuple = STEPS,\n"
       "         p: Optional[dict] = None, at=None) -> list:")


def main() -> int:
    if not PRICE.exists():
        print(f"ОТКАЗ — нет файла {PRICE}")
        return 0
    text = PRICE.read_text(encoding="utf-8")

    #  Метка — ЦЕЛИКОМ новая подпись из трёх строк, а не её хвост. Именно
    #  хвост и подвёл в прошлый раз.
    if NEW in text:
        print("scan: уже было")
        print("итог: применено 0, уже было 1, отказов 0")
    else:
        found = text.count(OLD)
        if found != 1:
            print(f"scan: ОТКАЗ — якорь найден {found} раз(а)")
            print("итог: применено 0, уже было 0, отказов 1")
            return 0
        PRICE.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
        print("scan: применено")
        print("итог: применено 1, уже было 0, отказов 0")

    try:
        Path(__file__).unlink()
        print("Скрипт удалил себя.")
    except OSError as exc:                                   # noqa: BLE001
        print(f"Скрипт не смог удалить себя: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
