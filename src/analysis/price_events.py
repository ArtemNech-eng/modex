"""
Сканер цены: восемь событий по ЗАКРЫТЫМ барам, без стакана и без потока.

ЗАЧЕМ ОТДЕЛЬНО ОТ book_events. Тот детектор смотрит на связки стакана, потока и
цены — и это правильно для своей задачи. Но вопрос здесь другой: что делает
САМА ЦЕНА, безотносительно того, кто её двигает. Смешивать нельзя: событие,
которому нужен стакан, на закрытой бирже не сработает вовсе, а цена есть всегда.

ПОЧЕМУ НЕ КАЖДУЮ СЕКУНДУ. События живут на ЗАКРЫТЫХ барах. Пока минута не
закрылась, «резкого ускорения» не существует: есть незакрытый бар, границы
которого ещё изменятся. Считать по нему — та же ошибка, что 31.07 превратила
3078 пробоев в 6. Поэтому пересчёт при закрытии минуты, а не по таймеру.

ПОРОГИ ОТ САМОЙ БУМАГИ. «Резкое движение» в рублях у SBER и у UGLD — разные
величины, а в процентах разные у спокойного LKOH и у прыгающего MTLR. Поэтому
масштаб берётся из её же баров: медиана модуля хода за последние N штук.

    ЛОВУШКА, уже пойманная в ленте сделок. На ровном ряду медиана равна нулю, и
    тогда РЕЗКИМ становится любое шевеление. Если медиана нулевая, масштаба нет
    и события не выдаются вовсе — пустой ответ честнее выдуманного.

ЧТО ИЗ ЭТОГО УЖЕ ИЗМЕРЕНО, И РЕЗУЛЬТАТ ОТРИЦАТЕЛЬНЫЙ:

    BREAKOUT / PULLBACK / RETEST / REVERSAL   преимущества нет после издержек
                                              в 0.05%
    покупка на откате при «структуре вверх»   ВРЕДНО: t = -12.57, положительных
                                              дней 16%

Это не довод против детектора: детектор ОПИСЫВАЕТ, что случилось. Но ни одно
поле здесь не называется сигналом, и «найден откат» не значит «пора покупать».
"""
from statistics import median
from typing import Optional

from src.analysis.price_levels import levels as _chart_levels
from src.analysis.timeframes import bars

# ПЯТЬ ИНТЕРВАЛОВ, как в задании Артёма: 1, 3, 5, 15, 30.
#
# До 03.08 стояло (1, 5, 15), то есть 3 и 30 не считались вовсе, а 15 не
# срабатывал ни разу из-за короткой памяти. Окно 240 баров обеспечивает все
# пять: 30м даёт 8 закрытых баров при NEED=6.
#
#     шаг    баров из 240    события на реальных барах 03.08
#      1м        240                  20
#      3м         80                  19
#      5м         48                  33
#     15м         16                  23
#     30м          8                  28
STEPS = (1, 3, 5, 15, 30)
LOOK = 20              # сколько закрытых баров берём за масштаб
NEED = 6               # меньше этого баров события не ищем

#  Во сколько раз ход должен превысить обычный, чтобы считаться РЕЗКИМ.
#  Догадка. Калибровать по живому рынку: смотреть, у какой доли бумаг срабатывает.
SHARP = 2.5

#  Насколько «тихо» должно быть до начала движения и после остановки.
QUIET = 0.4            # ход меньше этой доли обычного считается тишиной
QUIET_BARS = 3         # столько баров подряд тишины

#  Откат: какую часть пройденного ноги цена должна вернуть.
#  Меньше нижней границы — шум, больше верхней — уже не откат, а разворот.
PULL_MIN, PULL_MAX = 0.25, 0.75

