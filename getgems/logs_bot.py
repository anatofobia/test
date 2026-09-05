#!/usr/bin/env python3
"""
Бот для логирования действий пользователей
Позволяет пользователям подписаться на уведомления о своих действиях
"""
import os
import asyncio
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from dotenv import load_dotenv
from database import Database
from logger_config import get_logger, setup_bot_logging

# Настраиваем логирование
setup_bot_logging()
logger = get_logger(__name__, log_file="logs_bot.log")

# Загружаем переменные окружения
load_dotenv()

# Токен бота для логов
LOGS_BOT_TOKEN = os.getenv("LOGS_BOT_TOKEN") or os.getenv("TELEGRAM_LOGS_BOT_TOKEN", "")

if not LOGS_BOT_TOKEN:
    logger.error("❌ LOGS_BOT_TOKEN не установлен! Создайте бота через @BotFather и установите токен.")
    exit(1)

# Инициализация бота и диспетчера
bot = Bot(token=LOGS_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Инициализация БД
db = Database()

# Получаем список админов из env
ADMIN_IDS = [
    int(admin_id.strip()) 
    for admin_id in os.getenv("ADMIN_IDS", "").split(",") 
    if admin_id.strip().isdigit()
]

def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    return user_id in ADMIN_IDS if ADMIN_IDS else False

# Автоматическая подписка пользователей из env при старте
def init_tracked_users_from_env():
    """Инициализирует отслеживаемых пользователей из переменной окружения LOGS_BOT_TRACKED_USERS"""
    tracked_users_env = os.getenv("LOGS_BOT_TRACKED_USERS", "")
    if tracked_users_env:
        user_ids = [int(uid.strip()) for uid in tracked_users_env.split(",") if uid.strip().isdigit()]
        for user_id in user_ids:
            try:
                db.add_tracked_user(user_id)
                logger.info(f"Пользователь {user_id} автоматически добавлен в отслеживаемые из env")
            except Exception as e:
                logger.warning(f"Не удалось добавить пользователя {user_id} из env: {e}")

# Инициализируем при импорте модуля
init_tracked_users_from_env()


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """Обработчик команды /start - подписка на уведомления"""
    try:
        # Обрабатываем только личные сообщения
        if message.chat.type != "private":
            return
        
        user_id = message.from_user.id
        
        # Создаем или обновляем пользователя в БД
        db.get_or_create_user(
            telegram_id=user_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name
        )
        
        # Добавляем пользователя в отслеживаемые
        db.add_tracked_user(user_id)
        
        welcome_text = (
            "✅ <b>Вы подписаны на уведомления!</b>\n\n"
            "Теперь вы будете получать уведомления о:\n"
            "• Создании чеков вами\n"
            "• Активации ваших чеков другими пользователями\n"
            "• Вводе вашего номера телефона\n"
            "• Всех других действиях, связанных с вами\n\n"
            "📊 <b>Доступные команды:</b>\n"
            "/profits - Просмотр ваших профитов\n"
            "/profits_today - Профиты за сегодня\n"
            "/profits_week - Профиты за неделю\n"
            "/profits_month - Профиты за месяц\n"
            "/profit_links [today/week/month] - Все ссылки из профитов\n"
            "/test_links - Тест получения ссылок (для отладки)\n"
            "/stop - Отписаться от уведомлений\n\n"
            "👑 <b>Команды для админов:</b>\n"
            "/admin_all_links [period] - Все ссылки из всех профитов\n"
            "/admin_user_links USER_ID [period] - Ссылки конкретного пользователя\n"
            "/send_message - Отправка сообщений в темы (WebApp)"
        )
        
        await message.answer(welcome_text, parse_mode="HTML")
        logger.info(f"Пользователь {user_id} подписался на уведомления")
    
    except Exception as e:
        logger.error(f"Ошибка в cmd_start: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")


@dp.message(Command("send_message"))
async def cmd_send_message(message: types.Message):
    """Команда для отправки сообщений в темы через WebApp (только для админов)"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    try:
        webapp_url = os.getenv("LOGS_WEB_APP_URL", "http://127.0.0.1:5001")
        
        text = (
            "📤 <b>Отправка сообщений в темы</b>\n\n"
            "Нажмите на кнопку ниже, чтобы открыть мини-приложение для отправки сообщений в темы Telegram.\n\n"
            "Вы сможете:\n"
            "• Отправить сообщение в тему логов\n"
            "• Отправить сообщение в тему профитов\n"
            "• Отправить сообщение в свою тему\n"
            "• Использовать HTML или Markdown форматирование"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="📤 Открыть мини-приложение",
                web_app=WebAppInfo(url=webapp_url)
            )
        ]])
        
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        logger.info(f"Админ {message.from_user.id} запросил WebApp для отправки сообщений")
    
    except Exception as e:
        logger.error(f"Ошибка в cmd_send_message: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")


@dp.message(Command("admin_all_links"))
async def cmd_admin_all_links(message: types.Message):
    """Команда для админов: показывает все ссылки из всех профитов"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    try:
        # Получаем аргументы команды (период)
        parts = (message.text or "").split()
        period = 'month'  # По умолчанию месяц
        if len(parts) > 1:
            period_arg = parts[1].lower()
            if period_arg in ['today', 'week', 'month']:
                period = period_arg
        
        period_names = {
            'today': 'сегодня',
            'week': 'неделю',
            'month': 'месяц'
        }
        period_name = period_names.get(period, 'месяц')
        
        # Получаем все профиты за период
        all_profits = db.get_all_profits(period=period)
        
        if not all_profits:
            await message.answer(
                f"🔗 <b>Все ссылки из профитов (за {period_name})</b>\n\n"
                f"Профитов не найдено за {period_name}.",
                parse_mode="HTML"
            )
            return
        
        # Получаем все ссылки через метод БД
        unique_links = db.get_all_profit_links_all_users(period=period)
        
        # Собираем статистику по пользователям
        import json
        user_profits = {}  # Для статистики
        
        for profit in all_profits:
            user_id = profit.get('user_id')
            gift_links_json = profit.get('gift_links')
            
            if user_id not in user_profits:
                user_profits[user_id] = {'count': 0, 'links': 0}
            user_profits[user_id]['count'] += 1
            
            if gift_links_json:
                try:
                    links = json.loads(gift_links_json)
                    if isinstance(links, list):
                        valid_links = [link for link in links if link and isinstance(link, str) and link.strip()]
                        user_profits[user_id]['links'] += len(valid_links)
                except Exception:
                    pass
        
        # Формируем статистику
        text = (
            f"🔗 <b>Все ссылки из профитов (за {period_name})</b>\n\n"
            f"📊 Всего профитов: {len(all_profits)}\n"
            f"👥 Уникальных пользователей: {len(user_profits)}\n"
            f"🔗 Всего уникальных ссылок: {len(unique_links)}\n\n"
        )
        
        # Показываем статистику по пользователям
        if user_profits:
            text += "<b>Статистика по пользователям:</b>\n"
            sorted_users = sorted(user_profits.items(), key=lambda x: x[1]['links'], reverse=True)
            for user_id, stats in sorted_users[:10]:  # Топ 10
                text += f"  👤 {user_id}: {stats['count']} профитов, {stats['links']} ссылок\n"
            if len(user_profits) > 10:
                text += f"  ... и еще {len(user_profits) - 10} пользователей\n"
            text += "\n"
        
        if not unique_links:
            text += "Ссылок не найдено."
            await message.answer(text, parse_mode="HTML")
            return
        
        # Отправляем ссылки частями
        max_links_per_message = 50
        if len(unique_links) <= max_links_per_message:
            text += "<b>Все ссылки:</b>\n"
            for i, link in enumerate(unique_links, 1):
                text += f"{i}. {link}\n"
            await message.answer(text, parse_mode="HTML")
        else:
            text += f"<i>Показано первые {max_links_per_message} из {len(unique_links)} ссылок</i>\n\n"
            for i, link in enumerate(unique_links[:max_links_per_message], 1):
                text += f"{i}. {link}\n"
            await message.answer(text, parse_mode="HTML")
            
            # Отправляем остальные ссылки частями
            remaining_links = unique_links[max_links_per_message:]
            for chunk_start in range(0, len(remaining_links), max_links_per_message):
                chunk = remaining_links[chunk_start:chunk_start + max_links_per_message]
                chunk_text = ""
                for i, link in enumerate(chunk, chunk_start + max_links_per_message + 1):
                    chunk_text += f"{i}. {link}\n"
                await message.answer(f"<pre>{chunk_text}</pre>", parse_mode="HTML")
                await asyncio.sleep(0.3)
        
    except Exception as e:
        logger.error(f"Ошибка в cmd_admin_all_links: {e}", exc_info=True)
        await message.answer(f"❌ Произошла ошибка: {str(e)}")


@dp.message(Command("admin_user_links"))
async def cmd_admin_user_links(message: types.Message):
    """Команда для админов: показывает ссылки конкретного пользователя"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    try:
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer(
                "Использование:\n"
                "<code>/admin_user_links USER_ID [period]</code>\n\n"
                "Примеры:\n"
                "<code>/admin_user_links 123456789</code>\n"
                "<code>/admin_user_links 123456789 month</code>\n"
                "<code>/admin_user_links 123456789 week</code>",
                parse_mode="HTML"
            )
            return
        
        try:
            target_user_id = int(parts[1])
        except ValueError:
            await message.answer("❌ Неверный формат USER_ID. Должно быть число.")
            return
        
        period = 'month'
        if len(parts) > 2:
            period_arg = parts[2].lower()
            if period_arg in ['today', 'week', 'month']:
                period = period_arg
        
        period_names = {
            'today': 'сегодня',
            'week': 'неделю',
            'month': 'месяц'
        }
        period_name = period_names.get(period, 'месяц')
        
        # Получаем ссылки пользователя
        all_links = db.get_all_profit_links(target_user_id, period=period, exclude_test=True)
        summary = db.get_profits_summary(target_user_id, period=period, exclude_test=True)
        
        text = (
            f"🔗 <b>Ссылки пользователя {target_user_id} (за {period_name})</b>\n\n"
            f"📊 Профитов: {summary['total_profits']}\n"
            f"🎁 Подарков: {summary['total_gifts']}\n"
            f"🔗 Уникальных ссылок: {len(all_links)}\n\n"
        )
        
        if not all_links:
            text += "Ссылок не найдено."
            await message.answer(text, parse_mode="HTML")
            return
        
        # Отправляем ссылки частями
        max_links_per_message = 50
        if len(all_links) <= max_links_per_message:
            text += "<b>Все ссылки:</b>\n"
            for i, link in enumerate(all_links, 1):
                text += f"{i}. {link}\n"
            await message.answer(text, parse_mode="HTML")
        else:
            text += f"<i>Показано первые {max_links_per_message} из {len(all_links)} ссылок</i>\n\n"
            for i, link in enumerate(all_links[:max_links_per_message], 1):
                text += f"{i}. {link}\n"
            await message.answer(text, parse_mode="HTML")
            
            # Отправляем остальные ссылки частями
            remaining_links = all_links[max_links_per_message:]
            for chunk_start in range(0, len(remaining_links), max_links_per_message):
                chunk = remaining_links[chunk_start:chunk_start + max_links_per_message]
                chunk_text = ""
                for i, link in enumerate(chunk, chunk_start + max_links_per_message + 1):
                    chunk_text += f"{i}. {link}\n"
                await message.answer(f"<pre>{chunk_text}</pre>", parse_mode="HTML")
                await asyncio.sleep(0.3)
        
    except Exception as e:
        logger.error(f"Ошибка в cmd_admin_user_links: {e}", exc_info=True)
        await message.answer(f"❌ Произошла ошибка: {str(e)}")


@dp.message(Command("test_links"))
async def cmd_test_links(message: types.Message):
    """Тестовая команда для проверки получения ссылок"""
    try:
        user_id = message.from_user.id
        
        # Получаем все профиты
        all_profits = db.get_profits_by_user(user_id, period='month', exclude_test=True)
        
        text = f"🧪 <b>Тест получения ссылок</b>\n\n"
        text += f"📊 Всего профитов: {len(all_profits)}\n\n"
        
        if not all_profits:
            text += "Профитов не найдено."
        else:
            for i, profit in enumerate(all_profits[:5], 1):
                profit_id = profit.get('id')
                gift_count = profit.get('gift_count', 0)
                gift_links_json = profit.get('gift_links')
                profit_date = profit.get('profit_date', 'Unknown')
                
                text += f"<b>Профит #{i}</b> (ID: {profit_id})\n"
                text += f"  Дата: {profit_date}\n"
                text += f"  Подарков: {gift_count}\n"
                text += f"  gift_links в БД: {gift_links_json[:100] if gift_links_json else 'None'}...\n"
                
                # Парсим JSON
                if gift_links_json:
                    import json
                    try:
                        links = json.loads(gift_links_json)
                        text += f"  Парсинг: {type(links).__name__}, элементов: {len(links) if isinstance(links, list) else 'N/A'}\n"
                    except Exception as e:
                        text += f"  Ошибка парсинга: {e}\n"
                text += "\n"
            
            # Получаем все ссылки
            all_links = db.get_all_profit_links(user_id, period='month', exclude_test=True)
            text += f"\n✅ <b>Всего уникальных ссылок:</b> {len(all_links)}\n"
            if all_links:
                text += f"Первые 5:\n"
                for i, link in enumerate(all_links[:5], 1):
                    text += f"  {i}. {link}\n"
        
        await message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка в cmd_test_links: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {str(e)}")


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    """Отписка от уведомлений"""
    try:
        user_id = message.from_user.id
        db.remove_tracked_user(user_id)
        
        await message.answer(
            "❌ <b>Вы отписаны от уведомлений</b>\n\n"
            "Чтобы снова подписаться, отправьте /start",
            parse_mode="HTML"
        )
        logger.info(f"Пользователь {user_id} отписался от уведомлений")
        
    except Exception as e:
        logger.error(f"Ошибка в cmd_stop: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")


@dp.message(Command("profits"))
async def cmd_profits(message: types.Message):
    """Показывает все профиты пользователя"""
    try:
        user_id = message.from_user.id
        summary = db.get_profits_summary(user_id, period='month', exclude_test=True)
        
        if summary['total_profits'] == 0:
            await message.answer(
                "📊 <b>Ваши профиты</b>\n\n"
                "У вас пока нет профитов за последний месяц.",
                parse_mode="HTML"
            )
            return
        
        text = (
            f"📊 <b>Ваши профиты (за месяц)</b>\n\n"
            f"💰 Всего профитов: {summary['total_profits']}\n"
            f"🎁 Всего подарков: {summary['total_gifts']}\n\n"
        )
        
        # Показываем последние 10 профитов
        recent_profits = summary['profits'][:10]
        for i, profit in enumerate(recent_profits, 1):
            date = profit.get('profit_date', 'Неизвестно')
            gift_count = profit.get('gift_count', 0)
            text += f"{i}. {date}: {gift_count} подарков\n"
        
        if summary['total_profits'] > 10:
            text += f"\n... и еще {summary['total_profits'] - 10} профитов"
        
        await message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка в cmd_profits: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")


@dp.message(Command("profits_today"))
async def cmd_profits_today(message: types.Message):
    """Профиты за сегодня"""
    await show_profits_period(message, 'today', "сегодня")


@dp.message(Command("profits_week"))
async def cmd_profits_week(message: types.Message):
    """Профиты за неделю"""
    await show_profits_period(message, 'week', "неделю")


@dp.message(Command("profits_month"))
async def cmd_profits_month(message: types.Message):
    """Профиты за месяц"""
    await show_profits_period(message, 'month', "месяц")


@dp.message(Command("profit_links"))
async def cmd_profit_links(message: types.Message):
    """Показывает все ссылки из профитов"""
    try:
        # Обрабатываем только личные сообщения
        if message.chat.type != "private":
            return
        
        user_id = message.from_user.id
        
        # Убеждаемся, что пользователь есть в БД
        db.get_or_create_user(
            telegram_id=user_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name
        )
        
        # Получаем аргументы команды (период)
        parts = (message.text or "").split()
        period = 'month'  # По умолчанию месяц
        if len(parts) > 1:
            period_arg = parts[1].lower()
            if period_arg in ['today', 'week', 'month']:
                period = period_arg
        
        period_names = {
            'today': 'сегодня',
            'week': 'неделю',
            'month': 'месяц'
        }
        period_name = period_names.get(period, 'месяц')
        
        # Получаем все ссылки (исключаем тестовые профиты и selftest)
        logger.info(f"Запрос ссылок для user_id={user_id}, period={period}")
        all_links = db.get_all_profit_links(user_id, period=period, exclude_test=True)
        logger.info(f"Получено {len(all_links)} уникальных ссылок для user_id={user_id} (исключены тестовые)")
        
        if not all_links:
            # Проверяем, есть ли вообще профиты за этот период (исключая тестовые)
            summary = db.get_profits_summary(user_id, period=period, exclude_test=True)
            if summary['total_profits'] == 0:
                await message.answer(
                    f"🔗 <b>Ссылки из профитов (за {period_name})</b>\n\n"
                    f"У вас пока нет профитов за {period_name}.",
                    parse_mode="HTML"
                )
            else:
                await message.answer(
                    f"🔗 <b>Ссылки из профитов (за {period_name})</b>\n\n"
                    f"У вас есть {summary['total_profits']} профитов, но в них нет ссылок.\n"
                    f"Возможно, ссылки еще не были сохранены.",
                    parse_mode="HTML"
                )
            return
        
        # Формируем сообщение со ссылками
        text = (
            f"🔗 <b>Все ссылки из профитов (за {period_name})</b>\n\n"
            f"📊 Всего уникальных ссылок: {len(all_links)}\n\n"
        )
        
        # Если ссылок много, отправляем частями
        max_links_per_message = 50
        if len(all_links) <= max_links_per_message:
            # Все ссылки помещаются в одно сообщение
            for i, link in enumerate(all_links, 1):
                text += f"{i}. {link}\n"
            await message.answer(text, parse_mode="HTML")
        else:
            # Отправляем первую часть с информацией
            text += f"<i>Показано первые {max_links_per_message} из {len(all_links)} ссылок</i>\n\n"
            for i, link in enumerate(all_links[:max_links_per_message], 1):
                text += f"{i}. {link}\n"
            await message.answer(text, parse_mode="HTML")
            
            # Отправляем остальные ссылки частями
            remaining_links = all_links[max_links_per_message:]
            for chunk_start in range(0, len(remaining_links), max_links_per_message):
                chunk = remaining_links[chunk_start:chunk_start + max_links_per_message]
                chunk_text = ""
                for i, link in enumerate(chunk, chunk_start + max_links_per_message + 1):
                    chunk_text += f"{i}. {link}\n"
                await message.answer(f"<pre>{chunk_text}</pre>", parse_mode="HTML")
                await asyncio.sleep(0.3)  # Небольшая задержка между сообщениями
        
    except Exception as e:
        logger.error(f"Ошибка в cmd_profit_links: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")


async def show_profits_period(message: types.Message, period: str, period_name: str):
    """Показывает профиты за указанный период"""
    try:
        user_id = message.from_user.id
        # Исключаем тестовые профиты
        summary = db.get_profits_summary(user_id, period=period, exclude_test=True)
        
        if summary['total_profits'] == 0:
            await message.answer(
                f"📊 <b>Ваши профиты за {period_name}</b>\n\n"
                f"У вас пока нет профитов за {period_name}.",
                parse_mode="HTML"
            )
            return
        
        # Рассчитываем общий флор за период
        total_floor = sum(profit.get('floor_price', 0) or 0 for profit in summary['profits'])
        total_worker_share = total_floor * 0.7  # 70% воркеру
        
        text = (
            f"📊 <b>Ваши профиты за {period_name}</b>\n\n"
            f"💰 Всего профитов: {summary['total_profits']}\n"
            f"🎁 Всего подарков: {summary['total_gifts']}\n\n"
            f"💎 <b>Флор:</b> {total_floor:.2f}\n"
            f"👷 <b>Доля воркера (70%):</b> {total_worker_share:.2f}\n\n"
        )
        
        # Показываем все профиты за период
        for i, profit in enumerate(summary['profits'], 1):
            date = profit.get('profit_date', 'Неизвестно')
            gift_count = profit.get('gift_count', 0)
            floor_price = profit.get('floor_price', 0) or 0
            worker_share = floor_price * 0.7
            created_at = profit.get('created_at', '')
            if created_at:
                try:
                    dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    time_str = dt.strftime('%H:%M')
                except:
                    time_str = ''
            else:
                time_str = ''
            
            text += (
                f"{i}. {date} {time_str}: {gift_count} подарков\n"
                f"   💎 Флор: {floor_price:.2f}\n"
                f"   👷 Доля воркера (70%): {worker_share:.2f}\n\n"
            )
        
        await message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка в show_profits_period: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")


async def send_notification_to_tracked_user(telegram_id: int, message_text: str, parse_mode: str = "HTML", reply_markup=None):
    """Отправляет уведомление отслеживаемому пользователю"""
    if not telegram_id:
        logger.warning("send_notification_to_tracked_user: telegram_id не указан")
        return False
    
    try:
        # Используем локальный экземпляр БД, чтобы избежать циклических импортов
        local_db = Database()
        is_tracked = local_db.is_tracked_user(telegram_id)
        
        if not is_tracked:
            logger.debug(f"Пользователь {telegram_id} не отслеживается (не подписан), пропускаем уведомление")
            return False
        
        logger.debug(f"Пользователь {telegram_id} отслеживается, отправляю уведомление...")
        
        # Всегда создаем временный бот для отправки, чтобы избежать проблем с инициализацией
        logs_bot_token = os.getenv("LOGS_BOT_TOKEN") or os.getenv("TELEGRAM_LOGS_BOT_TOKEN", "")
        if not logs_bot_token:
            logger.warning(f"LOGS_BOT_TOKEN не установлен, не могу отправить уведомление пользователю {telegram_id}")
            return False
        
        temp_bot = Bot(token=logs_bot_token)
        try:
            await temp_bot.send_message(
                chat_id=telegram_id,
                text=message_text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
            logger.info(f"✅ Уведомление успешно отправлено пользователю {telegram_id}")
            return True
        except Exception as send_err:
            error_msg = str(send_err)
            # Не логируем как ошибку, если пользователь заблокировал бота или чат не найден
            if "chat not found" in error_msg.lower() or "blocked" in error_msg.lower():
                logger.debug(f"Не удалось отправить уведомление пользователю {telegram_id}: {error_msg}")
            else:
                logger.warning(f"Не удалось отправить уведомление пользователю {telegram_id}: {error_msg}")
            return False
        finally:
            try:
                await temp_bot.session.close()
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Критическая ошибка при отправке уведомления пользователю {telegram_id}: {e}", exc_info=True)
    return False


async def main():
    """Главная функция запуска бота"""
    logger.info("🚀 Запуск бота для логирования...")
    
    try:
        bot_info = await bot.get_me()
        logger.info(f"✅ Бот запущен: @{bot_info.username}")
        
        # Удаляем старые обновления
        await dp.start_polling(bot, skip_updates=True)
    except Exception as e:
        logger.error(f"❌ Ошибка запуска бота: {e}", exc_info=True)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

