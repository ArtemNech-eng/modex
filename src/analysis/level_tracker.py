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
        # УКАЗАТЕЛЬ (тикер|источник, сторона) -> множество цен.
        #
        # Без него обход исчезнувших и пробитых шёл по ВСЕМ уровням всех бумаг —
        # 6400 записей на каждый пакет, который касается сорока. Замер: 0.92 мс
        # на пакет, то есть 1085 пакетов в секунду при потребности 800. Запас
        # 1.36x, и это до добавления счёта тестов. С указателем обходится только
        # своя бумага.
        self.index: dict = {}
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
        # Округление ОБЯЗАТЕЛЬНО: цены уровней лежат в ключах как round(..., 6),
        # и без него сравнение «лучшая цена равна цене уровня» не сходилось бы
        # никогда — тест не засчитался бы ни один раз.
        bb = round(max((p for p, q in bids if q > 0), default=0.0), 6)
        ba = round(min((p for p, q in asks if q > 0), default=0.0), 6)
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
            # Известные уровни ЭТОЙ бумаги и стороны, которых в пакете НЕТ.
            # Обход по указателю, а не по всем уровням всех бумаг.
            for price in tuple(self.index.get((tk, side), ())):
                if price in present:
                    continue
                lv = self.levels.get((tk, side, price))
                if lv is not None and lv["size"] > 0:
                    k = (tk, side, price)
                    # ИСЧЕЗНОВЕНИЕ ТОЖЕ ДЕЛИТСЯ на съеденное и снятое.
                    #
                    # Найдено на живых данных 02.08: у TATN ask 519.9 уровень
                    # пропадал трижды, а в итоге стояло «снято 0 ₽». Пропажа
                    # целиком — чистейший случай снятия, и не считать её значило
                    # врать заголовочным числом ровно в ту сторону, которая важна.
                    #
                    # Оговорка та же, что у GONE_SHARE: биржа отдаёт 20 уровней,
                    # и заявка может выпасть из окна, не исчезнув. Для уровней,
                    # которым мы ведём журнал, это маловероятно — они крупнейшие
                    # на своей стороне, то есть у самого верха.
                    pend = lv.get("pending_traded", 0)
                    eaten = min(lv["size"], pend)
                    gone_pulled = lv["size"] - eaten
                    lv["pending_traded"] = pend - eaten
                    self.history.accrue(k, minute, lh.GONE, lots=lv["size"],
                                        size=0, traded=eaten,
                                        pulled=gone_pulled)
                    if sec is not None and k in self.history.log:
                        self.history.add(k, sec, lh.GONE, lots=lv["size"],
                                         size=0, traded=eaten,
                                         pulled=gone_pulled)
                    lv["size"] = 0
                    lv["gone_count"] += 1
                    lv["gone_at"] = minute

        # ТЕСТЫ И ПРОБОЙ — один обход, по указателю своей бумаги.
        #
        # ТЕСТ — это когда цена ДОШЛА до уровня, то есть он стал лучшим на своей
        # стороне. Тест ЗАКОНЧИЛСЯ, когда цена ушла: либо отступила (уровень
        # выдержал), либо прошла насквозь (не выдержал).
        #
        # Это не то же самое, что «на уровне были сделки». Цена может подойти и
        # отступить, не задев ни одной заявки, — и это тоже тест, причём
        # выдержанный. Считать тестом только исполнение значило бы не увидеть
        # именно те случаи, когда уровень остановил движение.
        for side in ("bid", "ask"):
            for price in tuple(self.index.get((tk, side), ())):
                lv = self.levels.get((tk, side, price))
                if lv is None:
                    continue
                best = bb if side == "bid" else ba
                if not best:
                    continue
                beyond = (best < price) if side == "bid" else (best > price)
                at_touch = (best == price)
                was = lv.get("at_touch")
                if beyond:
                    lv["broken"] = True
                    if lv.get("in_test"):
                        lv["test_failed"] = lv.get("test_failed", 0) + 1
                        lv["in_test"] = False
                elif at_touch:
                    # Тест засчитывается только на ПРИХОДЕ: цена была не у
                    # уровня и подошла. `was is None` — первое наблюдение, приход
                    # никто не видел, значит и теста не было.
                    if was is False:
                        lv["tests"] = lv.get("tests", 0) + 1
                        lv["in_test"] = True
                        lv["last_test_at"] = minute
                elif lv.get("in_test"):
                    # Цена отошла от уровня, не пробив: тест выдержан. Закрываем
                    # только тот тест, который был засчитан, иначе выдержанных
                    # оказалось бы больше, чем самих тестов.
                    lv["test_held"] = lv.get("test_held", 0) + 1
                    lv["in_test"] = False
                lv["at_touch"] = at_touch

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
                # ВРЕМЯ ЖИЗНИ в секундах. Минуты для этого не годятся: уровень,
                # проживший сорок секунд, и уровень, стоящий час, в минутах
                # выглядят как «одна минута» и «шестьдесят», а разница между
                # сорока секундами и двумя минутами теряется целиком.
                "first_sec": sec, "last_sec": sec, "alive_sec": 0,
                "tests": 0, "test_held": 0, "test_failed": 0, "in_test": False,
                "last_test_at": None,
                # None = ещё не наблюдали, у лучшей цены уровень или нет.
                # Уровень, РОЖДЁННЫЙ лучшей ценой, тестом не считается: прихода
                # цены никто не видел. Иначе любой лучший бид рождался бы
                # «выдержавшим», и слово перестало бы что-либо значить.
                "at_touch": None,
            }
            self.index.setdefault((tk, side), set()).add(price)
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
        # ВРЕМЯ ЖИЗНИ. `alive_sec` копится только пока уровень стоит: уровень,
        # который час назад появился, полчаса отсутствовал и вернулся, прожил не
        # час. `first_sec` при этом остаётся первым появлением — «когда впервые
        # увидели» и «сколько простоял» это разные вопросы.
        if sec is not None:
            if lv.get("first_sec") is None:
                lv["first_sec"] = sec
            prev_sec = lv.get("last_sec")
            if prev_sec is not None and prev > 0:
                gap = sec - prev_sec
                if 0 <= gap <= 60:        # разрыв больше минуты — не «стоял»
                    lv["alive_sec"] = lv.get("alive_sec", 0) + gap
            lv["last_sec"] = sec

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

    @staticmethod
    def state(lv: dict) -> str:
        """
        Состояние уровня ОДНИМ СЛОВОМ — по тому, что с ним уже случилось.

        Артём просил STRONG / WEAK / DEFENDED / FAILED «без выдуманного Score», и
        насчёт Score он прав: балл склеивает несравнимое придуманными весами.

        Но STRONG и WEAK — тоже догадка, только короче. «Сильный» означает «в
        следующий раз выдержит», а это утверждение о БУДУЩЕМ, которого измерения
        не подтверждали. 31.07 ровно такая метка — требование «структура вверх» —
        измерялась ВРЕДНОЙ: t=-12.57, положительных дней 16%. В price_levels.py
        по этой же причине нет пометок «сильный» и «слабый», и это закреплено
        тестом.

        Поэтому здесь только ПРОШЕДШЕЕ ВРЕМЯ. Каждое слово — пересказ счётчиков,
        а не прогноз:

            broken      цена прошла насквозь
            defended    цену встречали и она отступала, ни одного пробоя
            failed      тесты были, и хотя бы один закончился проходом
            eaten       уменьшился в основном сделками — его выкупили
            pulled      уменьшился в основном без сделок — заявку убрали
            untested    цена до него не доходила ни разу

        Чего здесь нет и почему: связи «defended сегодня» с «удержит завтра» я не
        мерил. Как только на биржевых данных наберётся выборка, это проверяемо
        напрямую — и тогда слово «сильный» можно будет заслужить.
        """
        if lv.get("broken"):
            return "broken"
        tests = lv.get("tests", 0)
        if tests:
            return "failed" if lv.get("test_failed", 0) else "defended"
        traded = lv.get("traded", 0)
        peak = lv.get("peak", 0)
        # «Съеден» и «снят» различаются долей исполненного от пика: половина
        # выбрана как явное большинство, а не как измеренный порог.
        if peak and lv.get("size", 0) < peak * GONE_SHARE:
            return "eaten" if traded >= peak * 0.5 else "pulled"
        return "untested"

    def life(self, lv: dict, now_sec: Optional[int] = None,
             lot: int = 1) -> dict:
        """
        Жизнь уровня девятью числами — ровно тот список, который просил Артём.

        Время в СЕКУНДАХ. В минутах разница между сорока секундами и двумя
        минутами теряется целиком, а для заявки это разные жизни.
        """
        lot = max(1, int(lot or 1))
        money = lv["price"] * lot
        out = {
            "state": self.state(lv),
            "first_seen": lv.get("first_seen"),
            "gone_count": lv.get("gone_count", 0),
            "restored_count": lv.get("restored_count", 0),
            "traded_rub": round(lv.get("traded", 0) * money),
            "traded_since_restore_rub": round(
                lv.get("traded_since_restore", 0) * money),
            "tests": lv.get("tests", 0),
            "test_held": lv.get("test_held", 0),
            "test_failed": lv.get("test_failed", 0),
            "in_test": bool(lv.get("in_test")),
            "broken": bool(lv.get("broken")),
            "alive_sec": lv.get("alive_sec", 0),
            "peak_rub": round(lv.get("peak", 0) * money),
            "now_rub": round(lv.get("size", 0) * money),
        }
        first = lv.get("first_sec")
        if first is not None and now_sec is not None:
            out["age_sec"] = max(0, int(now_sec) - int(first))
        # Доля пика, которую уже исполнили. Не оценка, а частное двух счётчиков.
        if lv.get("peak"):
            out["traded_share_of_peak"] = round(
                lv.get("traded", 0) / lv["peak"], 3)
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
            raw = self.levels.get(k)
            if raw is not None:
                lv["life"] = self.life(raw, now_sec=now_sec, lot=lot)
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
            idx = self.index.get((k[0], k[1]))
            if idx is not None:
                idx.discard(k[2])       # иначе указатель растёт весь день
                if not idx:
                    del self.index[(k[0], k[1])]
        return len(dead)

    def stats(self) -> dict:
        by_source: dict = {}
        for t, _, _ in self.levels:
            src = t.split("|")[-1]
            by_source[src] = by_source.get(src, 0) + 1
        return {"levels_tracked": len(self.levels),
                "tickers": len({t.split("|")[0] for t, _, _ in self.levels}),
                "by_source": by_source}