#  Сколько последних баров должны идти ПРОТИВ ноги.
#
#  Без этого условия откат находился у 61% бумаг на случайном блуждании — и это
#  не изъян порога, а изъян определения: «цена вернула 25-75% ноги» описывает,
#  ГДЕ цена, а не что она делает. Между недавними крайностями она и так почти
#  всегда. Откат как СОБЫТИЕ — это идущее встречное движение, а не положение.
PULL_BARS = 2

#  Нога и структурный ход должны быть ОСМЫСЛЕННОГО размера относительно
#  обычного хода бумаги.
#
#  Замер на случайном блуждании: без этого условия откат находился у 49 бумаг из
#  80, смена направления у 45. Детектор, срабатывающий почти везде, не отмечает
#  ничего — та же болезнь, что у процентиля на ровной ленте сделок.
#
#  Догадка. Калибровать по живому рынку через долю сработавших бумаг.
LEG_SCALE = 3.0

# ОКНО ПАМЯТИ и число уровней — ОДНИ на весь продукт.
#
# 03.08 сканер и карточка бумаги давали по одной и той же бумаге разные ответы:
# RAGR 8 событий против 4, POSI 3 против 9. Обе цифры были верны для своих
# входных данных — сканер считал по 60 барам из памяти и 4 уровням, карточка по
# 262 барам из базы и 6. На экране это выглядит как противоречие, и такой
# дефект хуже арифметической ошибки: обе стороны убедительны.
#
# 240 БАРОВ, А НЕ 60, потому что 60 их и не хватало. Шаг 15м требует шести
# закрытых баров, то есть 90 минут; при памяти в 60 минут он не срабатывал НИ
# РАЗУ, хотя ручка объявляла steps [1, 5, 15]. Замер на живой доске: 1м — 16
# событий, 5м — 54, 15м — ноль. Третий шаг в списке был неправдой с самого
# начала.
#
#     шаг    баров из 60    баров из 240
#      1м        60             240
#      5м        12              48
#     15м         4  МАЛО        16
#     30м         2  МАЛО         8
#
# Доли по КАЖДОМУ шагу при окне 240 остаются ниже трети (максимум 28%), то есть
# длинное окно ничего не расстраивает.
WINDOW = 240
LEVELS_TOP = 4

BREAK_TICKS = 2        # НИЖНЯЯ граница выхода за уровень, в шагах цены
# Основной порог пробоя — В ДОЛЯХ ОБЫЧНОГО ХОДА САМОЙ БУМАГИ.
#
# Порог в шагах цены означал у разных бумаг разное. Замер 03.08 по 43 бумагам:
# два шага это 0.29 обычного хода у MDMG и 2.0 у HEAD — разброс в семь раз при
# одном и том же детекторе. Отсюда «ложный пробой» на 72% бумаг: у MOEX два
# шага при 161.3 — это 0.012%, тень свечи, а не пробой.
#
# Доля выбрана по замеру на ТРЁХ окнах одного дня, а не на одном:
#
#     окно                   ×0.0    ×1.0    ×1.5    ×2.0
#     09:45, последние 60    72.1%   37.2%   23.3%   11.6%
#     часом раньше           41.9%   11.6%    4.7%    0.0%
#     первый час сессии      53.5%   18.6%    0.0%    0.0%
#
# ×1.5 и ×2.0 выглядят лучше на первом окне и дают НОЛЬ на двух других:
# детектор молчал бы две трети дня. Это подгонка под час. ×1.0 держится везде.
BREAK_SCALE = 1.0
FALSE_BARS = 3         # столько баров есть у ложного пробоя, чтобы вернуться


def _moves(closed: list) -> list:
    """Ходы между закрытиями соседних баров."""
    return [closed[i]["close"] - closed[i - 1]["close"]
            for i in range(1, len(closed))]


def _scale(moves: list) -> Optional[float]:
    """
    Обычный размер хода: медиана модулей.

    Ноль означает, что ряд стоит. Тогда масштаба НЕТ, и это не то же самое, что
    «масштаб маленький»: при нулевом делителе резким оказалось бы любое
    шевеление. Ровно этот случай уже ловился на ленте сделок — тридцать
    одинаковых сделок дали тридцать «крупных» из тридцати.
    """
    vals = [abs(m) for m in moves if m]
    if not vals:
        return None
    m = median(vals)
    return m if m > 0 else None


