FROM python:3.11-slim

WORKDIR /app

# Системные зависимости.
#
# curl нужен НЕ для приложения, а для healthcheck. Раньше его тут не было, а
# docker-compose проверял живость командой ["CMD","curl","-f",...]. В образе
# python:3.11-slim (Debian bookworm-slim) curl отсутствует, поэтому проверка
# падала не по причине «сервис мёртв», а по причине «нет такого файла». Для
# оркестратора это неотличимо: контейнер просто никогда не становится healthy,
# деплой считается неудачным и остаётся крутиться прежний образ.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Зависимости Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения
COPY . .

# Каталог БД создаём явно. В .dockerignore добавлен data/, чтобы локальная база
# не попадала в образ и не перезатирала журнал прогнозов на томе. Но тогда путь
# ./data/moodex.db из DATABASE_URL некому создать, если том вдруг не смонтирован
# — sqlite в этом случае падает на открытии файла.
RUN mkdir -p /app/data

# Порт дашборда
EXPOSE 8000

# Проверка живости.
#
# start-period обязателен. Без него отсчёт retries идёт с первой секунды, а
# холодный старт тут не мгновенный: импорт всей цепочки модулей, setup_db и
# первые обращения к диску. При interval 30s и retries 3 контейнер объявляется
# больным примерно через полторы минуты — раньше, чем успевает прогреться.
#
# Проверка ходит на /api/stats: этот маршрут отвечает из памяти агрегатора и не
# ждёт ни биржу, ни Claude. Маршрут, зависящий от внешнего API, превратил бы
# healthcheck в проверку чужой доступности.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/stats || exit 1

# Запуск
CMD ["python", "main.py"]
