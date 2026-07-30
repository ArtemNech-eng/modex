"""Тесты привязки текста к бумаге и словарной тональности.

Запуск: python3 tests/test_text_attribution.py

Два разных дефекта, найденных 30.07 при сверке источников данных.

1. ПОДМЕНА БУМАГИ. Псевдонимы искались независимо друг от друга, поэтому
   короткий перетягивал текст на другую компанию:
     «Газпром нефть» -> GAZP вместо SIBN (разные компании);
     «Сургутнефтегаз преф» -> SNGS вместо SNGSP (у префов не было ни одного
     псевдонима, любой текст про них уходил в обычку).
   Плюс поиск по точной границе слова находил только именительный падеж, а
   русский текст пишет «Сбера», «по Лукойлу», «норникелем». На живых заголовках
   RSS доля с распознанной бумагой выросла с 5% до 8% без ложных привязок.

2. ТОНАЛЬНОСТЬ ПО ПОДСТРОКЕ. Стояло `w in text`, без границ слова:
     «выгодно покупать» -> neutral: «выгодно» давало плюс, «дно» внутри него минус;
     «support level»    -> positive: внутри «support» находилось «up».
   И оговорки не учитывались вовсе: «падение маловероятно» -> negative,
   «обвал не грозит» -> negative, «не вижу роста» -> positive.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.nlp.ticker_extractor import extract_tickers as ex, RUSSIAN_ALIASES  # noqa: E402
from src.nlp.sentiment_analyzer import keyword_sentiment as ks              # noqa: E402


# ───────────────── подмена бумаги: длинное совпадение важнее ──────────────────

def test_gazprom_neft_is_not_gazprom():
    """ГЛАВНОЕ: это две разные компании."""
    assert set(ex("Газпром нефть отчиталась за квартал")) == {"SIBN"}
    assert set(ex("Газпром-нефть отчиталась")) == {"SIBN"}


def test_plain_gazprom_still_gazprom():
    assert set(ex("Газпром отчитался")) == {"GAZP"}


def test_preferred_shares_are_distinct():
    """У SNGSP не было ни одного псевдонима — префы уходили в обычку."""
    assert set(ex("Сургутнефтегаз преф интересен")) == {"SNGSP"}
    assert set(ex("сургут ап держу")) == {"SNGSP"}
    assert set(ex("Сургутнефтегаз обычка")) == {"SNGS"}


def test_preferred_shares_have_aliases():
    for tk in ("SNGSP", "TRNFP"):
        assert [a for a, t in RUSSIAN_ALIASES.items() if t == tk], tk


# ───────────────────────── падежи ─────────────────────────────────────────────

def test_oblique_cases_are_recognized():
    """Русский текст склоняется — раньше находился только именительный."""
    for text, tk in (("Сбера акции упали", "SBER"), ("Сберу плохо", "SBER"),
                     ("по Лукойлу вниз", "LKOH"), ("держу Газпрома", "GAZP"),
                     ("норникелем доволен", "GMKN"), ("роснефтью доволен", "ROSN"),
                     ("Северсталью довольны", "CHMF"), ("татнефти отчёт", "TATN"),
                     ("русгидрой не интересуюсь", "HYDR")):
        assert tk in ex(text), (text, ex(text))


def test_short_alias_stays_strict():
    """«лук» убран как обычное слово; трёхбуквенные не получают окончаний."""
    assert "лук" not in RUSSIAN_ALIASES
    assert set(ex("сварил лук на обед")) == set()
    assert set(ex("лукойл вниз")) == {"LKOH"}


def test_no_alias_typos_left():
    """В таблице была запись «мосq» — опечатка, не совпадавшая ни с чем."""
    import re
    for a in RUSSIAN_ALIASES:
        assert not re.search(r"[а-яё][a-z]|[a-z][а-яё]", a) or a in ("сберbank",), a


# ─────────────────── тональность: границы слова и оговорки ────────────────────

def test_substring_no_longer_flips_meaning():
    """«выгодно» содержит «дно» — раньше фраза схлопывалась в нейтральную."""
    assert ks("выгодно покупать сейчас").label == "positive"
    assert ks("это очень выгодно").label == "positive"


def test_english_substring_not_matched():
    """Внутри «support» находилось «up»."""
    assert ks("support level holds").label == "neutral"
    assert ks("upside limited").label == "neutral"


def test_hedges_neutralize():
    """Оговорка переворачивает смысл; признать неопределённость честнее, чем
    инвертировать наугад."""
    for t in ("падение маловероятно", "обвал не грозит", "не вижу роста",
              "снижения не ожидается", "вряд ли вырастет"):
        assert ks(t).label == "neutral", (t, ks(t).label)


def test_plain_polarity_still_works():
    assert ks("рост продолжится").label == "positive"
    assert ks("обвал на рынке").label == "negative"
    assert ks("дно найдено").label == "negative"


def test_emoji_still_matched():
    """У эмодзи границ слова нет — для них подстрока остаётся."""
    assert ks("🚀 полетели").label == "positive"



# ───── имя эмитента совпадает с названием площадки или индекса ────────────────

def test_venue_mention_is_not_company_news():
    """ГЛАВНОЕ. «На Мосбирже» по-русски значит «на бирже» — это указание места,
    а не новость об эмитенте MOEX. Замер на живых новостях 30.07: из семи
    привязок к MOEX пять были указанием места или упоминанием индекса, и одна
    подняла ложное новостное событие news_observe (запас 9.7 мин до выноса)."""
    assert set(ex("На Мосбирже очередные чудеса — Элемент растёт")) == set()
    assert set(ex("Сижу, смотрю на график индекса Мосбиржи")) == set()
    assert set(ex("Механизм роста индекса Мосбиржи после 20 июля")) == set()


def test_real_issuer_news_kept():
    """Настоящие новости эмитента терять нельзя: их было две из семи."""
    assert set(ex("Мосбиржа 4 августа запускает вечные фьючерсы")) == {"MOEX"}
    assert set(ex("Мосбиржа начнет торги вечными фьючерсами на ETF")) == {"MOEX"}


def test_mixed_mention_keeps_ticker():
    """Если есть упоминание ВНЕ площадочного контекста — привязка остаётся."""
    assert "MOEX" in ex("Мосбиржа отчиталась, а на Мосбирже тихо")


def test_venue_context_does_not_hide_other_tickers():
    """Отмена по контексту касается только своей бумаги."""
    assert set(ex("Индекс Мосбиржи вырос, Сбер отчитался")) == {"SBER"}


def test_venue_rule_is_declarative():
    """Правило описано данными, а не зашито в алгоритм: так его видно и можно
    добавить другую бумагу с тем же свойством."""
    from src.nlp.ticker_extractor import VENUE_CONTEXT
    assert "MOEX" in VENUE_CONTEXT and VENUE_CONTEXT["MOEX"]

if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ПАДАЕТ {name}: {e}")
        except Exception as e:                      # noqa: BLE001
            failed += 1
            print(f"  ОШИБКА {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed} из {len(tests)} пройдено")
    sys.exit(1 if failed else 0)
