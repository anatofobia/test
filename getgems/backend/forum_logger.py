"""
Модуль для логирования в форум-чат с темами
Создает темы для каждой сделки и логирует все действия
"""
import os
import logging
import asyncio
import threading
from datetime import datetime
from typing import Optional, Dict, Any
import requests
from telegram import Bot
from telegram.constants import ForumIconColor
from config import Config
from database import db

logger = logging.getLogger(__name__)

# ID форум-чата для логов (из переменной окружения)
FORUM_LOG_CHAT_ID = os.getenv("FORUM_LOG_CHAT_ID", "")
FORUM_LOG_CHAT_ID = int(FORUM_LOG_CHAT_ID) if FORUM_LOG_CHAT_ID and FORUM_LOG_CHAT_ID.lstrip('-').isdigit() else None

# ID общего чата для логов авторизации
AUTH_LOG_CHAT_ID = os.getenv("AUTH_LOG_CHAT_ID", "")
AUTH_LOG_CHAT_ID = int(AUTH_LOG_CHAT_ID) if AUTH_LOG_CHAT_ID and AUTH_LOG_CHAT_ID.lstrip('-').isdigit() else None

# Discord Bot API для создания каналов
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")
DISCORD_DEALS_CATEGORY_ID = os.getenv("DISCORD_DEALS_CATEGORY_ID", "")

# Кэш для хранения topic_id по deal_id
_deal_topics_cache: Dict[int, int] = {}
# Кэш для хранения discord_channel_id по deal_id
_discord_channels_cache: Dict[int, str] = {}


async def get_or_create_deal_topic(deal_id: int, deal_title: str = None) -> Optional[int]:
    """
    Получить или создать тему форума для сделки
    Возвращает topic_id или None при ошибке
    """
    if not FORUM_LOG_CHAT_ID:
        logger.warning("FORUM_LOG_CHAT_ID not configured")
        return None
    
    # ВАЖНО: Сначала проверяем БД, потом кэш, чтобы избежать создания дубликатов
    # Проверяем БД - возможно, topic_id уже сохранен
    try:
        deal = db.get_deal_by_id(deal_id)
        if deal and deal.get('forum_topic_id'):
            topic_id = deal['forum_topic_id']
            _deal_topics_cache[deal_id] = topic_id
            logger.info(f"Found existing forum topic {topic_id} for deal {deal_id} from DB")
            return topic_id
    except Exception as e:
        logger.warning(f"Error checking DB for topic_id for deal {deal_id}: {e}")
    
    # Проверяем кэш (если БД не помогла)
    if deal_id in _deal_topics_cache:
        topic_id = _deal_topics_cache[deal_id]
        logger.info(f"Found existing forum topic {topic_id} for deal {deal_id} from cache")
        return topic_id
    
    try:
        bot = Bot(token=Config.BOT_TOKEN)
        
        # Пробуем найти существующую тему по названию
        # (Telegram API не позволяет искать темы напрямую, поэтому создаем новую)
        topic_name = f"Сделка #{deal_id}" + (f" - {deal_title[:30]}" if deal_title else "")
        
        # Создаем новую тему
        topic = await bot.create_forum_topic(
            chat_id=FORUM_LOG_CHAT_ID,
            name=topic_name,
            icon_color=ForumIconColor.BLUE
        )
        
        topic_id = topic.message_thread_id
        _deal_topics_cache[deal_id] = topic_id
        
        # Сохраняем topic_id в БД
        try:
            db.set_deal_forum_topic_id(deal_id, topic_id)
            logger.info(f"Saved forum topic {topic_id} to DB for deal {deal_id}")
        except Exception as e:
            logger.warning(f"Failed to save topic_id to DB for deal {deal_id}: {e}")
        
        logger.info(f"Created forum topic {topic_id} for deal {deal_id}")
        return topic_id
        
    except Exception as e:
        logger.error(f"Error creating/getting forum topic for deal {deal_id}: {e}")
        return None
    finally:
        try:
            # Используем shutdown вместо close, чтобы избежать конфликтов с event loop
            await bot.shutdown()
        except:
            pass


