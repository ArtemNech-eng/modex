#!/usr/bin/env python3
# Одноразовый патч: вердикт TLS отделяется от вердикта «сеть».
#
# Почему скриптом, а не правкой файла целиком: tinkoff_client.py весит 25 КБ,
# правок три, и перепечатывание такого объёма ради трёх вставок уже один раз
# обрывалось на середине. Здесь точные замены, идемпотентные: если правка уже
# на месте, шаг молчит и ничего не портит. После применения скрипт удаляет себя.
import os

REPORT = []
FAILED = False

CLIENT = "src/collector/tinkoff_client.py"
TESTS = "tests/test_token_and_board.py"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def patch(path, old, new, tag):
    global FAILED
    src = _read(path)
    if new in src:
        REPORT.append("уже применено: " + tag)
        return
    n = src.count(old)
    if n != 1:
        REPORT.append("ЯКОРЬ НЕ НАЙДЕН ИЛИ НЕ УНИКАЛЕН (%d вхождений): %s" % (n, tag))
        FAILED = True
        return
    _write(path, src.replace(old, new))
    REPORT.append("применено: " + tag)


def append(path, text, marker, tag):
    src = _read(path)
    if marker in src:
        REPORT.append("уже применено: " + tag)
        return
    _write(path, src.rstrip("\n") + "\n\n\n" + text.strip("\n") + "\n")
    REPORT.append("дописано: " + tag)


# ─── 1. Помощник: разбор причины отказа транспорта ────────────────────────────

OLD_CLASS = '''class TinkoffClient:
    """Клиент Tinkoff Invest REST API."""'''

HELPER = '''# Отказ проверки сертификата — НЕ сетевая ошибка, и это не придирка к словам.
#
# 03.08 прод стоял четыре часа, утром 04.08 — ещё три, и оба раза вердикт был
# один: «сеть или таймаут до Tinkoff — запрос не дошёл». По нему перевыпускали
# токен, проверяли фаервол и egress. Измерения в живом контейнере показали
# совсем другое:
#
#     getent hosts invest-public-api.tinkoff.ru -> 178.130.128.33   DNS жив
#     socket.create_connection((..., 443), 10)  -> TCP OK           сеть жива
#     httpx.post(...)                           -> CERTIFICATE_VERIFY_FAILED
#
# Пакеты ходили, соединение открывалось, отвергала нас СВОЯ ЖЕ проверка TLS:
# в образе python:3.11-slim нет корня Минцифры, которым подписан *.tinkoff.ru
# (цепочка: лист *.tinkoff.ru <- Russian Trusted Sub CA <- The Ministry of
# Digital Development and Communications). Вердикт называл слой, где всё было
# исправно, и умалчивал о единственном слое, где была поломка.
#
# Отдельная тонкость, из-за которой одной подсказки мало: хранилищ доверия в
# контейнере ТРИ, и они независимы. Системное /etc/ssl/certs обслуживает curl,
# openssl и healthcheck; бандл certifi в site-packages — httpx, то есть весь
# наш REST за FIGI, свечами и стаканом; grpcio носит свой вшитый список и
# слушает только GRPC_DEFAULT_SSL_ROOTS_FILE_PATH. Утром 04.08 корень положили
# в системное хранилище и в gRPC — и httpx продолжал падать при зелёном curl.
# Поэтому подсказка перечисляет все три места: починить два из трёх означает
# получить самый дорогой исход — часть проверок зелёные, данных нет.
TLS_MARK = "CERTIFICATE_VERIFY_FAILED"

TLS_VERDICT = ("сертификат не проверился: нет корня Минцифры. Сеть и TCP исправны, "
               "запрос отвергает локальная проверка TLS. Корень нужен в ТРЁХ местах: "
               "системный бандл (curl, healthcheck), certifi (httpx — FIGI, свечи, "
               "стакан) и GRPC_DEFAULT_SSL_ROOTS_FILE_PATH (стрим)")


def _transport_reason(e: Exception) -> str:
    # Причина отказа транспорта, с РАЗДЕЛЕНИЕМ проверки сертификата и сети.
    # Текст исключения сохраняется в квадратных скобках: вердикт объясняет, что
    # делать, а исходная строка нужна, когда причина окажется четвёртой,
    # непредусмотренной. Ничего не выбрасываем — именно потерянная подробность
    # и стоила двух простоев.
    text = "%s: %s" % (type(e).__name__, str(e)[:160])
    if TLS_MARK in str(e):
        return TLS_VERDICT + " [" + text + "]"
    return text


class TinkoffClient:
    """Клиент Tinkoff Invest REST API."""'''

patch(CLIENT, OLD_CLASS, HELPER, "tinkoff_client.py: помощник _transport_reason")


# ─── 2. _post: причина вместо схлопывания всего в имя класса ──────────────────

OLD_POST = '''        except Exception as e:
            logger.warning(f"Tinkoff API error {endpoint}: {e}")
            self._fail(f"{type(e).__name__}: {str(e)[:160]}")
            return None'''

