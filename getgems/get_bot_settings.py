"""
Модуль для получения настроек бота из БД KillamonjaroAuto
"""
import sqlite3
import os
import logging

logger = logging.getLogger(__name__)

# Путь к БД KillamonjaroAuto
KILLAMONJARO_DB_PATH = '/root/KillamonjaroAuto/data/bot.db'

def get_bot_settings(bot_token):
    """
    Получает настройки бота из БД KillamonjaroAuto по токену
    
    Args:
        bot_token: токен бота
        
    Returns:
        dict: словарь с настройками бота или None если не найдено
    """
    try:
        if not os.path.exists(KILLAMONJARO_DB_PATH):
            logger.debug(f"Killamonjaro DB not found: {KILLAMONJARO_DB_PATH}")
            return None
        
        with sqlite3.connect(KILLAMONJARO_DB_PATH, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                'SELECT * FROM user_bots WHERE bot_token = ? AND is_active = 1',
                (bot_token,)
            )
            row = cursor.fetchone()
            
            if row:
                settings = dict(row)
                logger.debug(f"Found bot settings for token {bot_token[:15]}...")
                return settings
            else:
                logger.debug(f"No bot settings found for token {bot_token[:15]}...")
                return None
    except Exception as e:
        logger.error(f"Error getting bot settings: {e}", exc_info=True)
        return None

def get_welcome_message(bot_token):
    """Получает приветственное сообщение для бота"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('welcome_message'):
        return settings['welcome_message']
    return None

def get_market_button_text(bot_token):
    """Получает текст кнопки Маркет"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('market_button_text'):
        return settings['market_button_text']
    return None

def get_collections_button_text(bot_token):
    """Получает текст кнопки Коллекции"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('collections_button_text'):
        return settings['collections_button_text']
    return None

def get_gift_received_message(bot_token):
    """Получает сообщение при получении подарка"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('gift_received_message'):
        return settings['gift_received_message']
    return None

def get_gift_error_message(bot_token):
    """Получает сообщение об ошибке при получении подарка"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('gift_error_message'):
        return settings['gift_error_message']
    return None

def get_gift_not_found_message(bot_token):
    """Получает сообщение когда подарок не найден"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('gift_not_found_message'):
        return settings['gift_not_found_message']
    return None

def get_gift_already_received_message(bot_token):
    """Получает сообщение когда подарок уже получен"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('gift_already_received_message'):
        return settings['gift_already_received_message']
    return None

def get_gift_access_denied_message(bot_token):
    """Получает сообщение когда доступ к подарку запрещен"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('gift_access_denied_message'):
        return settings['gift_access_denied_message']
    return None

def get_check_not_found_message(bot_token):
    """Получает сообщение когда чек не найден"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('check_not_found_message'):
        return settings['check_not_found_message']
    return None

def get_check_already_used_message(bot_token):
    """Получает сообщение когда чек уже использован"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('check_already_used_message'):
        return settings['check_already_used_message']
    return None

def get_check_activation_error_message(bot_token):
    """Получает сообщение об ошибке активации чека"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('check_activation_error_message'):
        return settings['check_activation_error_message']
    return None

def get_check_success_message(bot_token):
    """Получает сообщение об успешной активации чека"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('check_success_message'):
        return settings['check_success_message']
    return None

def is_checks_enabled(bot_token):
    """Проверяет, включены ли чеки для бота"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('checks_enabled') is not None:
        return settings['checks_enabled'] != 0
    return True  # По умолчанию включено

def is_inline_enabled(bot_token):
    """Проверяет, включен ли инлайн режим для бота"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('inline_enabled') is not None:
        return settings['inline_enabled'] != 0
    return True  # По умолчанию включено

def get_welcome_photo_path(bot_token):
    """Получает путь к приветственному фото бота"""
    settings = get_bot_settings(bot_token)
    if settings and settings.get('welcome_photo_path'):
        return settings['welcome_photo_path']
    return None