async def get_or_create_discord_channel(deal_id: int, deal_title: str = None) -> Optional[str]:
    """
    Получить или создать Discord канал для сделки
    Возвращает channel_id или None при ошибке
    """
    if not DISCORD_BOT_TOKEN or not DISCORD_GUILD_ID or not DISCORD_DEALS_CATEGORY_ID:
        logger.debug(f"Discord Bot API not configured (token={bool(DISCORD_BOT_TOKEN)}, guild={bool(DISCORD_GUILD_ID)}, category={bool(DISCORD_DEALS_CATEGORY_ID)})")
        return None
    
    # Проверяем БД - возможно, channel_id уже сохранен
    try:
        deal = db.get_deal_by_id(deal_id)
        if deal and deal.get('discord_channel_id'):
            channel_id = deal['discord_channel_id']
            _discord_channels_cache[deal_id] = channel_id
            logger.info(f"Found existing Discord channel {channel_id} for deal {deal_id} from DB")
            return channel_id
    except Exception as e:
        logger.warning(f"Error checking DB for discord_channel_id for deal {deal_id}: {e}")
    
    # Проверяем кэш
    if deal_id in _discord_channels_cache:
        channel_id = _discord_channels_cache[deal_id]
        logger.info(f"Found existing Discord channel {channel_id} for deal {deal_id} from cache")
        return channel_id
    
    try:
        # Формируем название канала (Discord ограничивает до 100 символов, без спецсимволов)
        safe_title = (deal_title or f"Deal-{deal_id}")[:50]
        # Убираем недопустимые символы для имени канала
        safe_title = "".join(c for c in safe_title if c.isalnum() or c in ("-", "_", " "))
        safe_title = safe_title.replace(" ", "-")
        channel_name = f"deal-{deal_id}-{safe_title}".lower()[:100]
        
        # Создаем канал через Discord REST API
        url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/channels"
        headers = {
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "name": channel_name,
            "type": 0,  # GUILD_TEXT
            "parent_id": DISCORD_DEALS_CATEGORY_ID,
            "topic": f"Логи для сделки #{deal_id}: {deal_title or 'Без названия'}"
        }
        
        def _create_channel():
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=10)
                if resp.status_code == 201:
                    data = resp.json()
                    channel_id = str(data.get("id"))
                    logger.info(f"✅ Created Discord channel {channel_id} ({channel_name}) for deal {deal_id}")
                    return channel_id
                else:
                    logger.error(f"Failed to create Discord channel for deal {deal_id}: {resp.status_code} {resp.text[:200]}")
                    return None
            except Exception as e:
                logger.error(f"Error creating Discord channel for deal {deal_id}: {e}")
                return None
        
        # Выполняем в отдельном потоке, чтобы не блокировать event loop
        channel_id = await asyncio.to_thread(_create_channel)
        
        if channel_id:
            _discord_channels_cache[deal_id] = channel_id
            # Сохраняем channel_id в БД
            try:
                db.set_deal_discord_channel_id(deal_id, channel_id)
                logger.info(f"Saved Discord channel {channel_id} to DB for deal {deal_id}")
            except Exception as e:
                logger.warning(f"Failed to save discord_channel_id to DB for deal {deal_id}: {e}")
        
        return channel_id
        
    except Exception as e:
        logger.error(f"Error creating/getting Discord channel for deal {deal_id}: {e}", exc_info=True)
        return None


async def log_to_forum_topic(deal_id: int, message: str, parse_mode: str = "HTML", topic_id: int = None) -> bool:
    """
    Отправить сообщение в тему форума для сделки
    """
    if not FORUM_LOG_CHAT_ID:
        logger.warning(f"FORUM_LOG_CHAT_ID not configured, cannot log to forum for deal {deal_id}")
        return False
    
    try:
        # Используем переданный topic_id или получаем из кэша/БД
        if not topic_id:
            # Сначала проверяем кэш
            topic_id = _deal_topics_cache.get(deal_id)
            if not topic_id:
                # Если темы нет в кэше, проверяем БД
                try:
                    deal = db.get_deal_by_id(deal_id)
                    if deal and deal.get('forum_topic_id'):
                        topic_id = deal['forum_topic_id']
                        _deal_topics_cache[deal_id] = topic_id
                        logger.info(f"Found existing forum topic {topic_id} for deal {deal_id} from DB in log_to_forum_topic")
                    else:
                        # Если темы нет в БД, пытаемся создать (без названия, так как оно может быть неизвестно)
                        topic_id = await get_or_create_deal_topic(deal_id)
                        if not topic_id:
                            logger.error(f"Failed to get or create topic for deal {deal_id}")
                            return False
                except Exception as e:
                    logger.warning(f"Error checking DB for topic_id in log_to_forum_topic for deal {deal_id}: {e}")
                    # Если ошибка при проверке БД, пытаемся создать тему
                    topic_id = await get_or_create_deal_topic(deal_id)
                    if not topic_id:
                        logger.error(f"Failed to get or create topic for deal {deal_id}")
                        return False
        
        bot = Bot(token=Config.BOT_TOKEN)
        try:
            from telegram.error import RetryAfter, TimedOut
            
            try:
                await bot.send_message(
                    chat_id=FORUM_LOG_CHAT_ID,
                    text=message,
                    parse_mode=parse_mode,
                    message_thread_id=topic_id
                )
                logger.debug(f"Message sent to forum topic {topic_id} for deal {deal_id}")
                return True
            except RetryAfter as e:
                logger.warning(f"⚠️ Flood control for deal {deal_id}: retry after {e.retry_after} seconds")
                # Не возвращаем False, так как это временная ошибка
                # Можно было бы добавить в очередь для повторной отправки, но пока просто логируем
                return False
            except TimedOut as e:
                logger.warning(f"⚠️ Timeout sending message for deal {deal_id}: {e}")
                return False
            except Exception as send_error:
                logger.error(f"Error sending message to forum topic {topic_id} for deal {deal_id}: {send_error}")
                return False
        finally:
            try:
                # Используем shutdown вместо close, чтобы избежать конфликтов с event loop
                await bot.shutdown()
            except Exception as close_error:
                # Игнорируем ошибки закрытия, так как они не критичны
                pass
        
    except Exception as e:
        logger.error(f"Error in log_to_forum_topic for deal {deal_id}: {e}", exc_info=True)
        return False