NEW_POST = '''        except Exception as e:
            logger.warning(f"Tinkoff API error {endpoint}: {e}")
            # Раньше здесь стояло f"{type(e).__name__}: ..." — наружу уходило
            # «ConnectError», и выше по стеку это превращалось в «сеть». Теперь
            # причина разбирается до записи, потому что читать её будет агент,
            # у которого нет доступа к логу.
            self._fail(_transport_reason(e))
            return None'''

patch(CLIENT, OLD_POST, NEW_POST, "tinkoff_client.py: _post называет причину")


# ─── 3. probe: вердикт TLS вместо вердикта «сеть» ─────────────────────────────

OLD_PROBE = '''            else:
                verdict = "сеть или таймаут до Tinkoff — запрос не дошёл"'''

NEW_PROBE = '''            else:
                # Статуса нет — значит ответ не пришёл. Но причин этому две, и
                # они лечатся противоположными действиями: сеть чинит хостер, а
                # доверие к сертификату — мы сами, в образе. Различаем по тексту
                # ошибки, который теперь доходит сюда неискажённым.
                err = str(full.get("error") or "")
                if TLS_MARK in err or TLS_VERDICT[:24] in err:
                    verdict = TLS_VERDICT
                else:
                    verdict = "сеть или таймаут до Tinkoff — запрос не дошёл"'''

patch(CLIENT, OLD_PROBE, NEW_PROBE, "tinkoff_client.py: probe различает TLS и сеть")


# ─── 4. Тесты ────────────────────────────────────────────────────────────────
#
# Дописываются в существующий файл намеренно: он уже перечислен в CI, и правка
# workflow не требуется. Тесты самодостаточны — свои импорты внутри функций,
# чтобы не зависеть от помощников соседних тестов.

NEW_TESTS = '''# ─── Вердикт TLS отделён от вердикта «сеть» ───────────────────────────────────
#
# Регрессия на два реальных простоя: 03.08 (четыре часа) и утро 04.08. Оба раза
# сеть была исправна — DNS отвечал, TCP до 178.130.128.33:443 открывался, — а
# рукопожатие отвергала локальная проверка сертификата, потому что в образе не
# было корня Минцифры. Вердикт при этом говорил «сеть или таймаут», и по нему
# перевыпускали токен. Эти тесты сторожат, чтобы диагноз больше не путал слой,
# где всё исправно, со слоем, где поломка.


def _run_tls(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


def _client_failing_with(err):
    # Клиент, у которого каждый запрос падает с заданной причиной. Подменяем
    # _post, а не httpx: проверяем разбор причины, а не работу библиотеки.
    from src.collector.tinkoff_client import TinkoffClient

    c = TinkoffClient("t0ken")

    async def _post(endpoint, body):
        c._fail(err)
        return None

    c._post = _post
    return c


def test_certificate_failure_is_not_called_network():
    import httpx
    from src.collector.tinkoff_client import _transport_reason

    e = httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "self-signed certificate in certificate chain (_ssl.c:1016)")
    reason = _transport_reason(e)

    assert "сертификат" in reason
    assert "сеть" not in reason
    # Исходный текст исключения сохраняется: он понадобится, когда причина
    # окажется не из известных трёх.
    assert "CERTIFICATE_VERIFY_FAILED" in reason


def test_real_network_failure_is_still_called_network():
    import httpx
    from src.collector.tinkoff_client import _transport_reason

    reason = _transport_reason(httpx.ConnectTimeout("timed out"))

    assert "сертификат" not in reason
    assert "ConnectTimeout" in reason


def test_probe_names_the_certificate_and_the_three_stores():
    reason = ("сертификат не проверился: нет корня Минцифры [ConnectError: "
              "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed]")
    out = _run_tls(_client_failing_with(reason).probe("SBER"))
    verdict = out["verdict"]

    assert "сертификат" in verdict
    # Подсказка обязана перечислить все три хранилища доверия. Утром 04.08
    # починили два из трёх — системное и gRPC — и httpx продолжал падать при
    # зелёном curl. Названное неполно лечится неполно.
    assert "certifi" in verdict
    assert "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH" in verdict


def test_probe_still_says_network_when_the_request_really_never_left():
    out = _run_tls(_client_failing_with("ConnectTimeout: timed out").probe("SBER"))

    assert "сеть" in out["verdict"]
    assert "сертификат" not in out["verdict"]


def test_the_two_verdicts_are_different_texts():
    # Смысл всей правки: два диагноза требуют разных действий, поэтому они
    # обязаны звучать по-разному. Раньше оба звучали как «сеть».
    tls = _run_tls(_client_failing_with(
        "ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] verify failed").probe("SBER"))
    net = _run_tls(_client_failing_with("ConnectTimeout: timed out").probe("SBER"))

    assert tls["verdict"] != net["verdict"]'''

append(TESTS, NEW_TESTS, "test_certificate_failure_is_not_called_network",
       "tests/test_token_and_board.py: пять тестов на вердикт TLS")


# ─── Отчёт и самоудаление ─────────────────────────────────────────────────────

print("### Патч: вердикт TLS")
for line in REPORT:
    print(" - " + line)

try:
    os.remove(__file__)
    print(" - скрипт удалил себя")
except OSError as e:
    print(" - скрипт не смог удалить себя: %s" % e)

raise SystemExit(1 if FAILED else 0)
