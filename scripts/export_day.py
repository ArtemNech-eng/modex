"""
Выгрузка дня по бумаге одним JSON.

Запуск в контейнере:
    python scripts/export_day.py SBER            # сегодня
    python scripts/export_day.py SBER 2026-08-05 # конкретный день
    python scripts/export_day.py SBER 2026-08-05 5   # и 5 дней истории для нормы

ПРО ПУТЬ ИМПОРТА. При запуске python scripts/файл.py интерпретатор кладёт
в sys.path каталог САМОГО ФАЙЛА, а не корень проекта, и пакет src оказывается
невидим. Поэтому корень добавляется явно и ДО импортов проекта.

ДВЕ ЛОВУШКИ ЧИТАТЕЛЕЙ, ОПЛАЧЕННЫЕ ПУСТЫМИ БЛОКАМИ.

1. У micro_series третий аргумент — source, а не шаг ряда. Переданный туда
   "1m" превращается в фильтр по несуществующему источнику и даёт МОЛЧАЛИВЫЙ
   пустой список. Здесь источник оставлен по умолчанию (exchange).
2. У level_series limit=400 по умолчанию при сортировке по возрастанию ts,
   то есть без явного лимита видно только утро. За день по одной бумаге
   там десятки тысяч событий (61 615 на 1020 минут у SBER 06.08).

Вся логика сборки лежит в src/analysis/day_slice.py и покрыта тестами.
Здесь только чтение таблиц: база в CI недоступна, поэтому этот файл
никогда не импортируется тестами.
"""
import asyncio
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import db                                    # noqa: E402
from src.analysis import day_slice as ds              # noqa: E402

# Потолок для чтения уровней. Наружу едет не сырой ряд, а сводка,
# поэтому большое число строк раздувает память, а не ответ.
LEVEL_LIMIT = 200000


def msk_today():
    return (datetime.datetime.utcnow()
            + datetime.timedelta(hours=ds.MSK_SHIFT_H)).strftime("%Y-%m-%d")


def prev_days(day, n):
    """Предыдущие календарные дни. Выходные не отсеиваются специально:
    пустой день просто не даст строк и не попадёт в выборку нормы."""
    base = datetime.date.fromisoformat(day)
    return [(base - datetime.timedelta(days=i)).isoformat()
            for i in range(1, n + 1)]


async def main():
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "SBER").upper()
    day = sys.argv[2] if len(sys.argv) > 2 else msk_today()
    hist_days = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    await db.setup_db()

    blocks = {
        "свечи": await db.candle_series(ticker, day, "1m"),
        "поток": await db.flow_series(ticker, day, "1m"),
        "стакан": await db.book_series(ticker, day, "1m"),
        "секунды": await db.micro_series(ticker, day),
        "уровни": await db.level_series(ticker, day, limit=LEVEL_LIMIT),
    }

    history = []
    for d in prev_days(day, hist_days):
        history.extend(await db.candle_series(ticker, d, "1m"))

    try:
        market = await db.market_series("IMOEX", day)
    except Exception as e:                      # таблицы может ещё не быть
        print(f"# фон рынка не прочитан: {e}", file=sys.stderr)
        market = []

    res = ds.assemble(ticker, day, blocks, history=history, market=market)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))

    miss = res["missing"]
    print(f"\n# пробелов: {len(miss)}", file=sys.stderr)
    for m in miss:
        print(f"#   {m}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
