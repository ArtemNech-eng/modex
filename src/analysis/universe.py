"""
Динамический список бумаг: кого вообще смотреть.

Ошибка, ради которой написан модуль. До 31.07 список из 48 тикеров был зашит
в config/settings.py руками. 31.07 владелец прислал скриншот «Взлёты дня» —
и десять из пятнадцати лидеров роста оказались вне системы: MVID +8.29% при
обороте 388 млн, ETLN +6.61%, KZOSP +6.55%, UGLD +5.17%, DATA, GEMC, CNRU,
MTLRP, BLNG. Я в это время докладывал, что «лидер дня SMLT +4.92%».

Крупнейшее падение дня, SGZH −4.28% при обороте 635 млн, тоже отсутствовало.

Рукописный список стареет молча: бумага набирает обороты, становится
интересной, а система её не видит и никак об этом не сообщает. Поэтому список
строится по факту — из оборота на бирже.

ЧТО ВКЛЮЧАЕМ. Только акции: SECTYPE 1 (обыкновенные) и 2 (привилегированные).

ЧТО ИСКЛЮЧАЕМ и почему:
  J — биржевые фонды (AKMM, LQDT, SBMM…). Дают огромный оборот от
      маркетмейкера, но не ходят внутри дня как акции. 31.07 шестнадцать из
      двадцати девяти «пропущенных ликвидных бумаг» оказались именно фондами —
      если их не отсечь, они забьют весь список.
  9, A, B — ПИФы. Та же причина.
  D — депозитарные расписки. Обычно неликвидны, проверять отдельно.

ПРЕФЫ ВКЛЮЧЕНЫ НАМЕРЕННО. SBERP и MTLRP ходят не так, как обыкновенные:
31.07 MTLRP дал +3.04% отдельным движением. Держать обыкновенную и не держать
преф — значит видеть половину бумаги.
"""
import json
import logging
import os
import urllib.request
from datetime import date

logger = logging.getLogger(__name__)

ISS_TQBR = ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/"
            "TQBR/securities.json?iss.meta=off")

# Обыкновенные и привилегированные акции. Всё остальное — фонды и расписки.
SHARE_TYPES = {"1", "2"}

# Порог оборота. 100 млн ₽ в день — примерно та граница, ниже которой позиция
# владельца (150–250 тыс ₽) начинает заметно двигать цену на выходе.
MIN_TURNOVER_RUB = 100_000_000

CACHE_DIR = os.getenv("UNIVERSE_CACHE_DIR", "/tmp/universe")


def _fetch(timeout: int = 30) -> list:
    """Сырые строки с биржи: (тикер, тип, имя, оборот, цена, изменение)."""
    req = urllib.request.Request(ISS_TQBR, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    sec = d["securities"]
    md = d["marketdata"]
    si = {c: k for k, c in enumerate(sec["columns"])}
    mi = {c: k for k, c in enumerate(md["columns"])}
    mkt = {r[mi["SECID"]]: r for r in md["data"]}
    out = []
    for r in sec["data"]:
        sid = r[si["SECID"]]
        m = mkt.get(sid)
        if not m:
            continue
        val = m[mi["VALTODAY"]] or 0
        last = m[mi["LAST"]]
        prev = r[si["PREVPRICE"]]
        chg = ((last / prev - 1) * 100) if (last and prev) else None
        out.append({
            "ticker": sid,
            "sectype": r[si["SECTYPE"]],
            "name": r[si["SHORTNAME"]],
            "turnover": val,
            "price": last,
            "change_pct": chg,
            "lot": r[si["LOTSIZE"]],
        })
    return out


def build_universe(min_turnover: float = MIN_TURNOVER_RUB,
                   max_n: int = 80,
                   fallback: list = None) -> dict:
    """
    Список бумаг на сегодня по факту оборота.

    Возвращает {"tickers": [...], "rows": [...], "source": "iss"|"fallback"}.
    При недоступности биржи отдаёт fallback — молча пустой список хуже, чем
    вчерашний: без бумаг встанет весь сканер.
    """
    try:
        rows = _fetch()
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"universe: биржа недоступна ({e}), беру запасной список")
        return {"tickers": list(fallback or []), "rows": [], "source": "fallback"}

    shares = [x for x in rows
              if x["sectype"] in SHARE_TYPES and (x["turnover"] or 0) >= min_turnover]
    shares.sort(key=lambda x: -(x["turnover"] or 0))
    shares = shares[:max_n]
    if not shares:
        return {"tickers": list(fallback or []), "rows": [], "source": "fallback"}
    return {"tickers": [x["ticker"] for x in shares], "rows": shares, "source": "iss"}


def cached_universe(min_turnover: float = MIN_TURNOVER_RUB,
                    max_n: int = 80,
                    fallback: list = None) -> dict:
    """
    То же, но с дневным кэшем на диске. Биржу дёргаем раз в сутки: состав по
    обороту за день не меняется настолько, чтобы опрашивать чаще.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, f"{date.today().isoformat()}.json")
    if os.path.exists(p):
        try:
            d = json.load(open(p))
            if d.get("tickers"):
                d["source"] = "cache"
                return d
        except Exception:                                     # noqa: BLE001
            pass
    d = build_universe(min_turnover, max_n, fallback)
    if d["source"] == "iss":
        try:
            json.dump(d, open(p, "w"), ensure_ascii=False)
        except Exception:                                     # noqa: BLE001
            pass
    return d


def diff_against(static_list) -> dict:
    """
    Что рукописный список теряет, а что держит зря. Нужно, чтобы расхождение
    было ВИДНО, а не копилось молча — именно молчание и было проблемой.
    """
    u = build_universe()
    live = set(u["tickers"])
    old = set(static_list or [])
    by = {x["ticker"]: x for x in u["rows"]}
    missing = sorted(live - old, key=lambda t: -(by[t]["turnover"] or 0))
    return {
        "source": u["source"],
        "live_count": len(live),
        "static_count": len(old),
        "missing": [{"ticker": t, "name": by[t]["name"],
                     "turnover_mln": int((by[t]["turnover"] or 0) / 1e6),
                     "change_pct": by[t]["change_pct"]} for t in missing],
        "stale": sorted(old - live),
    }
