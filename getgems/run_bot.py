#!/usr/bin/env python3
"""
Скрипт для запуска бота с указанным токеном
Позволяет запускать несколько экземпляров бота одновременно
"""
import os
import sys
import asyncio
import logging
from pathlib import Path

# Добавляем текущую директорию в путь
sys.path.insert(0, str(Path(__file__).parent))

# Получаем токен из переменной окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_INSTANCE_NAME = os.getenv("BOT_INSTANCE_NAME", "default")

if not BOT_TOKEN:
    print("❌ Ошибка: BOT_TOKEN не установлен!")
    print("Использование: BOT_TOKEN=your_token BOT_INSTANCE_NAME=bot1 python3 run_bot.py")
    sys.exit(1)

# Устанавливаем токен в переменные окружения перед импортом config_bot
os.environ["BOT_TOKEN"] = BOT_TOKEN

# Импортируем после установки токена
from config_bot import BotConfig

# Обновляем конфиг с новым токеном ПЕРЕД импортом telegram_bot
BotConfig.BOT_TOKEN = BOT_TOKEN

# Теперь импортируем telegram_bot
import telegram_bot
from telegram_bot import main as bot_main, logger

# Пересоздаем бота с новым токеном (так как он создается при импорте модуля)
from aiogram import Bot
telegram_bot.bot = Bot(token=BOT_TOKEN)

# Убеждаемся что в telegram_bot используется новый бот
import telegram_bot as tb
if hasattr(tb, 'bot'):
    logger.info(f"✅ Бот переинициализирован с новым токеном")

# Настраиваем логирование для этого экземпляра
log_file = f"logs/bot_{BOT_INSTANCE_NAME}.log"
os.makedirs("logs", exist_ok=True)

# Добавляем handler для файла логов этого экземпляра
file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

logger.info(f"🚀 Запуск экземпляра бота: {BOT_INSTANCE_NAME}")
logger.info(f"📝 Логи будут записываться в: {log_file}")

if __name__ == "__main__":
    try:
        asyncio.run(bot_main())
    except KeyboardInterrupt:
        logger.info(f"⏹️ Остановка экземпляра бота: {BOT_INSTANCE_NAME}")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в экземпляре {BOT_INSTANCE_NAME}: {e}", exc_info=True)
        sys.exit(1)

