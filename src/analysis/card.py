"""
Карточка бумаги: плотный срез ТОЧНЫХ чисел, по которым выносится вердикт.

ЗАЧЕМ. Сканер отвечает только на вопрос «ГДЕ смотреть». Решение принимается
по ОБСТАНОВКЕ: где цена относительно средней дня, кто агрессивен в ленте,
что стоит в стакане, близко ли уровень и что с ним уже случалось. Раньше
это собиралось на каждом маршруте заново и по-разному.

ГЛАВНОЕ ПРАВИЛО: здесь НЕТ вердиктов. Ни «лонг», ни «сильный уровень», ни
«покупатель контролирует». Только измеренные числа и то, что по ним прямо
видно. Сетапы (orb_signal, consolidation_breakout) сюда не вошли СОЗНАТЕЛЬНО:
они отдают готовое решение, а карточка обязана оставлять решение читателю.
Иначе получается то, что уже было: вердикт пересказывает чужой вывод, не имея
возможности его оспорить.

ТРИ РЕШЕНИЯ, КАЖДОЕ ИЗ СВОЕГО УБЫТКА.

1. Кратность при тонкой базе не показывается вовсе (None), а не сопровождается
   оговоркой. 04.08 в проде: ASTR получил times=30.0 при базе 11 тыс ₽, и рядом
   стояло честное «кратность считать не по чему». Читатель видит число 30 и
   называет это событием дня, хотя это пробуждение после тишины.
   Числа, которое нельзя трактовать, лучше не давать.

2. Геометрия без привязки к депозиту. У каждого свой счёт, и числа обязаны
   годиться любому: ATR, шаг цены, лотность, ширина спреда и расстояния до
   уровней — в процентах и в ATR. Сколько это в рублях риска — вопрос того,
   кто торгует, а не того, кто смотрит на бумагу.

3. Блок честности данных рядом с самими данными: возраст последнего тика,
   сколько бар набрано, хватает ли их на ATR. Пустота обязана называть свою
   причину: поток не поднялся, рынок закрыт или рынок ОТКРЫТ, а пакетов нет —
   это три РАЗНЫЕ вещи, и третья — неисправность. За различение отвечает
   готовый no_data_note.

ЕДИНИЦЫ. Объёмы биржа отдаёт в ЛОТАХ, а лот у бумаг разный: у SBER 1, у GAZP 10,
у UGLD 1000. Поэтому наружу идут РУБЛИ: цена × лоты × лотность. Лотность есть
в ISS бесплатно и без токена.

Ни сети, ни базы: чистые функции над уже собранными данными, как и все сканеры.
"""
from typing import Optional

from src.analysis import intraday as it

BOOK_LEVELS = 5          # сколько уровней стакана показывать с каждой стороны
ATR_PERIOD = 14
OR_BARS = 6              # диапазон открытия: первые шесть бар СВОЕЙ сессии
MIN_BARS_FOR_ATR = 15    # меньше — ATR считать не по чему


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _r(x, n: int = 6):
    return None if x is None else round(float(x), n)


def _share_pct(part, whole):
    """Доля в процентах. None, если делить не на что — не ноль."""
    if part is None or not whole:
        return None
    return round(part / whole * 100, 3)


def _in_atr(distance, atr):
    """Расстояние в ATR — единица, одинаково читаемая у SBER и у FEES."""
    if distance is None or not atr:
        return None
    return round(distance / atr, 2)


