"""Причина отказа резолва FIGI обязана доходить до вызывающего.

Запуск: python3 tests/test_figi_failure_reason.py

Нужен потому, что 03.08 прод четыре часа стоял с 48 мёртвыми тикерами, и назвать
причину было НЕЛЬЗЯ. Статус ответа знали в `_post`, наружу уходил `None`, а лога
у агента нет. Владелец перевыпустил токен — не помогло и ничего не сообщило.

Три случая раньше сливались в один `None`, а лечатся они по-разному:
  • отказ HTTP (401 — токен, 429 — лимит, 5xx — сам API)
  • ответ 200 с пустым instruments — наш запрос отсеял всё сам
  • ответ 200 с инструментами, но без совпадения тикера
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import src.collector.tinkoff_client as tc                        # noqa: E402
from src.collector.tinkoff_client import TinkoffClient           # noqa: E402

FLAG_HINT = "apiTradeAvailableFlag"


def _run(coro):
    """Своя петля, закрыть её и ВЕРНУТЬ ПРЕЖНЮЮ на место.

    НЕ asyncio.run: на Python 3.9 он обнуляет текущую петлю, и падает СЛЕДУЮЩИЙ
    тест по алфавиту, а не этот. Возврат прежней петли — то же правило, что в
    tests/conftest.py; за ним следит test_suite_hygiene и он поймал этот файл,
    когда возврата здесь ещё не было.
    """
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


# ─── двойники httpx ───────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


class _Httpx:
    def __init__(self, resp):
        self._resp = resp

    def AsyncClient(self, **kw):                                 # noqa: N802
        return _Client(self._resp)


def _with_response(resp, coro_factory):
    """Подменить httpx на время одного вызова."""
    saved = tc.httpx
    tc.httpx = _Httpx(resp)
    try:
        return _run(coro_factory())
    finally:
        tc.httpx = saved


def _client():
    c = TinkoffClient(token="t.probe_dummy")
    tc.TICKER_TO_FIGI.clear()
    return c


def _patch_post(client, sequence):
    """Отдавать заготовленные ответы по порядку вместо сети."""
    calls = list(sequence)

    async def fake_post(endpoint, body):
        item = calls.pop(0) if calls else None
        if item is None:
            client._fail("двойник исчерпан")
            return None
        client._fail(None)
        return item
    client._post = fake_post
    return client


# ─── _post запоминает статус ──────────────────────────────────────────────────

def test_post_records_http_status():
    c = _client()
    out = _with_response(_Resp(429, "too many requests"),
                         lambda: c._post("Endpoint", {}))
    assert out is None
    assert c.last_status == 429, c.last_status
    assert "HTTP 429" in (c.last_error or ""), c.last_error


def test_post_records_transport_error():
    c = _client()
    out = _with_response(RuntimeError("connect timeout"),
                         lambda: c._post("Endpoint", {}))
    assert out is None
    assert "RuntimeError" in (c.last_error or ""), c.last_error
    assert c.last_status is None


def test_post_clears_reason_on_success():
    c = _client()
    c._fail("прошлая беда", 500)
    out = _with_response(_Resp(200, "", {"instruments": []}),
                         lambda: c._post("Endpoint", {}))
    assert out == {"instruments": []}
    assert c.last_error is None, c.last_error


def test_post_reports_missing_token():
    c = TinkoffClient(token="")
    out = _run(c._post("Endpoint", {}))
    assert out is None
    assert "TINKOFF_TOKEN" in (c.last_error or ""), c.last_error


# ─── resolve_live различает три случая ────────────────────────────────────────

def test_resolve_live_blames_own_filter_on_empty_list():
    c = _patch_post(_client(), [{"instruments": []}])
    assert _run(c.resolve_live("SBER")) is None
    assert FLAG_HINT in (c.last_error or ""), c.last_error


def test_resolve_live_reports_no_exact_match_and_names_what_came():
    c = _patch_post(_client(), [{"instruments": [{"ticker": "SBERP"},
                                                 {"ticker": "SBER_OLD"}]}])
    assert _run(c.resolve_live("SBER")) is None
    why = c.last_error or ""
    assert "совпадения тикера нет" in why, why
    assert "SBERP" in why, why


def test_resolve_live_clears_reason_when_found():
    c = _patch_post(_client(), [{"instruments": [{"ticker": "SBER", "figi": "BBG004730N88"}]}])
    got = _run(c.resolve_live("SBER"))
    assert got and got["figi"] == "BBG004730N88"
    assert c.last_error is None, c.last_error


def test_two_failure_kinds_give_different_reasons():
    """Главное утверждение: пустой список и отсутствие совпадения РАЗЛИЧИМЫ."""
    a = _patch_post(_client(), [{"instruments": []}])
    _run(a.resolve_live("SBER"))
    b = _patch_post(_client(), [{"instruments": [{"ticker": "GAZP"}]}])
    _run(b.resolve_live("SBER"))
    assert a.last_error != b.last_error, "оба отказа дали одну причину"


# ─── get_figi тоже записывает причину ─────────────────────────────────────────

def test_get_figi_records_reason_and_does_not_cache_miss():
    c = _patch_post(_client(), [{"instruments": []}])
    assert _run(c.get_figi("SBER")) is None
    assert FLAG_HINT in (c.last_error or ""), c.last_error
    assert "SBER" not in tc.TICKER_TO_FIGI, "промах не должен попадать в кэш"


# ─── probe называет причину ───────────────────────────────────────────────────

def test_probe_says_token_missing():
    out = _run(TinkoffClient(token="").probe())
    assert out["token_set"] is False
    assert "TINKOFF_TOKEN" in out["verdict"], out


def test_probe_names_invalid_token_on_401():
    c = _client()
    out = _with_response(_Resp(401, "unauthenticated"), lambda: c.probe())
    assert "недействителен" in out["verdict"], out["verdict"]


def test_probe_names_rate_limit_on_429():
    c = _client()
    out = _with_response(_Resp(429, "rate"), lambda: c.probe())
    assert "лимит" in out["verdict"], out["verdict"]


def test_probe_names_outage_on_5xx():
    c = _client()
    out = _with_response(_Resp(503, "unavailable"), lambda: c.probe())
    assert "недоступен" in out["verdict"], out["verdict"]


def test_probe_names_network_when_request_never_left():
    c = _client()
    out = _with_response(OSError("no route"), lambda: c.probe())
    assert "сеть" in out["verdict"], out["verdict"]


def test_probe_healthy_when_instruments_come_back():
    c = _client()
    out = _with_response(_Resp(200, "", {"instruments": [{"ticker": "SBER"}]}),
                         lambda: c.probe())
    assert "исправно" in out["verdict"], out["verdict"]


def test_probe_blames_trade_flag_filter():
    """Гипотеза 03.08: токен рабочий, а фильтр отсеивает всё."""
    c = _client()
    _patch_post(c, [
        {"instruments": []},                                # с фильтрами
        {"instruments": [{"ticker": "SBER"}]},              # без apiTradeAvailableFlag
        {"instruments": [{"ticker": "SBER"}]},              # только query
    ])
    out = _run(c.probe())
    assert FLAG_HINT in out["verdict"], out["verdict"]


def test_probe_blames_instrument_kind():
    c = _client()
    _patch_post(c, [
        {"instruments": []},
        {"instruments": []},
        {"instruments": [{"ticker": "SBER"}]},
    ])
    out = _run(c.probe())
    assert "instrumentKind" in out["verdict"], out["verdict"]


def test_probe_distinguishes_all_verdicts():
    """Прибор бесполезен, если на разные поломки отвечает одинаково."""
    c1 = _client()
    v401 = _with_response(_Resp(401, ""), lambda: c1.probe())["verdict"]
    c2 = _client()
    v429 = _with_response(_Resp(429, ""), lambda: c2.probe())["verdict"]
    c3 = _client()
    v503 = _with_response(_Resp(503, ""), lambda: c3.probe())["verdict"]
    c4 = _client()
    vnet = _with_response(OSError("x"), lambda: c4.probe())["verdict"]
    c5 = _client()
    _patch_post(c5, [{"instruments": []}, {"instruments": [{"ticker": "SBER"}]},
                     {"instruments": [{"ticker": "SBER"}]}])
    vflag = _run(c5.probe())["verdict"]
    verdicts = [v401, v429, v503, vnet, vflag]
    assert len(set(verdicts)) == len(verdicts), verdicts


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
        except Exception as e:                                    # noqa: BLE001
            failed += 1
            print(f"  ОШИБКА {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed} из {len(tests)} пройдено")
    sys.exit(1 if failed else 0)
