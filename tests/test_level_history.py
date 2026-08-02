"""
Секундная история уровня: что с ним происходило последние 10-60 секунд.

Артём попросил не «BID сейчас 882 тыс.», а последовательность:

    16:46:00 BID 500k
    16:46:03 BID 800k
    16:46:07 BID 600k
    16:46:10 исполнено 300k
    16:46:14 BID 900k

Третий случай одной болезни: секунды уже хранились в двух местах и ни одно не
давало этой картины. LevelTracker знал уровень, но время у него минута и хранил
он итоги. TickRing хранил секундный ряд, но только агрегатов стакана.

Что защищают эти тесты:

    исполнено ≠ снято   ГЛАВНОЕ. Уменьшение на 300 лотов — это либо съели, либо
                        владелец убрал заявку. Смысл ПРОТИВОПОЛОЖНЫЙ: съели —
                        покупатель забрал предложение; убрал — продавец
                        передумал. Различается по объёму сделок на этой цене

    только изменения    стоящий уровень дал бы шестьдесят одинаковых строк в
                        минуту, и нужные пять в них утонули бы

    перенос сделок      сделки и стакан идут ДВУМЯ потоками; сделка может прийти
                        позже пакета, который её уже учёл

    до нуля             крупную плиту, которую съедают, надо довести до конца:
                        по пути она выпадает из верхних уровней, и самые
                        интересные события потерялись бы ровно в финале

    рубли               у UGLD лот 1000, у SBER 1; «500 лотов» не значит ничего

    раздельно           добавлено и снято складываются ОТДЕЛЬНО: разность
                        уравнивает уровень с оборотом в миллион и мёртвый
"""
import pathlib

import pytest

from src.analysis import level_history as lh
from src.analysis.level_tracker import LevelTracker

ROOT = pathlib.Path(__file__).resolve().parents[1]
T0 = 1785000000
MIN = "2026-08-03T16:46"


def tracker(**kw):
    return LevelTracker(**kw)


def book(tr, sec, bids, asks=None, tk="SBER", src="exchange", minute=MIN):
    tr.on_book(tk, minute, bids, asks if asks is not None else [(101.0, 10)],
               source=src, sec=sec)


def key(price, side="bid", tk="SBER", src="exchange"):
    return (f"{tk}|{src}", side, price)


# ─── исполнено или снято ──────────────────────────────────────────────────────

def test_shrink_with_trades_is_execution():
    """
    ГЛАВНЫЙ тест. Было 800, стало 500, на цене прошло 300 сделок — съели.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 800)])
    tr.on_trade("SBER", 100.0, 300)
    book(tr, T0 + 1, [(100.0, 500)])
    tl = tr.history.timeline(key(100.0), T0 + 1)
    last = tl[-1]
    assert last["kind"] == lh.EXECUTED
    assert last["traded_lots"] == 300
    assert "pulled_lots" not in last


def test_shrink_without_trades_is_pulled():
    """
    Было 800, стало 500, сделок НЕ было — заявку убрали. Противоположный смысл:
    не покупатель забрал, а продавец передумал.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 800)])
    book(tr, T0 + 1, [(100.0, 500)])
    last = tr.history.timeline(key(100.0), T0 + 1)[-1]
    assert last["kind"] == lh.PULLED
    assert last["pulled_lots"] == 300
    assert "traded_lots" not in last


def test_partly_executed_partly_pulled():
    """
    Было 800, стало 500, сделок 100 — сто исполнено, двести снято. Оба числа
    отдаются: назвать это одним словом значило бы потерять половину.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 800)])
    tr.on_trade("SBER", 100.0, 100)
    book(tr, T0 + 1, [(100.0, 500)])
    last = tr.history.timeline(key(100.0), T0 + 1)[-1]
    assert last["kind"] == lh.EATEN
    assert last["traded_lots"] == 100
    assert last["pulled_lots"] == 200


def test_trades_without_shrink_is_refill():
    """
    Сделки прошли, а размер не упал — значит на ту же цену долили. Это не
    «ничего не произошло»: заявку держат под давлением.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 800)])
    tr.on_trade("SBER", 100.0, 300)
    book(tr, T0 + 1, [(100.0, 800)])
    last = tr.history.timeline(key(100.0), T0 + 1)[-1]
    assert last["kind"] == lh.REFILLED
    assert last["traded_lots"] == 300


