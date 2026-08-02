"""
Лента сделок в памяти: последовательность, а не свёртка.

ЗАЧЕМ, словами Артёма: отличать РЕАЛЬНОЕ ДАВЛЕНИЕ от просто большой заявки в
стакане. Это разные вещи, и путать их дорого:

    большая заявка в стакане   кто-то ГОТОВ купить и ждёт. Пассивно. Её можно
                               снять за секунду, ничего не потратив
    агрессивные сделки         кто-то УЖЕ купил по чужой цене. Активно. Деньги
                               потрачены, отменить нельзя

ЧЕТВЁРТЫЙ СЛУЧАЙ ОДНОЙ БОЛЕЗНИ. Количество сделок, объёмы агрессивных покупок и
продаж, накопленная дельта — всё это в потоке БЫЛО, но сворачивалось до минуты.
Внутриминутный порядок выбрасывался. А именно порядок и отвечает на вопрос: три
крупные покупки подряд за десять секунд и три покупки, разбросанные по минуте, —
разные события с одинаковой минутной суммой.

ЧТО СЧИТАТЬ КРУПНОЙ СДЕЛКОЙ. Единого числа лотов нет: у SBER лот 1, у UGLD 1000,
и обычная сделка у одной бумаги — событие у другой. Поэтому порог берётся ИЗ ЕЁ
ЖЕ ленты: девяностый процентиль последних сделок. Ничего не выдумано, порог
подстраивается под бумагу и под время суток сам.

    ОГОВОРКА, которую легко проглядеть. Если крупной считать верхние 10%, то
    крупных всегда будет около 10% — само по себе их количество не значит
    ничего. Значение имеет СГУЩЕНИЕ: сколько их подряд и в одну ли сторону.
    Поэтому главная выдача здесь — серия, а не счётчик.

ЧТО ОТДАЁТСЯ И ЧЕГО НЕТ. Только числа и то, что по ним прямо видно. Ни «сильное
давление», ни «покупатель контролирует» — что из этого предшествует движению
цены, не измерено. 31.07 несколько таких меток измерялись бесполезными, а одна
вредной: t=-12.57.
"""
from collections import deque
from typing import Optional

SECONDS = 180          # глубина ленты
MAX_TRADES = 400       # и не больше стольких сделок на бумагу с источником
BIG_PCT = 0.90         # крупная сделка — выше этого процентиля своей же ленты

#  И одновременно не меньше стольких МЕДИАН.
#
#  Зачем второе условие. Один процентиль вырождается на ровной ленте: если все
#  тридцать сделок по десять лотов, то p90 равен медиане, и «верхние 10%»
#  превращаются в «все». Проверено тестом — вышло 30 крупных сделок из 30.
#  На реальной ленте это частый случай: круглые лоты дают много одинаковых сделок.
#
#  Обратная беда у одного процентиля тоже есть: единственный выброс среди
#  тридцати одинаковых сделок p90 не сдвигает, и настоящая крупная сделка
#  осталась бы незамеченной.
#
#  Поэтому порог — БОЛЬШЕЕ из двух: процентиль подстраивается под тяжёлый
#  хвост, кратность медианы спасает на ровной ленте.
#
#  Значение 3 — ДОГАДКА. Калибровать по живому рынку.
BIG_MULT = 3.0

MIN_SAMPLE = 20        # меньше стольких сделок порога нет: считать не по чему
GAP_SEC = 5            # разрыв больше этого рвёт серию крупных сделок

BUY, SELL = 1, 2       # как в протоколе биржи


def _pct(sorted_vals: list, q: float) -> float:
    """Процентиль по отсортированному списку. Без numpy: он тут не нужен."""
    if not sorted_vals:
        return 0.0
    i = int(q * (len(sorted_vals) - 1))
    return float(sorted_vals[i])


