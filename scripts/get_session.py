"""
Запусти этот файл локально на своём компьютере:
    pip install telethon
    python scripts/get_session.py

Получишь строку TELEGRAM_STRING_SESSION для вставки в Coolify.
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = 23095914
API_HASH = "66d66f87260087b7178ded16ee1f140a"
PHONE = "+79399314905"

async def main():
    print("\n=== MOODEX — Генерация сессии ===\n")
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.start(phone=PHONE)
    session_string = client.session.save()
    print("\n✅ ГОТОВО! Скопируй эту строку в Coolify:\n")
    print(f"TELEGRAM_STRING_SESSION={session_string}\n")
    with open("session_string.txt", "w") as f:
        f.write(session_string)
    print("📄 Также сохранено в файл session_string.txt")
    await client.disconnect()

asyncio.run(main())