def _over(close: float, price: float, scale: float, floor_px: float) -> str:
    """
    НАСКОЛЬКО вышли за уровень — в ходах самой бумаги.

    Печатать сам порог бесполезно: он у всех событий бумаги одинаков, и строка
    «на 1 при обычном ходе 1» повторяет одно число дважды. А вот глубина выхода
    у каждого события своя, и она отвечает на вопрос, решителен ли пробой.
    """
    d = abs(close - price)
    if scale > 0:
        return f"{d / scale:.1f} хода бумаги"
    return f"{_num(d)} (ход бумаги нулевой, порог {_num(floor_px)})"


def _num(v: float) -> str:
    """Цена без хвоста нулей: 0.05 вместо 0.05000000000000001."""
    return f"{v:.6f}".rstrip("0").rstrip(".") or "0"


def _ev(kind: str, why: str, step: int, bar: dict, **nums) -> dict:
    """
    Событие. Только описание: что случилось, когда и с какими числами.

    Ни направления сделки, ни силы, ни совета — что из этого предшествует
    движению цены, не измерено.
    """
    out = {"kind": kind, "why": why, "step_min": step, "ts": bar.get("ts")}
    out.update({k: v for k, v in nums.items() if v is not None})
    return out


def detect_step(rows: list, step: int, tick: float = 0.01,
                levels: Optional[list] = None, p: Optional[dict] = None) -> list:
    """
    События одного шага. `rows` — минутные бары по возрастанию времени.

    Считается ТОЛЬКО по закрытым барам: незакрытый отбрасывается целиком.
    """
    p = {**DEFAULTS, **(p or {})}
    bs = bars(rows, step)
    closed = [b for b in bs if b.get("complete")]
    if len(closed) < NEED:
        return []
    moves = _moves(closed)
    scale = _scale(moves[-p["look"]:])
    if scale is None:
        return []                      # ряд стоит — масштаба нет, событий нет

    last, prev = closed[-1], closed[-2]
    mv = moves[-1]
    out = []

    # 1-2. РЕЗКОЕ УСКОРЕНИЕ. Ход последнего бара против обычного для этой бумаги.
    if abs(mv) >= scale * p["sharp"]:
        out.append(_ev(
            "sharp_up" if mv > 0 else "sharp_down",
            f"ход бара в {abs(mv) / scale:.1f} раза больше обычного",
            step, last, move=round(mv, 6), scale=round(scale, 6),
            times=round(abs(mv) / scale, 2)))

    # 3. НАЧАЛО ДВИЖЕНИЯ: до этого было тихо, теперь пошло.
    #    Отличается от ускорения тем, что ускорению нужно предыдущее движение, а
    #    здесь его как раз не было.
    qb = p["quiet_bars"]
    if len(moves) > qb and abs(mv) >= scale * p["sharp"] * 0.6:
        before = moves[-qb - 1:-1]
        if before and all(abs(m) <= scale * p["quiet"] for m in before):
            out.append(_ev(
                "move_started",
                f"{qb} бара стояли, затем ход в {abs(mv) / scale:.1f} раза больше обычного",
                step, last, quiet_bars=qb, move=round(mv, 6),
                times=round(abs(mv) / scale, 2)))

    # 4. ОСТАНОВКА ДВИЖЕНИЯ: шло в одну сторону, встало.
    if len(moves) >= qb + 1:
        run = moves[-qb - 1:-1]
        same = run and all(m > 0 for m in run) or run and all(m < 0 for m in run)
        moved = run and all(abs(m) >= scale * 0.8 for m in run)
        if same and moved and abs(mv) <= scale * p["quiet"]:
            out.append(_ev(
                "move_stalled",
                f"{qb} бара шли в одну сторону, последний встал",
                step, last, was_side="up" if run[0] > 0 else "down",
                move=round(mv, 6)))

    # 5. ОТКАТ: прошли ногу и вернули её часть, не сломав направления.
    leg = _leg(closed, p["look"])
    if leg:
        back = _pullback(closed, leg, {**p, "step": step, "scale": scale})
        if back:
            out.append(back)

    # 6-7. ПРОБОЙ И ЛОЖНЫЙ ПРОБОЙ по уровням С ГРАФИКА.
    if levels:
        out.extend(_breaks(closed, levels, tick, step, p))

    # 8. СМЕНА НАПРАВЛЕНИЯ по СТРУКТУРЕ, а не по знаку одного бара.
    flip = _structure_flip(closed, scale * p["leg_scale"] / 3)
    if flip:
        out.append(_ev("direction_changed", flip[1], step, last,
                       was=flip[0], now=flip[2]))
    return out


