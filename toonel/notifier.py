"""
Telegram Bot API notifications.
All functions are fire-and-forget — errors are logged, never raised.
"""
import logging
import requests
from config import TG_BOT_TOKEN, TG_CHAT_ID

logger = logging.getLogger(__name__)
_SESSION = requests.Session()
_SESSION.headers.update({"Content-Type": "application/json"})


def _base_url() -> str:
    return f"https://api.telegram.org/bot{TG_BOT_TOKEN}"


def notify(text: str, silent: bool = False) -> bool:
    """Send HTML-formatted message to configured chat."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False
    try:
        resp = _SESSION.post(
            f"{_base_url()}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_notification": silent,
                "link_preview_options": {"is_disabled": True},
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"Notifier HTTP {resp.status_code}: {resp.text[:100]}")
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"Notifier error: {e}")
        return False


def notify_gift_found(name: str, msg_id: int, stars: int) -> None:
    notify(
        f"🎁 <b>Подарок найден</b>\n"
        f"Название: <b>{name}</b>\n"
        f"Telegram msg_id: <code>{msg_id}</code>\n"
        f"Stars: {stars}"
    )


def notify_transfer_ok(name: str, msg_id: int) -> None:
    notify(
        f"🚀 <b>Трансфер → Tonnel</b>\n"
        f"<b>{name}</b> (msg_id: <code>{msg_id}</code>)\n"
        f"✅ Успешно отправлен"
    )


def notify_transfer_fail(name: str, msg_id: int, error: str) -> None:
    notify(
        f"⚠️ <b>Ошибка трансфера</b>\n"
        f"<b>{name}</b> (msg_id: <code>{msg_id}</code>)\n"
        f"<code>{error[:300]}</code>"
    )


def notify_listed(name: str, gift_id, price: float, floor: float) -> None:
    notify(
        f"✅ <b>Выставлен на продажу</b>\n"
        f"<b>{name}</b>\n"
        f"Цена: <b>{price} TON</b>  |  Флор: {floor} TON"
    )


def notify_no_price(name: str, gift_id) -> None:
    notify(
        f"⚠️ <b>Нет данных о цене</b>\n"
        f"<b>{name}</b> (tonnel id: <code>{gift_id}</code>)\n"
        f"Требуется ручное выставление на market.tonnel.network"
    )


def notify_withdraw(amount: float, wallet: str) -> None:
    notify(
        f"💸 <b>Вывод выполнен</b>\n"
        f"<b>{amount} TON</b>\n"
        f"Кошелёк: <code>{wallet}</code>"
    )


def notify_withdraw_fail(error: str) -> None:
    notify(f"⚠️ <b>Ошибка вывода</b>\n<code>{error[:300]}</code>")


def notify_auth_expired() -> None:
    notify(
        "🔴 <b>TONNEL_AUTH_DATA истёк!</b>\n"
        "Обнови web-initData в .env:\n"
        "1. Открой market.tonnel.network\n"
        "2. F12 → Application → LocalStorage\n"
        "3. Скопируй web-initData\n"
        "4. Перезапусти скрипт"
    )
