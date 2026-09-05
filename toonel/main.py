"""
Tonnel NFT Gift Transfer Bot
════════════════════════════════════════════════════════════

Flow:
  1. Pyrogram session login (StringSession or phone+code)
  2. Get authData from Tonnel Mini App via RequestWebView
  3. Connect @Tonnel_Network_bot as Business Bot (grants gift rights)
  4. fetchMangedGifts → list gifts Tonnel sees via Business Bot
     quickTransfer(owned_gift_id) → @giftrelayer deposits each gift
     (no MTProto TransferStarGift — no 7-day new-session restriction)
  5. Polling loop: auto-list received gifts → auto-withdraw proceeds

Dependencies: pyrofork 2.3.69+ (layer 220), tonnelmp 1.2+
"""
import asyncio
import logging
import sys
from config import (
    API_ID, API_HASH, TONNEL_AUTH_DATA, TONNEL_BOT_USERNAME,
    CHECK_INTERVAL, AUTO_TRANSFER, MIN_WITHDRAW_TON, validate,
    LOG_PATH,
)
from session_manager import build_client, init_session, fetch_tonnel_init_data
from business_setup import is_tonnel_connected, connect_business_bot
from gifts_manager import fetch_all_gifts
from tonnel_market import (
    process_unlisted_queue, try_withdraw, get_ton_balance, deposit_managed_gifts,
)
from notifier import notify
from state import get_summary