DEFAULTS = {"look": LOOK, "sharp": SHARP, "quiet": QUIET,
            "quiet_bars": QUIET_BARS, "pull_min": PULL_MIN,
            "pull_max": PULL_MAX, "break_ticks": BREAK_TICKS,
            "break_scale": BREAK_SCALE,
            "false_bars": FALSE_BARS, "leg_scale": LEG_SCALE,
            "pull_bars": PULL_BARS}


def _leg(closed: list, look: int) -> Optional[dict]:
    """
    Последняя нога: от крайней точки окна до противоположной крайности ПОСЛЕ неё.

    Порядок важен: минимум должен быть РАНЬШЕ максимума, чтобы говорить о ноге
    вверх. Иначе «нога» получилась бы из двух несвязанных точек.
    """
    win = closed[-look:]
    if len(win) < 3:
        return None
    lows = [b["low"] for b in win]
    highs = [b["high"] for b in win]
    i_lo, i_hi = lows.index(min(lows)), highs.index(max(highs))
    if i_hi > i_lo:
        return {"side": "up", "from": min(lows), "to": max(highs),
                "i_from": i_lo, "i_to": i_hi, "win": win}
    if i_lo > i_hi:
        return {"side": "down", "from": max(highs), "to": min(lows),
                "i_from": i_hi, "i_to": i_lo, "win": win}
    return None


def _pullback(closed: list, leg: dict, p: dict) -> Optional[dict]:
    """
    Откат — возврат ЧАСТИ ноги. Больше верхней границы это уже не откат, а
    разворот, и называть их одним словом значило бы стереть разницу.
    """
    size = abs(leg["to"] - leg["from"])
    scale = p.get("scale") or 0
    if size <= 0 or (scale and size < scale * p["leg_scale"]):
        return None      # нога с шум размером — возвращать в ней нечего
    now = closed[-1]["close"]
    back = (leg["to"] - now) if leg["side"] == "up" else (now - leg["to"])
    share = back / size
    if not (p["pull_min"] <= share <= p["pull_max"]):
        return None
    # Откат должен идти ПОСЛЕ конца ноги, а не быть самой ногой.
    if leg["i_to"] >= len(leg["win"]) - 1:
        return None
    # И он должен ИДТИ: последние бары движутся против ноги. Иначе это просто
    # «цена где-то в середине диапазона», что на случайном ряду верно всегда.
    nb = max(1, int(p.get("pull_bars", 1)))
    if len(closed) < nb + 1:
        return None
    tail = [closed[-i - 1]["close"] for i in range(nb + 1)][::-1]
    steps_ = [tail[i + 1] - tail[i] for i in range(len(tail) - 1)]
    if leg["side"] == "up" and not all(x < 0 for x in steps_):
        return None
    if leg["side"] == "down" and not all(x > 0 for x in steps_):
        return None
    return _ev("pullback",
               f"вернулось {share * 100:.0f}% хода, направление не сломано",
               p.get("step", 0), closed[-1], leg_side=leg["side"],
               retrace=round(share, 3), leg_size=round(size, 6))


