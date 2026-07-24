"""
MOODEX — Сборщик сделок трейдеров Пульса («умные деньги»).

Идея: следить за РЕАЛЬНЫМИ сделками выбранных трейдеров (кто что купил/продал),
а не за их постами. Это более сильный сигнал. Полученные сделки агрегируются в
нетто-поток по тикерам и подаются Claude как ещё один вход + идут в форвард-трек.

⚠️ Честные ограничения (держим в голове):
  • Сделку видно ПОСЛЕ факта, с задержкой — цена может уже уйти (см. entry_status).
  • Не виден размер позиции относительно капитала и наличие плеча/стопа.
  • Только те авторы, кто открыл сделки в профиле.
  • Публичный эндпоинт Пульса недокументирован и может меняться — поэтому
    коллектор сам перебирает кандидатов и берёт первый рабочий, а разбор сделок
    сделан терпимым к разным схемам ответа. Не является инвестрекомендацией.
"""
import asyncio
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BASE = "https://www.tinkoff.ru/api/invest-gw/social/v1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://www.tinkoff.ru",
    "Referer": "https://www.tinkoff.ru/invest/social/",
}


def _endpoint_candidates(nick: str) -> list[str]:
    """Вероятные адреса, отдающие ленту/сделки автора. Первый рабочий кешируется."""
    return [
        f"{BASE}/profile/{nick}/operations",
        f"{BASE}/profile/{nick}/operation",
        f"{BASE}/profile/{nick}/deals",
        f"{BASE}/profile/{nick}/post",
        f"{BASE}/profile/{nick}/posts",
        f"{BASE}/post/user/{nick}",
        f"{BASE}/profile/{nick}/timeline",
    ]


@dataclass
class Deal:
    author: str
    ticker: str
    action: str            # "buy" | "sell"
    price: Optional[float]
    quantity: Optional[float]
    timestamp: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _norm_action(val) -> Optional[str]:
    """Привести тип операции к 'buy'/'sell' из разных форматов Пульса."""
    if val is None:
        return None
    s = str(val).strip().lower()
    if s in ("buy", "1", "long"):
        return "buy"
    if s in ("sell", "2", "short"):
        return "sell"
    if "покуп" in s or "buy" in s or "куп" in s:
        return "buy"
    if "прод" in s or "sell" in s:
        return "sell"
    return None


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("value", "amount", "price"):
            if k in v and isinstance(v[k], (int, float)):
                return float(v[k])
    if isinstance(v, str):
        try:
            return float(v.replace(",", ".").replace(" ", ""))
        except Exception:
            return None
    return None


