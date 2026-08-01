"""
Секундный ряд стакана: то, что было потеряно между пакетом и минутой.

ЧТО БЫЛО НЕ ТАК. Стрим получает стакан до десяти раз в секунду, но всё складывалось
в МИНУТНУЮ корзину, а последовательность внутри минуты выбрасывалась — от неё
оставались два числа, минимум и максимум перекоса. По размаху нельзя сказать, что
было десять секунд назад.

Из-за этого не работали три вещи: изменение перекоса за 10 и 30 секунд, скорость
появления и исчезновения ликвидности, исполнение возле лучших цен. Данные с нужной
частотой ПРИХОДИЛИ, но не сохранялись — та же болезнь, что была с полем источника
сделки: API отдавал, код выбрасывал.

ПОЧЕМУ В ПАМЯТИ, А НЕ В БАЗЕ. Арифметика:

    каждую секунду по каждой бумаге    4.9 млн строк в день
    за 90 дней                         440 млн строк, около 35 ГБ
    свободно на сервере                20 ГБ

Не помещается, и не будет помещаться. Поэтому здесь — кольцо в памяти на последние
несколько минут, а в базу уходят ПРОИЗВОДНЫЕ по минутам: размах перекоса за 10 и 30
секунд, добавлено и снято лотов, исполнение у лучшей цены и глубже. Их 80 тысяч
строк в день и 0.9 ГБ за 90 дней.

Так теряется возможность посмотреть точный ряд трёхнедельной давности. Зато
сохраняется главное: НАСКОЛЬКО быстро и НАСКОЛЬКО сильно менялся стакан — а именно
это и нужно, чтобы потом измерить, значит ли оно что-нибудь.

УСТРОЙСТВО. Кольцо с фиксированным числом слотов, ключ слота — секунда эпохи по
модулю размера кольца. В слоте лежит и сама секунда: без неё нельзя отличить
свежий слот от прошлогоднего на том же месте.

ЧЕГО ЗДЕСЬ НЕТ. Ни направления, ни рекомендации. «Перекос сменился за 10 секунд» —
факт. «Значит цена пойдёт» — утверждение, которого у меня нет оснований делать.
"""
from typing import Optional

SECONDS = 180              # глубина кольца: три минуты
NEAR_TICKS = 2             # «возле лучшей цены» = столько шагов от неё


