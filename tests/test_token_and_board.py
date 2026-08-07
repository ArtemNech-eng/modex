"""
Дыры, которые зелёный прогон НЕ закрывал.

Все 71 тест прошли, и при этом оба дефекта ниже были в коде. Так бывает, когда
тест проверяет случай, где все признаки согласны друг с другом: такой тест
подтверждает результат, но не ПОРЯДОК предпочтений. Порядок виден только там,
где признаки СПОРЯТ.
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.token import clean_token          # noqa: E402
from src.collector import tinkoff_client as tc  # noqa: E402


def _run(coro):
    """Свой цикл событий, как в test_figi_failure_reason."""
    try:
        prev = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        prev = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        except Exception:                                        # noqa: BLE001
            pass
        asyncio.set_event_loop(prev)


def _share(figi, board, flag, ticker="SBER"):
    return {"ticker": ticker, "figi": figi, "classCode": board,
            "instrumentType": "share", "apiTradeAvailableFlag": flag}


# ── борд против флага ────────────────────────────────────────────────────────

def test_moex_board_wins_over_trade_flag():
    """
    Самый важный случай: признаки указывают НА РАЗНЫЕ бумаги.

    Старый ключ (flag, board) при reverse=True выбирал внебиржевую бумагу.
    Мы торгуем Мосбиржу, и FIGI нужен именно её: чужой инструмент даст свои
    свечи и свой стакан, и это ровно та подмена, из-за которой HYDR получал
    цены FEES.
    """
    spb = _share("BBG_SPB_CHUZHOY", "SPBXM", True)
    tqbr = _share("BBG004730N88", "TQBR", False)
    got = tc._pick_share([spb, tqbr], "SBER")
    assert got["figi"] == "BBG004730N88", \
        "борд TQ* должен быть важнее apiTradeAvailableFlag"


def test_order_of_input_does_not_matter():
    """Сортировка, а не «первый подходящий из ответа»."""
    spb = _share("BBG_SPB_CHUZHOY", "SPBXM", True)
    tqbr = _share("BBG004730N88", "TQBR", False)
    assert tc._pick_share([tqbr, spb], "SBER")["figi"] == "BBG004730N88"
    assert tc._pick_share([spb, tqbr], "SBER")["figi"] == "BBG004730N88"


def test_flag_still_decides_within_the_same_board():
    """Флаг не отменён, он понижен в приоритете: при равных бордах решает он."""
    no_flag = _share("BBG_NO", "TQBR", False)
    with_flag = _share("BBG_YES", "TQBR", True)
    assert tc._pick_share([no_flag, with_flag], "SBER")["figi"] == "BBG_YES"


def test_wrong_ticker_is_never_picked_however_attractive():
    """Точное совпадение тикера выше любых предпочтений."""
    other = _share("BBG_FEES", "TQBR", True, ticker="FEES")
    assert tc._pick_share([other], "HYDR") is None


# ── чистка токена в общем модуле ──────────────────────────────────────────────

def test_client_uses_the_shared_cleaner():
    """
    Клиент и общий модуль — ОДНА функция, а не две похожие.

    Две похожие — это и есть исходная болезнь: REST чистил кавычки, конфиг нет.
    """
    assert tc._clean_token is clean_token


def test_quotes_and_newline_never_reach_the_header():
    client = tc.TinkoffClient('"t.secret"\n')
    assert client.token == "t.secret"
    assert client.headers["Authorization"] == "Bearer t.secret"


def test_quote_inside_the_value_is_kept():
    """Непарная кавычка — часть токена, а не мусор окружения."""
    assert clean_token('t.se"cret') == 't.se"cret'


# ── цена счастливого пути ────────────────────────────────────────────────────

class _CountingPost:
    """Подмена _post: считает запросы и запоминает тела."""

    def __init__(self, payload):
        self.payload = payload
        self.bodies = []

    async def __call__(self, endpoint, body):
        self.bodies.append(body)
        return self.payload


def test_happy_path_costs_exactly_one_request():
    """
    Каскад из трёх фильтров — АВАРИЙНЫЙ режим, а не норма.

    Без этой границы подъём 48 бумаг незаметно превратился бы в 144 запроса —
    и упёрся бы в тот самый 429, который probe умеет распознавать.
    """
    tc.TICKER_TO_FIGI.pop("SBER", None)
    client = tc.TinkoffClient("t.token")
    post = _CountingPost({"instruments": [_share("BBG004730N88", "TQBR", True)]})
    client._post = post
    try:
        figi = _run(client.get_figi("SBER"))
    finally:
        tc.TICKER_TO_FIGI.pop("SBER", None)
    assert figi == "BBG004730N88"
    assert len(post.bodies) == 1, f"запросов {len(post.bodies)}, а должен быть один"
    assert post.bodies[0].get("apiTradeAvailableFlag") is True


def test_relaxation_happens_only_after_an_empty_answer():
    """А вот когда строгий запрос пуст — фильтры снимаются по очереди."""
    tc.TICKER_TO_FIGI.pop("SBER", None)
    client = tc.TinkoffClient("t.token")
    answers = [{"instruments": []},
               {"instruments": [_share("BBG004730N88", "TQBR", False)]}]
    bodies = []

    async def _post(endpoint, body):
        bodies.append(body)
        return answers[len(bodies) - 1]

    client._post = _post
    try:
        figi = _run(client.get_figi("SBER"))
    finally:
        tc.TICKER_TO_FIGI.pop("SBER", None)
    assert figi == "BBG004730N88"
    assert len(bodies) == 2
    assert "apiTradeAvailableFlag" not in bodies[1]


def test_settings_uses_the_very_same_cleaner():
    """
    Главный тест всего коммита: токен чистится ОДИНАКОВО на двух путях.

    Проверка по ИСХОДНИКУ, а не по импорту, сознательно: config.settings тянет
    десятки переменных окружения и dotenv, и перезагружать его внутри теста
    значит менять глобальное состояние под остальными 943 тестами.
    В этом репозитории такая проверка уже принята (см. test_volume_events).
    """
    src = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    assert "from config.token import clean_token" in src, \
        "конфиг не берёт общую чистку"
    assert 'clean_token(os.getenv("TINKOFF_TOKEN"' in src, \
        "токен в конфиге всё ещё чистится голым .strip()"
    assert '.strip()' not in src.split("TINKOFF_TOKEN")[1][:80], \
        "рядом с TINKOFF_TOKEN остался старый .strip()"


# ─── Вердикт TLS отделён от вердикта «сеть» ───────────────────────────────────
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

    assert tls["verdict"] != net["verdict"]
