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

from src.analysis import level_history as lh

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

    def __init__(self, keep_minutes: int = KEEP_MINUTES,
                 history: Optional["lh.LevelLog"] = None):
        self.keep_minutes = keep_minutes
        self.levels: dict = {}
        self.best: dict = {}          # тикер -> (лучший бид, лучший аск)
        # СЕКУНДНАЯ история уровня. События определяются ЗДЕСЬ, потому что здесь
        # уже вычисляются исчезновение, восстановление и изменение размера.
        # Отдельный модуль со своим определением слова «восстановился» разошёлся
        # бы с этим — так модули и расходятся.
        self.history = history if history is not None else lh.LevelLog()

    # ─── приём данных ────────────────────────────────────────────────────────

    def on_book(self, ticker: str, minute: str, bids: list, asks: list,
                source: str = "exchange", sec: Optional[int] = None) -> None:
        """
        bids/asks — списки (цена, лоты). Порядок не важен, сортируем сами.

        Что происходит на каждом пакете:
          новые уровни заводятся, известные обновляются;
          уровень, пропавший из стакана, помечается ушедшим — но не удаляется;
          вернувшийся считается восстановленным, и отсчёт исполнения обнуляется;
          уровень за лучшей ценой помечается пробитым.

        `sec` — секунда эпохи пакета. Без неё история уровня не ведётся: минута
        для вопроса «что было десять секунд назад» бесполезна.
        """
        tk = (ticker or "").upper()
        if not tk:
            return
        # Источник в ключе. Дилерский стакан отслеживается ТОЖЕ, но отдельно:
        # первая версия кормила трекер только биржевым, и на закрытой бирже он
        # оставался пустым — проверить механику до понедельника было нельзя.
        # Смешивать нельзя по той же причине, что и везде: дилерский это
        # котировки брокера, там нет чужих заявок, которые можно съесть.
        tk = f"{tk}|{source}"
        bb = max((p for p, q in bids if q > 0), default=0.0)
        ba = min((p for p, q in asks if q > 0), default=0.0)
        self.best[tk] = (bb, ba)

        for side, book in (("bid", bids), ("ask", asks)):
            present = {}
            for price, qty in book:
                if price <= 0 or qty <= 0:
                    continue
                present[round(float(price), 6)] = int(qty)
            # Журнал ведётся для КРУПНЫХ уровней плюс тех, у кого он уже есть.
            # Второе обязательно: крупную плиту, которую съедают, надо довести до
            # нуля, а она по пути выпадает из верхних — и самые интересные
            # события потерялись бы ровно в конце.
            #
            # «Крупный» — не «пятый по счёту», а «сопоставимый с крупнейшим на
            # своей стороне». Пятое место занимает кто угодно: на синтетическом
            # прогоне отбор по месту дал 36 тысяч вытеснений за 2400 пакетов, и
            # журналы не успевали накопить историю.
            ranked = sorted(present.items(), key=lambda kv: -kv[1])
            biggest = ranked[0][1] if ranked else 0
            floor_qty = biggest * lh.ENTER_SHARE
            top = {p for p, q in ranked[:self.history.top_levels]
                   if q >= floor_qty}
            for price, qty in present.items():
                self._touch(tk, side, price, qty, minute, sec, price in top)
            # Известные уровни этой стороны, которых в пакете НЕТ.
            for (t, s, price), lv in self.levels.items():
                if t != tk or s != side or price in present:
                    continue
                if lv["size"] > 0:
                    k = (t, s, price)
                    self.history.accrue(k, minute, lh.GONE, size=0)
                    if sec is not None and k in self.history.log:
                        self.history.add(k, sec, lh.GONE, lots=lv["size"],
                                         size=0)
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
               minute: str, sec: Optional[int] = None,
               in_top: bool = False) -> None:
        k = (tk, side, price)
        lv = self.levels.get(k)
        if lv is None:
            self.levels[k] = {
                "side": side, "price": price, "size": qty, "peak": qty,
                "traded": 0, "traded_since_restore": 0, "gone_count": 0,
                "restored_count": 0, "first_seen": minute, "last_seen": minute,
                "gone_at": None, "restored_at": None, "broken": False,
                # Сделки, ещё не приписанные ни одному уменьшению. Нужны потому,
                # что сделки и стакан — ДВА независимых потока: сделка может
                # прийти позже пакета, который её уже учёл.
                "pending_traded": 0,
            }
            self.history.accrue(k, minute, lh.APPEARED, lots=qty, size=qty)
            if sec is not None and in_top:
                self.history.add(k, sec, lh.APPEARED, lots=qty, size=qty)
            return

        prev = lv["size"]
        delta = qty - prev
        # ИСПОЛНЕНО ИЛИ СНЯТО. Уменьшение на 300 лотов — это либо съели, либо
        # владелец убрал заявку. Смысл противоположный, и различает их только
        # объём сделок на этой цене между пакетами.
        pending = lv.get("pending_traded", 0)
        traded = pulled = 0
        if delta < 0:
            traded = min(-delta, pending)
            pulled = -delta - traded
            lv["pending_traded"] = pending - traded
        elif pending > 0 and delta >= 0:
            # Сделки прошли, а размер не упал — значит долили на ту же цену.
            traded = pending
            lv["pending_traded"] = 0

        was_gone = prev <= lv["peak"] * GONE_SHARE
        # ВОССТАНОВЛЕНИЕ. Считается только если уровень действительно уходил и
        # вернулся к сопоставимому размеру: иначе каждое дрожание объёма
        # записывалось бы как восстановление.
        restored = bool(was_gone and qty >= lv["peak"] * 0.5
                        and lv["gone_count"] > 0)
        if restored:
            lv["restored_count"] += 1
            lv["restored_at"] = minute
            lv["traded_since_restore"] = 0
            lv["pending_traded"] = 0

        kind = self._kind(restored, delta, traded, pulled)
        if kind:
            # В МИНУТУ — всегда, включая мелочь: сумма мелких исполнений за
            # минуту может быть больше одного крупного, и терять её нельзя.
            self.history.accrue(k, minute, kind, lots=abs(delta), size=qty,
                                traded=traded, pulled=pulled)
            # В СЕКУНДНЫЙ журнал — только заметное: стоящий уровень дал бы
            # шестьдесят строк в минуту, и нужные в них утонули бы.
            if sec is not None and (in_top or k in self.history.log):
                floor = max(1, lv["peak"] * lh.MIN_SHARE)
                big = (abs(delta) >= floor or traded >= floor
                       or pulled >= floor)
                if big or kind in (lh.RESTORED, lh.GONE):
                    self.history.add(k, sec, kind, lots=abs(delta), size=qty,
                                     traded=traded, pulled=pulled)

        lv["size"] = qty
        lv["peak"] = max(lv["peak"], qty)
        lv["last_seen"] = minute

    @staticmethod
    def _kind(restored: bool, delta: int, traded: int, pulled: int):
        """
        Как назвать произошедшее. Только описание, без оценок.

        Разделение исполненного и снятого важнее самого факта уменьшения:
        «съели» и «передумал» означают противоположное.
        """
        if restored:
            return lh.RESTORED
        if delta > 0:
            return lh.GREW
        if delta < 0:
            if traded and pulled:
                return lh.EATEN          # часть исполнена, часть снята
            return lh.EXECUTED if traded else lh.PULLED
        return lh.REFILLED if traded else None

    def on_trade(self, ticker: str, price: float, qty: int,
                 source: str = "exchange") -> None:
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
        tk = f"{tk}|{source}"
        for side in ("bid", "ask"):
            lv = self.levels.get((tk, side, p))
            if lv is not None:
                lv["traded"] += int(qty)
                lv["traded_since_restore"] += int(qty)
                # Ждёт ближайшего уменьшения, чтобы отличить исполнение от
                # снятия. Ограничено пиком: иначе неприписанные сделки копились
                # бы весь день и потом объявили бы исполненным чистое снятие.
                lv["pending_traded"] = min(
                    lv["peak"], lv.get("pending_traded", 0) + int(qty))

    # ─── выдача ──────────────────────────────────────────────────────────────

    def notable(self, ticker: str, lot: int = 1, top: int = 1,
                source: str = "exchange") -> list:
        """
        Самые заметные уровни бумаги: по максимальному размеру, который на них
        когда-либо стоял.

        Отдаётся в РУБЛЯХ. Лотность обязательна: у UGLD лот 1000, у SBER 1, и
        сравнивать их в лотах бессмысленно.
        """
        tk = f"{(ticker or '').upper()}|{source}"
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

    def with_history(self, ticker: str, now_sec: int, lot: int = 1,
                     top: int = 1, source: str = "exchange",
                     window: int = lh.WINDOW) -> list:
        """
        Заметные уровни ВМЕСТЕ с их секундной историей.

        То, ради чего всё: не «BID сейчас 882 тыс.», а что с ним происходило
        последние десять-шестьдесят секунд.
        """
        tk = f"{(ticker or '').upper()}|{source}"
        out = self.notable(ticker, lot=lot, top=top, source=source)
        for lv in out:
            k = (tk, lv["side"], lv["price"])
            tl = self.history.timeline(k, now_sec, lot=lot, window=window)
            if tl:
                lv["timeline"] = tl
                lv["history"] = self.history.summary(k, now_sec, lot=lot,
                                                     window=window)
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
            self.history.forget(k)      # иначе журналы копятся весь день
        return len(dead)

    def stats(self) -> dict:
        by_source: dict = {}
        for t, _, _ in self.levels:
            src = t.split("|")[-1]
            by_source[src] = by_source.get(src, 0) + 1
        return {"levels_tracked": len(self.levels),
                "tickers": len({t.split("|")[0] for t, _, _ in self.levels}),
                "by_source": by_source}
