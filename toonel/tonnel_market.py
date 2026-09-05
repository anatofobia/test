"""
Tonnel Marketplace automation via tonnelmp (unofficial API).

Flow after gift arrives in Tonnel:
  1. get_unlisted_gifts()  → new gifts waiting to be priced
  2. list_for_sale()       → set price at floor × PRICE_MULTIPLIER
  3. check_balance()       → if >= MIN_WITHDRAW_TON → withdraw()

All functions are synchronous (tonnelmp is sync).
Retry logic handles CloudFlare 429 errors.
"""
import time
import logging
import json
from typing import Optional

import tonnelmp
import curl_cffi.requests as _requests

from config import PRICE_MULTIPLIER, WITHDRAW_WALLET, MIN_WITHDRAW_TON
TONNEL_AUTH_DATA = ""  # устанавливается снаружи: tm.TONNEL_AUTH_DATA = auth_data
from state import is_listed, mark_listed
from notifier import (
    notify_listed, notify_no_price, notify_withdraw,
    notify_withdraw_fail, notify_auth_expired,
)

_COFFIN_URL = "https://gifts.coffin.meme"
_GIFTS2_URL = "https://gifts2.tonnel.network"

_CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"

_WEB_HEADERS = {
    "authority": "gifts.coffin.meme",
    "accept": "*/*",
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "content-type": "application/json",
    "origin": "https://market.tonnel.network",
    "priority": "u=1, i",
    "referer": "https://market.tonnel.network/",
    "sec-ch-ua": '"Google Chrome";v="137", "Chromium";v="137", "Not/A)Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": _CHROME_UA,
}

logger = logging.getLogger(__name__)

# Backoff delays for 429 / CloudFlare
_BACKOFFS = [5, 15, 30, 60]

_PROXIES = None  # Прокси отключён — прямой IP работает


def _call(fn, *args, **kwargs):
    """
    Call a tonnelmp function with automatic retry on CloudFlare/429.
    Raises RuntimeError on auth failure so the caller can handle it.
    """
    kwargs.setdefault("proxies", _PROXIES)
    for attempt, delay in enumerate(_BACKOFFS + [None], 1):
        try:
            result = fn(*args, **kwargs)
            time.sleep(0.6)  # rate-limit between API calls
            return result
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "403" in msg or "CloudFlare" in msg or "cloudflare" in msg.lower() or "Likely CloudFlare" in msg:
                if delay is None:
                    raise
                logger.warning(f"CloudFlare/429 [{fn.__name__}] — retry {attempt} через {delay}с")
                time.sleep(delay)
            elif "401" in msg or "Unauthorized" in msg or "authData" in msg.lower():
                notify_auth_expired()
                raise RuntimeError("TONNEL_AUTH_DATA истёк — обнови в .env") from exc
            else:
                raise


# ── Gift listing ─────────────────────────────────────────

def get_unlisted_gifts() -> list[dict]:
    """Gifts received by Tonnel but not yet priced."""
    raw = _call(tonnelmp.myGifts, listed=False, authData=TONNEL_AUTH_DATA, limit=30)
    return raw if isinstance(raw, list) else []


def get_listed_gifts() -> list[dict]:
    """Gifts currently on sale in Tonnel marketplace."""
    raw = _call(tonnelmp.myGifts, listed=True, authData=TONNEL_AUTH_DATA, limit=30)
    return raw if isinstance(raw, list) else []


def _floor_price(gift_name: str, model: Optional[str] = None) -> Optional[float]:
    """Get the current lowest listed price for a gift type."""
    try:
        kwargs = dict(
            gift_name=gift_name,
            sort="price_asc",
            limit=5,
            authData=TONNEL_AUTH_DATA,
        )
        if model:
            kwargs["model"] = model
        results = _call(tonnelmp.getGifts, **kwargs)
        prices = [g.get("price") for g in (results or []) if g.get("price")]
        return float(min(prices)) if prices else None
    except Exception as e:
        logger.warning(f"Флор для '{gift_name}': {e}")
        return None