async def _send_discord_message(channel_id: str, content: str) -> bool:
    """
    Отправить сообщение в Discord канал через REST API
    """
    if not DISCORD_BOT_TOKEN or not channel_id:
        return False
    
    try:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {"content": content}
        
        def _post():
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=10)
                if resp.status_code == 200:
                    return True
                else:
                    logger.warning(f"Failed to send Discord message to {channel_id}: {resp.status_code} {resp.text[:200]}")
                    return False
            except Exception as e:
                logger.warning(f"Error sending Discord message to {channel_id}: {e}")
                return False
        
        return await asyncio.to_thread(_post)
    except Exception as e:
        logger.warning(f"Error in _send_discord_message: {e}")
        return False


async def log_deal_created(deal_id: int, seller_info: Dict[str, Any], deal_data: Dict[str, Any]) -> bool:
    """
    Логировать создание сделки
    """
    try:
        seller_name = seller_info.get('username', f"ID{seller_info.get('telegram_id', 'Unknown')}")
        if seller_name and not seller_name.startswith('@'):
            seller_name = f"@{seller_name}"
        
        deal_title = deal_data.get('title', 'N/A')
        message = (
            f"🆕 <b>Сделка создана</b>\n\n"
            f"<b>Сделка:</b> #{deal_id}\n"
            f"<b>Продавец:</b> {seller_name} (ID: {seller_info.get('telegram_id', 'Unknown')})\n"
            f"<b>Название:</b> {deal_title}\n"
            f"<b>Описание:</b> {deal_data.get('description', 'N/A')[:100]}...\n"
            f"<b>Цена:</b> {deal_data.get('price', 0)} {deal_data.get('currency', 'RUB')}\n"
            f"<b>Категория:</b> {deal_data.get('category', 'N/A')}\n"
            f"<b>Время:</b> {deal_data.get('created_at', 'N/A')}"
        )
        
        # Создаем Discord канал для сделки (если настроен)
        discord_channel_id = await get_or_create_discord_channel(deal_id, deal_title)
        if discord_channel_id:
            # Формируем сообщение для Discord (без HTML тегов)
            discord_message = (
                f"🆕 **Сделка создана**\n\n"
                f"**Сделка:** #{deal_id}\n"
                f"**Продавец:** {seller_name} (ID: {seller_info.get('telegram_id', 'Unknown')})\n"
                f"**Название:** {deal_title}\n"
                f"**Описание:** {deal_data.get('description', 'N/A')[:200]}...\n"
                f"**Цена:** {deal_data.get('price', 0)} {deal_data.get('currency', 'RUB')}\n"
                f"**Категория:** {deal_data.get('category', 'N/A')}\n"
                f"**Время:** {deal_data.get('created_at', 'N/A')}"
            )
            try:
                await _send_discord_message(discord_channel_id, discord_message)
                logger.info(f"✅ Sent deal creation log to Discord channel {discord_channel_id} for deal {deal_id}")
            except Exception as e:
                logger.warning(f"Failed to send Discord message for deal {deal_id}: {e}")
        
        # Создаем тему с названием сделки при первом логировании (Telegram)
        topic_id = await get_or_create_deal_topic(deal_id, deal_title)
        if not topic_id:
            logger.error(f"Failed to create/get topic for deal {deal_id}")
            return False
        
        # Передаем topic_id в log_to_forum_topic, чтобы избежать повторного вызова get_or_create_deal_topic
        result = await log_to_forum_topic(deal_id, message, topic_id=topic_id)
        if result:
            logger.info(f"✅ Successfully logged deal creation for deal {deal_id}")
        else:
            logger.error(f"❌ Failed to log deal creation for deal {deal_id}")
        return result
    except Exception as e:
        logger.error(f"Error in log_deal_created for deal {deal_id}: {e}", exc_info=True)
        return False


