"""
Разовый патч: убрать установку torch из образа и закрыть падение
анализатора тональности без модели.

Почему патчем, а не перезаписью файлов: Dockerfile — 14 КБ подробных
разборов прошлых инцидентов (корень Минцифры, три хранилища доверия,
CUDA-заслон). Такой файл переписывать целиком нельзя: любая потеря
комментария стоит следующего расследования с нуля. Правим по якорю,
и отказываемся, если якорь не единственный.

Скрипт всегда завершается кодом 0: отказ должен быть виден тестом и
отчётом, а не падением job'а на середине.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def patch(rel: str, old: str, new: str, label: str) -> bool:
    path = os.path.join(ROOT, rel)
    with open(path, encoding="utf-8") as f:
        src = f.read()

    if new in src:
        print(f"{label}: уже применено, файл не тронут")
        return True

    found = src.count(old)
    if found != 1:
        print(f"{label}: ОТКАЗ — якорь найден {found} раз(а), ожидался один")
        return False

    with open(path, "w", encoding="utf-8") as f:
        f.write(src.replace(old, new))
    print(f"{label}: применено")
    return True


DOCKER_OLD = """RUN pip install --no-cache-dir \\
    --index-url https://download.pytorch.org/whl/cpu \\
    torch==2.4.1

# Зависимости Python. torch здесь уже установлен как 2.4.1+cpu и требованию
# torch==2.4.1 удовлетворяет (PEP 440: локальная версия совпадает по базовой),
# поэтому повторно не скачивается.
COPY requirements.txt ."""

DOCKER_NEW = """# ШАГ УСТАНОВКИ torch УДАЛЁН 05.08. Разбор выше оставлен как история: он
# объясняет, почему при возврате модели нужен именно --index-url. Причина
# удаления другая и практическая: 195 МБ колеса разворачиваются в образе в
# ~1.5 ГБ, вместе с transformers и tokenizers — около 2.2 ГБ. В этот вечер
# сборка умерла дважды: «no space left on device» на экспорте слоёв, затем
# pip убили на распаковке (exit 255, без строки ошибки). Тональность в
# торговом контуре не участвует: сигналы строятся на объёме, стакане и цене.
#
# Модель нужна локально:  pip install -r requirements.txt -r requirements-nlp.txt
# Возвращаешь в образ: верни этот шаг С --index-url и проверь, что на сервере
# есть свободные три-четыре гигабайта, иначе повторится ровно то же.

# Зависимости Python.
COPY requirements.txt ."""

NLP_OLD = """        if not texts:
            return []

        loop = asyncio.get_event_loop()"""

NLP_NEW = """        if not texts:
            return []

        # Модели в образе может не быть вовсе: с 05.08 transformers и torch
        # вынесены в requirements-nlp.txt. _load_sync такой случай переживал
        # (ловит ImportError и оставляет _pipeline пустым), а вот здесь путь
        # вёл в _predict_sync, который бросает RuntimeError — и разбор
        # сообщений падал целиком. Словарный метод для этого случая написан
        # ниже в этом же файле, его и используем.
        if self._pipeline is None:
            return [keyword_sentiment(t) for t in texts]

        loop = asyncio.get_event_loop()"""

ok_docker = patch("Dockerfile", DOCKER_OLD, DOCKER_NEW, "Dockerfile: шаг torch")
ok_nlp = patch(
    "src/nlp/sentiment_analyzer.py",
    NLP_OLD,
    NLP_NEW,
    "sentiment_analyzer: словарный путь без модели",
)

print(f"итог: dockerfile {'ok' if ok_docker else 'отказ'}, nlp {'ok' if ok_nlp else 'отказ'}")

if ok_docker and ok_nlp:
    os.remove(os.path.abspath(__file__))
    print("Скрипт удалил себя.")
else:
    print("Скрипт оставлен на месте: применено не всё, смотри отказы выше.")
