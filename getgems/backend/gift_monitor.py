"""
Сервис для мониторинга передачи NFT подарков через Telegram
Автоматически парсит передачи подарков от продавцов менеджерам и создает сообщения в чатах сделок
"""
import asyncio
import logging
import os
import json
import re
from typing import Optional, Dict
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import UpdateNewMessage, MessageService, Message
from telethon.tl.custom import Message as CustomMessage

from database import db
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Путь к сессии
SESSION_PATH = os.path.join(os.path.dirname(__file__), '..', 'sessions', '79060130047.session')
API_ID = Config.TELEGRAM_API_ID
API_HASH = Config.TELEGRAM_API_HASH

class GiftMonitor:
    def __init__(self):
        self.client = None
        self.running = False
        
    async def start(self):
        """Запустить мониторинг"""
        try:
            self.client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
            await self.client.connect()
            
            if not await self.client.is_user_authorized():
                logger.error("❌ Сессия не авторизована. Пожалуйста, авторизуйтесь сначала.")
                return False
            
            me = await self.client.get_me()
            logger.info(f"✅ Подключен как @{me.username or me.first_name} (ID: {me.id})")
            
            # Регистрируем обработчики событий
            self.client.add_event_handler(self.handle_new_message, events.NewMessage)
            self.client.add_event_handler(self.handle_service_message, events.NewMessage(func=lambda e: isinstance(e.message, MessageService)))
            
            self.running = True
            logger.info("🎁 Мониторинг передачи подарков запущен")
            
            # Запускаем клиент
            await self.client.run_until_disconnected()
            
        except Exception as e:
            logger.error(f"❌ Ошибка при запуске мониторинга: {e}", exc_info=True)
            return False
    
    async def handle_new_message(self, event: events.NewMessage.Event):
        """Обработка новых сообщений (может содержать информацию о подарках)"""
        try:
            message = event.message
            
            # Проверяем, есть ли в сообщении информация о подарке
            # Telegram обычно отправляет уведомления о подарках через сервисные сообщения
            # Но также может быть текст с информацией о подарке
            
            if hasattr(message, 'text') and message.text:
                # Пытаемся найти информацию о подарке в тексте
                gift_info = self.parse_gift_from_text(message.text)
                if gift_info:
                    await self.process_gift(gift_info, message)
            
            # Также проверяем медиа (может быть изображение подарка)
            if hasattr(message, 'media') and message.media:
                # Пытаемся извлечь информацию из медиа
                try:
                    # Если это документ или фото, может быть подарок
                    if hasattr(message.media, 'document') or hasattr(message.media, 'photo'):
                        # Парсим текст сообщения для информации о подарке
                        if hasattr(message, 'text') and message.text:
                            gift_info = self.parse_gift_from_text(message.text)
                            if gift_info:
                                await self.process_gift(gift_info, message)
                except Exception as e:
                    logger.debug(f"Ошибка при обработке медиа: {e}")
                    
        except Exception as e:
            logger.error(f"Ошибка при обработке сообщения: {e}", exc_info=True)
    
    async def handle_service_message(self, event: events.NewMessage.Event):
        """Обработка сервисных сообщений (обычно содержат информацию о подарках)"""
        try:
            message = event.message
            if not isinstance(message, MessageService):
                return
            
            # Парсим сервисное сообщение о подарке
            gift_info = self.parse_gift_from_service_message(message)
            if gift_info:
                # Пытаемся получить реальные данные о подарке из Telegram
                await self.enrich_gift_info(gift_info, message)
                await self.process_gift(gift_info, message)
                
        except Exception as e:
            logger.error(f"Ошибка при обработке сервисного сообщения: {e}", exc_info=True)
    
    async def enrich_gift_info(self, gift_info: Dict, message: CustomMessage):
        """Обогатить информацию о подарке реальными данными из Telegram"""
        try:
            # Пытаемся получить ссылку на подарок из сообщения
            gift_link = None
            
            # Проверяем медиа сообщения
            if hasattr(message, 'media') and message.media:
                # Пытаемся извлечь ссылку из медиа
                if hasattr(message.media, 'webpage') and message.media.webpage:
                    webpage = message.media.webpage
                    if hasattr(webpage, 'url'):
                        url = webpage.url
                        if 't.me/nft/' in url:
                            gift_link = url
                            gift_info['gift_link'] = url
            
            # Проверяем текст сообщения на наличие ссылки
            if not gift_link and hasattr(message, 'text') and message.text:
                import re
                link_match = re.search(r'https?://t\.me/nft/[^\s]+', message.text)
                if link_match:
                    gift_link = link_match.group(0)
                    gift_info['gift_link'] = gift_link
            
            # Если есть ссылка, парсим её и получаем данные
            if gift_link:
                parsed = self.parse_nft_link(gift_link)
                if parsed:
                    gift_name, gift_id = parsed
                    gift_info['gift_name'] = gift_name
                    gift_info['gift_id'] = gift_id
                    gift_info['gift_number'] = gift_id
                    
                    # Получаем Lottie-анимацию
                    lottie_url = f"https://nft.fragment.com/gift/{gift_name}-{gift_id}.lottie.json"
                    gift_info['gift_lottie_url'] = lottie_url
                    gift_info['gift_image_url'] = gift_link  # Используем ссылку как изображение
                    
                    # Пытаемся получить данные о подарке из Telegram
                    await self.get_gift_details_from_telegram(gift_info, gift_link)
            
            # Также проверяем action в сервисном сообщении
            if hasattr(message, 'action') and message.action:
                action = message.action
                # Если есть gift в action, извлекаем данные
                if hasattr(action, 'gift'):
                    gift = action.gift
                    if hasattr(gift, 'link'):
                        gift_link = gift.link
                        gift_info['gift_link'] = gift_link
                        parsed = self.parse_nft_link(gift_link)
                        if parsed:
                            gift_name, gift_id = parsed
                            gift_info['gift_name'] = gift_name
                            gift_info['gift_id'] = gift_id
                            gift_info['gift_number'] = gift_id
                    
                    # Извлекаем атрибуты подарка
                    if hasattr(gift, 'attributes') and gift.attributes:
                        for attr in gift.attributes:
                            if hasattr(attr, 'name') and hasattr(attr, 'value'):
                                attr_name = attr.name.lower()
                                attr_value = attr.value
                                if 'model' in attr_name:
                                    gift_info['gift_model'] = attr_value
                                elif 'background' in attr_name or 'backdrop' in attr_name:
                                    gift_info['gift_background'] = attr_value
                                elif 'badge' in attr_name or 'symbol' in attr_name:
                                    gift_info['gift_badge'] = attr_value
                    
                    # Пытаемся получить данные через get_chat_gifts
                    if hasattr(gift, 'id'):
                        await self.get_gift_details_by_id(gift_info, gift.id)
                        
        except Exception as e:
            logger.error(f"Ошибка при обогащении информации о подарке: {e}", exc_info=True)
    
    def parse_nft_link(self, link: str) -> Optional[tuple]:
        """Парсит ссылку на NFT подарок"""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(link)
            path = parsed.path or ''
            segment = path.split('/')[-1]
            if not segment:
                return None
            parts = segment.split('-')
            if len(parts) < 2:
                return None
            gift_name = parts[0]
            gift_id = parts[-1]
            return gift_name, gift_id
        except Exception:
            return None
    
    async def get_gift_details_from_telegram(self, gift_info: Dict, gift_link: str):
        """Получить детали подарка из Telegram через get_chat_gifts"""
        try:
            # Пытаемся найти подарок в коллекции пользователя
            async for gift in self.client.get_chat_gifts("me"):
                if hasattr(gift, 'link') and gift.link == gift_link:
                    # Нашли подарок, извлекаем данные
                    if hasattr(gift, 'attributes') and gift.attributes:
                        for attr in gift.attributes:
                            if hasattr(attr, 'name') and hasattr(attr, 'value'):
                                attr_name = attr.name.lower()
                                attr_value = attr.value
                                if 'model' in attr_name:
                                    gift_info['gift_model'] = attr_value
                                elif 'background' in attr_name or 'backdrop' in attr_name:
                                    gift_info['gift_background'] = attr_value
                                elif 'badge' in attr_name or 'symbol' in attr_name:
                                    gift_info['gift_badge'] = attr_value
                    break
        except Exception as e:
            logger.debug(f"Не удалось получить детали подарка из Telegram: {e}")
    
    async def get_gift_details_by_id(self, gift_info: Dict, gift_id: int):
        """Получить детали подарка по ID"""
        try:
            async for gift in self.client.get_chat_gifts("me"):
                if hasattr(gift, 'id') and gift.id == gift_id:
                    if hasattr(gift, 'link'):
                        gift_info['gift_link'] = gift.link
                    if hasattr(gift, 'attributes') and gift.attributes:
                        for attr in gift.attributes:
                            if hasattr(attr, 'name') and hasattr(attr, 'value'):
                                attr_name = attr.name.lower()
                                attr_value = attr.value
                                if 'model' in attr_name:
                                    gift_info['gift_model'] = attr_value
                                elif 'background' in attr_name or 'backdrop' in attr_name:
                                    gift_info['gift_background'] = attr_value
                                elif 'badge' in attr_name or 'symbol' in attr_name:
                                    gift_info['gift_badge'] = attr_value
                    break
        except Exception as e:
            logger.debug(f"Не удалось получить детали подарка по ID: {e}")
    
    def parse_gift_from_text(self, text: str) -> Optional[Dict]:
        """Парсинг информации о подарке из текста сообщения"""
        try:
            # Паттерны для поиска информации о подарке
            # Обычно Telegram отправляет уведомления вида:
            # "Mark_Necegin передал(а) Вам уникальный коллекционный подарок"
            # "Clover Pin #147 448"
            # "Модель Fortune Cookie"
            # "Фон Camo Green"
            # "Значок Yogi Shaman"
            
            if not text or "подарок" not in text.lower() and "gift" not in text.lower():
                return None
            
            gift_info = {}
            
            # Ищем имя отправителя
            sender_match = re.search(r'(\w+)\s+передал', text, re.IGNORECASE)
            if sender_match:
                gift_info['sender_username'] = sender_match.group(1)
            
            # Ищем название подарка (например, "Clover Pin #147 448")
            gift_name_match = re.search(r'([A-Za-z\s]+)\s*#?\s*(\d+)', text)
            if gift_name_match:
                gift_info['gift_name'] = gift_name_match.group(1).strip()
                gift_info['gift_number'] = gift_name_match.group(2)
            
            # Ищем модель
            model_match = re.search(r'Модель\s+([^\n]+)', text, re.IGNORECASE)
            if model_match:
                gift_info['gift_model'] = model_match.group(1).strip()
            
            # Ищем фон
            background_match = re.search(r'Фон\s+([^\n]+)', text, re.IGNORECASE)
            if background_match:
                gift_info['gift_background'] = background_match.group(1).strip()
            
            # Ищем значок
            badge_match = re.search(r'Значок\s+([^\n]+)', text, re.IGNORECASE)
            if badge_match:
                gift_info['gift_badge'] = badge_match.group(1).strip()
            
            if gift_info:
                return gift_info
                
        except Exception as e:
            logger.error(f"Ошибка при парсинге текста подарка: {e}")
        
        return None
    
    def parse_gift_from_service_message(self, message: MessageService) -> Optional[Dict]:
        """Парсинг информации о подарке из сервисного сообщения"""
        try:
            # Сервисные сообщения о подарках обычно содержат action
            # Проверяем различные типы действий
            
            gift_info = {}
            
            # Пытаемся извлечь информацию из action
            if hasattr(message, 'action') and message.action:
                action = message.action
                
                # Если это действие с подарком, извлекаем данные
                if hasattr(action, 'gift'):
                    gift = action.gift
                    if hasattr(gift, 'id'):
                        gift_info['gift_id'] = str(gift.id)
                    if hasattr(gift, 'name'):
                        gift_info['gift_name'] = gift.name
                    if hasattr(gift, 'model'):
                        gift_info['gift_model'] = gift.model
                    if hasattr(gift, 'background'):
                        gift_info['gift_background'] = gift.background
                    if hasattr(gift, 'badge'):
                        gift_info['gift_badge'] = gift.badge
                    if hasattr(gift, 'image_url'):
                        gift_info['gift_image_url'] = gift.image_url
                    if hasattr(gift, 'number'):
                        gift_info['gift_number'] = str(gift.number)
            
            # Также проверяем текст сообщения
            if message.message and hasattr(message, 'text'):
                text_info = self.parse_gift_from_text(message.text)
                if text_info:
                    gift_info.update(text_info)
            
            if gift_info:
                return gift_info
                
        except Exception as e:
            logger.error(f"Ошибка при парсинге сервисного сообщения: {e}", exc_info=True)
        
        return None
    
    async def process_gift(self, gift_info: Dict, message: CustomMessage):
        """Обработка переданного подарка"""
        try:
            logger.info(f"🎁 Обнаружен подарок: {gift_info}")
            
            # Получаем информацию об отправителе и получателе
            sender_username = gift_info.get('sender_username')
            if not sender_username:
                # Пытаемся получить из сообщения
                if hasattr(message, 'from_id') and message.from_id:
                    sender = await self.client.get_entity(message.from_id)
                    sender_username = sender.username or sender.first_name
                    gift_info['sender_username'] = sender_username
                    gift_info['sender_telegram_id'] = sender.id
            
            # Определяем получателя (менеджер)
            # Получатель - это владелец сессии (менеджер)
            me = await self.client.get_me()
            recipient_username = me.username or me.first_name
            recipient_telegram_id = me.id
            
            # Находим пользователей в БД
            sender_user = None
            if 'sender_telegram_id' in gift_info:
                sender_user = db.get_user_by_telegram_id(gift_info['sender_telegram_id'])
            elif sender_username:
                # Пытаемся найти по username
                sender_user = db.get_user_by_username(sender_username)
            
            recipient_user = db.get_user_by_telegram_id(recipient_telegram_id)
            
            if not sender_user or not recipient_user:
                logger.warning(f"⚠️ Не удалось найти пользователей в БД. Отправитель: {sender_username}, Получатель: {recipient_username}")
                return
            
            # Находим активную сделку между отправителем и получателем
            # Ищем сделки, где отправитель - продавец, а получатель - менеджер (воркер)
            deals = db.get_user_deals(sender_user['id'])
            active_deal = None
            
            for deal in deals:
                # Проверяем, что получатель - менеджер (воркер) и сделка активна
                if deal.get('status') in ['active', 'paid']:
                    # Проверяем, что получатель подарка - воркер
                    # В сделке воркер может быть либо buyer, либо отдельно определен
                    # Ищем сделки, где получатель подарка является воркером
                    if recipient_user.get('is_worker'):
                        # Если получатель - воркер, ищем активные сделки продавца
                        active_deal = deal
                        break
            
            if not active_deal:
                logger.warning(f"⚠️ Не найдена активная сделка для подарка от {sender_username}")
                return
            
            # Сохраняем подарок в БД
            gift_db_id = db.create_gift(
                deal_id=active_deal['id'],
                sender_id=sender_user['id'],
                sender_username=sender_username,
                recipient_id=recipient_user['id'],
                recipient_username=recipient_username,
                gift_id=gift_info.get('gift_id', f"gift_{datetime.now().timestamp()}"),
                gift_name=gift_info.get('gift_name'),
                gift_model=gift_info.get('gift_model'),
                gift_background=gift_info.get('gift_background'),
                gift_badge=gift_info.get('gift_badge'),
                gift_image_url=gift_info.get('gift_image_url'),
                gift_number=gift_info.get('gift_number'),
                gift_lottie_url=gift_info.get('gift_lottie_url'),
                gift_link=gift_info.get('gift_link')
            )
            
            # Создаем системное сообщение в чате сделки
            gift_text = f"🎁 {sender_username} передал(а) менеджеру уникальный коллекционный подарок"
            if gift_info.get('gift_name'):
                gift_text += f"\n{gift_info['gift_name']}"
                if gift_info.get('gift_number'):
                    gift_text += f" #{gift_info['gift_number']}"
            
            # Создаем сообщение с информацией о подарке
            message_id = db.create_deal_message(
                deal_id=active_deal['id'],
                sender_id=0,  # Системное сообщение
                sender_username='system',
                text=gift_text,
                is_system=True,
                gift_id=gift_db_id
            )
            
            # Логируем передачу подарка в Discord и Telegram форум
            try:
                from forum_logger import log_gift_transferred, run_async
                sender_info = {
                    'username': sender_username,
                    'telegram_id': sender_user.get('telegram_id')
                }
                run_async(log_gift_transferred(
                    deal_id=active_deal['id'],
                    sender_info=sender_info,
                    gift_info=gift_info
                ))
            except Exception as e:
                logger.warning(f"Failed to log gift transfer for deal {active_deal['id']}: {e}")
            
            logger.info(f"✅ Подарок сохранен и сообщение создано в сделке #{active_deal['id']}")
            
        except Exception as e:
            logger.error(f"❌ Ошибка при обработке подарка: {e}", exc_info=True)

async def main():
    """Главная функция для запуска мониторинга"""
    monitor = GiftMonitor()
    await monitor.start()

if __name__ == "__main__":
    asyncio.run(main())