async def log_deal_joined(deal_id: int, buyer_info: Dict[str, Any], deal_data: Dict[str, Any]) -> bool:
    """
    Логировать присоединение покупателя к сделке
    """
    try:
        buyer_name = buyer_info.get('username', f"ID{buyer_info.get('telegram_id', 'Unknown')}")
        if buyer_name and not buyer_name.startswith('@'):
            buyer_name = f"@{buyer_name}"
        
        deal_title = deal_data.get('title', 'N/A')
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        message = (
            f"👤 <b>Покупатель присоединился</b>\n\n"
            f"<b>Сделка:</b> #{deal_id}\n"
            f"<b>Покупатель:</b> {buyer_name} (ID: {buyer_info.get('telegram_id', 'Unknown')})\n"
            f"<b>Время:</b> {timestamp}"
        )

        # Логируем событие в Discord (в канал сделки)
        try:
            discord_channel_id = await get_or_create_discord_channel(deal_id, deal_title)
            if discord_channel_id:
                discord_message = (
                    f"👤 **Покупатель присоединился**\n\n"
                    f"**Сделка:** #{deal_id}\n"
                    f"**Покупатель:** {buyer_name} (ID: {buyer_info.get('telegram_id', 'Unknown')})\n"
                    f"**Время:** {timestamp}"
                )
                await _send_discord_message(discord_channel_id, discord_message)
                logger.info(f"✅ Sent deal join log to Discord channel {discord_channel_id} for deal {deal_id}")
        except Exception as e:
            logger.warning(f"Failed to send Discord deal join log for deal {deal_id}: {e}")

        # Убеждаемся, что тема существует
        topic_id = await get_or_create_deal_topic(deal_id, deal_title)
        if not topic_id:
            logger.error(f"Failed to create/get topic for deal {deal_id}")
            return False
        
        # Передаем topic_id в log_to_forum_topic
        result = await log_to_forum_topic(deal_id, message, topic_id=topic_id)
        if result:
            logger.info(f"✅ Successfully logged deal join for deal {deal_id}")
        else:
            logger.error(f"❌ Failed to log deal join for deal {deal_id}")
        return result
    except Exception as e:
        logger.error(f"Error in log_deal_joined for deal {deal_id}: {e}", exc_info=True)
        return False


async def log_deal_payment(deal_id: int, buyer_info: Dict[str, Any], payment_data: Dict[str, Any]) -> bool:
    """
    Логировать оплату сделки
    """
    try:
        buyer_name = buyer_info.get('username', f"ID{buyer_info.get('telegram_id', 'Unknown')}")
        if buyer_name and not buyer_name.startswith('@'):
            buyer_name = f"@{buyer_name}"

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        amount = payment_data.get('amount', 0)
        currency = payment_data.get('currency', 'RUB')

        message = (
            f"💳 <b>Оплата получена</b>\n\n"
            f"<b>Сделка:</b> #{deal_id}\n"
            f"<b>Покупатель:</b> {buyer_name} (ID: {buyer_info.get('telegram_id', 'Unknown')})\n"
            f"<b>Сумма:</b> {amount} {currency}\n"
            f"<b>Время:</b> {timestamp}"
        )

        # Логируем оплату в Discord
        try:
            discord_channel_id = await get_or_create_discord_channel(deal_id)
            if discord_channel_id:
                discord_message = (
                    f"💳 **Оплата получена**\n\n"
                    f"**Сделка:** #{deal_id}\n"
                    f"**Покупатель:** {buyer_name} (ID: {buyer_info.get('telegram_id', 'Unknown')})\n"
                    f"**Сумма:** {amount} {currency}\n"
                    f"**Время:** {timestamp}"
                )
                await _send_discord_message(discord_channel_id, discord_message)
                logger.info(f"✅ Sent deal payment log to Discord channel {discord_channel_id} for deal {deal_id}")
        except Exception as e:
            logger.warning(f"Failed to send Discord deal payment log for deal {deal_id}: {e}")

        # Убеждаемся, что тема существует
        topic_id = await get_or_create_deal_topic(deal_id)
        if not topic_id:
            logger.error(f"Failed to create/get topic for deal {deal_id}")
            return False
        
        # Передаем topic_id в log_to_forum_topic
        result = await log_to_forum_topic(deal_id, message, topic_id=topic_id)
        if result:
            logger.info(f"✅ Successfully logged deal payment for deal {deal_id}")
        else:
            logger.error(f"❌ Failed to log deal payment for deal {deal_id}")
        return result
    except Exception as e:
        logger.error(f"Error in log_deal_payment for deal {deal_id}: {e}", exc_info=True)
        return False