def test_trade_is_attributed_to_the_next_size_change():
    """
    Сделки и стакан — ДВА независимых потока, поэтому сделка приписывается
    ближайшему следующему изменению размера, даже если пришла между пакетами.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 800)])
    tr.on_trade("SBER", 100.0, 300)        # между пакетами
    book(tr, T0 + 1, [(100.0, 500)])
    last = tr.history.timeline(key(100.0), T0 + 1)[-1]
    assert last["kind"] == lh.EXECUTED and last["traded_lots"] == 300


def test_refill_consumes_the_pending_trade_and_that_is_documented():
    """
    Случай, который данные различить НЕ МОГУТ, и его надо знать.

    Было 800, сделка 300, следующий пакет 800, через секунду 500. Оба чтения
    сходятся по арифметике:

        исполнено 300, долили 300, потом сняли 300      (так считает код)
        исполнено 300, а пакет просто опоздал           (тоже сходится)

    Код выбирает первое, потому что за секунду проходит около десяти пакетов: не
    отразилось — значит долили. Тест закрепляет ВЫБОР и требует, чтобы оговорка
    стояла рядом с кодом, а не жила в чьей-то голове.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 800)])
    tr.on_trade("SBER", 100.0, 300)
    book(tr, T0 + 1, [(100.0, 800)])
    book(tr, T0 + 2, [(100.0, 500)])
    kinds = [e["kind"] for e in tr.history.timeline(key(100.0), T0 + 2)]
    assert kinds == [lh.APPEARED, lh.REFILLED, lh.PULLED]
    src = (ROOT / "src/analysis/level_history.py").read_text()
    assert "различить НЕ МОГУТ" in src, "неразрешимый случай описан рядом с кодом"


def test_pending_is_capped_by_peak():
    """
    Неприписанные сделки не должны копиться весь день: иначе однажды чистое
    снятие объявили бы исполнением на основании сделок часовой давности.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 100)])
    for i in range(50):
        tr.on_trade("SBER", 100.0, 100)
    lv = tr.levels[key(100.0)]
    assert lv["pending_traded"] <= lv["peak"]


# ─── только изменения, а не каждая секунда ────────────────────────────────────

def test_unchanged_level_writes_nothing():
    """
    Стоящий уровень за минуту дал бы шестьдесят одинаковых строк, и те пять,
    которые нужны, в них утонули бы.
    """
    tr = tracker()
    for i in range(30):
        book(tr, T0 + i, [(100.0, 800)])
    tl = tr.history.timeline(key(100.0), T0 + 30)
    assert len(tl) == 1, "только появление"
    assert tl[0]["kind"] == lh.APPEARED


def test_tiny_wobble_is_not_an_event():
    """Дрожание объёма на процент — не событие, а шум."""
    tr = tracker()
    book(tr, T0, [(100.0, 1000)])
    book(tr, T0 + 1, [(100.0, 995)])
    book(tr, T0 + 2, [(100.0, 1000)])
    assert len(tr.history.timeline(key(100.0), T0 + 2)) == 1


def test_disappearance_is_always_recorded():
    """Исчезновение записывается независимо от размера: это не шум."""
    tr = tracker()
    book(tr, T0, [(100.0, 1000)])
    book(tr, T0 + 1, [])
    kinds = [e["kind"] for e in tr.history.timeline(key(100.0), T0 + 1)]
    assert lh.GONE in kinds


# ─── доведение плиты до нуля ──────────────────────────────────────────────────

def test_big_level_is_followed_after_it_leaves_the_top():
    """
    Крупную плиту, которую съедают, надо довести до конца. По пути она выпадает
    из верхних уровней по размеру — и самые интересные события, финал, потерялись
    бы ровно тогда, когда они важнее всего.
    """
    tr = tracker(history=lh.LevelLog(top_levels=1))
    book(tr, T0, [(100.0, 5000), (99.9, 100)])          # плита в верхних
    tr.on_trade("SBER", 100.0, 4900)
    book(tr, T0 + 1, [(100.0, 100), (99.9, 4000)])      # уже не верхняя
    tl = tr.history.timeline(key(100.0), T0 + 1)
    assert any(e["kind"] == lh.EXECUTED for e in tl), \
        "события уровня продолжают писаться, раз журнал у него уже есть"


def test_only_top_levels_start_a_log():
    """
    Журнал не заводится на все двадцать уровней: иначе на восьмидесяти бумагах
    это тысячи журналов, и память уходит на цены, которые никто не спросит.
    """
    tr = tracker(history=lh.LevelLog(top_levels=1))
    book(tr, T0, [(100.0, 5000), (99.9, 10)])
    assert key(100.0) in tr.history.log
    assert key(99.9) not in tr.history.log


# ─── восстановление ───────────────────────────────────────────────────────────

def test_restore_after_disappearance():
    tr = tracker()
    book(tr, T0, [(100.0, 1000)])
    book(tr, T0 + 1, [])                  # ушёл
    book(tr, T0 + 2, [(100.0, 1000)])     # вернулся
    kinds = [e["kind"] for e in tr.history.timeline(key(100.0), T0 + 2)]
    assert kinds[-1] == lh.RESTORED


def test_restore_resets_pending_trades():
    """
    Вернувшаяся заявка — другая заявка. Приписывать ей сделки по прошлой значило
    бы считать исполненным то, что исполнено не у неё.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 1000)])
    tr.on_trade("SBER", 100.0, 500)
    book(tr, T0 + 1, [])
    book(tr, T0 + 2, [(100.0, 1000)])
    assert tr.levels[key(100.0)]["pending_traded"] == 0