def _breaks(closed: list, levels: list, tick: float, step: int,
            p: dict) -> list:
    """
    Пробой и ложный пробой уровней С ГРАФИКА.

    ЛОЖНЫЙ ПРОБОЙ ДАТИРУЕТСЯ БАРОМ ВОЗВРАТА, а не баром пробоя. В момент
    пробоя ещё неизвестно, ложный он или нет; поставить туда метку значило бы
    утверждать, что мы знали будущее. Ровно эта ошибка 31.07 превратила 3078
    пробоев в 6.
    """
    out = []
    # Выход за уровень: не меньше обычного хода бумаги и не меньше двух шагов
    # цены. Второе — на случай, когда ряд стоит и масштаба нет: без нижней
    # границы порог обнулился бы и пробоем стало бы любое касание.
    sc = _scale(_moves(closed)) or 0.0
    step_px = max(max(tick, 0.0) * p["break_ticks"], sc * p["break_scale"])
    n = p["false_bars"]
    for lv in levels[:6]:
        price = lv.get("price")
        if not price:
            continue
        # Ищем бар, вышедший за уровень, среди последних n+1 закрытых.
        for i in range(max(1, len(closed) - n - 1), len(closed)):
            b, before = closed[i], closed[i - 1]
            up = b["close"] > price + step_px
            dn = b["close"] < price - step_px
            if not (up or dn):
                continue
            # ПЕРЕСЕЧЕНИЕ, а не «оказался за уровнем». Первая версия проверяла
            # только положение закрытия, и уровень ВЫШЕ рынка «пробивался вниз»
            # на каждом обычном баре: цена под ним стояла всё время, пересекать
            # его никто не пересекал. Нужно, чтобы предыдущий бар был по другую
            # сторону.
            if up and before["close"] > price:
                continue
            if dn and before["close"] < price:
                continue
            after = closed[i + 1:]
            if not after:
                # Пробой на последнем баре: возврата ещё не было и быть не могло.
                if i == len(closed) - 1:
                    out.append(_ev(
                        "level_break",
                        f"закрытие за уровнем {price} "
                        f"на {_over(b['close'], price, sc, step_px)}",
                        step, b, level=price, side="up" if up else "down"))
                break
            # Возврат: для пробоя вверх — закрытие обратно под уровень, для
            # пробоя вниз — над ним. Пишется развёрнуто: условное выражение
            # внутри фильтра списка читается неверно и уже дало синтаксическую
            # ошибку.
            if up:
                back = [x for x in after if x["close"] <= price]
            else:
                back = [x for x in after if x["close"] >= price]
            if back:
                # ЗА СКОЛЬКО баров вернулись, а не сколько баров закрылось
                # внутри. Прежняя подпись брала len(back) и расходилась с
                # фактом у 25 ложных пробоев из 48: писала «за 3 бара», когда
                # вернулись за один. Правильное число всё это время лежало
                # рядом, в bars_out, и не использовалось.
                took = after.index(back[0]) + 1
                out.append(_ev(
                    "false_break",
                    f"вышли за {price} на {_over(b['close'], price, sc, step_px)}"
                    f" и вернулись "
                    f"{'через ' + str(took) + ' бара' if took > 1 else 'сразу'}",
                    step, back[0], level=price,
                    side="up" if up else "down", bars_out=took))
            break
    return out


def _structure_flip(closed: list, min_move: float = 0.0):
    """
    Смена направления ПО СТРУКТУРЕ: были выше по максимумам и минимумам, стали
    ниже. Это сильнее, чем смена знака одного бара: один бар вниз внутри роста
    происходит постоянно и ничего не меняет.
    """
    if len(closed) < 4:
        return None

    def st(a, b):
        # Сдвиг должен быть заметным: смещение на копейку формально даёт
        # «выше по максимумам и минимумам», а по сути это то же место.
        if (b["high"] - a["high"] > min_move
                and b["low"] - a["low"] > min_move):
            return "up"
        if (a["high"] - b["high"] > min_move
                and a["low"] - b["low"] > min_move):
            return "down"
        return None

    was = st(closed[-4], closed[-3])
    now = st(closed[-2], closed[-1])
    if was and now and was != now:
        return (was, f"структура сменилась с «{was}» на «{now}»", now)
    return None


