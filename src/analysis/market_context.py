"""
Контекст всего рынка: индекс, широта, сектор и сила бумаги относительно них.

ЗАЧЕМ, словами Артёма: «IMOEX −0.4%, а SBER +0.8% — это гораздо интереснее, чем
просто SBER +0.8%». Верно: одно и то же движение бумаги означает разное в
зависимости от того, куда идёт всё остальное.

ДВА ЭТАЛОНА, И ПУТАТЬ ИХ НЕЛЬЗЯ. Сравнивать бумагу можно с двумя разными вещами,
и у каждой свой изъян:

    IMOEX            настоящий индекс, взвешенный по капитализации. Но он
                     приходит из ISS ОПРОСОМ, то есть с задержкой, и состав у
                     него свой — не наши восемьдесят бумаг

    медиана корзины  считается из НАШЕГО же потока: та же секунда, тот же
                     часовой пояс, никакой задержки. Но это равновзвешенная
                     медиана наших бумаг, а не индекс

Отдаются ОБА, с пометкой источника и возраста. На быстром движении задержка
индекса способна перевернуть знак разницы, и молча подставлять одно вместо
другого нельзя.

ШИРОТА РЫНКА — сколько бумаг растёт и падает — считается только по нашим данным
и потому по-настоящему свежая. Она отвечает на вопрос, который индекс скрывает:
индекс может расти на двух тяжёлых бумагах при том, что падают шестьдесят.

СЕКТОР берётся из отраслевых индексов самой Московской биржи, а не из моей
догадки о том, кто чем занимается. Состав приходит из ISS бесплатно.

ЧТО ЗДЕСЬ ФАКТ, А ЧТО НЕТ. «SBER +0.8% при индексе −0.4%, разница 1.2 п.п.» —
факт. «Значит SBER сильный и его надо покупать» — утверждение, которого у меня
нет оснований делать: связь опережения рынка с последующим движением я не мерил.
31.07 несколько похожих меток измерялись бесполезными, одна вредной (t=-12.57).
Поэтому здесь только числа и слова о ПРОШЕДШЕМ: обгоняет, отстаёт, идёт вместе.
"""
from statistics import median
from typing import Optional

FLAT_PCT = 0.05        # меньше этого движение считается боковым, %
MIN_SECTOR = 3         # меньше стольких бумаг в секторе — широта ненадёжна
LEAD_PP = 0.15         # расхождение меньше этого — «идёт вместе», п.п.

#  Старше этого индекс помечается несвежим.
#
#  Найдено на живых данных 02.08: поле возраста считалось от МОЕГО обращения к
#  ISS и показывало «11 секунд» для значения, снятого биржей двумя днями раньше
#  (SYSTIME 31.07 19:00, биржа закрыта). Вопрос был ровно обратный: когда это
#  было правдой, а не когда я спросил.
STALE_SEC = 180


def changes(minutes: dict, back: int = 1) -> dict:
    """
    Изменение каждой бумаги за `back` минут, в процентах.

    `minutes` — тикер -> последовательность БАРОВ вида {ts, open, high, low,
    close, volume}, от старых к новым. Та же форма, что у `timeframes.bars`:
    сканер цены и широта рынка едят одно и то же, и двух форматов быть не должно.

    Берётся то, что реально есть: если у бумаги истории меньше, чем просят, она в
    ответ не попадает. Подставлять ноль нельзя — «не изменилась» и «не знаем» это
    разные вещи.
    """
    out = {}
    for tk, series in (minutes or {}).items():
        rows = list(series or ())
        if len(rows) < back + 1:
            continue
        a, b = rows[-back - 1].get("close"), rows[-1].get("close")
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            continue
        if a > 0:
            out[tk.upper()] = round((b - a) / a * 100, 4)
    return out


def breadth(chg: dict, flat_pct: float = FLAT_PCT) -> dict:
    """
    Сколько бумаг растёт, падает и стоит.

    То, что индекс СКРЫВАЕТ: он может расти на двух тяжёлых бумагах при том, что
    падают шестьдесят. Боковик — отдельный ответ, а не отсутствие ответа.
    """
    if not chg:
        return {}
    up = sum(1 for v in chg.values() if v > flat_pct)
    down = sum(1 for v in chg.values() if v < -flat_pct)
    flat = len(chg) - up - down
    vals = sorted(chg.values())
    out = {"total": len(chg), "up": up, "down": down, "flat": flat,
           "median_pct": round(median(vals), 4),
           "best_pct": round(vals[-1], 4), "worst_pct": round(vals[0], 4)}
    if up + down:
        out["up_share"] = round(up / (up + down), 4)
    return out


def rank(chg: dict, ticker: str) -> Optional[dict]:
    """
    Место бумаги среди остальных: сколько бумаг она обгоняет.

    Доля, а не номер: «двенадцатая из восьмидесяти» и «двенадцатая из
    пятнадцати» — разные вещи, а номер их не различает.
    """
    tk = (ticker or "").upper()
    if tk not in chg or len(chg) < 2:
        return None
    mine = chg[tk]
    below = sum(1 for k, v in chg.items() if k != tk and v < mine)
    return {"beats": below, "of": len(chg) - 1,
            "percentile": round(below / (len(chg) - 1), 3)}


