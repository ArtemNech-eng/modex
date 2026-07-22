"""
Запусти этот файл в терминале Coolify:
    cd /app && python3 scripts/get_session.py
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = 23095914
API_HASH = "66d66f87260087b7178ded16ee1f140a"
PHONE = "+79399314905"

# Публичные SOCKS5 прокси для обхода блокировки Telegram
# Telethon поддерживает прокси нативно
PROXIES = [
    ("91.108.56.100", 443),   # Telegram DC1
    ("149.154.167.51", 443),  # Telegram DC2
]

async def main():
    print("\n=== MOODEX — Генерация сессии ===\n")

    # Пробуем подключиться через socks5 прокси
    import socks

    proxy = (socks.SOCKS5, "proxy.socks5.net", 1080)

    client = TelegramClient(
        StringSession(),
        API_ID,
        API_HASH,
        connection_retries=5,
        timeout=30,
        # proxy=proxy  # раскомментировать если нужен прокси
    )

    print("Подключаемся к Telegram...")
    await client.start(phone=PHONE)

    session_string = client.session.save()

    print("\n✅ ГОТОВО! Скопируй эту строку в Coolify Environment Variables:\n")
    print(f"TELEGRAM_STRING_SESSION={session_string}\n")

    with open("/app/session_string.txt", "w") as f:
        f.write(session_string)
    print("📄 Также сохранено в /app/session_string.txt")

    await client.disconnect()

asyncio.run(main())