def _bar(b: dict) -> dict:
    """
    Привести минутную запись к коротким ключам o/h/l/c/v.

    В системе живут ДВА вида минутной записи, и это не неряшливость:

      • короткий — {"ts", "o", "h", "l", "c", "v"};
      • длинный — {"ts", "open", "high", "low", "close", "volume"},
        именно он лежит в CURRENT.minutes и его ждёт timeframes.bars.

    Переименовать длинный вид в потоке значило бы тронуть сборку баров
    в 5/15/30 минут и два работающих сканера ради одного читателя.

    Почему не просто b.get("o") or b.get("open"): при отсутствии ключа
    получились бы нули, и карточка отдала бы правдоподобный ответ с
    нулевым ATR вместо отказа. Молчаливо неверное число опаснее
    пустого поля: по пустому полю никто не торгует.
    """
    if not isinstance(b, dict):
        return {}
    if "c" in b or "o" in b:
        return b
    return {"ts": b.get("ts"), "o": b.get("open"), "h": b.get("high"),
            "l": b.get("low"), "c": b.get("close"), "v": b.get("volume")}


def _series(bars: list) -> tuple:
    """Разложить бары в параллельные списки: так их ждёт intraday."""
    bars = [_bar(b) for b in (bars or [])]
    o = [_f(b.get("o")) for b in bars]
    h = [_f(b.get("h")) for b in bars]
    l = [_f(b.get("l")) for b in bars]
    c = [_f(b.get("c")) for b in bars]
    v = [_f(b.get("v")) for b in bars]
    ts = [b.get("ts") for b in bars]
    return o, h, l, c, v, ts


# ─── блоки карточки ──────────────────────────────────────────────

def price_block(bars: list, atr: Optional[float]) -> dict:
    """
    Где цена внутри дня. Не «дорого/дёшево», а расстояния.

    Отклонение от VWAP даётся и в процентах, и в ATR: полпроцента у тихого
    сетевого эмитента и полпроцента у Мечела — разные события.
    """
    if not bars:
        return {}
    o, h, l, c, v, _ts = _series(bars)
    last = c[-1]
    day_high, day_low = max(h), min(l)
    vw = it.vwap(h, l, c, v)
    vw_last = vw[-1] if vw else None
    rng = day_high - day_low
    out = {
        "last": _r(last, 4),
        "session_open": _r(o[0], 4),
        "day_high": _r(day_high, 4),
        "day_low": _r(day_low, 4),
        "change_from_open_pct": _share_pct(last - o[0], o[0]),
        "day_range_pct": _share_pct(rng, day_low),
        "vwap": _r(vw_last, 4),
        "dist_vwap_pct": _share_pct(last - vw_last, vw_last) if vw_last else None,
        "dist_vwap_atr": _in_atr(last - vw_last, atr) if vw_last else None,
    }
    # Место в диапазоне дня: 0% — на минимуме, 100% — на максимуме.
    # Заменяет слова «у вершины дня», в которых уже спрятана оценка.
    if rng > 0:
        out["place_in_day_range_pct"] = round((last - day_low) / rng * 100, 1)
    return out


def geometry_block(bars: list, atr: Optional[float], lot: int,
                   min_step: Optional[float], book: Optional[dict]) -> dict:
    """
    Геометрия без денег читателя.

    Здесь СОЗНАТЕЛЬНО нет ни размера позиции, ни рублей риска на сделку:
    депозит у каждого свой, и карточка не должна быть годной только одному
    счёту. Отдаются инварианты: ATR в цене, в процентах и в шагах цены,
    ширина спреда в процентах и в ATR.

    Спред в ATR важнее, чем в рублях: если спред составляет половину ATR, то
    любой вход стартует с потери половины ожидаемого хода — вне зависимости
    от размера счёта.
    """
    o, h, l, c, v, _ts = _series(bars) if bars else ([], [], [], [], [], [])
    last = c[-1] if c else None
    out = {
        "atr": _r(atr, 6),
        "atr_pct": _share_pct(atr, last) if (atr and last) else None,
        "lot": int(lot or 1),
        "min_step": _r(min_step, 6),
    }
    if atr and min_step:
        out["atr_in_steps"] = round(atr / min_step, 1)
    if bars:
        # Состояние волатильности — перцентиль ATR среди своих же значений,
        # то есть описание факта, а не прогноз расширения.
        vs = it.volatility_state(h, l, c)
        out["volatility"] = vs.get("state")
        out["atr_rank"] = vs.get("atr_rank")
    if book:
        bb, ba = best_prices(book)
        if bb and ba and ba > bb:
            spread = ba - bb
            out["spread"] = _r(spread, 6)
            out["spread_pct"] = _share_pct(spread, bb)
            out["spread_in_atr"] = _in_atr(spread, atr)
            if min_step:
                out["spread_in_steps"] = round(spread / min_step, 1)
    return out