def detect(rows: list, tick: float = 0.01, levels: Optional[list] = None,
           steps: tuple = STEPS, p: Optional[dict] = None) -> list:
    """Все события бумаги по всем шагам, от старых к новым."""
    out = []
    for st in steps:
        out.extend(detect_step(rows, st, tick=tick, levels=levels, p=p))
    return out


def events_for(rows: list, tick: float = 0.01, steps: tuple = STEPS,
               p: Optional[dict] = None) -> list:
    """
    События одной бумаги с КАНОНИЧЕСКИМИ входными данными.

    Единственная точка, где решается, сколько брать баров и сколько уровней.
    Сканер и карточка обязаны звать её, а не собирать входные данные каждый по
    своему — иначе они снова разойдутся, и обе будут правы.
    """
    rows = list(rows or ())[-WINDOW:]
    if not rows:
        return []
    try:
        lv = _chart_levels(rows, tick=tick or 0.01,
                           price_now=rows[-1].get("close"), top=LEVELS_TOP)
    except Exception:                                        # noqa: BLE001
        lv = []
    return detect(rows, tick=tick or 0.01, levels=lv, steps=steps, p=p)


def board(minutes: dict, steps: tuple = STEPS) -> dict:
    """
    НАПРАВЛЕНИЕ И СТРУКТУРА по каждой бумаге и каждому интервалу.

    Артём сформулировал задачу сканера так: «Цена действительно строит
    восходящее движение или просто случайно выросла на несколько тиков?» На это
    отвечают не события, а направление вместе со структурой: рост, у которого
    максимумы и минимумы идут выше, — это движение; рост без структуры — тики.

    До 03.08 всё это считалось ТОЛЬКО в карточке одной бумаги. По доске ответа не
    было: чтобы узнать, строит ли цена движение, надо было открыть каждую из
    восьмидесяти.

    Форма нарочно компактная: восемьдесят бумаг на пять интервалов, и полный
    профиль раздул бы ответ в разы. Замер: 109 мс на 80 бумаг.
    """
    from src.analysis.timeframes import profile
    out = {}
    for tk, rows in (minutes or {}).items():
        try:
            pr = profile(list(rows or ())[-WINDOW:], steps=steps)
        except Exception:                                    # noqa: BLE001
            continue
        fr = {}
        for name, f in (pr.get("frames") or {}).items():
            if not f.get("direction"):
                continue
            fr[name] = {"dir": f.get("direction"),
                        "struct": f.get("structure"),
                        "pct": f.get("change_pct")}
        if not fr:
            continue
        dirs = [v["dir"] for v in fr.values()]
        # СТРОИТ ЛИ ЦЕНА ДВИЖЕНИЕ — прямой ответ на вопрос задания, и он не про
        # согласие направлений, а про совпадение направления со СТРУКТУРОЙ. Рост,
        # у которого максимумы и минимумы идут выше, — движение; рост без
        # структуры — те самые «несколько тиков».
        #
        # Замер 03.08 по 43 бумагам: направление совпало со структурой на 42%
        # интервалов. Чаще всего расходятся «вниз при mixed» (33 случая) и «вниз
        # при inside» (22) — это и есть сходило-и-вернулось.
        bu = sum(1 for f in fr.values() if f["dir"] == "up" and f["struct"] == "up")
        bd = sum(1 for f in fr.values() if f["dir"] == "down" and f["struct"] == "down")
        out[tk] = {"frames": fr,
                   "up": dirs.count("up"), "down": dirs.count("down"),
                   "flat": dirs.count("flat"),
                   "built_up": bu, "built_down": bd, "built": bu - bd,
                   # СОГЛАСИЕ интервалов, строгое: все пять в одну сторону.
                   # Слово редкое намеренно — замер дал «расходятся» у 91%
                   # бумаг. Смягчать определение ради красивой доли значило бы
                   # подогнать ярлык: мягкое «большинство» даёт 14%, но и
                   # утверждает меньше. Считать интервалы читатель может сам —
                   # up/down/flat лежат рядом.
                   "agree": ("вверх" if dirs.count("up") == len(dirs) else
                             "вниз" if dirs.count("down") == len(dirs) else
                             "боковик" if dirs.count("flat") == len(dirs) else
                             "расходятся")}
    return out


