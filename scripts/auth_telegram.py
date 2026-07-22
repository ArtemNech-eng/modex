"""
MOODEX — Одноразовая авторизация в Telegram
Запускается ОДИН РАЗ перед первым запуском main.py

Создаёт файл сессии moodex_session.session
После этого main.py будет подключаться автоматически без SMS.

Запуск:
    python scripts/auth_telegram.py
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from config.settings import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, TELEGRAM_SESSION


async def auth():
    print("\n" + "═" * 50)
    print("  MOODEX — Авторизация в Telegram")
    print("═" * 50)

    if not TELEGRAM_API_ID or TELEGRAM_API_ID == 0:
        print("\n❌ TELEGRAM_API_ID не заполнен!")
        print("   1. Зайди на https://my.telegram.org/apps")
        print("   2. Создай приложение")
        print("   3. Заполни .env файл")
        return

    print(f"\n📱 Телефон: {TELEGRAM_PHONE}")
    print(f"🔑 API ID: {TELEGRAM_API_ID}")
    print(f"💾 Файл сессии: {TELEGRAM_SESSION}.session\n")

    client = TelegramClient(TELEGRAM_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH)

    try:
        await client.connect()

        if not await client.is_user_authorized():
            print("📲 Отправляем код в Telegram...")
            await client.send_code_request(TELEGRAM_PHONE)

            code = input("📟 Введи код из Telegram (или SMS): ").strip()

            try:
                await client.sign_in(TELEGRAM_PHONE, code)
            except SessionPasswordNeededError:
                # Двухфакторная аутентификация
                password = input("🔒 Введи пароль двухфакторной аутентификации: ").strip()
                await client.sign_in(password=password)

        me = await client.get_me()
        print(f"\n✅ Авторизован как: {me.first_name} {me.last_name or ''} (@{me.username or 'без username'})")
        print(f"✅ Файл сессии сохранён: {TELEGRAM_SESSION}.session")
        print("\n🚀 Теперь можно запускать: python main.py")

        # Проверяем доступность каналов
        from config.settings import TELEGRAM_CHANNELS
        print(f"\n🔍 Проверяем доступность {len(TELEGRAM_CHANNELS)} каналов...")
        available = []
        unavailable = []

        for channel in TELEGRAM_CHANNELS:
            try:
                entity = await client.get_entity(channel)
                title = getattr(entity, 'title', channel)
                available.append(f"  ✅ @{channel} — {title}")
            except Exception as e:
                unavailable.append(f"  ❌ @{channel} — {e}")

        for a in available:
            print(a)
        for u in unavailable:
            print(u)

        if unavailable:
            print(f"\n⚠️  {len(unavailable)} каналов недоступны.")
            print("   Возможно, нужно вступить в них с твоего аккаунта.")

    except Exception as e:
        print(f"\n❌ Ошибка авторизации: {e}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(auth())
