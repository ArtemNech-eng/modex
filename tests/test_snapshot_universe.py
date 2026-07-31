"""
Стакан снимается по тем бумагам, которые торгуют СЕГОДНЯ.

Ошибка, ради которой написан файл. 31.07 лидером роста стал MVID: +8.29% при
обороте 390 млн ₽ — восьмой оборот на бирже. Лидером падения — SGZH: −4.28%
при 635 млн ₽. Ни по одной из этих бумаг не собралось ни единого снимка
стакана, потому что сборщик брал список из константы MOEX_TICKERS на 48
тикеров, а ядро опроса было отдельным зашитым перечнем голубых фишек.

Вечером владелец спросил, какую позицию открыть по главной аномалии дня. По
MVID у меня были только свечи: «стакана нет — MVID не в списке сбора снимков».

Вторая половина той же поломки: эндпоинты проверяли тикер по тому же
справочнику и отвечали 404 «Тикер не найден». То есть даже при собранных
данных прочитать их по MVID было нельзя.

Справочник MOEX_TICKERS — это ПОДПИСИ КОМПАНИЙ, а не перечень того, что
существует на бирже. Ни состав опроса, ни проверка тикера не должны из него
выводиться.
"""
import pytest

from src.analysis.universe import SNAPSHOT_CORE_N, is_known, split_core_tail

# Справочник подписей в проде — это dict {тикер: название}. Здесь достаточно
# нескольких бумаг: проверяется поведение, а не полнота словаря.
DICTIONARY = {"SBER": "Сбербанк", "GAZP": "Газпром", "VTBR": "ВТБ"}

BLUE_CHIPS = ["SBER", "GAZP", "LKOH", "GMKN", "NVTK"]


def _rows(pairs):
    """pairs: [(тикер, оборот), ...] -> строки в форме build_universe."""
    return [{"ticker": t, "sectype": "1", "name": t, "turnover": v,
             "price": 100.0, "change_pct": 0.0, "lot": 1} for t, v in pairs]


def test_todays_leader_lands_in_core_without_config_edit():
    """
    Главный случай. MVID с оборотом 390 млн обязан попасть в ЯДРО — то есть
    под опрос каждый цикл — сам, без правки конфигурации.
    """
    rows = _rows([("SBER", 4.0e9), ("GAZP", 2.0e9), ("MVID", 3.9e8),
                  ("TINYA", 1.1e7), ("TINYB", 1.0e7)])
    core, tail = split_core_tail(rows, core_n=3, movers_n=0, pinned=[])
    assert "MVID" in core, "лидер дня по обороту не может оказаться вне ядра"
    assert "TINYA" in tail


def test_sgzh_case_falling_leader_also_covered():
    """Лидер ПАДЕНИЯ — такая же аномалия. Отбор по обороту, не по знаку."""
    rows = _rows([("SBER", 4.0e9), ("SGZH", 6.35e8), ("MVID", 3.9e8)])
    core, _ = split_core_tail(rows, core_n=3, movers_n=0, pinned=[])
    assert {"SGZH", "MVID"} <= set(core)


def test_core_is_ordered_by_turnover():
    """Ядро идёт по обороту: сверху то, что реально торгуется."""
    rows = _rows([("C", 1e8), ("A", 9e9), ("B", 5e8)])
    core, _ = split_core_tail(rows, core_n=3, movers_n=0, pinned=[])
    assert core == ["A", "B", "C"]


def test_pinned_stays_in_core_on_a_quiet_day():
    """
    Закреплённые владельцем бумаги остаются в ядре даже при тихом обороте:
    это его выбор, и молча выкидывать его нельзя.
    """
    rows = _rows([("AAA", 9e9), ("BBB", 8e9), ("VTBR", 1.2e7)])
    core, tail = split_core_tail(rows, core_n=2, movers_n=0, pinned=["VTBR"])
    assert "VTBR" in core and "VTBR" not in tail


def test_pinned_absent_from_live_list_is_still_polled():
    """Закреплённой бумаги сегодня нет в списке — всё равно опрашиваем."""
    rows = _rows([("AAA", 9e9)])
    core, _ = split_core_tail(rows, core_n=1, movers_n=0, pinned=["POSI"])
    assert "POSI" in core


def test_pinned_is_normalised():
    """Регистр и пробелы из переменной окружения не должны ломать отбор."""
    rows = _rows([("AAA", 9e9), ("VTBR", 1e7)])
    core, tail = split_core_tail(rows, core_n=1, movers_n=0,
                                 pinned=[" vtbr ", "", None])
    assert "VTBR" in core and "VTBR" not in tail