def _last_sale_price(gift_name: str) -> Optional[float]:
    """Fallback: last sold price from history."""
    try:
        history = _call(
            tonnelmp.saleHistory,
            authData=TONNEL_AUTH_DATA,
            gift_name=gift_name,
            sort="latest",
            limit=5,
        )
        prices = [h.get("price") for h in (history or []) if h.get("price")]
        return float(prices[0]) if prices else None
    except Exception as e:
        logger.warning(f"История продаж '{gift_name}': {e}")
        return None


def list_for_sale(gift_raw: dict) -> bool:
    """
    List a single unlisted gift for sale.

    gift_raw: dict from myGifts(listed=False)
      Expected keys: gift_id (or _id), gift_name (or name), model
    """
    gift_id = gift_raw.get("gift_id") or gift_raw.get("_id") or gift_raw.get("id")
    gift_name = gift_raw.get("gift_name") or gift_raw.get("name", "Unknown Gift")
    model = gift_raw.get("model")

    if not gift_id:
        logger.warning(f"list_for_sale: нет gift_id в {gift_raw}")
        return False

    if is_listed(gift_id):
        logger.info(f"Уже выставлен (пропуск): {gift_name} (id={gift_id})")
        return True

    # Determine price
    floor = _floor_price(gift_name, model)
    if floor is None:
        logger.info(f"Нет флора для '{gift_name}', пробую историю продаж...")
        floor = _last_sale_price(gift_name)

    if floor is None:
        logger.warning(f"Нет данных о цене для '{gift_name}' (id={gift_id})")
        notify_no_price(gift_name, gift_id)
        return False

    price = round(floor * PRICE_MULTIPLIER, 2)
    price = max(price, 0.5)  # Tonnel minimum

    try:
        _call(
            tonnelmp.listForSale,
            gift_id=gift_id,
            price=price,
            authData=TONNEL_AUTH_DATA,
        )
        logger.info(f"✅ Листинг: {gift_name} (id={gift_id}) → {price} TON (флор {floor})")
        mark_listed(gift_id)
        notify_listed(gift_name, gift_id, price, floor)
        return True
    except Exception as e:
        logger.error(f"Ошибка листинга {gift_name} (id={gift_id}): {e}")
        return False



def fetch_managed_gifts(page: int = 1) -> list[dict]:
    """
    Fetch gifts visible to Tonnel via Business Bot access.
    Returns list of dicts with keys: owned_gift_id, gift_name, gift_num, ...
    These gifts live in the user's Telegram account but Tonnel can manage them.
    """
    payload = {
        "authData": TONNEL_AUTH_DATA,
        "page": page,
        "sort": json.dumps({"message_post_time": -1, "gift_id": -1}),
        "filter": json.dumps({}),
    }
    headers = {**_WEB_HEADERS, "authority": "gifts2.tonnel.network", "user-agent": _CHROME_UA}
    for attempt, delay in enumerate(_BACKOFFS + [None], 1):
        try:
            resp = _requests.post(
                f"{_GIFTS2_URL}/api/fetchMangedGifts",
                headers=headers,
                json=payload,
                impersonate="chrome",
                proxies=_PROXIES,
                timeout=15,
            )
            if resp.status_code in (429, 403):
                if delay is None:
                    raise RuntimeError(f"fetchMangedGifts: {resp.status_code}")
                logger.warning(f"fetchMangedGifts 429/403 — retry {attempt} в {delay}с")
                time.sleep(delay)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", []) if isinstance(data, dict) else []
        except RuntimeError:
            raise
        except Exception as exc:
            if delay is None:
                raise
            logger.warning(f"fetchMangedGifts ошибка (попытка {attempt}): {exc}")
            time.sleep(delay)
    return []


