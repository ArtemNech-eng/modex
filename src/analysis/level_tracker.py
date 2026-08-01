"""
Жизнь конкретного ценового уровня: сколько стояло, сколько исполнено, сколько раз
восстанавливался, пробит ли.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. В book_minute хранится РАЗМЕР крупнейшей заявки, но не
её ЦЕНА. Поэтому на вопрос «уровень 100.50 держали или сняли» ответить было
нельзя: видно, что плита исчезла, а на какой цене она стояла — нет.

Здесь уровни отслеживаются по цене и в памяти. Это возможно потому, что стрим
получает ВЕСЬ стакан на 20 уровней до десяти раз в секунду: история уровня
строится из этих пакетов на ходу, без обращения к базе.

В базу это не пишется сознательно. 20 цен с объёмами десять раз в секунду на 80
бумаг — миллионы строк в день. Для живого экрана хранение не нужно: нужен ответ
про СЕЙЧАС, а он целиком в памяти. Хранение истории уровней — отдельный вопрос,
и решать его надо после того, как станет ясно, что из этой картины вообще
пригодилось.

ЕДИНИЦЫ. Объёмы в стакане приходят в ЛОТАХ. Лот у бумаг разный: у SBER 1, у GAZP
10, у UGLD 1000. Поэтому «1000 лотов» само по себе не значит ничего, и наружу
отдаются рубли: цена × лоты × лотность. Лотность берётся из ISS, она там есть
бесплатно и без токена.

ЧЕГО ЗДЕСЬ НЕТ. Ни направления, ни рекомендации. «Уровень пробит» — факт.
«Значит цена пойдёт дальше» — утверждение, которого у меня нет оснований делать.
"""
from typing import Optional

# Уровень считается ушедшим, когда от его максимума осталось меньше этой доли.
# Не ноль: биржа отдаёт 20 уровней, и заявка может просто выпасть из окна, не
# исчезнув. Это неотличимо от снятия, и лучше считать так, чем делать вид, что
# различаем.
GONE_SHARE = 0.15
KEEP_MINUTES = 20          # сколько держать в памяти уровень, которого уже нет


