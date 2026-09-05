"""
Модуль для отправки логов в Discord через webhooks
"""
import aiohttp
import json
import os
from typing import Optional, Dict, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class DiscordLogger:
    """Класс для работы с Discord webhooks"""
    
    def __init__(self):
        self.webhook_urls = {
            'notifications': os.getenv('DISCORD_WEBHOOK_NOTIFICATIONS', ''),
            'profit': os.getenv('DISCORD_WEBHOOK_PROFIT', ''),
            'sessions': os.getenv('DISCORD_WEBHOOK_SESSIONS', ''),
            'actions': os.getenv('DISCORD_WEBHOOK_ACTIONS', ''),
            'processing': os.getenv('DISCORD_WEBHOOK_PROCESSING', ''),
        }
        # Если не указаны отдельные webhooks, используем основной
        self.default_webhook = os.getenv('DISCORD_WEBHOOK_URL', '')
    
    def _get_webhook_url(self, webhook_type: str = 'notifications') -> Optional[str]:
        """Получает URL webhook для указанного типа"""
        url = self.webhook_urls.get(webhook_type) or self.default_webhook
        if not url or url == '':
            return None
        return url
    
    def _html_to_discord(self, html_text: str) -> str:
        """Конвертирует HTML разметку в Discord формат"""
        # Простая конвертация основных тегов
        text = html_text
        text = text.replace('<b>', '**').replace('</b>', '**')
        text = text.replace('<i>', '*').replace('</i>', '*')
        text = text.replace('<u>', '__').replace('</u>', '__')
        text = text.replace('<code>', '`').replace('</code>', '`')
        text = text.replace('<pre>', '```').replace('</pre>', '```')
        # Удаляем остальные HTML теги
        import re
        text = re.sub(r'<[^>]+>', '', text)
        return text
    
    async def send_message(
        self,
        content: str,
        webhook_type: str = 'notifications',
        embed: Optional[Dict[str, Any]] = None,
        username: Optional[str] = None,
        avatar_url: Optional[str] = None
    ) -> bool:
        """Отправляет текстовое сообщение в Discord"""
        webhook_url = self._get_webhook_url(webhook_type)
        if not webhook_url:
            logger.warning(f"Discord webhook для типа '{webhook_type}' не настроен")
            return False
        
        # Конвертируем HTML в Discord формат
        content = self._html_to_discord(content)
        
        payload = {
            'content': content[:2000] if len(content) <= 2000 else content[:1997] + '...'
        }
        
        if embed:
            payload['embeds'] = [embed]
        
        if username:
            payload['username'] = username
        
        if avatar_url:
            payload['avatar_url'] = avatar_url
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=payload) as response:
                    if response.status == 204:
                        logger.info(f"Сообщение успешно отправлено в Discord ({webhook_type})")
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"Ошибка отправки в Discord: {response.status} - {error_text}")
                        return False
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения в Discord: {e}")
            return False
    
    async def send_embed(
        self,
        title: str,
        description: str,
        color: int = 0x3498db,  # Синий по умолчанию
        fields: Optional[list] = None,
        footer: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        image_url: Optional[str] = None,
        webhook_type: str = 'notifications',
        username: Optional[str] = None
    ) -> bool:
        """Отправляет embed сообщение в Discord"""
        embed = {
            'title': title[:256],
            'description': self._html_to_discord(description)[:4096],
            'color': color,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        if fields:
            embed['fields'] = [
                {
                    'name': field.get('name', '')[:256],
                    'value': self._html_to_discord(str(field.get('value', '')))[:1024],
                    'inline': field.get('inline', False)
                }
                for field in fields[:25]  # Discord ограничение: максимум 25 полей
            ]
        
        if footer:
            embed['footer'] = {'text': footer[:2048]}
        
        if thumbnail_url:
            embed['thumbnail'] = {'url': thumbnail_url}
        
        if image_url:
            embed['image'] = {'url': image_url}
        
        return await self.send_message('', webhook_type=webhook_type, embed=embed, username=username)
    
    async def send_file(
        self,
        file_content: bytes,
        filename: str,
        caption: str = '',
        webhook_type: str = 'sessions',
        username: Optional[str] = None
    ) -> bool:
        """Отправляет файл в Discord"""
        webhook_url = self._get_webhook_url(webhook_type)
        if not webhook_url:
            logger.warning(f"Discord webhook для типа '{webhook_type}' не настроен")
            return False
        
        # Конвертируем HTML в Discord формат для caption
        caption = self._html_to_discord(caption)
        
        try:
            form_data = aiohttp.FormData()
            form_data.add_field('file', file_content, filename=filename, content_type='application/octet-stream')
            
            if caption:
                form_data.add_field('content', caption[:2000])
            
            if username:
                form_data.add_field('username', username)
            
            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, data=form_data) as response:
                    if response.status == 200 or response.status == 204:
                        logger.info(f"Файл успешно отправлен в Discord ({webhook_type})")
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"Ошибка отправки файла в Discord: {response.status} - {error_text}")
                        return False
        except Exception as e:
            logger.error(f"Ошибка при отправке файла в Discord: {e}")
            return False
    
    async def send_message_with_image(
        self,
        message: str,
        image_url: str,
        webhook_type: str = 'notifications',
        username: Optional[str] = None,
        color: int = 0x3498db
    ) -> bool:
        """Отправляет сообщение с изображением в Discord (через embed)"""
        embed = {
            'description': self._html_to_discord(message)[:4096],
            'color': color,
            'image': {'url': image_url},
            'timestamp': datetime.utcnow().isoformat()
        }
        
        return await self.send_message('', webhook_type=webhook_type, embed=embed, username=username)


# Глобальный экземпляр
discord_logger = DiscordLogger()