def _find_items(data) -> list:
    """Найти список операций/постов в разных схемах ответа."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    payload = data.get("payload", data)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("operations", "items", "deals", "posts", "events", "results"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


def _extract_deal(item: dict, author: str) -> Optional[Deal]:
    """Терпимо вытащить сделку из одного элемента (операция ИЛИ пост с операцией)."""
    if not isinstance(item, dict):
        return None

    # Пост Пульса может нести операцию во вложенном поле
    op = item
    for key in ("operation", "operationInfo", "deal", "trade"):
        inner = item.get(key)
        if isinstance(inner, dict):
            op = inner
            break
    content = item.get("content")
    if isinstance(content, dict):
        for key in ("operation", "instrument"):
            inner = content.get(key)
            if isinstance(inner, dict):
                op = {**op, **inner}

    # Направление
    action = None
    for k in ("operationType", "type", "direction", "action", "side"):
        action = _norm_action(op.get(k))
        if action:
            break
    if not action:
        return None

    # Тикер
    ticker = None
    for k in ("ticker", "instrumentTicker", "symbol"):
        if op.get(k):
            ticker = str(op[k]).upper()
            break
    if not ticker:
        for k in ("instrument", "security", "instrumentInfo"):
            inst = op.get(k) or item.get(k)
            if isinstance(inst, dict) and inst.get("ticker"):
                ticker = str(inst["ticker"]).upper()
                break
    if not ticker:
        return None

    price = None
    for k in ("price", "executionPrice", "averagePrice", "dealPrice"):
        price = _num(op.get(k))
        if price is not None:
            break

    qty = None
    for k in ("quantity", "lots", "count", "qty"):
        qty = _num(op.get(k))
        if qty is not None:
            break

    ts = None
    for k in ("inserted", "date", "timestamp", "operationTime", "createdAt", "time"):
        if op.get(k) or item.get(k):
            ts = str(op.get(k) or item.get(k))
            break

    return Deal(author=author, ticker=ticker, action=action,
                price=price, quantity=qty, timestamp=ts)


def parse_operations(data, author: str) -> list[Deal]:
    """Разобрать ответ эндпоинта в список сделок (чистая функция — легко тестируется)."""
    deals = []
    for item in _find_items(data):
        d = _extract_deal(item, author)
        if d:
            deals.append(d)
    return deals


class PulseAuthorTracker:
    """Тянет сделки отслеживаемых трейдеров и агрегирует их в сигнал «умных денег»."""

    def __init__(self, authors: list[str], limit_per_author: int = 30):
        self.authors = [a.strip() for a in authors if a.strip()]
        self.limit = limit_per_author
        self._endpoint: dict[str, str] = {}    # nick -> рабочий url (кеш)
        self.last_raw_sample: Optional[dict] = None  # для донастройки парсера

    async def _fetch_author(self, client: httpx.AsyncClient, nick: str) -> list[Deal]:
        # Сначала уже известный рабочий эндпоинт, иначе перебор кандидатов
        urls = [self._endpoint[nick]] if nick in self._endpoint else _endpoint_candidates(nick)
        for url in urls:
            try:
                r = await client.get(url, params={"limit": self.limit}, timeout=12)
                if r.status_code != 200:
                    continue
                data = r.json()
            except Exception as e:
                logger.debug(f"smart-money {nick} {url}: {e}")
                continue
            deals = parse_operations(data, nick)
            if deals:
                self._endpoint[nick] = url          # запоминаем рабочий адрес
                if self.last_raw_sample is None:
                    self.last_raw_sample = {"author": nick, "url": url,
                                            "first_items": _find_items(data)[:2]}
                return deals
            # 200, но сделок не нашли — сохраним образец, чтобы поправить парсер
            if self.last_raw_sample is None:
                self.last_raw_sample = {"author": nick, "url": url,
                                        "note": "200, но сделки не распознаны",
                                        "first_items": _find_items(data)[:2]}
        return []

    async def fetch_all(self) -> list[Deal]:
        deals: list[Deal] = []
        async with httpx.AsyncClient(headers=HEADERS) as client:
            for nick in self.authors:
                deals.extend(await self._fetch_author(client, nick))
                await asyncio.sleep(0.3)
        return deals

    async def snapshot(self) -> dict:
        """
        Свод «умных денег»: сделки по авторам + нетто покупки/продажи по тикерам.
        net > 0 — трейдеры в основном ПОКУПАЛИ тикер, net < 0 — продавали.
        """
        deals = await self.fetch_all()
        by_ticker: dict[str, dict] = {}
        for d in deals:
            t = by_ticker.setdefault(d.ticker, {"buys": 0, "sells": 0, "authors": set()})
            if d.action == "buy":
                t["buys"] += 1
            else:
                t["sells"] += 1
            t["authors"].add(d.author)

        agg = []
        for ticker, v in by_ticker.items():
            net = v["buys"] - v["sells"]
            agg.append({
                "ticker": ticker,
                "buys": v["buys"],
                "sells": v["sells"],
                "net": net,
                "bias": "покупки" if net > 0 else "продажи" if net < 0 else "нейтрально",
                "authors": sorted(v["authors"]),
            })
        agg.sort(key=lambda x: abs(x["net"]), reverse=True)

        return {
            "authors": self.authors,
            "resolved_endpoints": dict(self._endpoint),
            "deals": [d.to_dict() for d in deals[:100]],
            "by_ticker": agg,
            "deal_count": len(deals),
            "sample": self.last_raw_sample if not deals else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def context_for(self, ticker: str, snapshot: dict) -> Optional[str]:
        """Короткая строка для промпта Claude по конкретному тикеру."""
        for row in snapshot.get("by_ticker", []):
            if row["ticker"] == ticker.upper() and (row["buys"] or row["sells"]):
                who = ", ".join(row["authors"][:5])
                return (f"💼 УМНЫЕ ДЕНЬГИ (сделки трейдеров): по {ticker} "
                        f"нетто {row['bias']} (покупок {row['buys']}, продаж {row['sells']}; "
                        f"трейдеры: {who}). Учитывай задержку — сделка уже совершена.")
        return None