def scan(minutes: dict, ticks: Optional[dict] = None,
         levels: Optional[dict] = None, steps: tuple = STEPS,
         p: Optional[dict] = None) -> list:
    """
    Пройти по ВСЕМ бумагам и вернуть список тех, у кого что-то нашлось.

    Форма выдачи у сканера другая, чем у карточки: список бумаг с тем, что
    сработало, а не карточка на бумагу. Ради этого он и отдельно.

    Порядок — по числу событий, потом по алфавиту. Не по «важности»: какое
    событие важнее, не измерено, и придумывать вес значило бы выдать догадку за
    знание.
    """
    out = []
    total = 0
    for tk, rows in (minutes or {}).items():
        total += 1
        tick = (ticks or {}).get(tk) or 0.01
        if levels is not None and tk in levels:
            # Уровни переданы снаружи — уважаем их (так делают тесты).
            evs = detect(list(rows or ())[-WINDOW:], tick=tick,
                         levels=levels[tk], steps=steps, p=p)
        else:
            # Иначе КАНОНИЧЕСКИЙ путь, тот же, что у карточки.
            evs = events_for(rows, tick=tick, steps=steps, p=p)
        if evs:
            out.append({"ticker": tk, "events": evs, "count": len(evs),
                        "kinds": sorted({e["kind"] for e in evs})})
    out.sort(key=lambda x: (-x["count"], x["ticker"]))
    return out


def rates_by_step(scanned: list, total: int) -> dict:
    """
    Доля бумаг ПО КАЖДОМУ ШАГУ отдельно.

    Общая доля обманывает: она считает бумагу сработавшей, если сработал хоть
    какой шаг, и потому растёт от ЧИСЛА ШАГОВ, а не от шума. 03.08 включение
    15м и 30м подняло «ложный пробой» с 32% до 50%, и я едва не принял это за
    расстройку порогов. По шагам отдельно максимум был 28%.
    """
    if not total:
        return {}
    per: dict = {}
    for x in scanned:
        for e in x["events"]:
            per.setdefault(str(e["step_min"]), {}).setdefault(e["kind"], set()
                                                              ).add(x["ticker"])
    return {st: {k: {"tickers": len(v), "share": round(len(v) / total, 3)}
                 for k, v in kinds.items()}
            for st, kinds in per.items()}


def rates(scanned: list, total: int) -> dict:
    """
    У КАКОЙ ДОЛИ бумаг сработал каждый вид события.

    Зачем это в выдаче. Замер на случайном блуждании: откат находился у 49 бумаг
    из 80, смена направления у 45, а начало движения — у одной. Событие,
    срабатывающее почти везде, не отмечает ничего, и читателю надо видеть это
    прямо, а не догадываться.

    Долю считать честнее, чем подбирать порог до «красивой» частоты: порог —
    догадка, а доля — измерение. По ней и калибруют.
    """
    if not total:
        return {}
    per: dict = {}
    for row in scanned:
        for k in row["kinds"]:
            per[k] = per.get(k, 0) + 1
    return {k: {"tickers": n, "share": round(n / total, 3)}
            for k, n in sorted(per.items(), key=lambda kv: -kv[1])}
