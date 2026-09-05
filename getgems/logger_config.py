"""
Централизованная конфигурация логирования для всего приложения
Скопировано и улучшено из backend
"""
import logging
import os
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Уровень логирования из переменной окружения или INFO по умолчанию
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = os.getenv("LOG_DIR", "logs")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", "10485760"))  # 10MB по умолчанию
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))

# Создаем директорию для логов
os.makedirs(LOG_DIR, exist_ok=True)

# Формат логов
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'

# Формат для файлов (более подробный)
FILE_LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - [%(funcName)s] - %(message)s'

# Формат для консоли (более компактный)
CONSOLE_LOG_FORMAT = '%(asctime)s - %(levelname)s - [%(name)s] - %(message)s'

# Цвета для консоли (опционально)
class ColoredFormatter(logging.Formatter):
    """Форматтер с цветами для консоли"""
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
    }
    RESET = '\033[0m'
    
    def format(self, record):
        log_color = self.COLORS.get(record.levelname, '')
        record.levelname = f"{log_color}{record.levelname}{self.RESET}"
        return super().format(record)

# Глобальный словарь для отслеживания уже созданных handlers
_configured_handlers = set()

def setup_logging(module_name: str = None, log_file: str = None):
    """
    Настраивает централизованное логирование для всего приложения.
    
    Args:
        module_name: Имя модуля (для отдельного файла логов)
        log_file: Имя файла лога (если не указано, используется общий getgems.log)
    """
    # Получаем root logger
    root_logger = logging.getLogger()
    
    # Устанавливаем уровень логирования
    log_level = getattr(logging, LOG_LEVEL, logging.INFO)
    root_logger.setLevel(log_level)
    
    # Определяем имя файла лога
    if log_file:
        log_filename = log_file
    elif module_name:
        log_filename = f"{module_name}.log"
    else:
        log_filename = "getgems.log"
    
    log_path = os.path.join(LOG_DIR, log_filename)
    log_path_abs = os.path.abspath(log_path)
    
    # Проверяем, не добавлен ли уже handler для этого файла
    handler_key = f"file_{log_path_abs}"
    if handler_key not in _configured_handlers:
        # Создаем файловый handler с ротацией по размеру
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding='utf-8'
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(
            logging.Formatter(FILE_LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
        )
        root_logger.addHandler(file_handler)
        _configured_handlers.add(handler_key)
    
    # Добавляем консольный handler для вывода в stdout (только один раз)
    if "console" not in _configured_handlers:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        
        # Используем цветной форматтер для консоли (если поддерживается)
        try:
            console_handler.setFormatter(
                ColoredFormatter(CONSOLE_LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
            )
        except:
            # Если цвета не поддерживаются, используем обычный форматтер
            console_handler.setFormatter(
                logging.Formatter(CONSOLE_LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
            )
        root_logger.addHandler(console_handler)
        _configured_handlers.add("console")
    
    # Настраиваем логирование для сторонних библиотек (только один раз)
    if "third_party" not in _configured_handlers:
        logging.getLogger('telethon').setLevel(logging.WARNING)
        logging.getLogger('pyrogram').setLevel(logging.WARNING)
        logging.getLogger('aiogram').setLevel(logging.WARNING)
        # Отключаем логи о повторных попытках обновления от aiogram.dispatcher
        logging.getLogger('aiogram.dispatcher').setLevel(logging.CRITICAL)
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        logging.getLogger('requests').setLevel(logging.WARNING)
        logging.getLogger('asyncio').setLevel(logging.WARNING)
        logging.getLogger('telegram').setLevel(logging.WARNING)
        logging.getLogger('httpcore').setLevel(logging.WARNING)
        logging.getLogger('httpx').setLevel(logging.WARNING)
        _configured_handlers.add("third_party")
    
    return root_logger

def get_logger(name: str, log_file: str = None) -> logging.Logger:
    """
    Получает logger с указанным именем.
    Используйте __name__ в качестве аргумента.
    
    Args:
        name: Имя модуля (обычно __name__)
        log_file: Имя файла лога (опционально, для отдельного файла)
        
    Returns:
        Настроенный logger
    """
    # Если указан отдельный файл, настраиваем логирование для него
    if log_file:
        module_name = os.path.basename(name).replace('.py', '')
        setup_logging(module_name=module_name, log_file=log_file)
    
    return logging.getLogger(name)

# Инициализируем базовое логирование при импорте модуля
setup_logging()

# Создаем отдельные логи для разных компонентов
def setup_app_logging():
    """Настройка логирования для Flask приложения"""
    setup_logging(module_name="app", log_file="app.log")

def setup_bot_logging():
    """Настройка логирования для Telegram бота"""
    setup_logging(module_name="bot", log_file="bot.log")

def setup_utils_logging():
    """Настройка логирования для утилит"""
    setup_logging(module_name="utils", log_file="utils.log")

def setup_gift_processor_logging():
    """Настройка логирования для обработчика подарков"""
    setup_logging(module_name="gift_processor", log_file="gift_processor.log")
