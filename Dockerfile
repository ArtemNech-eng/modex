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
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ─── Корень Минцифры: без него TLS до Tinkoff не поднимается вообще ───────
#
# Замерено в боевом контейнере 04.08, три проверки подряд:
#
#     getent hosts invest-public-api.tinkoff.ru  -> 178.130.128.33   DNS жив
#     socket.create_connection((..., 443), 10)   -> TCP OK           фаерволл жив
#     httpx.post(...)                            -> CERTIFICATE_VERIFY_FAILED
#                                                   self-signed certificate
#                                                   in certificate chain
#
# Издатель цепочки — Russian Trusted Sub CA, The Ministry of Digital
# Development and Communications; лист на *.tinkoff.ru (TBank), до 30.09.2026.
# Этого корня в Debian bookworm нет и не появится, а без него падает КАЖДЫЙ
# запрос: и REST за FIGI и свечами, и gRPC-стрим стакана.
#
# Почему искали сутки: TinkoffClient ловит ЛЮБУЮ ошибку транспорта httpx и
# докладывает «сеть или таймаут — запрос не дошёл». Формально верно, по сути
# уводит в сторону: 03.08 токен перевыпускали четыре часа, а дело было в
# доверии к сертификату. Само разделение вердиктов в probe() надо уточнить
# отдельно: TLS — это не «сеть».
#
# curl с -k здесь не беспечность, а неизбежность: gu-st.ru сам предъявляет
# сертификат Минцифры, т.е. тот самый корень, который мы только собираемся
# установить — курица и яйцо. Поэтому доверие проверяется после скачивания
# и ИНАЧЕ: собранный файл обязан оказаться самоподписанным корнем именно
# Минцифры, иначе сборка падает. В лог печатается sha256 — сверяйте его
# между деплоями: тихая смена отпечатка значит подмену файла на источнике.
RUN set -eu; \
    cd /usr/local/share/ca-certificates; \
    curl -fsSLk -o russian_trusted_root_ca.crt \
        https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer; \
    curl -fsSLk -o russian_trusted_sub_ca.crt \
        https://gu-st.ru/content/Other/doc/russian_trusted_sub_ca.cer; \
    python -c "import re, ssl, hashlib, sys; p='/usr/local/share/ca-certificates/russian_trusted_root_ca.crt'; raw=open(p).read(); der=ssl.PEM_cert_to_DER_cert(raw); txt=[x.decode() for x in re.findall(rb'[ -~]{6,}', der)]; print('Корень:', [x for x in txt if 'Russian' in x or 'Ministry' in x]); print('sha256:', hashlib.sha256(der).hexdigest()); sys.exit(0 if any('Russian Trusted Root CA' in x for x in txt) else 1)" \
        || { echo 'СБОРКА ОСТАНОВЛЕНА: с gu-st.ru пришёл не корень Минцифры.'; head -c 300 russian_trusted_root_ca.crt; exit 1; }; \
    update-ca-certificates; \
    python -c "import socket, ssl; ssl.create_default_context().wrap_socket(socket.create_connection(('invest-public-api.tinkoff.ru', 443), 10), server_hostname='invest-public-api.tinkoff.ru'); print('TLS до Tinkoff на сборке: OK')" \
        || echo 'ПРЕДУПРЕЖДЕНИЕ: проверка TLS на сборке не прошла (сеть сборщика?). Корень установлен, смотри /api/stream/health после деплоя.'