def instant_transfer(owned_gift_id: int) -> bool:
    """
    Instantly deposit a managed gift into Tonnel via @giftrelayer.
    Equivalent to clicking "Instant Transfer" in market.tonnel.network.

    Uses /api/quickTransfer — Tonnel's bot does the physical transfer
    via Business Bot API. No MTProto user session needed → no 7-day restriction.

    owned_gift_id: the `owned_gift_id` field from fetch_managed_gifts().
    """
    payload = {
        "authData": TONNEL_AUTH_DATA,
        "gift_id": owned_gift_id,
    }
    for attempt, delay in enumerate(_BACKOFFS + [None], 1):
        try:
            resp = _requests.post(
                f"{_COFFIN_URL}/api/quickTransfer",
                headers=_WEB_HEADERS,
                json=payload,
                impersonate="chrome",
                proxies=_PROXIES,
                timeout=15,
            )
            if resp.status_code in (429, 403):
                if delay is None:
                    raise RuntimeError(f"quickTransfer: {resp.status_code}")
                logger.warning(f"quickTransfer 429/403 — retry {attempt} в {delay}с")
                time.sleep(delay)
                continue
            resp.raise_for_status()
            result = resp.json()
            if result.get("status") == "success":
                logger.info(f"✅ Instant Transfer: owned_gift_id={owned_gift_id} → @giftrelayer")
                return True
            msg = result.get("message") or result.get("error") or result.get("msg", "")
            if "already sent" in msg or "Gift not found" in msg:
                logger.info(f"ℹ️ Instant Transfer: подарок {owned_gift_id} уже был передан ранее")
                return True  # treat as success — gift is already on Tonnel
            logger.warning(f"quickTransfer не успешен: {msg or result}")
            return False
        except RuntimeError:
            raise
        except Exception as exc:
            if delay is None:
                logger.error(f"quickTransfer ошибка: {exc}")
                return False
            logger.warning(f"quickTransfer ошибка (попытка {attempt}): {exc}")
            time.sleep(delay)
    return False


def multi_instant_transfer(owned_gift_ids: list[int]) -> bool:
    """
    Deposit multiple managed gifts in a single request via /api/multiInstantTransfer.
    Equivalent to selecting several gifts and clicking "Multi Transfer" in Mini App.
    Costs 25 Stars per gift; handled by Tonnel's bot — no MTProto restriction.
    """
    payload = {
        "authData": TONNEL_AUTH_DATA,
        "gift_ids": owned_gift_ids,
    }
    for attempt, delay in enumerate(_BACKOFFS + [None], 1):
        try:
            resp = _requests.post(
                f"{_COFFIN_URL}/api/multiInstantTransfer",
                headers=_WEB_HEADERS,
                json=payload,
                impersonate="chrome",
                proxies=_PROXIES,
                timeout=20,
            )
            if resp.status_code in (429, 403):
                if delay is None:
                    raise RuntimeError(f"multiInstantTransfer: {resp.status_code}")
                logger.warning(f"multiInstantTransfer 429/403 — retry {attempt} в {delay}с")
                time.sleep(delay)
                continue
            resp.raise_for_status()
            result = resp.json()
            if result.get("status") == "success":
                logger.info(f"✅ Multi Instant Transfer: {len(owned_gift_ids)} подарков → @giftrelayer")
                return True
            msg = result.get("message") or result.get("error") or result.get("msg", "")
            if "already sent" in msg or "Gift not found" in msg:
                logger.info(f"ℹ️ Multi Transfer: подарки уже были переданы ранее")
                return True  # treat as success
            logger.warning(f"multiInstantTransfer не успешен: {msg or result}")
            return False
        except RuntimeError:
            raise
        except Exception as exc:
            if delay is None:
                logger.error(f"multiInstantTransfer ошибка: {exc}")
                return False
            logger.warning(f"multiInstantTransfer ошибка (попытка {attempt}): {exc}")
            time.sleep(delay)
    return False


