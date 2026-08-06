"""
Опрос индексов для поминутного хранения. Сети здесь нет.

ПОЧЕМУ СЕТЬ СНАРУЖИ. Функция загрузки передаётся аргументом. Если вшить
запрос внутрь, то поведение при обрыве связи, таймауте и мусорном ответе
можно будет проверить только на живой бирже в момент аварии — то есть
никогда. А именно эти три случая и портят данные.

ДВОЕ ЧАСОВ, ИХ НЕЛЬЗЯ ПУТАТЬ:

    минута строки (ts)  — по НАШИМ часам: в какую ячейку кладём значение
    возраст (age_sec) — по метке БИРЖИ: насколько значение устарело

Если считать возраст от времени запроса, то на закрытой бирже позавчерашнее
число выглядит секундой от роду. Так уже ошибались 02.08.

НЕОТВЕТ — ЭТО НЕ НОЛЬ. Имя, по которому ISS не ответил, попадает в missing
и в базу не пишется. Ноль изменения — это самостоятельное утверждение
«рынок стоит на месте», а не отсутствие данных.

ОДНО ИМЯ НЕ РОНЯЕТ ОСТАЛЬНЫЕ. Ошибка на отраслевом индексе не должна
лишать нас IMOEX за ту же минуту.
"""
from datetime import datetime
from typing import Callable, Iterable, Optional

from src.collector.iss_index import index_url, now_msk, parse_index

#  Главный фон. Отраслевые имена добавляются сюда без правки схемы:
#  ключ строки в базе включает имя.
NAMES = ("IMOEX",)

#  Формат ключа минуты тот же, что у свечей и потока. Совпадение формата
#  обязательно: иначе фон не сойдётся с бумагами по минутам.
MINUTE_FMT = "%Y-%m-%dT%H:%M"


def minute_key(at: Optional[datetime] = None) -> str:
    """Ячейка минуты по московским часам."""
    return (at or now_msk()).strftime(MINUTE_FMT)


def to_row(payload, at: Optional[datetime] = None,
           name: str = "IMOEX") -> Optional[dict]:
    """
    Ответ ISS → строка для market_minute, или НИЧЕГО.

    Метка биржи лежит у разбора в поле ts, а в базе это поле exch_ts, потому
    что ts в базе — это ячейка минуты. Переименование сделано ЗДЕСЬ и один
    раз: если перепутать, в базу ляжет пустая метка, и защита от затирания
    свежего значения старым перестанет работать молча.
    """
    r = parse_index(payload, at=at, name=name)
    if r is None:
        return None
    row = {
        "ts": minute_key(at),
        "name": r.get("name") or (name or "").upper(),
        "value": r["value"],
        "exch_ts": r.get("ts") or "",
        "age_sec": r.get("age_sec"),
    }
    for k in ("change_pct", "change_to_open_pct", "open", "high", "low",
              "prev_close", "valtoday_rub"):
        if k in r:
            row[k] = r[k]
    if r.get("stale"):
        #  Застрявшее значение ВСр1 РАВНО пишется с пометкой. Выбросить его
        #  значит оставить дырку, неотличимую от обрыва связи.
        row["stale"] = True
    return row


def poll(fetch: Callable[[str], object],
         names: Iterable[str] = NAMES,
         at: Optional[datetime] = None) -> dict:
    """
    Спросить все имена и собрать строки.

    fetch получает АДРЕС и возвращает разобранный JSON. Любое исключение
    внутри fetch — это отсутствие данных по ОДНОМУ имени, а не падение всего
    опроса.

    Возвращает {"rows": [...], "missing": [...], "ts": "минута"}. missing нужен,
    чтобы в срезе было написано «не знаем», а не просто пусто.
    """
    at = at or now_msk()
    rows, missing = [], []
    for nm in names:
        nm = (nm or "").upper()
        if not nm:
            continue
        try:
            payload = fetch(index_url(nm))
        except Exception:                                        # noqa: BLE001
            missing.append(nm)
            continue
        row = to_row(payload, at=at, name=nm)
        if row is None:
            missing.append(nm)
        else:
            rows.append(row)
    return {"rows": rows, "missing": missing, "ts": minute_key(at)}
