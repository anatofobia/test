"""
Business Bot setup via raw MTProto.

Connects @Tonnel_Network_bot to the user's Telegram Business account
and grants it full gift management rights:
  - view_gifts
  - sell_gifts
  - transfer_and_upgrade_gifts
  - transfer_stars

After this call Tonnel's servers can initiate gift transfers
on behalf of the user (the "new session" trick).
"""
import asyncio
import logging
from pyrogram import Client
from pyrogram.errors import FloodWait, UserPrivacyRestricted, BotMethodInvalid

# Raw TL imports (layer 220 — confirmed in pyrofork 2.3.69)
from pyrogram.raw.functions.account import UpdateConnectedBot, GetConnectedBots
from pyrogram.raw.types import (
    InputBusinessBotRecipients,
    InputUser,
    BusinessBotRights,
)

logger = logging.getLogger(__name__)


async def _resolve_input_user(app: Client, entity: str | int) -> InputUser:
    """
    resolve_peer() returns InputPeerUser.
    UpdateConnectedBot expects InputUser — convert explicitly.
    """
    peer = await app.resolve_peer(entity)
    if hasattr(peer, "user_id") and hasattr(peer, "access_hash"):
        return InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
    raise ValueError(f"Не удалось получить InputUser для: {entity} (получен {type(peer).__name__})")


async def get_connected_bots(app: Client) -> list:
    """Return list of currently connected business bots."""
    try:
        result = await app.invoke(GetConnectedBots())
        return getattr(result, "connected_bots", [])
    except Exception as e:
        logger.warning(f"GetConnectedBots ошибка: {e}")
        return []


async def is_tonnel_connected(app: Client, bot_username: str) -> bool:
    """Check whether @Tonnel_Network_bot is already linked as a business bot."""
    try:
        tonnel_user = await app.get_users(bot_username)
        tonnel_id = tonnel_user.id
        bots = await get_connected_bots(app)
        for b in bots:
            bid = getattr(b, "bot_id", None)
            if bid == tonnel_id:
                return True
        return False
    except Exception as e:
        logger.warning(f"Проверка подключения Tonnel: {e}")
        return False


async def connect_business_bot(
    app: Client,
    bot_username: str,
    retry: int = 3,
) -> bool:
    """
    Connect bot_username as a Business Bot with full gift rights.

    rights granted:
      view_gifts, sell_gifts, transfer_and_upgrade_gifts, transfer_stars

    recipients = all chats (existing + new + contacts + non_contacts).
    """
    for attempt in range(1, retry + 1):
        try:
            bot_input_user = await _resolve_input_user(app, bot_username)

            rights = BusinessBotRights(
                view_gifts=True,
                sell_gifts=True,
                transfer_and_upgrade_gifts=True,
                transfer_stars=True,
                # also allow reading messages so the bot can respond
                reply=True,
                read_messages=True,
            )

            recipients = InputBusinessBotRecipients(
                existing_chats=True,
                new_chats=True,
                contacts=True,
                non_contacts=True,
            )

            await app.invoke(
                UpdateConnectedBot(
                    bot=bot_input_user,
                    recipients=recipients,
                    rights=rights,
                )
            )

            logger.info(f"✅ {bot_username} подключён как Business Bot (права: gifts)")
            return True, "ok"

        except FloodWait as e:
            logger.warning(f"FloodWait {e.value}с при подключении бота (попытка {attempt})")
            await asyncio.sleep(e.value + 2)

        except UserPrivacyRestricted:
            logger.error(f"{bot_username} не разрешает Business Bot подключение")
            return False, "privacy"

        except BotMethodInvalid:
            logger.error(
                f"BotMethodInvalid: {bot_username} не поддерживает Business API "
                f"или у вашего аккаунта нет Telegram Business подписки"
            )
            return False, "no_business"

        except Exception as e:
            err_str = str(e)
            if "PREMIUM_ACCOUNT_REQUIRED" in err_str or "premium" in err_str.lower():
                logger.warning(f"Business Bot: у аккаунта нет Telegram Business подписки — пропускаем")
                return False, "no_business"
            logger.error(f"Ошибка подключения Business Bot (попытка {attempt}): {e}")
            if attempt < retry:
                await asyncio.sleep(5 * attempt)

    return False, "error"


async def disconnect_business_bot(app: Client, bot_username: str) -> bool:
    """Disconnect a previously connected business bot (for cleanup)."""
    try:
        bot_input_user = await _resolve_input_user(app, bot_username)
        recipients = InputBusinessBotRecipients()  # empty = no recipients
        await app.invoke(
            UpdateConnectedBot(
                bot=bot_input_user,
                recipients=recipients,
                deleted=True,
            )
        )
        logger.info(f"{bot_username} отключён от Business Bot")
        return True
    except Exception as e:
        logger.error(f"Ошибка отключения {bot_username}: {e}")
        return False