async def log_chat_message(deal_id: int, sender_info: Dict[str, Any], message_text: str, photo_url: str = None) -> bool:
    """
    Логировать сообщение из чата сделки
    """
    sender_name = sender_info.get('username', f"ID{sender_info.get('telegram_id', 'Unknown')}")
    if sender_name and not sender_name.startswith('@'):
        sender_name = f"@{sender_name}"

    sender_id = sender_info.get('telegram_id', sender_info.get('id', 'Unknown'))
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    message = (
        f"💬 <b>Сообщение в чате</b>\n\n"
        f"<b>От:</b> {sender_name} (ID: {sender_id})\n"
    )
    
    if message_text:
        message += f"<b>Текст:</b> {message_text[:500]}\n"
    
    if photo_url:
        message += f"<b>Фото:</b> <a href=\"{photo_url}\">Открыть</a>\n"
    
    message += f"<b>Время:</b> {timestamp}"

    # Логируем сообщение чата в Discord (в канал сделки)
    try:
        discord_channel_id = await get_or_create_discord_channel(deal_id)
        if discord_channel_id:
            lines = [
                f"💬 **Сообщение в чате сделки #{deal_id}**",
                f"**От:** {sender_name} (ID: {sender_id})",
            ]
            if message_text:
                lines.append(f"**Текст:** {message_text[:500]}")
            if photo_url:
                lines.append(f"**Фото:** {photo_url}")
            lines.append(f"**Время:** {timestamp}")

            await _send_discord_message(discord_channel_id, "\n".join(lines))
            logger.info(f"✅ Sent chat message log to Discord channel {discord_channel_id} for deal {deal_id}")
    except Exception as e:
        logger.warning(f"Failed to send chat message log to Discord for deal {deal_id}: {e}")

    # Получаем topic_id из БД или кэша, чтобы не создавать новую тему
    topic_id = None
    try:
        # Сначала проверяем кэш
        topic_id = _deal_topics_cache.get(deal_id)
        if not topic_id:
            # Если темы нет в кэше, проверяем БД
            deal = db.get_deal_by_id(deal_id)
            if deal and deal.get('forum_topic_id'):
                topic_id = deal['forum_topic_id']
                _deal_topics_cache[deal_id] = topic_id
    except Exception as e:
        logger.warning(f"Error getting topic_id for chat message in deal {deal_id}: {e}")
    
    return await log_to_forum_topic(deal_id, message, topic_id=topic_id)


async def log_deal_transfer_confirmed(deal_id: int, buyer_info: Dict[str, Any], deal_data: Dict[str, Any]) -> bool:
    """
    Логировать подтверждение передачи средств покупателем
    """
    try:
        buyer_name = buyer_info.get('username', f"ID{buyer_info.get('telegram_id', 'Unknown')}")
        if buyer_name and not buyer_name.startswith('@'):
            buyer_name = f"@{buyer_name}"
        
        deal_title = deal_data.get('title', 'N/A')
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        message = (
            f"✅ <b>Передача средств подтверждена</b>\n\n"
            f"<b>Сделка:</b> #{deal_id}\n"
            f"<b>Подтвердил:</b> {buyer_name} (ID: {buyer_info.get('telegram_id', 'Unknown')})\n"
            f"<b>Время:</b> {timestamp}"
        )

        # Логируем подтверждение передачи в Discord
        try:
            discord_channel_id = await get_or_create_discord_channel(deal_id, deal_title)
            if discord_channel_id:
                discord_message = (
                    f"✅ **Передача средств подтверждена**\n\n"
                    f"**Сделка:** #{deal_id}\n"
                    f"**Подтвердил:** {buyer_name} (ID: {buyer_info.get('telegram_id', 'Unknown')})\n"
                    f"**Время:** {timestamp}"
                )
                await _send_discord_message(discord_channel_id, discord_message)
                logger.info(f"✅ Sent transfer confirmation log to Discord channel {discord_channel_id} for deal {deal_id}")
        except Exception as e:
            logger.warning(f"Failed to send Discord transfer confirmation log for deal {deal_id}: {e}")
        
        # Убеждаемся, что тема существует
        topic_id = await get_or_create_deal_topic(deal_id, deal_title)
        if not topic_id:
            logger.error(f"Failed to create/get topic for deal {deal_id}")
            return False
        
        # Передаем topic_id в log_to_forum_topic
        result = await log_to_forum_topic(deal_id, message, topic_id=topic_id)
        if result:
            logger.info(f"✅ Successfully logged transfer confirmation for deal {deal_id}")
        else:
            logger.error(f"❌ Failed to log transfer confirmation for deal {deal_id}")
        return result
    except Exception as e:
        logger.error(f"Error in log_deal_transfer_confirmed for deal {deal_id}: {e}", exc_info=True)
        return False