# ─── рубли и лотность ─────────────────────────────────────────────────────────

def test_output_is_in_rubles_with_lot_size():
    """
    У UGLD лот 1000, у SBER 1. «500 лотов» само по себе не значит ничего, и
    сравнивать бумаги в лотах бессмысленно.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 500)])
    tl = tr.history.timeline(key(100.0), T0, lot=10)
    assert tl[0]["size_rub"] == round(100.0 * 500 * 10)
    assert tl[0]["size_lots"] == 500


# ─── окно и итог ──────────────────────────────────────────────────────────────

def test_window_cuts_off_old_events():
    tr = tracker()
    book(tr, T0, [(100.0, 500)])
    book(tr, T0 + 1, [(100.0, 900)])
    book(tr, T0 + 100, [(100.0, 200)])
    tl = tr.history.timeline(key(100.0), T0 + 100, window=10)
    assert len(tl) == 1, "старое за окном не попало"
    assert tl[0]["back"] == 0


def test_seconds_back_are_reported():
    """
    «Сколько секунд назад» читается глазами, а секунда эпохи — нет.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 500)])
    book(tr, T0 + 7, [(100.0, 900)])
    tl = tr.history.timeline(key(100.0), T0 + 10)
    assert [e["back"] for e in tl] == [10, 3]


