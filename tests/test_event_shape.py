"""
Форма события базы знаний: где что лежит.

Ошибка, ради которой написан файл. 31.07 я читал текст новости из
`event["payload"]["text"]`, получал пусто и объявил новостной контур сломанным.
Текст всё это время лежал в `event["text"]` — верхним полем. В `payload` только
служебные `guid` и `published`.

Цена ошибки: в базе лежали два события по OZON —

    10:15:54  «Акции Ozon упали более чем на 8% на фоне эвакуации работников
              со склада» (опасность атаки БПЛА, Зеленодольск)
    10:20:07  «Ozon сообщил об ОТСУТСТВИИ ПОВРЕЖДЕНИЙ после атаки на
              логистический центр»

Событие было разрешено в 10:20. Шорт владельцу я выдал в 10:33 — через
тринадцать минут после публичного снятия риска, и построил его на общем
рассуждении вместо собственных данных.

Тест фиксирует форму события, чтобы следующий агент не гадал.
"""
import json

import pytest

# Ключи, которые MarketEvent.to_dict обязан отдавать верхним уровнем.
TOP_LEVEL_KEYS = {
    "id", "ts", "source", "kind", "ticker", "channel",
    "text", "label", "score", "signal", "payload",
}

# Что кладут в payload источники новостей — только служебное.
NEWS_PAYLOAD_KEYS = {"guid", "published"}


def _to_dict_source() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "src/db.py").read_text()


def test_text_is_a_top_level_field():
    """`text` отдаётся верхним полем, а не внутри payload."""
    src = _to_dict_source()
    assert '"text": self.text' in src, (
        "MarketEvent.to_dict больше не отдаёт text верхним полем — "
        "проверь, куда он переехал, и почини читателей"
    )


def test_all_documented_keys_are_returned():
    """Полный список ключей события зафиксирован — чтобы не гадать."""
    src = _to_dict_source()
    for k in TOP_LEVEL_KEYS:
        assert f'"{k}"' in src, f"ключ {k} пропал из to_dict"


def test_news_payload_holds_only_service_fields():
    """
    Источники новостей кладут в payload только guid и published. Если туда
    начнут класть текст — читатели, ищущие payload["text"], заработают, но
    молча разъедутся с теми, кто читает event["text"].
    """
    from pathlib import Path

    main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    # Блок записи RSS-новости
    i = main_src.find('"source": "rss", "kind": "news"')
    assert i > 0, "не найден блок записи RSS-новости"
    block = main_src[i:i + 700]
    assert '"text": item.full_text' in block, "текст новости больше не пишется в text"
    # В payload — только служебное
    pi = block.find('"payload"')
    assert pi > 0
    payload_block = block[pi:pi + 300]
    assert "published" in payload_block and "guid" in payload_block
    assert '"text"' not in payload_block, (
        "текст попал в payload — теперь он лежит в двух местах, и читатели разойдутся"
    )


def test_reader_helper_reads_the_right_field():
    """
    Эталон чтения текста события. Если понадобится читать текст — вот так,
    а не через payload.
    """
    ev = {
        "id": 1, "ts": "2026-07-31T07:20:07+00:00", "source": "rss",
        "kind": "news", "ticker": "OZON", "channel": "rbc",
        "text": "Ozon сообщил об отсутствии повреждений после атаки",
        "label": "neutral", "score": 0.5, "signal": 0.0,
        "payload": {"guid": "rssexport.rbc.ru:politics:6a6c", "published": "..."},
    }
    assert set(ev) == TOP_LEVEL_KEYS
    assert ev["text"]
    assert "text" not in ev["payload"]
    assert set(ev["payload"]) == NEWS_PAYLOAD_KEYS


@pytest.mark.parametrize("wrong", ["title", "full_text", "message", "body"])
def test_common_wrong_guesses_are_not_present(wrong):
    """Имена, которые хочется угадать, в событии отсутствуют — только `text`."""
    assert wrong not in TOP_LEVEL_KEYS


def test_payload_may_be_a_json_string():
    """
    payload приходит строкой JSON из БД и словарём из to_dict. Читатель обязан
    выдерживать оба варианта — иначе половина событий молча пропадёт.
    """
    for raw in ('{"guid": "x", "published": "y"}', {"guid": "x", "published": "y"}):
        p = json.loads(raw) if isinstance(raw, str) else raw
        assert p["guid"] == "x"