class TickRing:
    """
    Секундные слепки стакана по всем бумагам. Чистый класс: ни сети, ни базы.

    Ключ — (тикер, источник). Источник обязателен: дилерский стакан это котировки
    брокера, и скорость его изменения означает не то же самое.

    В секунду приходит до десяти пакетов. Слот перезаписывается ПОСЛЕДНИМ — нам
    нужно состояние на конец секунды, а не среднее по ней: среднее сгладило бы
    ровно те резкие смены, которые ищем.
    """

    def __init__(self, seconds: int = SECONDS):
        self.seconds = max(10, int(seconds))
        self.ring: dict = {}          # (тикер, источник) -> список слотов
        self.trades: dict = {}        # (тикер, источник) -> счётчики исполнения

    # ─── приём ───────────────────────────────────────────────────────────────

    def on_book(self, ticker: str, source: str, epoch_sec: int,
                bid_vol: float, ask_vol: float, bid5: float, ask5: float,
                best_bid: float, best_ask: float,
                bid_top: float = 0, ask_top: float = 0) -> None:
        tk = (ticker or "").upper()
        if not tk or epoch_sec <= 0:
            return
        total = (bid_vol or 0) + (ask_vol or 0)
        if total <= 0:
            return
        k = (tk, source or "exchange")
        buf = self.ring.get(k)
        if buf is None:
            buf = [None] * self.seconds
            self.ring[k] = buf
        buf[int(epoch_sec) % self.seconds] = {
            "sec": int(epoch_sec), "imb": bid_vol / total,
            "bid": float(bid_vol), "ask": float(ask_vol),
            "bid5": float(bid5 or 0), "ask5": float(ask5 or 0),
            "bb": float(best_bid or 0), "ba": float(best_ask or 0),
            "bid_top": float(bid_top or 0), "ask_top": float(ask_top or 0),
        }

    def on_trade(self, ticker: str, source: str, price: float, qty: int,
                 best_bid: float, best_ask: float, tick: float = 0.01) -> None:
        """
        Исполнение возле лучших цен.

        Разделяется на три корзины: точно по лучшей цене, в пределах NEAR_TICKS
        шагов от неё, и глубже. Различие содержательное: сделка по лучшей цене
        снимает верхнюю заявку, а сделка глубоко в стакане означает, что верх уже
        пробили и агрессор идёт дальше.

        Шаг цены передаётся снаружи: у бумаг он разный, и подставлять единый
        нельзя. Если шаг неизвестен, «возле» вырождается в «точно по цене» —
        лучше недосчитать, чем посчитать неверно.
        """
        tk = (ticker or "").upper()
        p = float(price or 0)
        if not tk or p <= 0 or not qty:
            return
        k = (tk, source or "exchange")
        c = self.trades.get(k)
        if c is None:
            c = {"at_best": 0, "near": 0, "deep": 0, "total": 0}
            self.trades[k] = c
        q = int(qty)
        c["total"] += q
        step = float(tick or 0)

        # РАССТОЯНИЕ СЧИТАЕТСЯ В ШАГАХ ЦЕНЫ, а не в рублях с допуском.
        #
        # Первая версия сравнивала рубли напрямую: abs(p - best) <= step * 2.
        # На настоящих ценах это ломается о двоичную арифметику — 100.12 минус
        # 100.10 даёт 0.020000000000010232, то есть БОЛЬШЕ двух шагов по 0.01, и
        # сделка ровно в двух шагах уезжала в «глубже». Ошибка размером 1e-14,
        # а последствие — неверная категория у всех сделок на границе.
        #
        # Деление на шаг с округлением снимает это целиком: цены стоят на сетке
        # шага, поэтому число шагов всегда целое.
        def _ticks(a: float, b: float) -> Optional[int]:
            if not b or step <= 0:
                return None
            return int(round(abs(a - b) / step))

        tb, ta = _ticks(p, best_bid), _ticks(p, best_ask)
        dist = min([x for x in (tb, ta) if x is not None], default=None)
        if dist is None:
            # Шаг неизвестен — «возле» вырождается в «точно по цене». Лучше
            # недосчитать, чем посчитать неверно.
            at = ((best_bid and abs(p - best_bid) < 1e-9)
                  or (best_ask and abs(p - best_ask) < 1e-9))
            c["at_best" if at else "deep"] += q
        elif dist == 0:
            c["at_best"] += q
        elif dist <= NEAR_TICKS:
            c["near"] += q
        else:
            c["deep"] += q

    # ─── чтение ──────────────────────────────────────────────────────────────

    def _series(self, ticker: str, source: str, now_sec: int) -> list:
        """Слоты по возрастанию времени, только не старше глубины кольца."""
        buf = self.ring.get(((ticker or "").upper(), source or "exchange"))
        if not buf:
            return []
        floor = int(now_sec) - self.seconds
        got = [s for s in buf if s and s["sec"] > floor]
        got.sort(key=lambda s: s["sec"])
        return got

    def _at(self, series: list, now_sec: int, back: int):
        """
        Ближайший слот НЕ НОВЕЕ, чем now-back.

        Именно «не новее», а не «ровно тогда»: в тихую секунду пакета может не
        быть вовсе, и требование точного совпадения давало бы пустоту на ровном
        рынке.
        """
        want = int(now_sec) - back
        best = None
        for s in series:
            if s["sec"] <= want:
                best = s
            else:
                break
        return best

    def deltas(self, ticker: str, source: str, now_sec: int) -> dict:
        """Перекос сейчас и насколько он сменился за 10, 30 и 60 секунд."""
        ser = self._series(ticker, source, now_sec)
        if not ser:
            return {}
        cur = ser[-1]
        out = {"imb": round(cur["imb"], 4), "age_sec": int(now_sec) - cur["sec"],
               "samples": len(ser)}
        for back in (10, 30, 60):
            was = self._at(ser, now_sec, back)
            if was is not None:
                out[f"imb_{back}s_ago"] = round(was["imb"], 4)
                out[f"imb_d{back}s"] = round(cur["imb"] - was["imb"], 4)
        return out

    def speed(self, ticker: str, source: str, now_sec: int,
              window: int = 30) -> dict:
        """
        Скорость появления и исчезновения ликвидности, лотов в секунду.

        Прибавления и убавления считаются ОТДЕЛЬНО, а не одной разностью.
        Разность скрывает главное: стакан, где за минуту добавили и сняли по
        миллиону, и стакан, где не было ничего, дают одинаковый ноль.
        """
        ser = self._series(ticker, source, now_sec)
        if len(ser) < 2:
            return {}
        ser = [s for s in ser if s["sec"] > int(now_sec) - window]
        if len(ser) < 2:
            return {}
        out = {"window_sec": window, "samples": len(ser)}
        span = max(1, ser[-1]["sec"] - ser[0]["sec"])
        for side in ("bid", "ask"):
            add = rem = 0.0
            peak_add = peak_rem = 0.0
            for i in range(1, len(ser)):
                d = ser[i][side] - ser[i - 1][side]
                gap = max(1, ser[i]["sec"] - ser[i - 1]["sec"])
                if d > 0:
                    add += d
                    peak_add = max(peak_add, d / gap)
                elif d < 0:
                    rem += -d
                    peak_rem = max(peak_rem, -d / gap)
            out[f"{side}_added_per_sec"] = round(add / span, 1)
            out[f"{side}_removed_per_sec"] = round(rem / span, 1)
            out[f"{side}_peak_add_per_sec"] = round(peak_add, 1)
            out[f"{side}_peak_remove_per_sec"] = round(peak_rem, 1)
        return out

    def near_best(self, ticker: str, source: str) -> dict:
        """Как исполнялось: по лучшей цене, рядом или глубже."""
        c = self.trades.get(((ticker or "").upper(), source or "exchange"))
        if not c or not c["total"]:
            return {}
        t = c["total"]
        return {"traded_lots": t,
                "at_best_lots": c["at_best"], "near_lots": c["near"],
                "deep_lots": c["deep"],
                "at_best_share": round(c["at_best"] / t, 4),
                "near_share": round(c["near"] / t, 4),
                "deep_share": round(c["deep"] / t, 4)}

    # ─── свёртка в минуту для базы ───────────────────────────────────────────

    def minute_summary(self, ticker: str, source: str, now_sec: int) -> dict:
        """
        Производные за последнюю минуту — то, что уходит в базу.

        Секундный ряд в базу не пишется: 4.9 млн строк в день и 35 ГБ за 90 дней
        при 20 ГБ свободных. Здесь сохраняется не ряд, а его СВОЙСТВА: насколько
        сильно и насколько быстро менялся стакан.

        Размах берётся крайними значениями, а не средним: одно среднее скрывает
        разворот, а именно резкая смена и интересна.
        """
        ser = self._series(ticker, source, now_sec)
        if len(ser) < 2:
            return {}
        d10, d30 = [], []
        for i, s in enumerate(ser):
            for back, acc in ((10, d10), (30, d30)):
                was = self._at(ser[:i + 1], s["sec"], back)
                if was is not None and was is not s:
                    acc.append(s["imb"] - was["imb"])
        sp = self.speed(ticker, source, now_sec, window=60)
        nb = self.near_best(ticker, source)
        out = {"samples": len(ser)}
        if d10:
            out["imb_d10_max"] = round(max(d10), 4)
            out["imb_d10_min"] = round(min(d10), 4)
        if d30:
            out["imb_d30_max"] = round(max(d30), 4)
            out["imb_d30_min"] = round(min(d30), 4)
        for side in ("bid", "ask"):
            out[f"{side}_added"] = sp.get(f"{side}_added_per_sec", 0.0) * 60
            out[f"{side}_removed"] = sp.get(f"{side}_removed_per_sec", 0.0) * 60
            out[f"{side}_peak_add"] = sp.get(f"{side}_peak_add_per_sec", 0.0)
            out[f"{side}_peak_remove"] = sp.get(f"{side}_peak_remove_per_sec", 0.0)
        out["traded_at_best"] = nb.get("at_best_lots", 0)
        out["traded_near"] = nb.get("near_lots", 0)
        out["traded_deep"] = nb.get("deep_lots", 0)
        return out

    def reset_trades(self, ticker: str = "", source: str = "") -> None:
        """
        Обнулить счётчики исполнения. Вызывается ПОСЛЕ свёртки минуты: счётчики
        накопительные, и без обнуления минутные значения превратились бы в
        суммы с начала дня.
        """
        if ticker:
            self.trades.pop(((ticker or "").upper(), source or "exchange"), None)
        else:
            self.trades = {}

    def stats(self) -> dict:
        filled = sum(1 for buf in self.ring.values() for s in buf if s)
        return {"series": len(self.ring), "slots_filled": filled,
                "depth_sec": self.seconds}