def best_prices(book: dict) -> tuple:
    """Лучшие цены из сырого стакана. Порядок уровней не важен."""
    bids = [(p, q) for p, q in (book.get("bids") or []) if p > 0 and q > 0]
    asks = [(p, q) for p, q in (book.get("asks") or []) if p > 0 and q > 0]
    bb = max((p for p, _q in bids), default=None)
    ba = min((p for p, _q in asks), default=None)
    return bb, ba


def book_block(book: Optional[dict], lot: int, levels: int = BOOK_LEVELS) -> dict:
    """
    Стакан в рублях и перекос ближних уровней.

    Перекос — доля покупки в сумме двух сторон, без ярлыков типа
    «покупатель доминирует»: стоящую заявку можно снять за секунду, и связь
    перекоса с будущим движением цены не измерена. Рядом в ленте есть
    traded_per_resting — вот там сравниваются потраченные деньги со стоящими.
    """
    if not book:
        return {}
    lot = max(1, int(lot or 1))
    bids = sorted([(p, q) for p, q in (book.get("bids") or []) if p > 0 and q > 0],
                  key=lambda kv: -kv[0])[:levels]
    asks = sorted([(p, q) for p, q in (book.get("asks") or []) if p > 0 and q > 0],
                  key=lambda kv: kv[0])[:levels]
    if not bids and not asks:
        return {}
    bid_rub = sum(p * q * lot for p, q in bids)
    ask_rub = sum(p * q * lot for p, q in asks)
    out = {
        "best_bid": _r(bids[0][0], 4) if bids else None,
        "best_ask": _r(asks[0][0], 4) if asks else None,
        "levels_shown": levels,
        "bid_rub": round(bid_rub),
        "ask_rub": round(ask_rub),
        "bids": [{"price": _r(p, 4), "lots": int(q), "rub": round(p * q * lot)}
                 for p, q in bids],
        "asks": [{"price": _r(p, 4), "lots": int(q), "rub": round(p * q * lot)}
                 for p, q in asks],
    }
    if bid_rub + ask_rub > 0:
        out["bid_share"] = round(bid_rub / (bid_rub + ask_rub), 4)
    if ask_rub > 0:
        out["bid_to_ask"] = round(bid_rub / ask_rub, 3)
    return out


def volume_block(volume: Optional[dict]) -> dict:
    """
    Объём в контексте своей же нормы — и главное правило карточки.

    КРАТНОСТЬ ПРИ ТОНКОЙ БАЗЕ НЕ ОТДАЁТСЯ. 04.08 в проде ASTR получил
    times=30.0 при базе 11 тыс ₽, RASP — 28.77 при базе 9 тыс ₽. В обоих случаях
    рядом стояло «кратность считать не по чему», и всё равно число 30 читалось
    как сильнейшее событие дня. Цифра перевешивает оговорку всегда, поэтому
    её здесь просто нет, а причина названа словами.
    """
    if not volume:
        return {}
    rub = volume.get("rub")
    base = volume.get("base_rub")
    thin = bool(volume.get("base_thin"))
    out = {
        "minute_rub": round(_f(rub)) if rub is not None else None,
        "base_rub": round(_f(base)) if base is not None else None,
        "base_source": volume.get("base_source"),
        "base_thin": thin,
        "step_min": volume.get("step_min"),
    }
    if thin or not base:
        out["times"] = None
        out["times_missing_why"] = (
            "база тонкая — кратность считать не по чему, это пробуждение после тишины"
            if thin else "нормы ещё нет — история не набрана")
    else:
        out["times"] = round(_f(rub) / _f(base), 2)
    return out


