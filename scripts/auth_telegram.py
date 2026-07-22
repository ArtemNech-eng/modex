"""
MOODEX — Авторизация в Telegram + генерация строковой сессии
Запускается ОДИН РАЗ локально перед деплоем на сервер.

Создаёт:
  1. Файл moodex_session.session (для локального запуска)
  2. Строку TELEGRAM_STRING_SESSION (для Coolify/Docker)

Запуск:
    python scripts/auth_telegram.py
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from config.settings import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE, TELEGRAM_SESSION


async def auth():
    print("\n" + "═" * 55)
    print("  MOODEX — Авторизация в Telegram")
    print("═" * 55)

    if not TELEGRAM_API_ID or TELEGRAM_API_ID == 0:
        print("\n❌ TELEGRAM_API_ID не заполнен в .env файле!")
        return

    print(f"\n📱 Телефон: {TELEGRAM_PHONE}")
    print(f"🔑 API ID:  {TELEGRAM_API_ID}\n")

    # Авторизуемся через StringSession — сразу получим строку для Coolify
    client = TelegramClient(StringSession(), TELEGRAM_API_ID, TELEGRAM_API_HASH)

    try:
        await client.connect()

        if not await client.is_user_authorized():
            print("📲 Отправляем код подтверждения в Telegram...")
            await client.send_code_request(TELEGRAM_PHONE)

            code = input("📟 Введи код из Telegram: ").strip()

            try:
                await client.sign_in(TELEGRAM_PHONE, code)
            except SessionPasswordNeededError:
                password = input("🔒 Введи пароль двухфакторной аутентификации: ").strip()
                await client.sign_in(password=password)

        me = await client.get_me()
        print(f"\n✅ Авторизован: {me.first_name} {me.last_name or ''} (@{me.username or 'нет username'})")

        # Получаем строковую сессию
        string_session = client.session.save()

        print("\n" + "═" * 55)
        print("  📋 СТРОКОВАЯ СЕССИЯ ДЛЯ COOLIFY:")
        print("═" * 55)
        print(f"\nTELEGRAM_STRING_SESSION={string_session}\n")
        print("═" * 55)
        print("\n👆 Скопируй эту строку и добавь в переменные окружения Coolify")
        print("   (Environment Variables в настройках приложения)\n")

        # Также сохраняем в файл для локального использования
        with open(".string_session.txt", "w") as f:
            f.write(f"TELEGRAM_STRING_SESSION={string_session}\n")
        print("💾 Строка сессии также сохранена в .string_session.txt")

        # Проверяем каналы
        from config.settings import TELEGRAM_CHANNELS
        print(f"\n🔍 Проверяем доступность {len(TELEGRAM_CHANNELS)} каналов...\n")

        ok, fail = [], []
        for channel in TELEGRAM_CHANNELS:
            try:
                entity = await client.get_entity(channel)
                title = getattr(entity, 'title', channel)
                ok.append(f"  ✅ @{channel:<25} {title}")
            except Exception as e:
                fail.append(f"  ❌ @{channel:<25} {e}")

        for line in ok:
            print(line)
        for line in fail:
            print(line)

        print(f"\n  Доступно: {len(ok)} / Недоступно: {len(fail)}")

        if fail:
            print("\n  ⚠️  Для недоступных каналов — вступи в них вручную")
            print("     через приложение Telegram и перезапусти этот скрипт.")

        print(f"\n🚀 Готово! Теперь добавь TELEGRAM_STRING_SESSION в Coolify и деплой.")

    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(auth())