# ── Logging ──────────────────────────────────────────────
def _setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
        ],
    )
    # Silence noisy third-party loggers
    for noisy in ("pyrogram", "httpx", "urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger("main")


# ── Polling loop ─────────────────────────────────────────
async def polling_loop() -> None:
    """
    Infinite loop: checks Tonnel every CHECK_INTERVAL seconds.
      - Lists newly received (unlisted) gifts for sale
      - Auto-withdraws when balance threshold is reached
    """
    logger.info(f"🔄 Polling loop запущен (интервал: {CHECK_INTERVAL}с)")
    notify(
        f"🤖 <b>Tonnel Bot активен</b>\n"
        f"Мониторинг подарков каждые {CHECK_INTERVAL}с\n"
        f"Автовывод при: ≥ {MIN_WITHDRAW_TON} TON"
    )

    iteration = 0
    while True:
        iteration += 1
        try:
            # ── Instant Transfer новых managed gifts ────
            transferred, tf_failed = deposit_managed_gifts()
            if transferred or tf_failed:
                logger.info(f"Iteration {iteration}: instant_transfer={transferred}, ошибок={tf_failed}")

            # ── List new gifts ──────────────────────────
            listed, failed = process_unlisted_queue()
            if listed or failed:
                logger.info(f"Iteration {iteration}: выставлено={listed}, ошибок={failed}")

            # ── Withdraw ────────────────────────────────
            withdrew = try_withdraw()
            if withdrew:
                logger.info(f"Iteration {iteration}: вывод выполнен")

            # ── Status every 10 iterations ──────────────
            if iteration % 10 == 0:
                bal = get_ton_balance()
                summary = get_summary()
                logger.info(
                    f"Status: баланс={bal} TON | "
                    f"трансференно={summary['transferred']} | "
                    f"выставлено={summary['listed']}"
                )

        except RuntimeError as e:
            # AUTH expired — stop the loop, user must restart
            logger.critical(f"КРИТИЧЕСКАЯ ОШИБКА: {e}")
            logger.critical("Перезапусти скрипт после обновления TONNEL_AUTH_DATA в .env")
            sys.exit(1)
        except Exception as e:
            logger.error(f"Polling ошибка: {e}")
            notify(f"⚠️ <b>Polling ошибка</b>\n<code>{str(e)[:300]}</code>")

        await asyncio.sleep(CHECK_INTERVAL)


# ── Main ─────────────────────────────────────────────────
async def main() -> None:
    _setup_logging()

    # ── Config validation ────────────────────────────────
    errors = validate()
    if errors:
        print("\n❌ Ошибки конфигурации:")
        for e in errors:
            print(f"   • {e}")
        print("\nСкопируй .env.example → .env и заполни все поля.\n")
        sys.exit(1)

    print("=" * 60)
    print("  TONNEL NFT GIFT TRANSFER BOT")
    print("  pyrofork layer 220 + tonnelmp 1.2")
    print("=" * 60)

    app = build_client()

    async with app:
        # ── 1. Session init ──────────────────────────────
        await init_session(app)
        me = await app.get_me()
        print(
            f"\n✅ Telegram аккаунт: {me.first_name} {me.last_name or ''} "
            f"(@{me.username or '—'})  ID: {me.id}\n"
        )

        # ── 2. Fetch authData from Tonnel Mini App ───────
        print("🔑 Получение authData из Tonnel Mini App...")
        import tonnel_market as _tm
        import config as _cfg
        try:
            init_data = await fetch_tonnel_init_data(app)
            _cfg.TONNEL_AUTH_DATA = init_data
            _tm.TONNEL_AUTH_DATA = init_data
            print("   ✅ authData получен из market.tonnel.network\n")
        except Exception as e:
            if _cfg.TONNEL_AUTH_DATA:
                print(f"   ⚠️  Не удалось получить authData из Mini App: {e}")
                print(f"   Используем TONNEL_AUTH_DATA из .env\n")
            else:
                print(f"\n   ❌ {e}")
                print("   Задай TONNEL_AUTH_DATA в .env вручную как запасной вариант.\n")
                sys.exit(1)

        # ── 4. Business Bot setup ────────────────────────
        print(f"⚙️  Проверка Business Bot ({TONNEL_BOT_USERNAME})...")
        already = await is_tonnel_connected(app, TONNEL_BOT_USERNAME)

        if already:
            print(f"   ✅ {TONNEL_BOT_USERNAME} уже подключён как Business Bot\n")
        else:
            print(f"   🔗 Подключаем {TONNEL_BOT_USERNAME}...")
            ok = await connect_business_bot(app, TONNEL_BOT_USERNAME)
            if ok:
                print(f"   ✅ Успешно подключён. Telegram нужно ~5с для активации.\n")
                notify(f"⚙️ <b>Business Bot подключён</b>\n{TONNEL_BOT_USERNAME}\nПрава: view/sell/transfer gifts")
                await asyncio.sleep(5)
            else:
                print(
                    f"\n   ⚠️  Не удалось подключить Business Bot через API.\n"
                    f"   Варианты:\n"
                    f"     A) Нет Telegram Business подписки на этом аккаунте\n"
                    f"     B) Подключи вручную:\n"
                    f"        Настройки → Telegram для бизнеса → Чат-боты\n"
                    f"        → вставь @Tonnel_Network_bot → разреши управление подарками\n"
                )
                choice = input("   Продолжить без Business Bot? [y/n]: ").strip().lower()
                if choice != "y":
                    sys.exit(0)

        # ── 5. Deposit managed gifts via Instant Transfer ────
        # fetchMangedGifts → list gifts Tonnel sees via Business Bot
        # quickTransfer(owned_gift_id) → @giftrelayer deposits to Tonnel
        # No MTProto TransferStarGift → no 7-day new-session restriction
        print("📤 Поиск подарков для Instant Transfer (через Business Bot API)...")
        print(f"{'─'*60}")
        transferred, failed = deposit_managed_gifts()
        if transferred or failed:
            print(f"   ✅ Передано: {transferred}  |  ❌ Ошибок: {failed}\n")
            notify(
                f"📤 <b>Instant Transfer</b>\n"
                f"✅ Передано: {transferred}\n"
                f"❌ Ошибок: {failed}"
            )
        else:
            print("   Управляемых подарков не найдено (Business Bot может видеть их не сразу)\n")
        print(f"{'─'*60}\n")

        # ── 7. Polling loop ──────────────────────────────
        # Pyrogram session stays alive during the loop
        await polling_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Остановлено пользователем")
        notify("🔴 <b>Tonnel Bot остановлен</b>")