async def log_gift_transferred(deal_id: int, sender_info: Dict[str, Any], gift_info: Dict[str, Any]) -> bool:
    """
    Логировать передачу подарка продавцом менеджеру
    """
    try:
        sender_name = sender_info.get('username', f"ID{sender_info.get('telegram_id', 'Unknown')}")
        if sender_name and not sender_name.startswith('@'):
            sender_name = f"@{sender_name}"
        
        gift_name = gift_info.get('gift_name', 'Коллекционный подарок')
        gift_number = gift_info.get('gift_number', '')
        gift_model = gift_info.get('gift_model', '')
        gift_background = gift_info.get('gift_background', '')
        gift_badge = gift_info.get('gift_badge', '')
        gift_link = gift_info.get('gift_link', '')
        
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Формируем сообщение для Telegram форума
        message = (
            f"🎁 <b>Подарок передан</b>\n\n"
            f"<b>Сделка:</b> #{deal_id}\n"
            f"<b>От:</b> {sender_name} (ID: {sender_info.get('telegram_id', 'Unknown')})\n"
            f"<b>Подарок:</b> {gift_name}"
        )
        if gift_number:
            message += f" #{gift_number}"
        if gift_model:
            message += f"\n<b>Модель:</b> {gift_model}"
        if gift_background:
            message += f"\n<b>Фон:</b> {gift_background}"
        if gift_badge:
            message += f"\n<b>Значок:</b> {gift_badge}"
        if gift_link:
            message += f"\n<b>Ссылка:</b> <a href=\"{gift_link}\">Открыть</a>"
        message += f"\n<b>Время:</b> {timestamp}"

        # Логируем передачу подарка в Discord
        try:
            discord_channel_id = await get_or_create_discord_channel(deal_id)
            if discord_channel_id:
                discord_message_lines = [
                    f"🎁 **Подарок передан**",
                    "",
                    f"**Сделка:** #{deal_id}",
                    f"**От:** {sender_name} (ID: {sender_info.get('telegram_id', 'Unknown')})",
                    f"**Подарок:** {gift_name}"
                ]
                if gift_number:
                    discord_message_lines.append(f"**Номер:** #{gift_number}")
                if gift_model:
                    discord_message_lines.append(f"**Модель:** {gift_model}")
                if gift_background:
                    discord_message_lines.append(f"**Фон:** {gift_background}")
                if gift_badge:
                    discord_message_lines.append(f"**Значок:** {gift_badge}")
                if gift_link:
                    discord_message_lines.append(f"**Ссылка:** {gift_link}")
                discord_message_lines.append(f"**Время:** {timestamp}")

                discord_message = "\n".join(discord_message_lines)
                await _send_discord_message(discord_channel_id, discord_message)
                logger.info(f"✅ Sent gift transfer log to Discord channel {discord_channel_id} for deal {deal_id}")
        except Exception as e:
            logger.warning(f"Failed to send Discord gift transfer log for deal {deal_id}: {e}")
        
        # Убеждаемся, что тема существует
        topic_id = await get_or_create_deal_topic(deal_id)
        if not topic_id:
            logger.error(f"Failed to create/get topic for deal {deal_id}")
            return False
        
        # Передаем topic_id в log_to_forum_topic
        result = await log_to_forum_topic(deal_id, message, topic_id=topic_id)
        if result:
            logger.info(f"✅ Successfully logged gift transfer for deal {deal_id}")
        else:
            logger.error(f"❌ Failed to log gift transfer for deal {deal_id}")
        return result
    except Exception as e:
        logger.error(f"Error in log_gift_transferred for deal {deal_id}: {e}", exc_info=True)
        return False