def test_summary_keeps_added_and_pulled_separate():
    """
    Разность скрыла бы главное: уровень, где долили и сняли по миллиону, и
    уровень, где не было ничего, дают одинаковый ноль.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 1000)])
    book(tr, T0 + 1, [(100.0, 2000)])       # долили 1000
    book(tr, T0 + 2, [(100.0, 1000)])       # сняли 1000
    s = tr.history.summary(key(100.0), T0 + 2)
    assert s["added_rub"] > 0
    assert s["pulled_rub"] > 0
    assert s["events"] == 3


def test_summary_empty_without_history():
    tr = tracker()
    assert tr.history.summary(key(100.0), T0) == {}
    assert tr.history.timeline(key(100.0), T0) == []


def test_log_is_bounded():
    """Журнал ограничен: иначе за день он растёт без предела."""
    tr = tracker(history=lh.LevelLog(events=5))
    for i in range(40):
        book(tr, T0 + i, [(100.0, 1000 + (i % 2) * 500)])
    assert len(tr.history.log[key(100.0)]) <= 5


# ─── воспроизведение примера из запроса ───────────────────────────────────────

def test_the_exact_sequence_from_the_request():
    """
    Ровно тот случай, который просил Артём: 500k → 800k → 600k → исполнено 300k
    → 900k. Здесь в лотах, рубли считает выдача.
    """
    tr = tracker()
    book(tr, T0 + 0, [(100.0, 500)])
    book(tr, T0 + 3, [(100.0, 800)])
    tr.on_trade("SBER", 100.0, 200)
    book(tr, T0 + 7, [(100.0, 600)])
    tr.on_trade("SBER", 100.0, 300)
    book(tr, T0 + 10, [(100.0, 300)])
    book(tr, T0 + 14, [(100.0, 900)])
    tl = tr.history.timeline(key(100.0), T0 + 14)
    assert [e["kind"] for e in tl] == [
        lh.APPEARED, lh.GREW, lh.EXECUTED, lh.EXECUTED, lh.GREW]
    assert [e["size_lots"] for e in tl] == [500, 800, 600, 300, 900]
    assert [e["back"] for e in tl] == [14, 11, 7, 4, 0]
    assert tl[3]["traded_lots"] == 300


def test_notable_level_carries_its_timeline():
    """Карточка должна получать историю вместе с уровнем, а не отдельным вызовом."""
    tr = tracker()
    book(tr, T0, [(100.0, 5000)])
    tr.on_trade("SBER", 100.0, 2000)
    book(tr, T0 + 1, [(100.0, 3000)])
    got = tr.with_history("SBER", T0 + 1, lot=1, top=1)
    bid = [x for x in got if x["side"] == "bid"][0]
    assert bid["timeline"], "таймлайн приложен к уровню"
    assert bid["history"]["traded_rub"] > 0


# ─── минутный итог для базы ───────────────────────────────────────────────────

def test_minute_totals_include_activity_below_the_event_floor():
    """
    В секундный журнал попадает только заметное, а в МИНУТУ должно попасть всё:
    сумма мелких исполнений за минуту может быть больше одного крупного.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 10000)])
    for i in range(20):
        tr.on_trade("SBER", 100.0, 50)
        book(tr, T0 + 1 + i, [(100.0, 10000 - 50 * (i + 1))])
    rows = tr.history.drop_minute(MIN)
    got = dict(rows)[key(100.0)]
    assert got["traded"] == 1000, "все двадцать мелких исполнений в минуте"
    tl = tr.history.timeline(key(100.0), T0 + 21)
    assert len(tl) < 20, "а в секундный журнал попали не все"


def test_drop_minute_removes_what_it_returns():
    """
    Иначе накопленное за день осталось бы в памяти навсегда — та же ошибка, что
    с журналами уровней без очистки.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 500)])
    assert tr.history.drop_minute(MIN)
    assert tr.history.drop_minute(MIN) == []


def test_prune_forgets_the_log_too():
    """Уровень удалён, а его журнал остался — так память и течёт."""
    tr = tracker(keep_minutes=0)
    book(tr, T0, [(100.0, 500)], minute="2026-08-03T10:00")
    book(tr, T0 + 1, [], minute="2026-08-03T10:00")
    tr.prune("2026-08-03T11:00")
    assert key(100.0) not in tr.history.log


# ─── источники и стороны не смешиваются ───────────────────────────────────────

def test_sources_are_separate():
    """Дилерский стакан — котировки брокера, там нет чужих заявок для съедания."""
    tr = tracker()
    book(tr, T0, [(100.0, 500)], src="exchange")
    book(tr, T0, [(100.0, 900)], src="dealer")
    assert tr.history.timeline(key(100.0), T0)[0]["size_lots"] == 500
    assert tr.history.timeline(
        key(100.0, src="dealer"), T0)[0]["size_lots"] == 900


def test_bid_and_ask_at_the_same_price_are_separate():
    tr = tracker()
    book(tr, T0, [(100.0, 500)], asks=[(100.5, 700)])
    assert tr.history.timeline(key(100.0, "bid"), T0)
    assert tr.history.timeline(key(100.5, "ask"), T0)


# ─── описание, а не совет ─────────────────────────────────────────────────────

def test_no_signal_fields():
    tr = tracker()
    book(tr, T0, [(100.0, 5000)])
    tr.on_trade("SBER", 100.0, 4000)
    book(tr, T0 + 1, [(100.0, 1000)])
    blob = str(tr.with_history("SBER", T0 + 1)).lower()
    for bad in ("signal", "recommend", "entry", "target", "strong",
                "strength", "absorption_confirmed"):
        assert bad not in blob, bad


def test_the_two_stream_caveat_is_written_next_to_the_code():
    src = (ROOT / "src/analysis/level_history.py").read_text()
    assert "ДВУМЯ независимыми потоками" in src
    assert "ИСПОЛНЕНО ИЛИ СНЯТО" in src


def test_broken_input_does_not_crash():
    tr = tracker()
    tr.on_book("", MIN, [(100.0, 5)], [], sec=T0)
    tr.on_book("SBER", MIN, [(0, 5), (100.0, 0), (100.0, 5)], [], sec=T0)
    tr.on_trade("SBER", 0, 5)
    tr.history.add((), T0, lh.GREW)
    tr.history.accrue((), "", lh.GREW)
    assert tr.history.timeline(key(100.0), T0)


# ─── подключение к базе, карточке и экрану ─────────────────────────────────────

def test_completed_minutes_only_are_dropped_for_the_db():
    """
    Текущая минута ещё набирается. Записать её половину, а потом дописать
    остаток второй строкой — значит разойтись с реальностью.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 500)], minute="2026-08-03T16:45")
    book(tr, T0 + 60, [(100.0, 900)], minute="2026-08-03T16:46")
    got = tr.history.drop_completed("2026-08-03T16:46")
    # Уровней за минуту несколько (бид и аск), поэтому проверяется МНОЖЕСТВО
    # минут, а не длина списка.
    assert {m for _, m, _ in got} == {"2026-08-03T16:45"}
    assert got, "завершённая минута отдана"
    assert tr.history.drop_completed("2026-08-03T16:46") == []


