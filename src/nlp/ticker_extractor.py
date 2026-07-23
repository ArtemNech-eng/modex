"""
MOODEX — Ticker Extractor
Извлечение тикеров Мосбиржи из текста сообщений.
"""
import re
import logging
from typing import Optional
from config.settings import MOEX_TICKERS

logger = logging.getLogger(__name__)

# Все тикеры (в верхнем регистре)
ALL_TICKERS = set(MOEX_TICKERS.keys())

# Паттерны упоминаний тикеров в тексте на русском трейдерском сленге
# Примеры: $SBER, #GAZP, LKOH, лукойл, сбер, газпром
TICKER_PATTERNS = [
    r"\$([A-Z]{2,8})\b",          # $SBER, $GAZP
    r"#([A-Z]{2,8})\b",           # #SBER, #GAZP
    r"\b([A-Z]{2,8})\b",          # просто SBER, GAZP (uppercase)
]

# Словарь: русское слово → тикер
RUSSIAN_ALIASES: dict[str, str] = {
    # Сбер
    "сбер": "SBER", "сбербанк": "SBER", "сберbank": "SBER",
    # Газпром
    "газпром": "GAZP", "газик": "GAZP",
    # Лукойл
    "лукойл": "LKOH", "лук": "LKOH",
    # Норникель
    "норникель": "GMKN", "гмкн": "GMKN", "норник": "GMKN",
    # Яндекс
    "яндекс": "YNDX", "yndx": "YNDX",
    # Татнефть
    "татнефть": "TATN",
    # МТС
    "мтс": "MTSS",
    # Магнит
    "магнит": "MGNT",
    # Алроса
    "алроса": "ALRS",
    # Полюс
    "полюс": "PLZL",
    # ВТБ
    "втб": "VTBR",
    # Аэрофлот
    "аэрофлот": "AFLT",
    # ММК
    "ммк": "MAGN",
    # НЛМК
    "нлмк": "NLMK",
    # Северсталь
    "северсталь": "CHMF",
    # ФосАгро
    "фосагро": "PHOR",
    # ПИК
    "пик": "PIKK",
    # Интер РАО
    "интеррао": "IRAO", "интер рао": "IRAO",
    # РусАл
    "русал": "RUAL",
    # Сургутнефтегаз
    "сургут": "SNGS", "сургутнефтегаз": "SNGS",
    # Мечел
    "мечел": "MTLR",
    # РусГидро
    "русгидро": "HYDR",
    # X5 Group
    "пятёрочка": "FIVE", "x5": "FIVE",
    # Ozon
    "озон": "OZON", "ozon": "OZON",
    # Мосбиржа
    "мосбиржа": "MOEX", "мосq": "MOEX",
    # Т-Банк
    "тинькофф": "TCSG", "tinkoff": "TCSG", "тбанк": "TCSG",
    # Газпромнефть
    "газпромнефть": "SIBN",
    # Транснефть
    "транснефть": "TRNFP",
    # Совкомфлот
    "совкомфлот": "FLOT",
    # Самолёт
    "самолёт": "SMLT", "самолет": "SMLT",
    # VK
    "вк": "VKCO", "vk": "VKCO",
    # Positive Technologies
    "позитив": "POSI", "positive": "POSI",
    # Астра
    "астра": "ASTR",
    # HeadHunter
    "хэдхантер": "HEAD", "headhunter": "HEAD", "hh": "HEAD",
    # Whoosh
    "вуш": "WUSH", "whoosh": "WUSH",
    # Новатэк
    "новатэк": "NVTK",
    # Роснефть
    "роснефть": "ROSN",
    # Диасофт
    "диасофт": "DIAS",
}

# Стоп-слова (не тикеры, хотя могут совпасть по паттерну)
STOPWORDS = {
    "ЦБ", "РФ", "НА", "ДО", "ОТ", "ПО", "ИЗ", "НЕ", "БЫ", "МНЕ",
    "ЕГО", "ЕЁ", "ИМ", "ИХ", "МЫ", "ВЫ", "ОН", "ОНА", "ОНИ", "ОНО",
    "США", "ЕС", "МВФ", "ВВП", "ПФ", "ПФР", "НДС", "НДФ", "ИИС",
    "ETF", "IPO", "SPO", "ОФЗ", "ВДО", "ТГ", "БКС",
    "GDP", "EUR", "USD", "RUB", "CNY", "GBP", "INR", "TRY",
}


def extract_tickers(text: str) -> list[str]:
    """
    Извлечь все упомянутые тикеры Мосбиржи из текста.
    
    Args:
        text: текст сообщения
        
    Returns:
        Список уникальных тикеров (например, ["SBER", "GAZP"])
        
    Examples:
        >>> extract_tickers("Сбер идёт вниз, газик держится")
        ["SBER", "GAZP"]
        
        >>> extract_tickers("$LKOH сегодня сильная, покупаю")
        ["LKOH"]
    """
    found = set()

    # 1. Русские алиасы (сленг)
    text_lower = text.lower()
    for alias, ticker in RUSSIAN_ALIASES.items():
        # Ищем алиас как отдельное слово (с границами)
        pattern = r"\b" + re.escape(alias) + r"\b"
        if re.search(pattern, text_lower):
            found.add(ticker)

    # 2. Английские тикеры с $ или # префиксом
    for pattern in TICKER_PATTERNS[:2]:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            ticker = match.group(1).upper()
            if ticker in ALL_TICKERS:
                found.add(ticker)

    # 3. Чистые uppercase слова (без $/#) — только если в нашем списке тикеров
    for match in re.finditer(r"\b([A-Z]{2,8})\b", text):
        ticker = match.group(1).upper()
        if ticker in ALL_TICKERS and ticker not in STOPWORDS:
            found.add(ticker)

    return sorted(found)


def get_ticker_name(ticker: str) -> Optional[str]:
    """Получить русское название компании по тикеру"""
    return MOEX_TICKERS.get(ticker.upper())


def is_market_related(text: str) -> bool:
    """
    Быстрая проверка: является ли текст рыночным (упоминает акции/торговлю)?
    Используем для отфильтровки нерелевантных сообщений.
    """
    market_keywords = [
        "акци", "рынок", "индекс", "портфель", "инвести", "торгов",
        "биржа", "бумаг", "дивиденд", "прибыл", "убыток", "позици",
        "шорт", "лонг", "buy", "sell", "hold", "стоп", "тейк",
        "profit", "loss", "иис", "офз", "ipo", "spo", "etf", "пай",
        "брокер", "сигнал", "тренд", "поддержк", "сопротивлен",
        "пробой", "отскок", "коррекц", "ралли", "pump", "dump",
        "%", "₽", "руб", "rub"
    ]
    text_lower = text.lower()
    
    # Если есть тикер — уже рыночное
    if extract_tickers(text):
        return True
    
    # Иначе проверяем ключевые слова
    return any(kw in text_lower for kw in market_keywords)


def is_noise(text: str) -> bool:
    """
    Обратная функция к is_market_related.
    Возвращает True если текст НЕ относится к рынку (шум, реклама, флуд).
    Используется в бэкфилле для отсева нерелевантных сообщений.
    """
    if not text or len(text.strip()) < 5:
        return True
    return not is_market_related(text)