def relative(ticker_pct: Optional[float], bench_pct: Optional[float],
             lead_pp: float = LEAD_PP, flat: float = FLAT_PCT) -> dict:
    """
    Насколько бумага расходится с эталоном, в процентных пунктах.

    Слово только о ПРОШЕДШЕМ: обгоняет, отстаёт, идёт вместе. «Сильная» и
    «слабая» здесь нет — это утверждения о будущем, а связь опережения рынка с
    последующим движением я не мерил.

    Отдельно помечается САМЫЙ интересный случай: бумага и эталон идут в РАЗНЫЕ
    стороны. Это не то же, что «обгоняет»: обогнать можно и падая медленнее.
    """
    if ticker_pct is None or bench_pct is None:
        return {}
    diff = round(ticker_pct - bench_pct, 4)
    out = {"ticker_pct": round(ticker_pct, 4),
           "bench_pct": round(bench_pct, 4), "diff_pp": diff}
    if abs(diff) < lead_pp:
        out["vs_bench"] = "вместе"
    else:
        out["vs_bench"] = "обгоняет" if diff > 0 else "отстаёт"
    # РАЗНЫЕ СТОРОНЫ — отдельный факт: бумага растёт, когда рынок падает.
    #
    # Но обе величины должны быть НЕ ШУМОМ. Найдено на живых данных 02.08: у
    # SBER вышло «вместе на 0.02 п.п. в разные стороны» — бумага +0.01%,
    # медиана −0.01%. Знаки формально разные, обе величины — округление. На
    # тихом рынке такой флаг горел бы постоянно и обесценился бы к тому
    # моменту, когда движение станет настоящим.
    if (abs(ticker_pct) > flat and abs(bench_pct) > flat
            and (ticker_pct > 0) != (bench_pct > 0)):
        out["opposite"] = True
    return out


def sector_view(chg: dict, sectors: dict, ticker: str,
                flat_pct: float = FLAT_PCT) -> dict:
    """
    Сектор бумаги и что происходит внутри него.

    Состав берётся из отраслевых индексов самой Московской биржи, а не из моей
    догадки, кто чем занимается.

    Если в секторе меньше нескольких бумаг, широта по нему ненадёжна, и это
    помечается: «два из трёх растут» звучит так же весомо, как «сорок из
    шестидесяти», а весит совсем иначе.
    """
    tk = (ticker or "").upper()
    sec = (sectors or {}).get(tk)
    if not sec:
        return {}
    peers = {k: v for k, v in chg.items()
             if (sectors or {}).get(k) == sec}
    if not peers:
        return {"sector": sec}
    b = breadth(peers, flat_pct=flat_pct)
    out = {"sector": sec, "peers": len(peers),
           "up": b.get("up", 0), "down": b.get("down", 0),
           "median_pct": b.get("median_pct")}
    if len(peers) < MIN_SECTOR:
        out["too_few"] = True
    if tk in chg and b.get("median_pct") is not None:
        out["vs_sector"] = relative(chg[tk], b["median_pct"])
    return out


def context(minutes: dict, ticker: str, sectors: Optional[dict] = None,
            index: Optional[dict] = None, steps: tuple = (1, 5, 15)) -> dict:
    """
    Весь контекст разом: широта, место бумаги, сектор и оба эталона.

    `index` — то, что пришло из ISS: {"value", "change_pct", "changes":
    {минут: процент}, "age_sec"}. Возраст ОБЯЗАТЕЛЕН в выдаче: на быстром
    движении задержка индекса способна перевернуть знак разницы.
    """
    tk = (ticker or "").upper()
    out: dict = {"frames": {}}
    for st in steps:
        chg = changes(minutes, back=st)
        if not chg:
            continue
        f = {"breadth": breadth(chg)}
        r = rank(chg, tk)
        if r:
            f["rank"] = r
        # Эталон 1: МЕДИАНА КОРЗИНЫ. Та же секунда, тот же поток, задержки нет.
        if tk in chg and f["breadth"].get("median_pct") is not None:
            f["vs_basket"] = relative(chg[tk], f["breadth"]["median_pct"])
        # Эталон 2: ИНДЕКС. Настоящий, взвешенный, но опрошенный с задержкой.
        idx_pct = ((index or {}).get("changes") or {}).get(st)
        if tk in chg and idx_pct is not None:
            f["vs_index"] = relative(chg[tk], idx_pct)
        out["frames"][f"{st}m"] = f
    if sectors:
        chg1 = changes(minutes, back=steps[0])
        sv = sector_view(chg1, sectors, tk)
        if sv:
            out["sector"] = sv
    if index:
        # `age_sec` — возраст ДАННЫХ по метке самой биржи, а не времени моего
        # запроса. На закрытой бирже это разница в двое суток, и путать их
        # значит выдавать позавчерашнее значение за свежее.
        out["index"] = {k: index.get(k) for k in
                        ("name", "value", "change_pct", "age_sec",
                         "fetch_age_sec", "ts")
                        if index.get(k) is not None}
        if (index.get("age_sec") or 0) > STALE_SEC:
            out["index"]["stale"] = True
    return out
