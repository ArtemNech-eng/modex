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


# ─── отсутствие таблицы не должно быть немым ──────────────────────────────────

def test_missing_table_is_reported_not_swallowed():
    """
    Найдено на проде 02.08: таблица level_minute не создалась, чтение отдавало
    голую пятисотку, а ЗАПИСЬ молча возвращала ноль — и ноль читался как «тихий
    рынок». Час данных потерян незаметно.

    Ошибка чтения обязана иметь свой тип, ошибка записи — попадать в лог
    предупреждением, а не в debug.
    """
    src = (ROOT / "src/db.py").read_text()
    assert "class LevelStoreError" in src
    assert "async def ensure_level_table" in src
    i = src.index("await init_db()")
    assert "ensure_level_table" in src[i:i + 800], \
        "явное создание таблицы стоит рядом со стартом"
    main = (ROOT / "main.py").read_text()
    j = main.index('counts["уровни"]')
    assert "logger.warning" in main[j:j + 500], \
        "ошибка записи уровней уходит предупреждением, а не в debug"


def test_route_returns_the_reason_not_a_bare_500():
    api = (ROOT / "src/api/main.py").read_text()
    i = api.index("async def get_levels")
    j = api.index("@app.get", i)          # до следующего маршрута
    body = api[i:j]
    assert "db.LevelStoreError" in body
    assert '"error"' in body and "create_attempt" in body


def test_refill_counts_as_added_in_the_summary():
    """
    Найдено НА ЭКРАНЕ 02.08: у GAZP стояли строки «долили 52 тыс» и «долили
    100 тыс», а в итоге — «долили 0 ₽».

    Причина: у события refilled размер НЕ меняется, abs(delta) равен нулю, и весь
    долитый объём лежит в поле сделок. Складывать только изменение размера значит
    не увидеть ровно тот случай, когда заявку держат под давлением.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 1000)])
    tr.on_trade("SBER", 100.0, 300)
    book(tr, T0 + 1, [(100.0, 1000)])          # съели 300 и столько же долили
    s = tr.history.summary(key(100.0), T0 + 1)
    assert s["traded_rub"] == round(100.0 * 300)
    assert s["added_rub"] >= round(100.0 * 300), \
        "долитое взамен съеденного входит в добавленное"


def test_growth_with_trades_counts_both_parts():
    """
    Уровень и съели, и он вырос: валовое добавление больше прироста на съеденное.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 1000)])
    tr.on_trade("SBER", 100.0, 200)
    book(tr, T0 + 1, [(100.0, 1500)])          # +500 сверх съеденных 200
    s = tr.history.summary(key(100.0), T0 + 1)
    # 1000 при появлении + 500 прироста + 200 возмещённых
    assert s["added_rub"] == round(100.0 * (1000 + 500 + 200))


def test_summary_added_is_never_zero_when_lines_say_added():
    """
    Итог не должен противоречить строкам над ним: это первое, что видит глаз.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 500)])
    tr.on_trade("SBER", 100.0, 100)
    book(tr, T0 + 1, [(100.0, 500)])
    rows = tr.history.timeline(key(100.0), T0 + 1)
    s = tr.history.summary(key(100.0), T0 + 1)
    shown = [r for r in rows if r["kind"] in (lh.GREW, lh.APPEARED,
                                              lh.RESTORED, lh.REFILLED)]
    assert shown and s["added_rub"] > 0


# ─── тесты уровня: цена дошла и что дальше ────────────────────────────────────

def test_price_reaching_the_level_counts_as_a_test():
    """
    ТЕСТ — это когда цена ДОШЛА до уровня, то есть он стал лучшим на своей
    стороне. Не «на уровне были сделки»: цена может подойти и отступить, не
    задев ни одной заявки, и это тоже тест, причём выдержанный.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    assert tr.levels[key(99.0)]["tests"] == 0, "до 99.0 цена не доходила"
    # лучший бид опустился ровно на 99.0 — уровень под тестом
    book(tr, T0 + 1, [(99.0, 500)], asks=[(101.0, 400)])
    lv = tr.levels[key(99.0)]
    assert lv["tests"] == 1 and lv["in_test"] is True