def test_core_and_tail_do_not_overlap_or_duplicate():
    """Бумага опрашивается либо каждый цикл, либо по кругу — но не дважды."""
    rows = _rows([(f"T{k}", 1e9 - k) for k in range(40)])
    core, tail = split_core_tail(rows, core_n=10, movers_n=0,
                                 pinned=["T35", "T36"])
    assert not (set(core) & set(tail))
    assert len(core) == len(set(core)) and len(tail) == len(set(tail))
    assert set(core) | set(tail) == {f"T{k}" for k in range(40)}


def test_everything_is_covered_when_core_n_exceeds_list():
    """Ядро шире списка — хвост пуст, ничего не теряется."""
    rows = _rows([("AAA", 2e9), ("BBB", 1e9)])
    core, tail = split_core_tail(rows, core_n=50, movers_n=0, pinned=[])
    assert set(core) == {"AAA", "BBB"} and tail == []


def test_falls_back_when_exchange_is_down():
    """
    Биржа недоступна, строк нет — работаем по запасному списку. Пустое ядро
    остановило бы сбор стакана целиком.
    """
    core, tail = split_core_tail([], core_n=2, movers_n=0, pinned=["SBER"],
                                 fallback_tickers=BLUE_CHIPS)
    assert core[:2] == ["SBER", "GAZP"] or "SBER" in core
    assert set(core) | set(tail) == set(BLUE_CHIPS)
    assert core, "без ядра сбор встанет"


def test_mover_outside_turnover_top_still_lands_in_core():
    """
    Одного оборота не хватает. Проверка на живых данных 31.07: ETLN +5.69% при
    обороте 106 млн, MVID +4.31% при 469 млн, DATA +3.33% при 87 млн — все вне
    топа по обороту, и все двигались сильнее большинства ядра.
    """
    rows = _rows([("BIG1", 9e9), ("BIG2", 8e9), ("BIG3", 7e9), ("MVID", 4.69e8)])
    rows[0]["change_pct"] = 0.1
    rows[1]["change_pct"] = -0.2
    rows[2]["change_pct"] = 0.3
    rows[3]["change_pct"] = 4.31
    core, tail = split_core_tail(rows, core_n=2, movers_n=1, pinned=[])
    assert "MVID" in core, "аномалия дня обязана идти под стакан каждый цикл"
    assert "MVID" not in tail


def test_a_fall_counts_as_a_move_too():
    """Отбор по МОДУЛЮ изменения: обвал — такая же аномалия, как взлёт."""
    rows = _rows([("BIG", 9e9), ("SGZH", 7.58e8)])
    rows[0]["change_pct"] = 0.1
    rows[1]["change_pct"] = -4.50
    core, _ = split_core_tail(rows, core_n=1, movers_n=1, pinned=[])
    assert "SGZH" in core


def test_missing_change_does_not_crash_the_split():
    """У бумаги без сделок изменения нет — она просто не участвует в отборе."""
    rows = _rows([("AAA", 9e9), ("NOTRADE", 2e7)])
    rows[0]["change_pct"] = 1.0
    rows[1]["change_pct"] = None
    core, tail = split_core_tail(rows, core_n=1, movers_n=3, pinned=[])
    assert core == ["AAA"] and tail == ["NOTRADE"]


def test_movers_can_be_switched_off():
    """Отбор по движению отключается нулём — на случай лимитов Tinkoff."""
    rows = _rows([("BIG", 9e9), ("MOVER", 2e7)])
    rows[0]["change_pct"] = 0.1
    rows[1]["change_pct"] = 9.9
    core, tail = split_core_tail(rows, core_n=1, movers_n=0, pinned=[])
    assert core == ["BIG"] and tail == ["MOVER"]


def test_default_core_size_is_sane():
    """Ядро по умолчанию сопоставимо с прежним перечнем из 26 фишек."""
    from src.analysis.universe import SNAPSHOT_MOVERS_N
    assert 10 <= SNAPSHOT_CORE_N <= 60
    assert 3 <= SNAPSHOT_MOVERS_N <= 20, "отбор по движению не должен раздувать ядро"


# ─── вторая половина поломки: проверка тикера в эндпоинтах ────────────────────

def test_ticker_outside_the_dictionary_is_not_a_404(monkeypatch):
    """
    MVID нет в справочнике из 48 подписей, но он торгуется на восьмом обороте
    биржи. Эндпоинт обязан его принять.
    """
    import src.analysis.universe as u

    monkeypatch.setattr(u, "cached_universe",
                        lambda *a, **kw: {"tickers": ["MVID", "SGZH"], "rows": [],
                                          "source": "iss"})
    assert is_known("MVID", DICTIONARY) is True
    assert is_known("sgzh", DICTIONARY) is True, "регистр не должен влиять"
    assert is_known(" mvid ", DICTIONARY) is True, "пробелы из URL не должны влиять"