class TradeTape:
    """
    Последние сделки по каждой бумаге. Чистый класс: ни сети, ни базы.

    Ключ — (тикер, источник). Источник обязателен: дилерская сделка это сделка с
    брокером, а не с рынком, и смешивать их значит считать давлением то, что им
    не является.
    """

    def __init__(self, seconds: int = SECONDS, max_trades: int = MAX_TRADES):
        self.seconds = max(10, int(seconds))
        self.max_trades = max(20, int(max_trades))
        self.tape: dict = {}          # (тикер, источник) -> deque кортежей
        self.cum: dict = {}           # накопленная дельта за день, в лотах

    # ─── приём ───────────────────────────────────────────────────────────────

    def on_trade(self, ticker: str, source: str, sec: int, price: float,
                 qty: int, direction: int, best_bid: float = 0.0,
                 best_ask: float = 0.0) -> None:
        """
        Одна сделка. Кортеж, а не словарь: их сотни в секунду.

        `direction` — сторона АГРЕССОРА, как её отдаёт биржа. Для дилерских
        сделок проверено 12 случаями из 12: покупка исполняется у аска, продажа
        у бида, то есть клиент всегда переходит спред. Значит и там это
        агрессор.
        """
        tk = (ticker or "").upper()
        if not tk or qty <= 0:
            return
        k = (tk, source or "exchange")
        dq = self.tape.get(k)
        if dq is None:
            dq = self.tape[k] = deque(maxlen=self.max_trades)
        # Расстояние до лучшей цены пригодится, чтобы отличить сделку по рынку
        # от сделки глубоко в стакане, но считается на чтении: здесь только
        # запись, и она должна быть дешёвой.
        dq.append((int(sec), float(price), int(qty), int(direction)))
        d = int(qty) if direction == BUY else -int(qty)
        self.cum[k] = self.cum.get(k, 0) + d

    def reset_day(self) -> None:
        """Накопленная дельта считается от начала дня, а не от запуска."""
        self.cum.clear()

    # ─── чтение ──────────────────────────────────────────────────────────────

    def _window(self, k: tuple, now_sec: int, back: int) -> list:
        cut = int(now_sec) - max(1, int(back))
        dq = self.tape.get(k) or ()
        return [t for t in dq if t[0] >= cut]

    def window(self, ticker: str, source: str, now_sec: int,
               back: int = 30) -> dict:
        """
        Сводка окна: сколько сделок, куда, каких размеров.

        Покупки и продажи считаются РАЗДЕЛЬНО, а не одной дельтой: окно, где
        купили и продали по миллиону, и окно, где не торговали вовсе, дают
        одинаковый ноль.
        """
        k = ((ticker or "").upper(), source or "exchange")
        rows = self._window(k, now_sec, back)
        if not rows:
            return {}
        buy = sum(q for _, _, q, d in rows if d == BUY)
        sell = sum(q for _, _, q, d in rows if d != BUY)
        sizes = sorted(q for _, _, q, _ in rows)
        tot = buy + sell
        out = {
            "window_sec": back,
            "trades": len(rows),
            "buy_lots": buy,
            "sell_lots": sell,
            "delta_lots": buy - sell,
            "avg_size": round(tot / len(rows), 1),
            "median_size": _pct(sizes, 0.5),
            "max_size": sizes[-1],
            "trades_per_sec": round(len(rows) / max(1, back), 2),
            "cum_delta_lots": self.cum.get(k, 0),
        }
        if tot:
            out["buy_share"] = round(buy / tot, 4)
        return out

    def big_threshold(self, ticker: str, source: str,
                      now_sec: Optional[int] = None) -> Optional[float]:
        """
        Порог крупной сделки — процентиль ЛЕНТЫ САМОЙ БУМАГИ.

        Единого числа лотов быть не может: у SBER лот 1, у UGLD 1000. Порог из
        собственной ленты калибруется и под бумагу, и под время суток.

        Берётся БОЛЬШЕЕ из двух: процентиля и кратности медианы. Один процентиль
        вырождается на ровной ленте — если все сделки одинаковы, p90 равен
        медиане и крупными становятся ВСЕ. Одна кратность медианы, наоборот,
        слепа к тяжёлому хвосту.

        Пока сделок мало, порога НЕТ. Пустой ответ честнее, чем порог по трём
        сделкам, который назовёт крупной любую вторую.
        """
        k = ((ticker or "").upper(), source or "exchange")
        dq = self.tape.get(k) or ()
        if len(dq) < MIN_SAMPLE:
            return None
        sizes = sorted(q for _, _, q, _ in dq)
        return max(_pct(sizes, BIG_PCT), _pct(sizes, 0.5) * BIG_MULT)

    def big_trades(self, ticker: str, source: str, now_sec: int,
                   back: int = 60, top: int = 10) -> list:
        """
        Крупные сделки окна, от старых к новым.

        Это ПОСЛЕДОВАТЕЛЬНОСТЬ, а не список: три покупки подряд за десять секунд
        и три, разбросанные по минуте, — разные события с одинаковой суммой.
        """
        thr = self.big_threshold(ticker, source, now_sec)
        if thr is None:
            return []
        k = ((ticker or "").upper(), source or "exchange")
        out = []
        for sec, price, qty, d in self._window(k, now_sec, back):
            if qty >= thr:
                out.append({"sec": sec, "back": int(now_sec) - sec,
                            "price": price, "lots": qty,
                            "side": "buy" if d == BUY else "sell"})
        return out[-top:] if top else out

    def streak(self, ticker: str, source: str, now_sec: int,
               back: int = 60) -> dict:
        """
        СЕРИЯ односторонних крупных сделок — то, ради чего вся лента.

        Само количество крупных сделок не значит ничего: если крупной считать
        верхние 10%, их и будет около 10%. Значение имеет сгущение — сколько
        подряд, в одну ли сторону и за какое время.

        Серия рвётся сменой стороны или разрывом больше GAP_SEC: две покупки с
        интервалом в минуту это не серия, а два отдельных события.
        """
        big = self.big_trades(ticker, source, now_sec, back=back, top=0)
        if not big:
            return {}
        best = cur = None
        for t in big:
            if (cur and t["side"] == cur["side"]
                    and t["sec"] - cur["last_sec"] <= GAP_SEC):
                cur["count"] += 1
                cur["lots"] += t["lots"]
                cur["last_sec"] = t["sec"]
            else:
                cur = {"side": t["side"], "count": 1, "lots": t["lots"],
                       "first_sec": t["sec"], "last_sec": t["sec"]}
            if best is None or cur["count"] > best["count"]:
                best = dict(cur)
        out = {
            "big_count": len(big),
            "big_buy_lots": sum(t["lots"] for t in big if t["side"] == "buy"),
            "big_sell_lots": sum(t["lots"] for t in big if t["side"] == "sell"),
            "longest_run": best["count"],
            "run_side": best["side"],
            "run_lots": best["lots"],
            "run_sec": max(0, best["last_sec"] - best["first_sec"]),
            "run_ended_sec_ago": max(0, int(now_sec) - best["last_sec"]),
        }
        w = self.window(ticker, source, now_sec, back=back)
        tot = (w.get("buy_lots", 0) + w.get("sell_lots", 0))
        if tot:
            out["big_share_of_volume"] = round(
                sum(t["lots"] for t in big) / tot, 4)
        return out

    def pressure_vs_resting(self, ticker: str, source: str, now_sec: int,
                            resting_lots: int, back: int = 60) -> dict:
        """
        Главный вопрос Артёма в одной строке: исполнено АГРЕССИВНО против
        стоящего В СТАКАНЕ.

        Это разные вещи. Заявку на миллион можно снять за секунду, ничего не
        потратив; миллион, прошедший сделками, потрачен и отменить его нельзя.
        Оба числа рядом — и видно, чего именно много.

        Отношения этих чисел к будущему движению цены я не измерял, поэтому
        здесь нет ни «давление сильное», ни «заявка фиктивная». Только два числа
        и их частное.
        """
        w = self.window(ticker, source, now_sec, back=back)
        if not w:
            return {}
        traded = w["buy_lots"] + w["sell_lots"]
        out = {"traded_lots": traded, "resting_lots": int(resting_lots or 0),
               "window_sec": back}
        if resting_lots:
            out["traded_per_resting"] = round(traded / resting_lots, 3)
        return out

    def stats(self) -> dict:
        return {"series": len(self.tape),
                "trades_held": sum(len(v) for v in self.tape.values())}