def structure_block(bars: list, minute_of_day: Optional[int]) -> dict:
    """
    Объективные ориентиры дня: диапазон открытия и профиль объёма.

    Срок годности диапазона открытия отдаётся числом (or_age_min), а не словом
    «просрочен»: в 14:04 пробой диапазона 10:00-10:30 — это уже просто
    «рынок выше утра», и решать, годится ли это, должен читатель.
    """
    if not bars:
        return {}
    o, h, l, c, v, ts = _series(bars)
    out = {}
    orng = it.opening_range(h, l, bars=OR_BARS, dates=ts)
    if orng:
        out["or_high"] = orng.get("or_high")
        out["or_low"] = orng.get("or_low")
        out["or_bars"] = orng.get("bars")
        out["session_bars"] = orng.get("session_bars")
        end = orng.get("or_end_min")
        if end is not None and minute_of_day is not None:
            out["or_age_min"] = max(0, int(minute_of_day) - int(end))
    prof = it.volume_profile(h, l, c, v)
    if prof:
        out["poc"] = prof.get("poc")
        out["value_area_low"] = prof.get("val")
        out["value_area_high"] = prof.get("vah")
        out["volume_nodes"] = prof.get("nodes")
    bands = it.vwap_bands(h, l, c, v)
    if bands:
        out["vwap_sigma"] = bands.get("sigma")
        out["vwap_upper"] = bands.get("upper")
        out["vwap_lower"] = bands.get("lower")
    return out


def nearest_levels(levels: Optional[list], price: Optional[float],
                   atr: Optional[float]) -> dict:
    """
    Ближайшие уровни сверху и снизу с расстоянием до них.

    Расстояние в ATR обязательно: «уровень в двух рублях» ничего не значит,
    «уровень в половине ATR» значит.
    """
    if not levels or price is None:
        return {}
    above = [lv for lv in levels if _f(lv.get("price")) > price]
    below = [lv for lv in levels if 0 < _f(lv.get("price")) < price]
    out = {}
    if above:
        lv = min(above, key=lambda x: _f(x.get("price")))
        d = _f(lv.get("price")) - price
        out["above"] = {"price": lv.get("price"), "side": lv.get("side"),
                        "peak_rub": lv.get("peak_rub"), "now_rub": lv.get("now_rub"),
                        "state": (lv.get("life") or {}).get("state"),
                        "tests": (lv.get("life") or {}).get("tests"),
                        "distance_pct": _share_pct(d, price),
                        "distance_atr": _in_atr(d, atr)}
    if below:
        lv = max(below, key=lambda x: _f(x.get("price")))
        d = price - _f(lv.get("price"))
        out["below"] = {"price": lv.get("price"), "side": lv.get("side"),
                        "peak_rub": lv.get("peak_rub"), "now_rub": lv.get("now_rub"),
                        "state": (lv.get("life") or {}).get("state"),
                        "tests": (lv.get("life") or {}).get("tests"),
                        "distance_pct": _share_pct(d, price),
                        "distance_atr": _in_atr(d, atr)}
    return out


def data_block(bars: list, now_sec: Optional[int], last_tick_sec: Optional[int],
               stream_running: bool, fresh_60s: int, phase: str) -> dict:
    """
    Честность данных рядом с данными, а не в отдельном маршруте.

    Вердикт по бедным данным опаснее отсутствия вердикта, а увидеть бедность
    по самим числам нельзя: пустой список выглядит как спокойный рынок.
    """
    n = len(bars or [])
    out = {
        "bars": n,
        "enough_for_atr": n >= MIN_BARS_FOR_ATR,
        "stream_running": bool(stream_running),
        "tickers_fresh_60s": int(fresh_60s or 0),
        "phase": phase,
    }
    if last_tick_sec is not None and now_sec is not None:
        out["last_tick_sec_ago"] = max(0, int(now_sec) - int(last_tick_sec))
    if n < MIN_BARS_FOR_ATR:
        # Три разные причины молчания, а не одна фраза на все случаи.
        out["note"] = it.no_data_note(bool(stream_running), phase,
                                      int(fresh_60s or 0))
    return out