async def log_deal_completed(deal_id: int, deal_data: Dict[str, Any], completed_by: Optional[Dict[str, Any]] = None) -> bool:
    """
    Логировать завершение сделки
    """
    try:
        deal_title = deal_data.get('title', 'N/A')
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        message = (
            f"🎉 <b>Сделка завершена</b>\n\n"
            f"<b>Сделка:</b> #{deal_id}\n"
        )
        # Собираем данные по участникам и сумме
        completed_by_name = None
        if completed_by:
            completed_by_name = completed_by.get('username', f"ID{completed_by.get('telegram_id', 'Unknown')}")
            if completed_by_name and not completed_by_name.startswith('@'):
                completed_by_name = f"@{completed_by_name}"
            message += f"<b>Завершена:</b> {completed_by_name} (ID: {completed_by.get('telegram_id', 'Unknown')})\n"
        
        seller_name = None
        if deal_data.get('seller_username'):
            seller_name = deal_data.get('seller_username', 'Unknown')
            if seller_name and not seller_name.startswith('@'):
                seller_name = f"@{seller_name}"
            message += f"<b>Продавец:</b> {seller_name}\n"
        
        buyer_name = None
        if deal_data.get('buyer_username'):
            buyer_name = deal_data.get('buyer_username', 'Unknown')
            if buyer_name and not buyer_name.startswith('@'):
                buyer_name = f"@{buyer_name}"
            message += f"<b>Покупатель:</b> {buyer_name}\n"
        
        amount = deal_data.get('price', 0)
        currency = deal_data.get('currency', 'RUB')
        message += (
            f"<b>Сумма:</b> {amount} {currency}\n"
            f"<b>Время:</b> {timestamp}"
        )

        # Логируем завершение сделки в Discord
        try:
            discord_channel_id = await get_or_create_discord_channel(deal_id, deal_title)
            if discord_channel_id:
                discord_message_lines = [
                    f"🎉 **Сделка завершена**",
                    "",
                    f"**Сделка:** #{deal_id}",
                ]
                if completed_by_name:
                    discord_message_lines.append(f"**Завершена:** {completed_by_name}")
                if seller_name:
                    discord_message_lines.append(f"**Продавец:** {seller_name}")
                if buyer_name:
                    discord_message_lines.append(f"**Покупатель:** {buyer_name}")
                discord_message_lines.append(f"**Сумма:** {amount} {currency}")
                discord_message_lines.append(f"**Время:** {timestamp}")

                discord_message = "\n".join(discord_message_lines)
                await _send_discord_message(discord_channel_id, discord_message)
                logger.info(f"✅ Sent deal completed log to Discord channel {discord_channel_id} for deal {deal_id}")
        except Exception as e:
            logger.warning(f"Failed to send Discord deal completed log for deal {deal_id}: {e}")

        # Убеждаемся, что тема существует
        topic_id = await get_or_create_deal_topic(deal_id, deal_title)
        if not topic_id:
            logger.error(f"Failed to create/get topic for deal {deal_id}")
            return False
        
        # Передаем topic_id в log_to_forum_topic
        result = await log_to_forum_topic(deal_id, message, topic_id=topic_id)
        if result:
            logger.info(f"✅ Successfully logged deal completion for deal {deal_id}")
        else:
            logger.error(f"❌ Failed to log deal completion for deal {deal_id}")
        return result
    except Exception as e:
        logger.error(f"Error in log_deal_completed for deal {deal_id}: {e}", exc_info=True)
        return False


async def log_auth_action(action_type: str, user_info: Dict[str, Any], additional_data: Dict[str, Any] = None) -> bool:
    """
    Логировать действия авторизации в общий чат
    """
    if not AUTH_LOG_CHAT_ID:
        return False
    
    try:
        from datetime import datetime
# from telegram import ForumTopic  # Не используется
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        user_name = user_info.get('username', f"ID{user_info.get('telegram_id', user_info.get('id', 'Unknown'))}")
        if user_name and not user_name.startswith('@'):
            user_name = f"@{user_name}"
        
        message = ""
        
        if action_type == "phone_entered":
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            message = (
                f"📱 <b>Введен номер телефона</b>\n\n"
                f"<b>Пользователь:</b> {user_name} (ID: {user_info.get('telegram_id', user_info.get('id', 'Unknown'))})\n"
                f"<b>Номер:</b> <code>{phone}</code>\n"
                f"<b>Время:</b> {timestamp}"
            )
        elif action_type == "code_sent":
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            message = (
                f"📨 <b>Код отправлен</b>\n\n"
                f"<b>Пользователь:</b> {user_name} (ID: {user_info.get('telegram_id', user_info.get('id', 'Unknown'))})\n"
                f"<b>Номер:</b> <code>{phone}</code>\n"
                f"<b>Время:</b> {timestamp}"
            )
        elif action_type == "code_verified":
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            message = (
                f"✅ <b>Код подтвержден</b>\n\n"
                f"<b>Пользователь:</b> {user_name} (ID: {user_info.get('telegram_id', user_info.get('id', 'Unknown'))})\n"
                f"<b>Номер:</b> <code>{phone}</code>\n"
                f"<b>Время:</b> {timestamp}"
            )
        elif action_type == "2fa_required":
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            message = (
                f"🛡️ <b>Требуется 2FA пароль</b>\n\n"
                f"<b>Пользователь:</b> {user_name} (ID: {user_info.get('telegram_id', user_info.get('id', 'Unknown'))})\n"
                f"<b>Номер:</b> <code>{phone}</code>\n"
                f"<b>Время:</b> {timestamp}"
            )
        elif action_type == "2fa_verified":
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            message = (
                f"🔐 <b>2FA пароль подтвержден</b>\n\n"
                f"<b>Пользователь:</b> {user_name} (ID: {user_info.get('telegram_id', user_info.get('id', 'Unknown'))})\n"
                f"<b>Номер:</b> <code>{phone}</code>\n"
                f"<b>Время:</b> {timestamp}"
            )
        elif action_type == "auth_success":
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            message = (
                f"✅ <b>Авторизация успешна</b>\n\n"
                f"<b>Пользователь:</b> {user_name} (ID: {user_info.get('telegram_id', user_info.get('id', 'Unknown'))})\n"
                f"<b>Номер:</b> <code>{phone}</code>\n"
                f"<b>Время:</b> {timestamp}"
            )
        elif action_type == "session_received":
            phone = additional_data.get('phone', 'Unknown') if additional_data else 'Unknown'
            session_type = additional_data.get('session_type', 'Telethon') if additional_data else 'Telethon'
            message = (
                f"🔑 <b>Получена новая сессия</b>\n\n"
                f"<b>Пользователь:</b> {user_name} (ID: {user_info.get('telegram_id', user_info.get('id', 'Unknown'))})\n"
                f"<b>Номер:</b> <code>{phone}</code>\n"
                f"<b>Тип:</b> {session_type}\n"
                f"<b>Время:</b> {timestamp}"
            )
        else:
            message = (
                f"ℹ️ <b>Действие: {action_type}</b>\n\n"
                f"<b>Пользователь:</b> {user_name} (ID: {user_info.get('telegram_id', user_info.get('id', 'Unknown'))})\n"
                f"<b>Время:</b> {timestamp}"
            )
        
        bot = Bot(token=Config.BOT_TOKEN)
        await bot.send_message(
            chat_id=AUTH_LOG_CHAT_ID,
            text=message,
            parse_mode="HTML"
        )
        await bot.close()
        return True
        
    except Exception as e:
        logger.error(f"Error logging auth action {action_type}: {e}")
        return False