# gRPC НЕ смотрит в системные корни — у него свой встроенный список, вшитый в
# колёсо grpcio. Без этой переменной установка корня выше починила бы только
# REST: /api/health/figi стал бы зелёным, а стрим продолжал бы молча не
# подключаться — ровно та же асимметрия «один путь чинишь, второй мёртв»,
# что только что была с чисткой токена. Два раза на одни грабли не наступаем.
#
# SSL_CERT_FILE — для всего остального, что ходит через OpenSSL напрямую.
ENV GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# ─── torch: ОТДЕЛЬНЫМ шагом и с --index-url, а не --extra-index-url ───────────
#
# В requirements.txt стояло:
#     --extra-index-url https://download.pytorch.org/whl/cpu
#     torch==2.4.1
# и это не гарантирует CPU-сборку. --extra-index-url не задаёт приоритет: pip
# сливает оба индекса и выбирает по версии, а не по источнику. Пин 2.4.1 без
# суффикса подходит и к 2.4.1 с PyPI, и к 2.4.1+cpu с индекса PyTorch.
#
# Разница в весе решающая:
#     torch 2.4.1+cpu (индекс PyTorch)     195 МБ
#     torch 2.4.1     (PyPI)               797 МБ + 12 пакетов nvidia-*
#     только семь из этих nvidia-пакетов  2 824 МБ
# Итого лишних примерно 3.4 ГБ загрузки на сборку. На небольшом сервере это
# заканчивается «no space left on device» — причём плавающе, в зависимости от
# того, сколько места оставил предыдущий образ и кэш сборки.
#
# --index-url (без extra) означает: брать ТОЛЬКО отсюда, PyPI как источник torch
# не рассматривать. Шаг стоит ДО requirements.txt, чтобы кэшировался отдельно и
# не пересобирался при каждой правке зависимостей.
RUN pip install --no-cache-dir \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.4.1

# Зависимости Python. torch здесь уже установлен как 2.4.1+cpu и требованию
# torch==2.4.1 удовлетворяет (PEP 440: локальная версия совпадает по базовой),
# поэтому повторно не скачивается.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Заслон: если CUDA-сборка всё-таки просочилась, сборка обязана упасть ЗДЕСЬ с
# понятной причиной, а не через десять минут по нехватке диска.
RUN if pip list 2>/dev/null | grep -qiE '^(nvidia-|triton[[:space:]])'; then \
        echo "СБОРКА ОСТАНОВЛЕНА: в образ попала CUDA-сборка torch (+3.4 ГБ)."; \
        echo "Проверь --index-url для torch выше по Dockerfile."; \
        pip list | grep -iE '^(nvidia-|triton[[:space:]])'; \
        exit 1; \
    fi

# ─── ТОТ ЖЕ корень — ещё и в бандл certifi ─────────────────────────────
#
# Урок, полученный на живом проде через десять минут после предыдущего
# деплоя. Всё было установлено правильно:
#
#     /usr/local/share/ca-certificates/russian_trusted_root_ca.crt   2088 байт
#     /etc/ssl/certs/ca-certificates.crt                           229155 байт
#     GRPC_DEFAULT_SSL_ROOTS_FILE_PATH                             выставлен
#
# и httpx ВСЁ РАВНО падал с CERTIFICATE_VERIFY_FAILED. Причина: httpx не
# смотрит в /etc/ssl/certs вообще — он проверяет цепочку по СВОЕМУ бандлу
# certifi в site-packages. Системная установка лечит curl, openssl и gRPC,
# но не клиент, которым мы ходим за FIGI и свечами.
#
# Итого три НЕЗАВИСИМЫХ хранилища доверия в одном контейнере:
#
#     системное  /etc/ssl/certs      -> curl, openssl, healthcheck
#     grpcio     своё, вшитое       -> стрим стакана (лечится ENV выше)
#     certifi    site-packages       -> httpx: FIGI, свечи, сделки
#
# Починенное одно из трёх даёт самый коварный исход: часть проверок зелёные,
# данные мёртвые. Поэтому заливаем во все три.
#
# Шаг стоит ПОСЛЕ requirements.txt: до этого момента certifi в образе нет,
# он приходит зависимостью httpx. Заканчивается боевой проверкой: настоящий
# запрос к FindInstrument. Ожидаемый ответ — 401: токена на сборке нет, значит
# сеть, TLS и HTTP работают целиком. Проверка НЕ валит сборку: у сборщика
# может не быть сети до биржи, и деплой из-за диагностики ломаться не должен.
RUN set -eu; \
    CB="$(python -c 'import certifi; print(certifi.where())')"; \
    echo "бандл certifi: $CB"; \
    cat /usr/local/share/ca-certificates/russian_trusted_root_ca.crt \
        /usr/local/share/ca-certificates/russian_trusted_sub_ca.crt >> "$CB"; \
    python -c "import httpx; r = httpx.post('https://invest-public-api.tinkoff.ru/rest/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument', json={'query': 'SBER'}, timeout=15); print('REST до Tinkoff на сборке, код:', r.status_code)" \
        || echo 'ПРЕДУПРЕЖДЕНИЕ: запрос к Tinkoff на сборке не прошёл. Смотри /api/stream/health после деплоя.'

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
