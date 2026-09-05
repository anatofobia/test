#!/usr/bin/env python3
"""
Бот для автоматической настройки других ботов
Просто отправьте токен бота, и он быстро настроит имя, описание и веб-апп
"""
import os
import re
import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SetMyName, SetMyDescription, SetMyShortDescription, SetChatMenuButton
from aiogram.types import MenuButtonWebApp, WebAppInfo
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения
load_dotenv()

# Токен бота для автоматической настройки
AUTO_CONFIG_BOT_TOKEN = os.getenv("AUTO_CONFIG_BOT_TOKEN", "")
ADMIN_IDS = [
    int(admin_id.strip()) 
    for admin_id in os.getenv("ADMIN_IDS", "").split(",") 
    if admin_id.strip().isdigit()
]

if not AUTO_CONFIG_BOT_TOKEN:
    logger.error("❌ AUTO_CONFIG_BOT_TOKEN не установлен! Создайте бота через @BotFather и установите токен.")
    exit(1)

if not ADMIN_IDS:
    logger.warning("⚠️ ADMIN_IDS не установлен. Бот будет доступен всем пользователям.")

# Инициализация бота и диспетчера
bot = Bot(token=AUTO_CONFIG_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class ConfigStates(StatesGroup):
    waiting_for_token = State()


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    return user_id in ADMIN_IDS if ADMIN_IDS else True


async def configure_bot(token: str) -> dict:
    """
    Автоматически настраивает бота через Telegram Bot API
    
    Args:
        token: Токен бота для настройки
        
    Returns:
        dict с результатами настройки
    """
    results = {
        'username': '',
        'name': False,
        'description': False,
        'short_description': False,
        'menu_button': False,
        'error': None
    }
    
    try:
        config_bot = Bot(token=token)
        
        # Получаем информацию о боте
        try:
            bot_info = await config_bot.get_me()
            results['username'] = bot_info.username or ""
        except Exception as e:
            results['error'] = f"Не удалось получить информацию о боте: {e}"
            await config_bot.session.close()
            return results
        
        # 1. Устанавливаем имя бота
        try:
            result = await config_bot(SetMyName(name="GetGems: sell and buy NFT", language_code="en"))
            if result:
                results['name'] = True
                logger.info("✅ Имя бота установлено: GetGems: sell and buy NFT")
            else:
                logger.warning("⚠️ Установка имени бота вернула False")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось установить имя бота: {e}", exc_info=True)
        
        # Небольшая задержка между запросами
        await asyncio.sleep(0.5)
        
        # 2. Устанавливаем описание (about)
        try:
            result = await config_bot(SetMyDescription(
                description="Trade gifts and stickers on GetGems! Lowest fees, secure wallet integration.",
                language_code="en"
            ))
            if result:
                results['description'] = True
                logger.info("✅ Описание бота установлено")
            else:
                logger.warning("⚠️ Установка описания бота вернула False")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось установить описание бота: {e}", exc_info=True)
        
        await asyncio.sleep(0.5)
        
        # 3. Устанавливаем короткое описание
        try:
            result = await config_bot(SetMyShortDescription(
                short_description="Trade gifts and stickers on GetGems! Lowest fees, secure wallet integration.",
                language_code="en"
            ))
            if result:
                results['short_description'] = True
                logger.info("✅ Короткое описание бота установлено")
            else:
                logger.warning("⚠️ Установка короткого описания бота вернула False")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось установить короткое описание бота: {e}", exc_info=True)
        
        await asyncio.sleep(0.5)
        
        # 4. Устанавливаем кнопку меню с веб-апп
        try:
            webapp_url = "https://getgems.mooo.com"
            menu_button = MenuButtonWebApp(text="Open GetGems", web_app=WebAppInfo(url=webapp_url))
            result = await config_bot(SetChatMenuButton(menu_button=menu_button))
            if result:
                results['menu_button'] = True
                logger.info(f"✅ Кнопка меню с веб-апп установлена: {webapp_url}")
            else:
                logger.warning("⚠️ Установка кнопки меню вернула False")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось установить кнопку меню: {e}", exc_info=True)
        
        await config_bot.session.close()
        
    except Exception as e:
        logger.error(f"❌ Ошибка автоматической настройки бота: {e}", exc_info=True)
        results['error'] = str(e)
    
    return results


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этому боту.")
        return
    
    help_text = (
        "⚙️ <b>Бот для автоматической настройки других ботов</b>\n\n"
        "📝 <b>Как использовать:</b>\n"
        "1. Отправьте команду /config\n"
        "2. Отправьте токен бота, который нужно настроить\n"
        "3. Бот автоматически настроит:\n"
        "   • Имя: GetGems: sell and buy NFT\n"
        "   • Описание: Trade gifts and stickers on GetGems! Lowest fees, secure wallet integration.\n"
        "   • Веб-апп кнопка: https://getgems.mooo.com\n\n"
        "⚠️ <b>Инлайн режим</b> нужно включить вручную через @BotFather командой /setinline"
    )
    await message.answer(help_text, parse_mode="HTML")


@dp.message(Command("config"))
async def cmd_config(message: types.Message, state: FSMContext):
    """Начинает процесс настройки бота"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этому боту.")
        return
    
    await message.answer(
        "📝 Отправьте токен бота, который нужно настроить.\n"
        "Формат: <code>1234567890:ABCdefGHIjklMNOpqrsTUVwxyz</code>\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    await state.set_state(ConfigStates.waiting_for_token)


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    """Отменяет текущую операцию"""
    await state.clear()
    await message.answer("❌ Операция отменена.")


@dp.message(ConfigStates.waiting_for_token)
async def process_token(message: types.Message, state: FSMContext):
    """Обрабатывает токен и настраивает бота"""
    token = message.text.strip()
    
    # Проверяем формат токена
    if not re.match(r'^\d+:[A-Za-z0-9_-]+$', token):
        await message.answer(
            "❌ Неверный формат токена!\n"
            "Правильный формат: <code>1234567890:ABCdefGHIjklMNOpqrsTUVwxyz</code>\n"
            "Попробуйте еще раз или отправьте /cancel",
            parse_mode="HTML"
        )
        return
    
    # Начинаем настройку
    await message.answer("⚙️ Настраиваю бота...")
    
    # Настраиваем бота
    results = await configure_bot(token)
    
    # Формируем отчет
    if results['error']:
        await message.answer(
            f"❌ <b>Ошибка настройки:</b>\n<code>{results['error']}</code>",
            parse_mode="HTML"
        )
        await state.clear()
        return
    
    report = "✅ <b>Настройка завершена!</b>\n\n"
    
    if results['username']:
        report += f"🆔 <b>Бот:</b> @{results['username']}\n\n"
    
    report += "📊 <b>Результаты:</b>\n"
    
    if results['name']:
        report += "✅ Имя: GetGems: sell and buy NFT\n"
    else:
        report += "❌ Имя: не установлено\n"
    
    if results['description']:
        report += "✅ Описание: установлено\n"
    else:
        report += "❌ Описание: не установлено\n"
    
    if results['short_description']:
        report += "✅ Короткое описание: установлено\n"
    else:
        report += "❌ Короткое описание: не установлено\n"
    
    if results['menu_button']:
        report += "✅ Веб-апп кнопка: https://getgems.mooo.com\n"
    else:
        report += "❌ Веб-апп кнопка: не установлена\n"
    
    report += "\n⚠️ <b>Инлайн режим:</b> Включите вручную через @BotFather:\n"
    report += "<code>/setinline</code>\n"
    if results['username']:
        report += f"Выберите бота @{results['username']} и включите инлайн режим."
    
    await message.answer(report, parse_mode="HTML")
    await state.clear()


async def main():
    """Главная функция запуска бота"""
    logger.info("🚀 Запуск бота для автоматической настройки...")
    
    try:
        bot_info = await bot.get_me()
        logger.info(f"✅ Бот запущен: @{bot_info.username}")
        
        # Удаляем старые обновления
        await dp.start_polling(bot, skip_updates=True)
    except Exception as e:
        logger.error(f"❌ Ошибка запуска бота: {e}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

