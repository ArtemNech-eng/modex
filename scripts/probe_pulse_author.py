"""
MOODEX — Разведчик эндпоинтов Пульса по автору.

Зачем: Пульс показывает сделки трейдеров, но точный публичный эндпоинт не
документирован и меняется. Этот скрипт перебирает вероятные адреса для
конкретного ника и показывает, какой отвечает 200 и что в ответе — чтобы
затем собрать коллектор сделок точно под рабочий эндпоинт.

⚠️ Запускать ЛОКАЛЬНО (не в песочнице — там Пульс отдаёт 403).
   Скрипт только читает публичные данные, ничего не отправляет,
   никаких токенов/куки не использует.

Запуск:
    python scripts/probe_pulse_author.py Rostislavzzz
    python scripts/probe_pulse_author.py Rostislavzzz VasilyOleynik   # несколько ников
"""
import asyncio
import json
import sys

import httpx

BASE = "https://www.tinkoff.ru/api/invest-gw/social/v1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://www.tinkoff.ru",
    "Referer": "https://www.tinkoff.ru/invest/social/",
}


def _candidates(nick: str) -> list[tuple[str, dict]]:
    """Список кандидатов (url, params) для одного ника."""
    return [
        (f"{BASE}/profile/{nick}", {}),
        (f"{BASE}/profile/nickname/{nick}", {}),
        (f"{BASE}/profile/{nick}/post", {"limit": 10}),
        (f"{BASE}/profile/{nick}/posts", {"limit": 10}),
        (f"{BASE}/post/user/{nick}", {"limit": 10}),
        (f"{BASE}/profile/{nick}/operations", {"limit": 10}),
        (f"{BASE}/profile/{nick}/operation", {"limit": 10}),
        (f"{BASE}/profile/{nick}/deals", {"limit": 10}),
        (f"{BASE}/profile/{nick}/trades", {"limit": 10}),
        (f"{BASE}/profile/{nick}/portfolio", {}),
        (f"{BASE}/profile/{nick}/instruments", {}),
        (f"{BASE}/profile/{nick}/timeline", {"limit": 10}),
        (f"{BASE}/profile/{nick}/activity", {"limit": 10}),
    ]


def _preview(data) -> str:
    """Короткое превью JSON + подсказка, где искать сделки."""
    try:
        text = json.dumps(data, ensure_ascii=False)
    except Exception:
        return str(data)[:600]
    hints = [k for k in ("operation", "operations", "deal", "trade", "buy", "sell",
                         "direction", "instrument", "ticker", "price", "quantity")
             if k.lower() in text.lower()]
    head = text[:700]
    return head + ("\n    ↳ найдены поля-подсказки: " + ", ".join(hints) if hints else "")


async def probe_nick(client: httpx.AsyncClient, nick: str):
    print(f"\n{'='*70}\n🔎 Ник: {nick}\n{'='*70}")
    hits = []
    for url, params in _candidates(nick):
        try:
            r = await client.get(url, params=params, timeout=12)
            tag = url.replace(BASE, "")
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    print(f"  200 (не JSON)  {tag}")
                    continue
                print(f"  ✅ 200  {tag}\n     {_preview(data)}")
                hits.append(url)
            else:
                print(f"  {r.status_code}     {tag}")
        except Exception as e:
            print(f"  ERR {type(e).__name__}: {str(e)[:60]}  {url.replace(BASE, '')}")
        await asyncio.sleep(0.3)
    return hits


async def main():
    nicks = sys.argv[1:]
    if not nicks:
        print("Использование: python scripts/probe_pulse_author.py <ник> [<ник2> ...]")
        return
    async with httpx.AsyncClient(headers=HEADERS) as client:
        all_hits = {}
        for nick in nicks:
            all_hits[nick] = await probe_nick(client, nick)

    print(f"\n{'='*70}\nИТОГ — рабочие эндпоинты (200):")
    for nick, hits in all_hits.items():
        print(f"  {nick}: {hits or '— ничего не ответило 200 —'}")
    print("\nСкопируйте сюда строки с ✅ и их превью — по ним я соберу коллектор сделок.")


if __name__ == "__main__":
    asyncio.run(main())
