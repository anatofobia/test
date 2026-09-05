#!/usr/bin/env python3
"""
Скрипт запуска Telegram бота
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot import run_bot
from config import Config

if __name__ == '__main__':
    if not Config.BOT_TOKEN:
        print("❌ BOT_TOKEN не установлен в .env файле")
        print("Создайте .env файл с BOT_TOKEN=your_bot_token")
        sys.exit(1)
    
    print("🤖 Запуск Telegram бота...")
    print(f"📱 Mini App URL: {Config.MINI_APP_URL}")
    if Config.MINI_APP_URL == "https://your-domain.com":
        print("⚠️  ВНИМАНИЕ: MINI_APP_URL не настроен!")
        print("   Установите правильный URL в backend/.env")
        print("   Для ngrok используйте: python update_bot_url.py 5173")
    
    run_bot()

