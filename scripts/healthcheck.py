"""
MOODEX — диагностика окружения (health check).

Проверяет, что подхватились переменные и доступны источники данных/ИИ:
  • какие ключи заданы (без вывода самих значений);
  • Tinkoff realtime (убирает задержку 15 мин) — реально ли отвечает;
  • MOEX ISS (бесплатный, с задержкой) — доступен ли;
  • Claude — отвечает ли по заданному ключу/провайдеру;
  • интрадей-контекст по SBER — какой источник данных и какой сетап вышел.

Запуск из корня проекта:
    python scripts/healthcheck.py
Секретные значения НЕ печатаются — только «задан / не задан».
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _yn(v) -> str:
    return "✅ задан" if v else "❌ не задан"


async def _timed(coro, seconds: int):
    return await asyncio.wait_for(coro, timeout=seconds)


async def check_tinkoff() -> None:
    from config.settings import TINKOFF_TOKEN
    if not TINKOFF_TOKEN:
        print("• Tinkoff realtime: ❌ токен не задан → цены пойдут из MOEX с задержкой ~15 мин")
        return
    try:
        from src.collector.tinkoff_client import TinkoffClient
        data = await _timed(TinkoffClient().get_intraday_candles("SBER", tf_min=5, hours=4), 15)
        if data and data.get("close"):
            print(f"• Tinkoff realtime: ✅ OK — получено свечей SBER: {len(data['close'])} (задержки нет)")
        else:
            print("• Tinkoff realtime: ⚠️ токен задан, но данных нет — проверь права токена/доступ к API")
    except Exception as e:
        print(f"• Tinkoff realtime: ❌ ошибка: {str(e)[:140]}")


async def check_moex() -> None:
    try:
        from src.collector.moex_price_collector import MOEXPriceCollector
        from datetime import date, timedelta
        raw = await _timed(MOEXPriceCollector().get_candles(
            "SBER", interval=10, from_date=date.today() - timedelta(days=1)), 15)
        if raw:
            print(f"• MOEX ISS (fallback): ✅ доступен — свечей SBER: {len(raw)} (данные с задержкой ~15 мин)")
        else:
            print("• MOEX ISS (fallback): ⚠️ пусто (возможно, биржа закрыта или нет данных за период)")
    except Exception as e:
        print(f"• MOEX ISS (fallback): ❌ ошибка: {str(e)[:140]}")


async def check_claude() -> None:
    from config.settings import ANTHROPIC_API_KEY
    from src.agent.claude_agent import _PROVIDER, _MODEL
    print(f"  (провайдер: {_PROVIDER}, модель: {_MODEL})")
    if not ANTHROPIC_API_KEY:
        print("• Claude: ❌ ключ не задан → решения будет принимать упрощённая модель")
        return
    try:
        from src.agent.claude_agent import ClaudeAgent
        reply = await _timed(ClaudeAgent()._ask(
            "Ты пинг-сервис.", "Ответь одним словом: OK", max_tokens=10), 30)
        print(f"• Claude: ✅ отвечает — «{reply.strip()[:40]}»")
    except Exception as e:
        print(f"• Claude: ❌ не ответил: {str(e)[:140]}")


async def check_intraday_pipeline() -> None:
    try:
        from src.agent import intraday_analyst as ia
        data = await _timed(ia.fetch_intraday("SBER", tf_min=5), 20)
        if not data or not data.get("close"):
            print("• Интрадей SBER: ⚠️ не удалось получить свечи (биржа закрыта или нет доступа)")
            return
        src = data.get("_source"); delayed = data.get("_delayed")
        ctx = await _timed(ia.build_intraday_context("SBER"), 20)
        if not ctx:
            print(f"• Интрадей SBER: источник {src} — свечей мало для контекста")
            return
        print(f"• Интрадей SBER: источник {src} ({'с задержкой' if delayed else 'реалтайм'})")
        print(f"    цена {ctx['price']} | {ctx.get('vwap_rel')} | волатильность {ctx.get('volatility_state')} | фаза {ctx.get('phase')}")
        print(f"    сетап: {ctx.get('setup')} | сигнал: {ctx.get('signal')} | наблюдение: {ctx.get('observe')}")
    except Exception as e:
        print(f"• Интрадей SBER: ❌ ошибка: {str(e)[:140]}")


async def main() -> None:
    from config.settings import (
        TINKOFF_TOKEN, ANTHROPIC_API_KEY, TELEGRAM_API_ID,
        INTRADAY_MODE, INTRADAY_TF_MIN, INTRADAY_HORIZON_HOURS, DATABASE_URL,
    )
    print("=== MOODEX health check ===")
    print("Переменные окружения (значения не показываю):")
    print(f"  TINKOFF_TOKEN:      {_yn(TINKOFF_TOKEN)}")
    print(f"  ANTHROPIC_API_KEY:  {_yn(ANTHROPIC_API_KEY)}")
    print(f"  TELEGRAM_API_ID:    {_yn(TELEGRAM_API_ID)}")
    print(f"  INTRADAY_MODE:      {INTRADAY_MODE} (ТФ {INTRADAY_TF_MIN}м, горизонт {INTRADAY_HORIZON_HOURS}ч)")
    print(f"  DATABASE_URL:       {DATABASE_URL.split('://', 1)[0]}")
    print("\nПроверка источников и ИИ:")
    await check_tinkoff()
    await check_moex()
    await check_claude()
    print("\nПроверка интрадей-пайплайна:")
    await check_intraday_pipeline()
    print("\nГотово. Если Tinkoff = OK и Claude отвечает — реалтайм и «мозг» подключены.")


if __name__ == "__main__":
    asyncio.run(main())
