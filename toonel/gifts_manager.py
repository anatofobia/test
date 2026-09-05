"""
NFT Gift discovery and transfer via raw MTProto (layer 220).

Key raw TL calls:
  payments.GetSavedStarGifts  — list user's saved NFT gifts
  payments.TransferStarGift   — transfer gift to another peer
  types.InputSavedStarGiftUser — reference to a saved gift by msg_id
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from pyrogram import Client
from pyrogram.errors import FloodWait

from pyrogram.raw.functions.payments import GetSavedStarGifts, TransferStarGift
from pyrogram.raw.types import InputSavedStarGiftUser, StarGiftUnique, StarGift

from config import TONNEL_BOT_USERNAME
from state import is_transferred, mark_transferred
from notifier import notify_gift_found, notify_transfer_ok, notify_transfer_fail

logger = logging.getLogger(__name__)


@dataclass
class GiftInfo:
    msg_id: int           # Telegram message ID in user's gift history
    saved_id: Optional[int]  # 64-bit saved_id (for unique gifts)
    name: str             # Gift title
    slug: str             # Short identifier (for unique) or gift id str
    num: Optional[int]    # Gift serial number (unique gifts only)
    stars: int            # Stars value
    is_unique: bool       # True = NFT unique gift; False = regular star gift
    can_transfer: bool    # Whether transfer is currently allowed
    transfer_stars: Optional[int]  # Stars cost to transfer (if any)


def _parse_gift(saved: object) -> Optional[GiftInfo]:
    """
    Parse a raw SavedStarGift object into GiftInfo.
    Handles both StarGift (regular) and StarGiftUnique (NFT) variants.
    """
    msg_id: Optional[int] = getattr(saved, "msg_id", None)
    if msg_id is None:
        logger.debug(f"Пропуск: нет msg_id у SavedStarGift")
        return None

    saved_id: Optional[int] = getattr(saved, "saved_id", None)
    transfer_stars: Optional[int] = getattr(saved, "transfer_stars", None)

    # transfer is blocked if saved.unsaved is True or can_export_at is set in the future
    unsaved: bool = bool(getattr(saved, "unsaved", False))
    refunded: bool = bool(getattr(saved, "refunded", False))

    if refunded:
        return None  # already refunded, skip

    gift = getattr(saved, "gift", None)
    if gift is None:
        return None

    is_unique = isinstance(gift, StarGiftUnique)

    if is_unique:
        name: str = getattr(gift, "title", "Unique Gift")
        slug: str = getattr(gift, "slug", str(msg_id))
        num: Optional[int] = getattr(gift, "num", None)
        stars: int = getattr(gift, "value_amount", 0) or 0
    else:
        # Regular StarGift — use sticker emoji as name fallback
        name = f"Star Gift #{getattr(gift, 'id', msg_id)}"
        slug = str(getattr(gift, "id", msg_id))
        num = None
        stars = int(getattr(gift, "stars", 0) or 0)

    # can_transfer: True when gift is not unsaved and transfer not restricted
    can_transfer = not unsaved

    return GiftInfo(
        msg_id=msg_id,
        saved_id=saved_id,
        name=name,
        slug=slug,
        num=num,
        stars=stars,
        is_unique=is_unique,
        can_transfer=can_transfer,
        transfer_stars=transfer_stars,
    )


async def fetch_all_gifts(app: Client) -> list[GiftInfo]:
    """
    Fetch all saved NFT/star gifts from the current user's account.
    Paginates automatically until exhausted.
    """
    me_peer = await app.resolve_peer("me")
    results: list[GiftInfo] = []
    offset = ""
    limit = 100

    logger.info("Загрузка списка подарков...")

    while True:
        try:
            resp = await app.invoke(
                GetSavedStarGifts(
                    peer=me_peer,
                    offset=offset,
                    limit=limit,
                    # exclude regular (non-unique) gifts — set to False to include all
                    exclude_unlimited=False,
                )
            )
        except FloodWait as e:
            logger.warning(f"FloodWait {e.value}с при загрузке подарков")
            await asyncio.sleep(e.value + 1)
            continue
        except Exception as e:
            logger.error(f"Ошибка GetSavedStarGifts: {e}")
            break

        gifts_raw = getattr(resp, "gifts", [])
        for g in gifts_raw:
            info = _parse_gift(g)
            if info is not None:
                results.append(info)

        logger.info(f"  Загружено: {len(results)} подарков")

        next_offset: Optional[str] = getattr(resp, "next_offset", None)
        if not next_offset:
            break
        offset = next_offset
        await asyncio.sleep(0.3)

    return results


async def transfer_gift(
    app: Client,
    gift: GiftInfo,
    target_username: str = TONNEL_BOT_USERNAME,
    delay_after: float = 2.5,
    max_retries: int = 3,
) -> bool:
    """
    Transfer a single gift to target_username via raw MTProto.

    Uses InputSavedStarGiftUser(msg_id=...) to reference the gift.
    to_id accepts InputPeer (confirmed from layer 220 signature).
    """
    if is_transferred(gift.msg_id):
        logger.info(f"Пропуск уже трансференного: {gift.name} (msg_id={gift.msg_id})")
        return True

    if not gift.can_transfer:
        logger.warning(f"Трансфер заблокирован для: {gift.name} (msg_id={gift.msg_id})")
        return False

    to_peer = await app.resolve_peer(target_username)
    stargift_ref = InputSavedStarGiftUser(msg_id=gift.msg_id)

    for attempt in range(1, max_retries + 1):
        try:
            await app.invoke(
                TransferStarGift(
                    stargift=stargift_ref,
                    to_id=to_peer,
                )
            )

            mark_transferred(gift.msg_id)
            logger.info(f"✅ Трансфер: {gift.name} #{gift.num} → {target_username}")
            notify_transfer_ok(gift.name, gift.msg_id)

            await asyncio.sleep(delay_after)
            return True

        except FloodWait as e:
            logger.warning(f"FloodWait {e.value}с (попытка {attempt})")
            await asyncio.sleep(e.value + 1)

        except Exception as e:
            err = str(e)
            if "GiftTransferNotAllowed" in err or "GIFT_TRANSFER_NOT_ALLOWED" in err:
                logger.warning(f"Трансфер не разрешён для {gift.name} (msg_id={gift.msg_id})")
                notify_transfer_fail(gift.name, gift.msg_id, "GiftTransferNotAllowed")
                return False
            if "StarGiftNotFound" in err or "STARGIFT_NOT_FOUND" in err:
                logger.error(f"Подарок не найден: {gift.name} (msg_id={gift.msg_id})")
                notify_transfer_fail(gift.name, gift.msg_id, "StarGiftNotFound")
                return False
            logger.error(f"Ошибка трансфера {gift.name} (попытка {attempt}): {err}")
            if attempt == max_retries:
                notify_transfer_fail(gift.name, gift.msg_id, err)
                return False
            await asyncio.sleep(5 * attempt)

    return False


async def transfer_all(
    app: Client,
    gifts: list[GiftInfo],
    target_username: str = TONNEL_BOT_USERNAME,
    auto_mode: bool = False,
) -> tuple[int, int]:
    """
    Transfer all eligible gifts to target_username.
    Returns (success_count, fail_count).

    If auto_mode=False → asks for confirmation per gift.
    """
    success = fail = 0
    pending = [g for g in gifts if not is_transferred(g.msg_id) and g.can_transfer]

    if not pending:
        logger.info("Нет новых подарков для трансфера")
        return 0, 0

    print(f"\n📋 Подарков для трансфера: {len(pending)}")
    print(f"{'─'*50}")

    for i, gift in enumerate(pending, 1):
        label = f"#{gift.num}" if gift.num else ""
        print(
            f"  [{i}/{len(pending)}] {'🌟 NFT ' if gift.is_unique else '⭐ '}"
            f"{gift.name} {label}  |  "
            f"{'slug: ' + gift.slug if gift.slug else ''}"
            f"  |  msg_id={gift.msg_id}"
        )

        if gift.transfer_stars:
            print(f"            ⚠️  Трансфер стоит {gift.transfer_stars} Stars")

        if not auto_mode:
            choice = input("  Трансфернуть? [y/n/a=все/q=стоп]: ").strip().lower()
            if choice == "q":
                print("  Трансфер прерван")
                break
            if choice == "a":
                auto_mode = True
                logger.info("Включён авто-режим")
            elif choice != "y":
                continue

        notify_gift_found(gift.name, gift.msg_id, gift.stars)
        ok = await transfer_gift(app, gift, target_username)
        if ok:
            success += 1
        else:
            fail += 1

    print(f"{'─'*50}")
    print(f"  ✅ Успешно: {success}  |  ❌ Ошибок: {fail}\n")
    return success, fail