def test_db_table_separates_traded_from_pulled():
    src = (ROOT / "src/db.py").read_text()
    i = src.index("class LevelMinute")
    body = src[i:i + 3000]
    assert "traded: Mapped[int]" in body and "pulled: Mapped[int]" in body
    assert "СТРОКА ПИШЕТСЯ НЕ ВСЕГДА" in body, "порог записи описан"
    assert "prune_level_minute" in src, "чистка есть, иначе таблица растёт вечно"


def test_floor_is_in_rubles_and_configurable():
    """
    Значимость в РУБЛЯХ: 1000 лотов у SBER и у UGLD — это 276 тысяч и 666 тысяч.
    И порог — догадка, поэтому он должен сниматься переменной окружения.
    """
    src = (ROOT / "src/db.py").read_text()
    assert 'os.getenv("LEVEL_FLOOR_RUB"' in src
    assert "ДОГАДКА" in src


def test_skipped_rows_are_counted_not_silently_dropped():
    """
    Без числа отброшенного нельзя понять, порог отсекает шум или половину
    полезного. Молчаливая потеря — это как раз то, что уже дважды выходило боком.
    """
    src = (ROOT / "src/db.py").read_text()
    i = src.index("async def merge_level_minutes")
    assert '"skipped"' in src[i:i + 3000]
    main = (ROOT / "main.py").read_text()
    assert "уровни/ниже порога" in main


def test_history_is_wired_into_the_light_response():
    """
    История должна идти в ЛЁГКИЙ ответ: он опрашивается раз в секунду, а это
    ровно та частота, на которой секундная история и имеет смысл.
    """
    api = (ROOT / "src/api/main.py").read_text()
    i = api.index("out[\"levels\"] = CURRENT.levels.with_history")
    j = api.index("if light:")
    assert i < j, "первый вызов with_history стоит ДО выхода по light"


def test_page_shows_the_sequence_and_the_caveat():
    page = (ROOT / "dashboard/book-live.html").read_text()
    assert "история уровня" in page
    for word in ("исполнено", "снято", "вернулся", "долили"):
        assert word in page, word
    assert "различить" in page, "неразрешимый случай назван на экране"


# ─── потолок памяти ────────────────────────────────────────────────────────────

def test_logs_per_ticker_are_hard_capped():
    """
    Найдено ЗАМЕРОМ, а не рассуждением: на 80 бумагах правило «раз заведён —
    ведём всегда» дало 6025 журналов из 6400 уровней, потому что верхняя пятёрка
    меняется от пакета к пакету. Нужен жёсткий предел.
    """
    tr = tracker(history=lh.LevelLog(top_levels=20, max_per_series=5))
    for i in range(40):
        price = 100.0 + i * 0.01
        book(tr, T0 + i, [(price, 5000)])
    assert len(tr.history.log) <= 5
    assert tr.history.stats()["evicted"] > 0