def test_price_stepping_away_means_the_test_held():
    tr = tracker()
    book(tr, T0, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    book(tr, T0 + 1, [(99.0, 500)], asks=[(101.0, 400)])          # тест
    book(tr, T0 + 2, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    lv = tr.levels[key(99.0)]
    assert lv["tests"] == 1 and lv["test_held"] == 1
    assert lv["test_failed"] == 0 and lv["in_test"] is False


def test_price_going_through_means_the_test_failed():
    tr = tracker()
    book(tr, T0, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    book(tr, T0 + 1, [(99.0, 500)], asks=[(101.0, 400)])          # тест
    book(tr, T0 + 2, [(98.5, 300)], asks=[(101.0, 400)])          # прошла ниже
    lv = tr.levels[key(99.0)]
    assert lv["test_failed"] == 1 and lv["test_held"] == 0
    assert lv["broken"] is True


def test_repeated_touches_are_separate_tests():
    """Три подхода — три теста, а не один длинный."""
    tr = tracker()
    far = [(100.0, 1000), (99.0, 500)]
    near = [(99.0, 500)]
    book(tr, T0, far, asks=[(101.0, 400)])
    for i in range(3):
        book(tr, T0 + 1 + i * 2, near, asks=[(101.0, 400)])
        book(tr, T0 + 2 + i * 2, far, asks=[(101.0, 400)])
    lv = tr.levels[key(99.0)]
    assert lv["tests"] == 3 and lv["test_held"] == 3


def test_staying_at_the_touch_is_not_counted_again():
    """Пока цена стоит на уровне, тест ОДИН, а не по одному на пакет."""
    tr = tracker()
    book(tr, T0, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    for i in range(10):
        book(tr, T0 + 1 + i, [(99.0, 500)], asks=[(101.0, 400)])
    assert tr.levels[key(99.0)]["tests"] == 1


def test_ask_side_is_tested_from_above():
    tr = tracker()
    book(tr, T0, [(100.0, 500)], asks=[(101.0, 400), (102.0, 700)])
    book(tr, T0 + 1, [(100.0, 500)], asks=[(102.0, 700)])        # аск дошёл
    lv = tr.levels[102.0, "ask"] if False else tr.levels[key(102.0, "ask")]
    assert lv["tests"] == 1
    book(tr, T0 + 2, [(100.0, 500)], asks=[(102.5, 700)])        # ушёл выше
    assert tr.levels[key(102.0, "ask")]["test_failed"] == 1


def test_best_price_is_rounded_so_equality_works():
    """
    Цены уровней лежат в ключах как round(..., 6). Без округления лучшей цены
    сравнение «дошла до уровня» не сходилось бы НИКОГДА, и тест не засчитался бы
    ни один раз — самая тихая из возможных поломок.
    """
    tr = tracker()
    p = 0.1 + 0.2                      # 0.30000000000000004, а в ключе 0.3
    book(tr, T0, [(0.4, 100), (p, 500)], asks=[(1.0, 100)])
    assert tr.levels[key(0.3)]["at_touch"] is False, "цена пока выше"
    book(tr, T0 + 1, [(p, 500)], asks=[(1.0, 100)])      # цена пришла
    assert tr.levels[key(0.3)]["tests"] == 1


# ─── сколько прожил ───────────────────────────────────────────────────────────

def test_lifetime_is_measured_in_seconds():
    """
    В минутах разница между сорока секундами и двумя минутами теряется целиком,
    а для заявки это разные жизни.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 500)])
    for i in range(1, 41):
        book(tr, T0 + i, [(100.0, 500 + i)])
    lv = tr.levels[key(100.0)]
    assert lv["alive_sec"] == 40
    assert tr.life(lv, now_sec=T0 + 45)["age_sec"] == 45


def test_time_while_absent_does_not_count_as_lived():
    """
    Уровень появился, полчаса отсутствовал и вернулся — прожил не полчаса.
    «Когда впервые увидели» и «сколько простоял» это разные вопросы.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 500)])
    book(tr, T0 + 5, [(100.0, 500)])
    book(tr, T0 + 6, [])                      # ушёл
    book(tr, T0 + 40, [(100.0, 500)])         # вернулся через 34 секунды
    lv = tr.levels[key(100.0)]
    assert lv["alive_sec"] <= 6, "простой в зачёт не идёт"
    assert tr.life(lv, now_sec=T0 + 40)["age_sec"] == 40


# ─── состояние словом ─────────────────────────────────────────────────────────

def test_state_defended_after_a_held_test():
    tr = tracker()
    book(tr, T0, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    book(tr, T0 + 1, [(99.0, 500)], asks=[(101.0, 400)])
    book(tr, T0 + 2, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    assert tr.state(tr.levels[key(99.0)]) == "defended"


def test_state_broken_wins_over_everything():
    tr = tracker()
    book(tr, T0, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    book(tr, T0 + 1, [(99.0, 500)], asks=[(101.0, 400)])
    book(tr, T0 + 2, [(98.0, 300)], asks=[(101.0, 400)])
    assert tr.state(tr.levels[key(99.0)]) == "broken"


def test_state_untested_when_price_never_came():
    tr = tracker()
    book(tr, T0, [(100.0, 1000), (95.0, 500)], asks=[(101.0, 400)])
    assert tr.state(tr.levels[key(95.0)]) == "untested"


def test_state_eaten_versus_pulled():
    """Съели и убрали — противоположный смысл, и слово должно их различать."""
    eaten = tracker()
    book(eaten, T0, [(90.0, 1000)], asks=[(101.0, 400)])
    eaten.on_trade("SBER", 90.0, 900)
    book(eaten, T0 + 1, [(90.0, 50)], asks=[(101.0, 400)])
    assert eaten.state(eaten.levels[key(90.0)]) == "eaten"

    pulled = tracker()
    book(pulled, T0, [(90.0, 1000)], asks=[(101.0, 400)])
    book(pulled, T0 + 1, [(90.0, 50)], asks=[(101.0, 400)])
    assert pulled.state(pulled.levels[key(90.0)]) == "pulled"


def test_no_strong_or_weak_verdicts():
    """
    Артём просил STRONG / WEAK / DEFENDED / FAILED «без выдуманного Score».
    Насчёт Score он прав. Но «сильный» означает «в следующий раз выдержит» — это
    утверждение о БУДУЩЕМ, которого измерения не подтверждали. 31.07 ровно такая
    метка измерялась ВРЕДНОЙ: t=-12.57, положительных дней 16%.

    Здесь только прошедшее время: каждое слово — пересказ счётчиков.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    book(tr, T0 + 1, [(99.0, 500)], asks=[(101.0, 400)])
    blob = str(tr.with_history("SBER", T0 + 1)).lower()
    for bad in ("strong", "weak", "score", "сильн", "слаб", "signal",
                "recommend", "reliable"):
        assert bad not in blob, bad
    src = (ROOT / "src/analysis/level_tracker.py").read_text()
    assert "-12.57" in src, "измеренный вред таких меток записан рядом с кодом"


def test_life_returns_the_nine_facts_asked_for():
    tr = tracker()
    book(tr, T0, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    book(tr, T0 + 1, [(99.0, 500)], asks=[(101.0, 400)])
    tr.on_trade("SBER", 99.0, 100)
    book(tr, T0 + 2, [(100.0, 1000), (99.0, 400)], asks=[(101.0, 400)])
    lf = tr.life(tr.levels[key(99.0)], now_sec=T0 + 2, lot=1)
    for f in ("state", "first_seen", "gone_count", "restored_count",
              "traded_rub", "tests", "test_held", "test_failed", "broken",
              "alive_sec", "age_sec", "traded_share_of_peak"):
        assert f in lf, f


def test_life_is_attached_to_the_notable_level():
    tr = tracker()
    book(tr, T0, [(100.0, 5000)], asks=[(101.0, 400)])
    got = tr.with_history("SBER", T0, lot=1, top=1)
    assert any("life" in x for x in got)


# ─── скорость: указатель по тикеру ────────────────────────────────────────────

def test_packet_handling_does_not_scan_all_tickers():
    """
    Замер, а не рассуждение. Обход исчезнувших и пробитых шёл по ВСЕМ уровням
    всех бумаг: 0.92 мс на пакет, 1085 пакетов в секунду при потребности 800.
    Запас 1.36x — и это ДО добавления счёта тестов.

    Тест держит указатель на месте: с двумя бумагами и с восемьюдесятью время
    обработки одного пакета должно отличаться незначительно.
    """
    import time

    def build(n):
        tr = tracker()
        for t in range(n):
            base = 100.0 + t
            bids = [(round(base - j * 0.01, 2), 500) for j in range(20)]
            asks = [(round(base + 0.1 + j * 0.01, 2), 500) for j in range(20)]
            tr.on_book(f"TK{t}", MIN, bids, asks, sec=T0)
        return tr

    def measure(tr, n=200):
        bids = [(round(100.0 - j * 0.01, 2), 500) for j in range(20)]
        asks = [(round(100.1 + j * 0.01, 2), 500) for j in range(20)]
        t0 = time.perf_counter()
        for i in range(n):
            tr.on_book("TK0", MIN, bids, asks, sec=T0 + i)
        return (time.perf_counter() - t0) / n

    small = measure(build(2))
    big = measure(build(80))
    assert big < small * 6, (
        f"обработка растёт с числом бумаг: {small*1000:.3f} мс против "
        f"{big*1000:.3f} мс — похоже, обход снова идёт по всем уровням")


def test_index_is_cleaned_by_prune():
    """Указатель без очистки растёт весь день — та же течь, что с журналами."""
    tr = tracker(keep_minutes=0)
    book(tr, T0, [(100.0, 500)], minute="2026-08-03T10:00")
    book(tr, T0 + 1, [], minute="2026-08-03T10:00")
    tr.prune("2026-08-03T11:00")
    assert not tr.index.get(("SBER|exchange", "bid"))


def test_page_shows_state_and_life_without_verdicts():
    page = (ROOT / "dashboard/book-live.html").read_text()
    for word in ("состояние", "выдержал", "не выдержал", "пробит",
                 "выкуплен", "снят", "живёт", "стоял"):
        assert word in page, word
    assert "-12.57" in page or "−12.57" in page, "оговорка про метки на экране"
    # Никаких вердиктов о будущем в разметке состояний.
    i = page.index("const STATE")
    assert not any(b in page[i:i + 400].lower()
                   for b in ("сильн", "слаб", "strong", "weak"))


def test_unfinished_test_is_not_called_defended():
    """
    Найдено на ЖИВЫХ данных 02.08: у LKOH bid 4587 стояло «выдержал» при счёте
    тестов 1 и нулях в обеих колонках исхода. То есть цена стояла у уровня прямо
    в тот момент, и ничего он ещё не выдержал.

    Незавершённый тест, выданный за пройденный, — то же переобещание, что и
    «сильный», только незаметнее.
    """
    tr = tracker()
    book(tr, T0, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    book(tr, T0 + 1, [(99.0, 500)], asks=[(101.0, 400)])      # цена пришла
    lv = tr.levels[key(99.0)]
    assert lv["tests"] == 1 and lv["test_held"] == 0 and lv["test_failed"] == 0
    assert tr.state(lv) == "testing", "исход ещё неизвестен"
    book(tr, T0 + 2, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    assert tr.state(tr.levels[key(99.0)]) == "defended", "теперь тест закрыт"


def test_defended_requires_a_finished_test_even_with_many_open():
    tr = tracker()
    book(tr, T0, [(100.0, 1000), (99.0, 500)], asks=[(101.0, 400)])
    for i in range(5):
        book(tr, T0 + 1 + i, [(99.0, 500)], asks=[(101.0, 400)])
    assert tr.state(tr.levels[key(99.0)]) == "testing"


def test_page_knows_the_testing_state():
    page = (ROOT / "dashboard/book-live.html").read_text()
    assert "цена у уровня сейчас" in page