# ─── сборка ────────────────────────────────────────────────────

def build(ticker: str, *, bars: Optional[list] = None,
          now_sec: Optional[int] = None, minute_of_day: Optional[int] = None,
          weekday: Optional[int] = None, lot: int = 1,
          min_step: Optional[float] = None, book: Optional[dict] = None,
          tape: Optional[dict] = None, levels: Optional[list] = None,
          volume: Optional[dict] = None, stream_running: bool = True,
          fresh_60s: int = 0, last_tick_sec: Optional[int] = None) -> dict:
    """
    Собрать карточку. Все входы — уже собранные данные, без сети и базы.

    Пустой вход — не ошибка: карточка вернётся с пустыми блоками и с
    названной причиной в data.note. Исключение вместо карточки выглядело бы как
    поломка всего маршрута из-за одной тихой бумаги.
    """
    bars = list(bars or [])
    o, h, l, c, v, ts = _series(bars) if bars else ([], [], [], [], [], [])
    atr = it.intraday_atr(h, l, c, period=ATR_PERIOD) if len(bars) >= MIN_BARS_FOR_ATR else None
    phase = (it.session_phase(minute_of_day, weekday)
             if minute_of_day is not None else "unknown")
    price = price_block(bars, atr)
    card = {
        "ticker": (ticker or "").upper(),
        "minute": ts[-1] if ts else None,
        "phase": phase,
        "data": data_block(bars, now_sec, last_tick_sec, stream_running,
                           fresh_60s, phase),
        "price": price,
        "geometry": geometry_block(bars, atr, lot, min_step, book),
        "volume": volume_block(volume),
        "book": book_block(book, lot),
        "tape": dict(tape or {}),
        "structure": structure_block(bars, minute_of_day),
        "levels": list(levels or []),
    }
    card["nearest_levels"] = nearest_levels(levels, price.get("last"), atr)
    if minute_of_day is not None:
        card["day_progress"] = round(it.trading_day_progress(minute_of_day), 3)
    return card


def from_state(ticker: str, *, bars: Optional[list] = None,
               now_sec: Optional[int] = None, tape_obj=None, tracker=None,
               source: str = "exchange", lot: int = 1, top_levels: int = 2,
               **kwargs) -> dict:
    """
    Тонкая обёртка над живым состоянием: дёрнуть TradeTape и LevelTracker
    и отдать результат в build.

    Разделение сделано ради тестов: build проверяется на синтетике, а
    from_state — на настоящих объектах, чтобы подписи не расходились тихо.
    """
    tape: dict = {}
    if tape_obj is not None and now_sec is not None:
        for back in (30, 60):
            w = tape_obj.window(ticker, source, now_sec, back=back)
            if w:
                tape["w%d" % back] = w
        st = tape_obj.streak(ticker, source, now_sec, back=60)
        if st:
            tape["streak"] = st
        big = tape_obj.big_trades(ticker, source, now_sec, back=60, top=10)
        if big:
            tape["big_trades"] = big
        thr = tape_obj.big_threshold(ticker, source, now_sec)
        tape["big_threshold_lots"] = thr
        if thr is None:
            tape["big_threshold_missing_why"] = (
                "сделок меньше порога выборки — порог крупной сделки считать не по чему")
    levels = None
    if tracker is not None and now_sec is not None:
        levels = tracker.with_history(ticker, now_sec, lot=lot, top=top_levels,
                                      source=source)
    return build(ticker, bars=bars, now_sec=now_sec, lot=lot, tape=tape,
                 levels=levels, **kwargs)