def test_eviction_takes_the_stalest_not_the_biggest():
    """
    Вытесняться должен тот, у кого давнее всех было событие. Плита, которую едят
    прямо сейчас, обязана остаться — её финал и есть самое интересное.
    """
    hist = lh.LevelLog(top_levels=20, max_per_series=2)
    tr = tracker(history=hist)
    book(tr, T0, [(100.0, 5000)])                       # заведён первым
    book(tr, T0 + 1, [(100.0, 5000), (99.0, 4000)])     # второй
    # Первый продолжает жить: у него свежее событие.
    tr.on_trade("SBER", 100.0, 2000)
    book(tr, T0 + 5, [(100.0, 3000), (99.0, 4000)])
    # Третий уровень вытеснит самый давний — это 99.0, а не активный 100.0.
    book(tr, T0 + 6, [(100.0, 3000), (99.0, 4000), (98.0, 9000)])
    assert key(100.0) in tr.history.log, "активный уровень остался"
    assert key(98.0) in tr.history.log


def test_cap_is_documented_with_the_measurement():
    src = (ROOT / "src/analysis/level_history.py").read_text()
    assert "6025" in src, "замер, из которого взялся потолок, записан рядом"
    assert "MAX_PER_SERIES" in src


def test_only_relatively_big_levels_get_a_log():
    """
    «Крупный» — свойство уровня, «пятый по счёту» — свойство очереди. Уровень на
    сотую долю от плиты журнала не заводит, даже если больше некого поставить.
    """
    tr = tracker(history=lh.LevelLog(top_levels=5))
    book(tr, T0, [(100.0, 10000), (99.99, 50), (99.98, 40), (99.97, 30)])
    assert key(100.0) in tr.history.log
    for p in (99.99, 99.98, 99.97):
        assert key(p) not in tr.history.log, f"{p} слишком мелкий рядом с плитой"


def test_comparable_levels_all_get_logs():
    """Когда уровни сопоставимы, журнал получают все — плиты тут нет."""
    tr = tracker(history=lh.LevelLog(top_levels=5))
    book(tr, T0, [(100.0, 1000), (99.99, 900), (99.98, 850)])
    for p in (100.0, 99.99, 99.98):
        assert key(p) in tr.history.log, p


def test_enter_share_is_documented_as_a_guess():
    src = (ROOT / "src/analysis/level_history.py").read_text()
    i = src.index("ENTER_SHARE = ")
    assert "ДОГАДКА" in src[max(0, i - 700):i], "порог помечен догадкой"


# ─── исчезновение это тоже снятие ──────────────────────────────────────────────

def test_disappearance_counts_as_pulled():
    """
    Найдено на ЖИВЫХ данных 02.08: у TATN ask 519.9 уровень пропадал трижды, а в
    итоге окна стояло «снято 0 ₽». Пропажа целиком — чистейший случай снятия, и
    не считать её значило врать заголовочным числом ровно в ту сторону, которая
    важна.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 1000)])
    book(tr, T0 + 1, [])
    s = tr.history.summary(key(100.0), T0 + 1)
    assert s["pulled_rub"] == round(100.0 * 1000), "весь пропавший объём снят"
    assert s["gone"] == 1


def test_disappearance_eaten_by_trades_is_not_pulled():
    """
    Если уровень исчез, а сделки его выбрали — это исполнение, а не снятие. То же
    различение, что при обычном уменьшении: иначе съеденная плита читалась бы как
    отменённая.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 1000)])
    tr.on_trade("SBER", 100.0, 1000)
    book(tr, T0 + 1, [])
    s = tr.history.summary(key(100.0), T0 + 1)
    assert s["traded_rub"] == round(100.0 * 1000)
    assert s["pulled_rub"] == 0


def test_disappearance_splits_partly():
    tr = tracker()
    book(tr, T0, [(100.0, 1000)])
    tr.on_trade("SBER", 100.0, 400)
    book(tr, T0 + 1, [])
    s = tr.history.summary(key(100.0), T0 + 1)
    assert s["traded_rub"] == round(100.0 * 400)
    assert s["pulled_rub"] == round(100.0 * 600)


def test_disappearance_reaches_the_minute_totals_too():
    """Иначе дыра осталась бы в базе, где её потом никто не найдёт."""
    tr = tracker()
    book(tr, T0, [(100.0, 1000)])
    book(tr, T0 + 1, [])
    got = dict((k, v) for k, _, v in tr.history.drop_completed("нет-такой-минуты"))
    assert got[key(100.0)]["pulled"] == 1000