def deposit_managed_gifts() -> tuple[int, int]:
    """
    Fetch all managed gifts (visible via Business Bot) and deposit them into Tonnel.
    - 1 gift  → /api/quickTransfer
    - 2+ gifts → /api/multiInstantTransfer  (single batch request)
    Returns (transferred, failed).
    """
    # Collect all gift ids across pages
    all_gifts: list[dict] = []
    page = 1
    while True:
        batch = fetch_managed_gifts(page)
        if not batch:
            break
        all_gifts.extend(batch)
        if len(batch) < 20:
            break
        page += 1

    if not all_gifts:
        return 0, 0

    valid = [(g.get("owned_gift_id"), g.get("gift", {}).get("base_name", "?"), g.get("gift", {}).get("number", "?"))
             for g in all_gifts if g.get("owned_gift_id")]
    invalid_count = len(all_gifts) - len(valid)

    if not valid:
        logger.warning("Нет owned_gift_id ни у одного управляемого подарка")
        return 0, invalid_count

    ids = [v[0] for v in valid]
    names = [f"{v[1]} #{v[2]}" for v in valid]
    logger.info(f"Управляемых подарков: {len(ids)} — {', '.join(names)}")

    if len(ids) == 1:
        ok = instant_transfer(ids[0])
        return (1, 0) if ok else (0, 1)
    else:
        ok = multi_instant_transfer(ids)
        return (len(ids), 0) if ok else (0, len(ids))


def process_unlisted_queue() -> tuple[int, int, list[str]]:
    """
    Check for new unlisted gifts and list them all.
    Returns (listed_count, failed_count, nft_links).
    nft_links: список ссылок вида https://t.me/nft/GiftName-12345 для успешно выставленных.
    """
    time.sleep(3)  # avoid CF rate-limit after instant_transfer
    try:
        unlisted = get_unlisted_gifts()
    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f"get_unlisted_gifts ошибка: {e}")
        return 0, 0, []

    if not unlisted:
        return 0, 0, []

    logger.info(f"Найдено неразмещённых: {len(unlisted)}")
    listed = failed = 0
    nft_links: list[str] = []

    for gift_raw in unlisted:
        if list_for_sale(gift_raw):
            listed += 1
            # Строим t.me/nft ссылку из имени и номера подарка
            gift_name = (gift_raw.get("gift_name") or gift_raw.get("name", "")).replace(" ", "-")
            gift_num = gift_raw.get("gift_num") or gift_raw.get("number") or gift_raw.get("num")
            if gift_name and gift_num:
                nft_links.append(f"https://t.me/nft/{gift_name}-{gift_num}")
        else:
            failed += 1
        time.sleep(0.5)  # soft rate limit

    return listed, failed, nft_links


# ── Balance & Withdrawal ─────────────────────────────────

def get_ton_balance() -> Optional[float]:
    """Return current TON balance on Tonnel. None on error."""
    try:
        data = _call(tonnelmp.info, authData=TONNEL_AUTH_DATA)
        # tonnelmp.info returns a dict; key may vary
        for key in ("balance", "ton", "TON", "tonBalance"):
            val = data.get(key)
            if val is not None:
                return float(val)
        logger.debug(f"info() response keys: {list(data.keys())}")
        return None
    except Exception as e:
        logger.error(f"get_ton_balance ошибка: {e}")
        return None


def try_withdraw() -> bool:
    """
    Withdraw TON if balance >= MIN_WITHDRAW_TON.
    Returns True if withdrawal was executed.
    """
    if not WITHDRAW_WALLET:
        logger.warning("WITHDRAW_WALLET не задан — вывод пропущен")
        return False

    balance = get_ton_balance()
    if balance is None:
        return False

    if balance < MIN_WITHDRAW_TON:
        logger.debug(f"Баланс {balance} TON < {MIN_WITHDRAW_TON} — вывод не нужен")
        return False

    # Leave 0.01 TON for potential fees
    amount = round(balance - 0.01, 4)
    if amount < 0.5:
        logger.info(f"После вычета комиссии {amount} TON < 0.5 (минимум Tonnel)")
        return False

    logger.info(f"Вывод {amount} TON → {WITHDRAW_WALLET}")
    try:
        _call(
            tonnelmp.withdraw,
            wallet=WITHDRAW_WALLET,
            authData=TONNEL_AUTH_DATA,
            amount=amount,
            asset="TON",
        )
        notify_withdraw(amount, WITHDRAW_WALLET)
        return True
    except Exception as e:
        logger.error(f"Ошибка вывода: {e}")
        notify_withdraw_fail(str(e))
        return False
