"""Патч: маршруты карточки в src/api/main.py. Идемпотентен."""

MAIN = "src/api/main.py"
TEST = "tests/test_card.py"

# Якорь — точная строка декоратора следующего маршрута (строка 973).
ANCHOR = '@app.get("/api/volume-scan", summary="Сканер объёма: пришли ли деньги")'
MARK = "def _card_for("

ROUTES = '''def _card_for(cur, ticker: str) -> dict:
    # Карточка собирается ЦЕЛИКОМ ИЗ ПАМЯТИ: ни базы, ни сети. Запись в
    # базу идёт раз в 20 секунд, а вердикт нужен по тем цифрам, которые
    # есть прямо сейчас.
    from datetime import datetime, timezone, timedelta
    from src.analysis import card as C

    tk = ticker.upper()
    now_utc = datetime.now(timezone.utc)
    now_sec = int(now_utc.timestamp())
    msk = now_utc + timedelta(hours=3)

    bars = list((getattr(cur, "minutes", None) or {}).get(tk) or [])
    lot = int((getattr(cur, "lots", None) or {}).get(tk) or 1)
    step = (getattr(cur, "steps", None) or {}).get(tk)
    last_tick = (getattr(cur, "last_msg", None) or {}).get(tk)

    # Свежесть — из того же health(), что показывает диагностика, чтобы
    # два эндпоинта не могли рассказывать разное об одном стриме.
    try:
        health = cur.health() or {}
    except Exception:
        health = {}
    fresh = int(health.get("tickers_fresh_60s") or 0)

    out = C.from_state(
        tk,
        bars=bars,
        now_sec=now_sec,
        tape_obj=getattr(cur, "tape", None),
        tracker=getattr(cur, "levels", None),
        lot=lot,
        minute_of_day=msk.hour * 60 + msk.minute,
        weekday=msk.weekday(),
        min_step=step,
        book=None,
        volume=None,
        stream_running=True,
        fresh_60s=fresh,
        last_tick_sec=last_tick,
    )

    # СТАКАН ТОЛЬКО МИНУТНОЙ АГРЕГАЦИЕЙ. Уровневого стакана в памяти нет
    # сознательно, и подставлять агрегаты в блок, ждущий список уровней,
    # нельзя: либо нули в рублях, либо плита в роли всего бида.
    try:
        snap = cur.agg.snapshot(tk) or {}
    except Exception:
        snap = {}
    books = snap.get("book") or {}
    # Источник по наличию, как в /api/book-live: на закрытой бирже
    # биржевых данных нет вовсе, а дилерские есть.
    src = "exchange" if books.get("exchange") else (
        "dealer" if books.get("dealer") else None)
    atr = ((out.get("price") or {}) if isinstance(out, dict) else {}).get("atr")
    out["book"] = C.book_minute_block(books.get(src) or {}, lot=lot,
                                      min_step=step, atr=atr)
    out["book_source"] = src
    return out


def _card_silence(tk: str) -> dict:
    # ТРИ ПРИЧИНЫ ТИШИНЫ РАЗЛИЧИМЫ. Одинаковое «нет данных» на выключенном
    # стриме, на неисправности и на запуске — три разные реакции человека.
    from src.collector import stream as _st
    try:
        from config.settings import STREAM_ENABLED
    except Exception:
        STREAM_ENABLED = False
    err = getattr(_st, "START_ERROR", None)
    if err:
        note = f"НЕИСПРАВНОСТЬ: {err}"
    elif not STREAM_ENABLED:
        note = "стрим выключен"
    else:
        note = "стрим поднимается, резолв FIGI занимает до минуты"
    return {"ticker": tk, "card": None, "note": note}


@app.get("/api/card/{ticker}", summary="Карточка бумаги: факты для вердикта")
async def get_card(ticker: str):
    # Карточка не даёт сигнала и не советует. Она собирает факты в одном
    # месте и в одних единицах, чтобы вердикт ставил агент, а не код.
    from fastapi import HTTPException
    from src.collector import stream as _st

    tk = (ticker or "").upper()
    if not ticker_known(tk):
        raise HTTPException(status_code=404, detail=f"Тикер {tk} не найден")
    cur = getattr(_st, "CURRENT", None)
    if cur is None:
        return _card_silence(tk)
    return {"ticker": tk, "card": _card_for(cur, tk)}


@app.get("/api/cards", summary="Карточки всех бумаг с живыми данными")
async def get_cards():
    # Без ограничения на число бумаг: вердикт ставит агент, а не платная
    # модель со счётчиком токенов, и урезать выборку было бы слепотой.
    from src.collector import stream as _st

    cur = getattr(_st, "CURRENT", None)
    if cur is None:
        return _card_silence("*")
    tickers = sorted((getattr(cur, "minutes", None) or {}).keys())
    cards = []
    for tk in tickers:
        try:
            cards.append(_card_for(cur, tk))
        except Exception as e:
            # Одна сломавшаяся бумага не должна уносить весь ответ,
            # но и молча исчезать из списка ей тоже нельзя.
            cards.append({"ticker": tk, "card": None,
                          "note": f"ошибка сборки карточки: {e}"})
    return {"count": len(cards), "cards": cards}


'''

TEST_CODE = '''

def test_card_routes_exist_and_use_the_minute_aggregate():
    """Маршруты есть, и стакан в них идёт через минутную агрегацию."""
    import pathlib
    src = pathlib.Path("src/api/main.py").read_text(encoding="utf-8")
    assert '"/api/card/{ticker}"' in src
    assert '"/api/cards"' in src
    assert "book_minute_block" in src


def test_card_route_names_three_reasons_for_silence():
    import pathlib
    src = pathlib.Path("src/api/main.py").read_text(encoding="utf-8")
    assert "стрим выключен" in src
    assert "НЕИСПРАВНОСТЬ: " in src
    assert "резолв FIGI занимает до минуты" in src
'''


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, t):
    with open(p, "w", encoding="utf-8") as f:
        f.write(t)


main = read(MAIN)
if MARK in main:
    print(" = маршруты уже есть")
elif ANCHOR not in main:
    raise SystemExit("НЕ ПРИМЕНЕНО: якорь /api/volume-scan не найден")
else:
    if main.count(ANCHOR) != 1:
        raise SystemExit("НЕ ПРИМЕНЕНО: якорь не единственен")
    write(MAIN, main.replace(ANCHOR, ROUTES + ANCHOR, 1))
    print(" + маршруты вставлены перед /api/volume-scan")

test = read(TEST)
if "def test_card_routes_exist_and_use_the_minute_aggregate(" in test:
    print(" = тесты маршрутов уже есть")
else:
    write(TEST, test.rstrip("\n") + "\n" + TEST_CODE)
    print(" + тесты маршрутов дописаны")