class LevelTracker:
    """
    Состояние уровней по всем бумагам. Чистый класс: ни сети, ни базы.

    Ключ — (тикер, сторона, цена). Сторона важна: одна и та же цена бывает и
    заявкой на покупку, и на продажу в разные моменты.
    """

    def __init__(self, keep_minutes: int = KEEP_MINUTES):
        self.keep_minutes = keep_minutes
        self.levels: dict = {}
        self.best: dict = {}          # тикер -> (лучший бид, лучший аск)

    # ─── приём данных ────────────────────────────────────────────────────────

    def on_book(self, ticker: str, minute: str, bids: list, asks: list) -> None:
        """
        bids/asks — списки (цена, лоты). Порядок не важен, сортируем сами.

        Что происходит на каждом пакете:
          новые уровни заводятся, известные обновляются;
          уровень, пропавший из стакана, помечается ушедшим — но не удаляется;
          вернувшийся считается восстановленным, и отсчёт исполнения обнуляется;
          уровень за лучшей ценой помечается пробитым.
        """
        tk = (ticker or "").upper()
        if not tk:
            return
        bb = max((p for p, q in bids if q > 0), default=0.0)
        ba = min((p for p, q in asks if q > 0), default=0.0)
        self.best[tk] = (bb, ba)

        for side, book in (("bid", bids), ("ask", asks)):
            present = {}
            for price, qty in book:
                if price <= 0 or qty <= 0:
                    continue
                present[round(float(price), 6)] = int(qty)
            for price, qty in present.items():
                self._touch(tk, side, price, qty, minute)
            # Известные уровни этой стороны, которых в пакете НЕТ.
            for (t, s, price), lv in self.levels.items():
                if t != tk or s != side or price in present:
                    continue
                if lv["size"] > 0:
                    lv["size"] = 0
                    lv["gone_count"] += 1
                    lv["gone_at"] = minute

        # ПРОБОЙ. Для заявки на покупку — цена ушла НИЖЕ её; для продажи — ВЫШЕ.
        # Это факт о том, что уровень перестал быть границей, и ничего больше.
        for (t, s, price), lv in self.levels.items():
            if t != tk:
                continue
            if s == "bid" and bb and bb < price:
                lv["broken"] = True
            elif s == "ask" and ba and ba > price:
                lv["broken"] = True

    def _touch(self, tk: str, side: str, price: float, qty: int,
               minute: str) -> None:
        k = (tk, side, price)
        lv = self.levels.get(k)
        if lv is None:
            self.levels[k] = {
                "side": side, "price": price, "size": qty, "peak": qty,
                "traded": 0, "traded_since_restore": 0, "gone_count": 0,
                "restored_count": 0, "first_seen": minute, "last_seen": minute,
                "gone_at": None, "restored_at": None, "broken": False,
            }
            return
        was_gone = lv["size"] <= lv["peak"] * GONE_SHARE
        # ВОССТАНОВЛЕНИЕ. Считается только если уровень действительно уходил и
        # вернулся к сопоставимому размеру: иначе каждое дрожание объёма
        # записывалось бы как восстановление.
        if was_gone and qty >= lv["peak"] * 0.5 and lv["gone_count"] > 0:
            lv["restored_count"] += 1
            lv["restored_at"] = minute
            lv["traded_since_restore"] = 0
        lv["size"] = qty
        lv["peak"] = max(lv["peak"], qty)
        lv["last_seen"] = minute

    def on_trade(self, ticker: str, price: float, qty: int) -> None:
        """
        Сделка приписывается уровню по ТОЧНОЙ цене: биржевые цены стоят на сетке
        шага, поэтому совпадение точное и обходится без допусков.

        Сторона неизвестна заранее — сделка могла пройти и по биду, и по аску,
        поэтому объём приписывается обеим найденным сторонам этой цены. Иначе
        пришлось бы угадывать, а угадывать здесь нечем.
        """
        tk = (ticker or "").upper()
        p = round(float(price or 0), 6)
        if not tk or p <= 0 or qty <= 0:
            return
        for side in ("bid", "ask"):
            lv = self.levels.get((tk, side, p))
            if lv is not None:
                lv["traded"] += int(qty)
                lv["traded_since_restore"] += int(qty)

    # ─── выдача ──────────────────────────────────────────────────────────────

    def notable(self, ticker: str, lot: int = 1, top: int = 1) -> list:
        """
        Самые заметные уровни бумаги: по максимальному размеру, который на них
        когда-либо стоял.

        Отдаётся в РУБЛЯХ. Лотность обязательна: у UGLD лот 1000, у SBER 1, и
        сравнивать их в лотах бессмысленно.
        """
        tk = (ticker or "").upper()
        lot = max(1, int(lot or 1))
        out = []
        for side in ("bid", "ask"):
            got = [lv for (t, s, _), lv in self.levels.items()
                   if t == tk and s == side and lv["peak"] > 0]
            got.sort(key=lambda x: -x["peak"])
            for lv in got[:top]:
                money = lv["price"] * lot
                out.append({
                    "side": side, "price": lv["price"],
                    "peak_rub": round(lv["peak"] * money),
                    "now_rub": round(lv["size"] * money),
                    "traded_rub": round(lv["traded"] * money),
                    "peak_lots": lv["peak"], "now_lots": lv["size"],
                    "traded_lots": lv["traded"],
                    "gone_count": lv["gone_count"],
                    "restored_count": lv["restored_count"],
                    "traded_since_restore_rub": round(
                        lv["traded_since_restore"] * money),
                    "restored_at": lv["restored_at"],
                    "broken": lv["broken"],
                    "first_seen": lv["first_seen"],
                })
        return out

    def prune(self, current_minute: str) -> int:
        """
        Убрать уровни, которых давно нет. Без этого карта растёт весь день: цена
        ходит, и уровней за сессию набегают тысячи на бумагу.
        """
        if not current_minute or len(current_minute) < 16:
            return 0
        cutoff_h, cutoff_m = int(current_minute[11:13]), int(current_minute[14:16])
        cutoff = cutoff_h * 60 + cutoff_m - self.keep_minutes
        dead = []
        for k, lv in self.levels.items():
            seen = lv["last_seen"]
            if not seen or len(seen) < 16:
                continue
            mm = int(seen[11:13]) * 60 + int(seen[14:16])
            if lv["size"] <= 0 and mm < cutoff:
                dead.append(k)
        for k in dead:
            del self.levels[k]
        return len(dead)

    def stats(self) -> dict:
        return {"levels_tracked": len(self.levels),
                "tickers": len({t for t, _, _ in self.levels})}