def test_dictionary_ticker_still_works_without_touching_the_exchange(monkeypatch):
    """Бумага из справочника принимается, даже если биржа недоступна."""
    import src.analysis.universe as u

    def boom(*a, **kw):
        raise OSError("сеть недоступна")

    monkeypatch.setattr(u, "cached_universe", boom)
    assert is_known("SBER", DICTIONARY) is True


def test_unknown_ticker_is_still_rejected(monkeypatch):
    """Проверка ослаблена, но не снята: мусор по-прежнему отвергается."""
    import src.analysis.universe as u

    monkeypatch.setattr(u, "cached_universe",
                        lambda *a, **kw: {"tickers": ["MVID"], "rows": [],
                                          "source": "iss"})
    assert is_known("НЕТТАКОЙ", DICTIONARY) is False
    assert is_known("", DICTIONARY) is False
    assert is_known(None, DICTIONARY) is False


def test_no_endpoint_gates_on_the_static_dictionary_anymore():
    """
    Регрессия. Ни один эндпоинт не должен снова начать отказывать по
    справочнику подписей — именно это давало 404 по лидеру дня.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/api/main.py").read_text()
    assert "if ticker not in MOEX_TICKERS:" not in src
    assert "def ticker_known(" in src
    assert src.count("if not ticker_known(ticker):") >= 7, \
        "все семь проверок должны идти через общую функцию"


def test_collector_takes_its_list_from_turnover():
    """
    Регрессия по сборщику: список берётся из оборота и пересобирается,
    а не читается один раз из константы при старте контейнера.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert "cached_universe" in src and "split_core_tail" in src
    assert "all_tickers = list(MOEX_TICKERS.keys())" not in src, \
        "состав снова читается из константы"
    assert "plan_day" in src, "состав обязан пересобираться раз в сутки"


def test_incident_recorded_in_module():
    """Обстоятельства рядом с кодом, иначе список снова зашьют руками."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/analysis/universe.py").read_text()
    assert "MVID" in src and "8.29" in src
    assert "SNAPSHOT_CORE" in src, "почему ядро больше не перечень — должно быть записано"


def test_cache_key_includes_the_threshold():
    """
    Кэш обязан различать РАЗНЫЕ вопросы. Сначала имя файла было просто датой:
    сборщик просил порог 10 млн, маршрут /api/universe — 100 млн, а получал
    список того, кто успел записаться первым. Я полдня искал причину в деплое.
    """
    import src.analysis.universe as u

    calls = []

    def fake(pairs_min, *a, **kw):
        calls.append(pairs_min)
        return [{"ticker": "AAA", "sectype": "1", "name": "A", "turnover": 2e7,
                 "price": 10, "change_pct": 1, "lot": 1}]

    u.CACHE_DIR = "/tmp/universe-test-key"
    import shutil
    shutil.rmtree(u.CACHE_DIR, ignore_errors=True)
    orig = u._fetch
    try:
        u._fetch = lambda *a, **kw: fake(None)
        u.cached_universe(min_turnover=1e7, max_n=80)
        u.cached_universe(min_turnover=1e8, max_n=80)
        assert len(calls) == 2, "разные пороги обязаны быть разными записями кэша"
        u.cached_universe(min_turnover=1e7, max_n=80)
        assert len(calls) == 2, "тот же порог — берём из кэша, биржу не трогаем"
    finally:
        u._fetch = orig
        shutil.rmtree(u.CACHE_DIR, ignore_errors=True)


def test_universe_endpoint_threshold_is_derived_not_hardcoded():
    """
    Регрессия. У маршрута стоял порог 100 млн по умолчанию — он и отдавал
    25 бумаг вместо 110, пока я искал причину в незашедшем деплое.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src/api/main.py").read_text()
    assert "min_turnover_mln: float = 100" not in src
    assert "MIN_TURNOVER_RUB if min_turnover_mln is None" in src


def test_exchange_calls_do_not_block_the_event_loop():
    """
    Обращение к бирже синхронное, до 30 секунд. В цикле событий его держать
    нельзя: на старте оно задержит подъём API и healthcheck, а при суточной
    пересборке — заморозит сбор стакана в торговые часы.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    collector = (root / "main.py").read_text()
    api = (root / "src/api/main.py").read_text()
    assert "await asyncio.to_thread(_plan)" in collector
    assert collector.count("await asyncio.to_thread(_plan)") == 2, \
        "и на старте, и при суточной пересборке"
    assert "await asyncio.to_thread(cached_universe" in api