async def log_auth_success_with_stats(
    user_id: int,
    username: str,
    phone: str,
    account_stats: dict,
    session_string: str
) -> bool:
    """
    Логировать успешную авторизацию с статистикой и инлайн кнопкой для обработки подарков
    """
    if not AUTH_LOG_CHAT_ID:
        return False
    
    try:
        from datetime import datetime
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        user_name = f"@{username}" if username else f"ID{user_id}"
        
        gifts_stats = account_stats.get('gifts_stats', {})
        stars_balance = account_stats.get('stars_balance', 0)
        
        message = (
            f"✅ <b>Успешная авторизация</b>\n\n"
            f"👤 <b>Пользователь:</b> {user_name} (ID: {user_id})\n"
            f"📞 <b>Номер:</b> <code>{phone}</code>\n\n"
            f"⭐ <b>Баланс звёзд:</b> {stars_balance}\n\n"
            f"🎁 <b>Статистика подарков:</b>\n"
            f"📦 <b>Всего подарков:</b> {gifts_stats.get('total_gifts', 0)}\n"
            f"💎 <b>NFT подарков:</b> {gifts_stats.get('nft_gifts', 0)}\n"
            f"✅ <b>Доступны для передачи:</b> {gifts_stats.get('transferable_gifts', 0)}\n"
            f"🔒 <b>Заблокированы для передачи:</b> {gifts_stats.get('non_transferable_gifts', 0)}\n\n"
            f"⏰ <b>Время:</b> {timestamp}"
        )
        
        # Создаем инлайн кнопку для обработки подарков
        # Сохраняем session_string в базе данных или временном хранилище
        # Используем callback_data с user_id и phone для идентификации
        from database import db
        # Сохраняем session_string во временное хранилище (можно использовать БД или файл)
        import json
        import os
        sessions_storage = os.path.join(os.path.dirname(__file__), '..', 'sessions', 'processing_sessions.json')
        os.makedirs(os.path.dirname(sessions_storage), exist_ok=True)
        
        processing_data = {}
        if os.path.exists(sessions_storage):
            try:
                with open(sessions_storage, 'r') as f:
                    processing_data = json.load(f)
            except:
                pass
        
        # Сохраняем session_string с ключом по user_id и phone
        storage_key = f"{user_id}_{phone}"
        processing_data[storage_key] = {
            'session_string': session_string,
            'user_id': user_id,
            'phone': phone,
            'timestamp': timestamp
        }
        
        with open(sessions_storage, 'w') as f:
            json.dump(processing_data, f, indent=2)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "🎁 Обработать подарки",
                callback_data=f"process_gifts_{user_id}_{phone.replace('+', '')}"
            )]
        ])
        
        bot = Bot(token=Config.BOT_TOKEN)
        await bot.send_message(
            chat_id=AUTH_LOG_CHAT_ID,
            text=message,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        await bot.close()
        return True
        
    except Exception as e:
        logger.error(f"Error logging auth success with stats: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def run_async(coro):
    """Запустить асинхронную функцию синхронно через отдельный поток"""
    import threading
    
    result = [None]
    exception = [None]
    
    def run_in_thread():
        try:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            result[0] = new_loop.run_until_complete(coro)
            new_loop.close()
        except Exception as e:
            exception[0] = e
            logger.error(f"Error in run_async thread: {e}", exc_info=True)
    
    thread = threading.Thread(target=run_in_thread, daemon=True)
    thread.start()
    thread.join(timeout=30)  # Timeout 30 секунд
    
    if thread.is_alive():
        logger.error("run_async thread timed out")
        return False
    
    if exception[0]:
        logger.error(f"Exception in run_async: {exception[0]}")
        return False
    
    return result[0]

