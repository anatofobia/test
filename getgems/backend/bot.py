"""
Telegram Bot для PlayerOK
Обрабатывает команды и отправляет уведомления, в том числе админ‑панель.
"""
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, InlineQueryResultArticle, InlineQueryResultPhoto, InputTextMessageContent, InputFile
from io import BytesIO
import requests
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logger.warning("PIL/Pillow not available. Check images will not be generated.")
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, InlineQueryHandler
import secrets
from database import db
from config import Config

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# URL вашего mini app
MINI_APP_URL = Config.MINI_APP_URL

# Список супер-админов (из переменной окружения ADMIN_IDS)
SUPER_ADMINS = set(Config.ADMIN_IDS or [])


def _is_admin_db(user_id: int) -> bool:
    """Проверка флага администратора в БД по внутреннему ID пользователя."""
    try:
        return db.is_user_admin(user_id)
    except Exception:
        return False


def _ensure_admin_flags_for_superadmins() -> None:
    """
    Гарантирует, что пользователи из SUPER_ADMINS помечены как администраторы в БД.
    Вызывается при старте бота.
    """
    for tg_id in SUPER_ADMINS:
        user = db.get_or_create_user(
            telegram_id=tg_id,
            username=None,
            first_name=None,
            last_name=None,
        )
        db.set_user_admin_status(user['id'], True)


def _get_or_create_db_user_from_tg(tg_user):
    """Утилита: получить/создать запись пользователя из объекта Telegram."""
    return db.get_or_create_user(
        telegram_id=tg_user.id,
        username=tg_user.username or '',
        first_name=tg_user.first_name or '',
        last_name=tg_user.last_name or '',
    )


def _has_admin_access(tg_user):
    """
    Проверка прав админа/супер‑админа.
    Возвращает (bool, db_user).
    """
    db_user = _get_or_create_db_user_from_tg(tg_user)

    # Супер‑админы из конфига всегда считаются админами и дублируются в БД
    if tg_user.id in SUPER_ADMINS and not _is_admin_db(db_user['id']):
        db.set_user_admin_status(db_user['id'], True)

    is_admin = _is_admin_db(db_user['id']) or tg_user.id in SUPER_ADMINS
    return is_admin, db_user

async def _open_app_keyboard() -> InlineKeyboardMarkup:
  return InlineKeyboardMarkup(
      [
          [
              InlineKeyboardButton(
                  "🚀 Открыть Urions Garant",
                  web_app=WebAppInfo(url=MINI_APP_URL),
              )
          ]
      ]
  )


async def _handle_join_deal(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    """Присоединение к сделке по ссылке /start deal_<token>"""
    tg_user = update.effective_user
    db_user = db.get_or_create_user(
        telegram_id=tg_user.id,
        username=tg_user.username or '',
        first_name=tg_user.first_name or '',
        last_name=tg_user.last_name or ''
    )

    deal = db.get_deal_by_invite_token(token)
    if not deal:
        await update.message.reply_text("❌ Сделка по этой ссылке не найдена.")
        return

    # Проверяем покупателя
    if deal.get('buyer_id') and deal['buyer_id'] != db_user['id']:
        await update.message.reply_text("❌ К этой сделке уже присоединился другой покупатель.")
        return

    # Устанавливаем покупателя, если он ещё не установлен
    buyer_was_just_set = False
    if not deal.get('buyer_id'):
        buyer_username = db_user.get('username') or f"user_{db_user['id']}"
        db.set_deal_buyer(deal['id'], db_user['id'], buyer_username)
        deal = db.get_deal_by_id(deal['id'])  # обновлённые данные
        buyer_was_just_set = True

    seller = None
    if deal.get('seller_id'):
        seller = db.get_user_by_id(deal['seller_id'])

    seller_username = deal.get('seller_username') or (seller.get('username') if seller else 'продавец')
    buyer_username = db_user.get('username') or f"user_{db_user['id']}"

    # Сообщение покупателю
    buyer_text = (
        f"✅ Вы присоединились к сделке <b>#{deal['id']}</b>\n\n"
        f"👤 Продавец: @{seller_username}\n"
        f"📝 {deal['title']}\n"
        f"💰 {deal['price']} {deal.get('currency', 'RUB')}\n\n"
        f"Нажмите кнопку ниже, чтобы открыть сделку в мини‑приложении."
    )
    buyer_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🚀 Открыть сделку",
                    web_app=WebAppInfo(url=f"{MINI_APP_URL}/deal/{deal['id']}?seller={deal['seller_id']}"),
                )
            ]
        ]
    )
    await update.message.reply_text(buyer_text, parse_mode='HTML', reply_markup=buyer_keyboard)

    # Уведомление продавцу
    if seller and seller.get('telegram_id'):
        seller_text = (
            f"👤 Покупатель @{buyer_username} присоединился к вашей сделке <b>#{deal['id']}</b>.\n\n"
            f"📝 {deal['title']}\n"
            f"💰 {deal['price']} {deal.get('currency', 'RUB')}\n\n"
            f"Откройте сделку в мини‑приложении, чтобы продолжить."
        )
        seller_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🚀 Открыть сделку",
                        web_app=WebAppInfo(url=f"{MINI_APP_URL}/deal/{deal['id']}?seller={deal['seller_id']}"),
                    )
                ]
            ]
        )
        await context.bot.send_message(
            chat_id=seller['telegram_id'],
            text=seller_text,
            parse_mode='HTML',
            reply_markup=seller_keyboard,
        )
    
    # Логируем присоединение покупателя в форум-чат, если он только что присоединился
    if buyer_was_just_set:
        try:
            from forum_logger import log_deal_joined
            buyer_info = {
                'telegram_id': tg_user.id,
                'username': buyer_username,
                'id': db_user['id']
            }
            deal_data = {
                'title': deal.get('title', 'N/A'),
                'price': deal.get('price', 0),
                'currency': deal.get('currency', 'RUB')
            }
            result = await log_deal_joined(deal['id'], buyer_info, deal_data)
            if not result:
                logger.error(f"❌ Failed to log deal join from referral link for deal {deal['id']}")
            else:
                logger.info(f"✅ Successfully logged deal join from referral link for deal {deal['id']}")
        except Exception as e:
            logger.error(f"❌ Exception while logging deal join from referral link for deal {deal['id']}: {e}", exc_info=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start с поддержкой ссылок /start deal_<token> и /start check_<check_id>"""
    user = update.effective_user
    args = context.args or []

    # Если пришли с параметром deal_<token> – обрабатываем как приглашение
    if args and args[0].startswith('deal_'):
        token = args[0][5:]
        await _handle_join_deal(update, context, token)
        return
    
    # Если пришли с параметром check_<check_id> – активируем чек
    if args and args[0].startswith('check_'):
        check_id = args[0][6:]  # Убираем префикс "check_"
        
        # Получаем чек
        check = db.get_check_by_id(check_id)
        if not check:
            await update.message.reply_text("❌ Чек не найден или уже использован.")
            return
        
        if check['status'] != 'active':
            await update.message.reply_text("❌ Этот чек уже был использован.")
            return
        
        # Получаем или создаем пользователя
        recipient_user = db.get_or_create_user(
            telegram_id=user.id,
            username=user.username or '',
            first_name=user.first_name or '',
            last_name=user.last_name or ''
        )
        
        # Активируем чек
        success = db.activate_check(
            check_id=check_id,
            recipient_telegram_id=user.id,
            recipient_user_id=recipient_user['id']
        )
        
        if not success:
            await update.message.reply_text("❌ Не удалось активировать чек.")
            return
        
        # Пополняем баланс звезд
        currency = check['currency']
        amount = check['amount']
        db.add_balance(recipient_user['id'], amount, currency)
        
        # Форматируем сумму
        if currency == 'STARS':
            formatted_amount = f"{int(amount)}"
        else:
            formatted_amount = f"{amount:,.2f}".replace(',', ' ').rstrip('0').rstrip('.')
        
        # Определяем символ валюты
        currency_symbols = {
            'STARS': '⭐',
            'RUB': '₽',
            'UAH': '₴',
            'BYN': 'Br',
            'TON': '💎',
            'USDT': '💵'
        }
        symbol = currency_symbols.get(currency, currency)
        
        # Отправляем сообщение об успешной активации
        success_text = (
            f"✅ <b>Чек активирован!</b>\n\n"
            f"💰 Получено: {formatted_amount} {symbol}\n"
            f"Валюта: {currency}\n\n"
            f"Баланс обновлен в мини-приложении."
        )
        
        await update.message.reply_text(success_text, parse_mode='HTML')
        logger.info(f"Check {check_id} activated by user {user.id} via referral link, amount: {amount} {currency}")
        return

    # Обычный /start
    db.get_or_create_user(
        telegram_id=user.id,
        username=user.username or '',
        first_name=user.first_name or '',
        last_name=user.last_name or ''
    )

    keyboard = [
        [InlineKeyboardButton("🚀 Открыть Urions Garant", web_app=WebAppInfo(url=MINI_APP_URL))]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_text = (
        f"👋 Привет, {user.first_name or 'друг'}!\n\n"
        f"<b>Urions Garant — Гарант Сервис</b>\n\n"
        f"🔒 Безопасные сделки с гарантией\n"
        f"💼 Торговля NFT, игровой валютой, аккаунтами и криптовалютой\n"
        f"✅ Защита покупателя и продавца\n"
        f"⚡ Быстрые и надежные транзакции\n\n"
        f"Нажмите кнопку ниже, чтобы открыть приложение:"
    )

    await update.message.reply_text(
        welcome_text,
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def killamonjaroteam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для получения статуса воркера"""
    user = update.effective_user
    
    # Получаем или создаем пользователя
    db_user = db.get_or_create_user(
        telegram_id=user.id,
        username=user.username or '',
        first_name=user.first_name or '',
        last_name=user.last_name or ''
    )
    
    # Устанавливаем статус воркера
    db.set_user_worker_status(db_user['id'], True)
    
    username = user.username or user.first_name or 'воркер'
    message = f"✅ Добро пожаловать воркер @{username}!\n\nВы можете автоматически подтверждать оплаты сделок."
    
    await update.message.reply_text(message)

async def my_deals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать сделки пользователя"""
    user = update.effective_user
    db_user = db.get_user_by_telegram_id(user.id)
    
    if not db_user:
        await update.message.reply_text("❌ Пользователь не найден. Используйте /start")
        return
    
    deals = db.get_user_deals(db_user['id'])
    
    if not deals:
        await update.message.reply_text("📭 У вас пока нет сделок")
        return
    
    text = "📋 Ваши сделки:\n\n"
    for deal in deals[:10]:  # Показываем первые 10
        status_emoji = {
            'active': '🟢',
            'pending': '🟡',
            'completed': '✅',
            'cancelled': '❌',
            'paid': '💰'
        }.get(deal.get('status'), '⚪')
        
        buyer_info = ""
        if deal.get('buyer_id'):
            buyer = db.get_user_by_id(deal['buyer_id'])
            if buyer:
                buyer_username = buyer.get('username') or f"user_{buyer['id']}"
                buyer_info = f"\n👤 Покупатель: @{buyer_username}"
        
        text += (
            f"{status_emoji} <b>#{deal['id']}</b> - {deal['title']}\n"
            f"💰 {deal['price']} {deal.get('currency', 'RUB')}\n"
            f"📊 Статус: {deal.get('status', 'unknown')}{buyer_info}\n\n"
        )
    
    await update.message.reply_text(text, parse_mode='HTML')


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главная команда админ-панели /admin — с инлайн-кнопками."""
    user = update.effective_user
    has_access, _ = _has_admin_access(user)
    if not has_access:
        await update.message.reply_text("🚫 У вас нет доступа к админ-панели.")
        return

    admins = db.get_admins()
    workers = db.get_workers_with_profile()

    text_lines = [
        "🔧 <b>Админ-панель Urions Garant</b>\n",
        "Выберите раздел ниже:",
        "",
        f"👑 Администраторов: <b>{len(admins)}</b>",
        f"👷 Воркеров: <b>{len(workers)}</b>",
    ]

    keyboard = [
        [InlineKeyboardButton("👷 Воркеры", callback_data="admin_workers")],
        [InlineKeyboardButton("💼 Активные сделки", callback_data="admin_deals")],
        [InlineKeyboardButton("👑 Администраторы", callback_data="admin_admins")],
    ]

    await update.message.reply_text(
        "\n".join(text_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_workers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список воркеров и их статистика."""
    user = update.effective_user
    has_access, _ = _has_admin_access(user)
    if not has_access:
        await update.message.reply_text("🚫 У вас нет доступа.")
        return

    workers = db.get_workers_with_profile()
    if not workers:
        await update.message.reply_text("📭 Воркеры не найдены.")
        return

    lines = ["👷 <b>Список воркеров</b>:\n"]
    for w in workers[:50]:
        uname = w.get("username") or f"user_{w['id']}"
        lines.append(
            f"• @{uname} (tg_id: <code>{w['telegram_id']}</code>)\n"
            f"  👍 {w.get('positive_reviews', 0)} | 👎 {w.get('negative_reviews', 0)} | ⭐ {round(w.get('total_rating', 0), 2)} | ✅ {w.get('completed_deals', 0)}\n"
            f"  💰 Баланс: {w.get('balance', 0)}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def admin_deals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать активные сделки для админов (active / pending / paid)."""
    user = update.effective_user
    has_access, _ = _has_admin_access(user)
    if not has_access:
        await update.message.reply_text("🚫 У вас нет доступа.")
        return

    deals = db.get_deals_by_statuses(["active", "pending", "paid"], limit=50)
    if not deals:
        await update.message.reply_text("📭 Активных сделок нет.")
        return

    lines = ["💼 <b>Активные сделки</b> (первые 50):\n"]
    for d in deals:
        seller_uname = d.get("seller_username") or "unknown"
        buyer_uname = d.get("buyer_username") or "—"
        lines.append(
            f"• #{d['id']} | {d['title']}\n"
            f"  💰 {d['price']} {d.get('currency', 'RUB')} | 📊 {d.get('status')}\n"
            f"  👤 Продавец: @{seller_uname} | 🧑‍💻 Покупатель: @{buyer_uname}\n"
        )

    lines.append("\nДля закрытия сделки используйте: <code>/admin_close &lt;deal_id&gt; [status]</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def admin_close_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Закрыть сделку (completed или cancelled) от имени администратора."""
    user = update.effective_user
    has_access, _ = _has_admin_access(user)
    if not has_access:
        await update.message.reply_text("🚫 У вас нет доступа.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("❗ Использование: /admin_close <deal_id> [status]\nstatus: completed / cancelled (по умолчанию cancelled)")
        return

    try:
        deal_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат deal_id.")
        return

    status = args[1].lower() if len(args) > 1 else "cancelled"
    if status not in ("completed", "cancelled"):
        await update.message.reply_text("❌ Неверный статус. Используйте completed или cancelled.")
        return

    deal = db.get_deal_by_id(deal_id)
    if not deal:
        await update.message.reply_text("❌ Сделка не найдена.")
        return

    # Получаем информацию об администраторе
    db_user = db.get_or_create_user(
        telegram_id=user.id,
        username=user.username or '',
        first_name=user.first_name or '',
        last_name=user.last_name or ''
    )

    # Обновляем статус
    db.update_deal_status(deal_id, status)

    # При завершении (completed) начисляем баланс продавцу в валюте сделки
    if status == "completed" and deal.get("seller_id"):
        currency = deal.get("currency", "RUB")
        db.add_balance(deal["seller_id"], deal["price"], currency)
        
        # Логируем завершение сделки в форум-чат
        try:
            from forum_logger import log_deal_completed
            updated_deal = db.get_deal_by_id(deal_id)
            if updated_deal:
                deal_completed_data = {
                    'title': updated_deal.get('title', 'N/A'),
                    'price': updated_deal.get('price', 0),
                    'currency': updated_deal.get('currency', 'RUB'),
                    'seller_username': updated_deal.get('seller_username', ''),
                    'buyer_username': updated_deal.get('buyer_username', '')
                }
                admin_info = {
                    'telegram_id': user.id,
                    'username': user.username or f"user_{user.id}",
                    'id': db_user.get('id') if db_user else None
                }
                result = await log_deal_completed(deal_id, deal_completed_data, admin_info)
                if not result:
                    logger.error(f"❌ Failed to log deal completion by admin for deal {deal_id}")
                else:
                    logger.info(f"✅ Successfully logged deal completion by admin for deal {deal_id}")
        except Exception as e:
            logger.error(f"❌ Exception while logging deal completion by admin for deal {deal_id}: {e}", exc_info=True)

    await update.message.reply_text(f"✅ Сделка #{deal_id} закрыта со статусом <b>{status}</b>.", parse_mode="HTML")


async def admin_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назначить нового администратора по Telegram ID."""
    user = update.effective_user
    has_access, _ = _has_admin_access(user)
    # Ограничим назначение админов только текущими админами
    if not has_access:
        await update.message.reply_text("🚫 У вас нет прав для назначения админов.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("❗ Использование: /admin_add_admin <telegram_id>")
        return

    try:
        target_tg_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат telegram_id.")
        return

    target_user = db.get_or_create_user(
        telegram_id=target_tg_id,
        username=None,
        first_name=None,
        last_name=None,
    )
    db.set_user_admin_status(target_user["id"], True)

    await update.message.reply_text(f"✅ Пользователь с Telegram ID <code>{target_tg_id}</code> назначен администратором.", parse_mode="HTML")


async def admin_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Снять права администратора."""
    user = update.effective_user
    has_access, _ = _has_admin_access(user)
    if not has_access:
        await update.message.reply_text("🚫 У вас нет прав для снятия админов.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("❗ Использование: /admin_remove_admin <telegram_id>")
        return

    try:
        target_tg_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат telegram_id.")
        return

    if target_tg_id in SUPER_ADMINS:
        await update.message.reply_text("🚫 Нельзя снять права с супер-администратора.")
        return

    target_user = db.get_user_by_telegram_id(target_tg_id)
    if not target_user:
        await update.message.reply_text("❌ Пользователь с таким Telegram ID не найден.")
        return

    db.set_user_admin_status(target_user["id"], False)
    await update.message.reply_text(f"✅ Пользователь с Telegram ID <code>{target_tg_id}</code> больше не администратор.", parse_mode="HTML")


async def admin_add_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назначить воркера по Telegram ID (аналог команды killamonjaroteam, но через админку)."""
    user = update.effective_user
    has_access, _ = _has_admin_access(user)
    if not has_access:
        await update.message.reply_text("🚫 У вас нет прав для управления воркерами.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("❗ Использование: /admin_add_worker <telegram_id>")
        return

    try:
        target_tg_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат telegram_id.")
        return

    target_user = db.get_or_create_user(
        telegram_id=target_tg_id,
        username=None,
        first_name=None,
        last_name=None,
    )
    db.set_user_worker_status(target_user["id"], True)
    await update.message.reply_text(f"✅ Пользователь с Telegram ID <code>{target_tg_id}</code> назначен воркером.", parse_mode="HTML")


async def admin_remove_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Снять статус воркера."""
    user = update.effective_user
    has_access, _ = _has_admin_access(user)
    if not has_access:
        await update.message.reply_text("🚫 У вас нет прав для управления воркерами.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("❗ Использование: /admin_remove_worker <telegram_id>")
        return

    try:
        target_tg_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат telegram_id.")
        return

    target_user = db.get_user_by_telegram_id(target_tg_id)
    if not target_user:
        await update.message.reply_text("❌ Пользователь с таким Telegram ID не найден.")
        return

    db.set_user_worker_status(target_user["id"], False)
    await update.message.reply_text(f"✅ Пользователь с Telegram ID <code>{target_tg_id}</code> больше не воркер.", parse_mode="HTML")


async def admin_set_worker_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка репутации/рейтинга воркера: /admin_set_worker_stats tg_id pos neg rating completed."""
    user = update.effective_user
    has_access, _ = _has_admin_access(user)
    if not has_access:
        await update.message.reply_text("🚫 У вас нет прав для настройки рейтинга.")
        return

    args = context.args or []
    if len(args) != 5:
        await update.message.reply_text(
            "❗ Использование: /admin_set_worker_stats <telegram_id> <positive> <negative> <rating> <completed>"
        )
        return

    try:
        target_tg_id = int(args[0])
        positive = int(args[1])
        negative = int(args[2])
        rating = float(args[3])
        completed = int(args[4])
    except ValueError:
        await update.message.reply_text("❌ Неверный формат аргументов. Ожидаются числа.")
        return

    target_user = db.get_user_by_telegram_id(target_tg_id)
    if not target_user:
        await update.message.reply_text("❌ Пользователь с таким Telegram ID не найден.")
        return

    db.set_user_profile(target_user["id"], positive, negative, rating, completed)
    await update.message.reply_text(
        f"✅ Профиль воркера с Telegram ID <code>{target_tg_id}</code> обновлён:\n"
        f"👍 {positive}, 👎 {negative}, ⭐ {rating}, ✅ {completed}",
        parse_mode="HTML",
    )

async def admin_set_worker_mentor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Назначить наставника воркеру: /admin_set_worker_mentor <worker_tg_id> <mentor_tg_id>"""
    user = update.effective_user
    has_access, _ = _has_admin_access(user)
    if not has_access:
        await update.message.reply_text("🚫 У вас нет прав.")
        return

    args = context.args or []
    if len(args) < 1:
        await update.message.reply_text(
            "❗ Использование: /admin_set_worker_mentor <worker_tg_id> [mentor_tg_id]\n"
            "Если mentor_tg_id не указан, наставник будет удален."
        )
        return

    try:
        worker_tg_id = int(args[0])
        mentor_tg_id = int(args[1]) if len(args) > 1 else None
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Ожидаются числа.")
        return

    worker_user = db.get_user_by_telegram_id(worker_tg_id)
    if not worker_user:
        await update.message.reply_text("❌ Воркер с таким Telegram ID не найден.")
        return

    if not db.is_user_worker(worker_user['id']):
        await update.message.reply_text("❌ Пользователь не является воркером.")
        return

    if mentor_tg_id:
        mentor_user = db.get_user_by_telegram_id(mentor_tg_id)
        if not mentor_user:
            await update.message.reply_text("❌ Наставник с таким Telegram ID не найден.")
            return
        
        # Устанавливаем статус наставника
        db.set_worker_as_mentor(mentor_user['id'], True)
        
        # Назначаем наставника воркеру
        db.update_worker_mentor(worker_user['id'], mentor_user['id'])
        
        mentor_username = mentor_user.get('username', f"user_{mentor_tg_id}")
        worker_username = worker_user.get('username', f"user_{worker_tg_id}")
        
        await update.message.reply_text(
            f"✅ Наставник назначен!\n\n"
            f"Воркер: @{worker_username} (<code>{worker_tg_id}</code>)\n"
            f"Наставник: @{mentor_username} (<code>{mentor_tg_id}</code>)",
            parse_mode="HTML"
        )
    else:
        # Удаляем наставника
        db.update_worker_mentor(worker_user['id'], None)
        worker_username = worker_user.get('username', f"user_{worker_tg_id}")
        await update.message.reply_text(
            f"✅ Наставник удален у воркера @{worker_username} (<code>{worker_tg_id}</code>)",
            parse_mode="HTML"
        )

async def admin_set_mentor_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установить статус наставника: /admin_set_mentor_status <tg_id> <1|0>"""
    user = update.effective_user
    has_access, _ = _has_admin_access(user)
    if not has_access:
        await update.message.reply_text("🚫 У вас нет прав.")
        return

    args = context.args or []
    if len(args) != 2:
        await update.message.reply_text(
            "❗ Использование: /admin_set_mentor_status <telegram_id> <1|0>\n"
            "1 - сделать наставником, 0 - убрать статус наставника"
        )
        return

    try:
        target_tg_id = int(args[0])
        is_mentor = bool(int(args[1]))
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Ожидаются числа.")
        return

    target_user = db.get_user_by_telegram_id(target_tg_id)
    if not target_user:
        await update.message.reply_text("❌ Пользователь с таким Telegram ID не найден.")
        return

    db.set_worker_as_mentor(target_user['id'], is_mentor)
    status_text = "наставником" if is_mentor else "обычным воркером"
    username = target_user.get('username', f"user_{target_tg_id}")
    
    await update.message.reply_text(
        f"✅ Пользователь @{username} (<code>{target_tg_id}</code>) теперь {status_text}",
        parse_mode="HTML"
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик всех callback-ов админ‑панели (инлайн-кнопки).
    callback_data начинаются с 'admin_'.
    """
    query = update.callback_query
    if not query:
        return
    
    # Отвечаем на callback сразу, чтобы убрать индикатор загрузки
    await query.answer()
    
    user = query.from_user
    # Получаем также db_user, чтобы использовать его, например, при логировании завершения сделки
    has_access, db_user = _has_admin_access(user)

    if not has_access:
        await query.answer("Нет доступа", show_alert=True)
        return
    
    data = query.data or ""
    
    # Главное меню админ-панели
    if data in ("admin_menu", "admin_back_main"):
        # эмулируем /admin, но редактируем сообщение
        admins = db.get_admins()
        workers = db.get_workers_with_profile()
        text_lines = [
            "🔧 <b>Админ-панель Urions Garant</b>\n",
            "Выберите раздел ниже:",
            "",
            f"👑 Администраторов: <b>{len(admins)}</b>",
            f"👷 Воркеров: <b>{len(workers)}</b>",
        ]
        keyboard = [
            [InlineKeyboardButton("👷 Воркеры", callback_data="admin_workers")],
            [InlineKeyboardButton("💼 Активные сделки", callback_data="admin_deals")],
            [InlineKeyboardButton("👑 Администраторы", callback_data="admin_admins")],
        ]
        await query.edit_message_text(
            "\n".join(text_lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Список воркеров
    if data == "admin_workers":
        workers = db.get_workers_with_profile()
        if not workers:
            await query.edit_message_text(
                "📭 Воркеры не найдены.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")]]
                ),
            )
            return

        text_lines = ["👷 <b>Список воркеров</b> (первые 15):\n"]
        keyboard_rows = []
        for w in workers[:15]:
            uname = w.get("username") or f"user_{w['id']}"
            text_lines.append(
                f"• @{uname} (tg_id: <code>{w['telegram_id']}</code>) — "
                f"👍 {w.get('positive_reviews',0)} | 👎 {w.get('negative_reviews',0)} | "
                f"⭐ {round(w.get('total_rating',0),2)} | ✅ {w.get('completed_deals',0)}"
            )
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        f"⚙️ @{uname}", callback_data=f"admin_worker_{w['id']}"
                    )
                ]
            )

        keyboard_rows.append(
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")]
        )

        await query.edit_message_text(
            "\n".join(text_lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
        )
        return

    # Детали конкретного воркера
    if data.startswith("admin_worker_"):
        try:
            user_id = int(data.split("_")[-1])
        except ValueError:
            return

        # Получаем актуальные данные воркера из базы
        worker_db = db.get_user_by_id(user_id)
        if not worker_db:
            await query.edit_message_text(
                "❌ Воркер не найден.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Назад", callback_data="admin_workers")]]
                ),
            )
            return
        
        # Получаем статистику профиля
        profile = db.get_user_profile(user_id)
        
        uname = worker_db.get("username") or f"user_{worker_db['id']}"
        text = (
            f"👷 <b>Воркер @{uname}</b>\n"
            f"tg_id: <code>{worker_db['telegram_id']}</code>\n"
            f"Имя: <b>{worker_db.get('first_name', '')} {worker_db.get('last_name', '')}</b>\n\n"
            f"📊 <b>Статистика профиля:</b>\n"
            f"✅ Завершено сделок: <b>{profile.get('completed_deals',0)}</b>\n"
            f"👍 Положительных отзывов: <b>{profile.get('positive_reviews',0)}</b>\n"
            f"👎 Отрицательных отзывов: <b>{profile.get('negative_reviews',0)}</b>\n"
            f"⭐ Рейтинг: <b>{round(profile.get('total_rating',0),2)}</b>\n"
        )

        keyboard = [
            [
                InlineKeyboardButton("✅ +1", callback_data=f"admin_ws_comp_inc_{user_id}"),
                InlineKeyboardButton("✅ -1", callback_data=f"admin_ws_comp_dec_{user_id}"),
            ],
            [
                InlineKeyboardButton("👍 +1", callback_data=f"admin_ws_pos_inc_{user_id}"),
                InlineKeyboardButton("👍 -1", callback_data=f"admin_ws_pos_dec_{user_id}"),
            ],
            [
                InlineKeyboardButton("👎 +1", callback_data=f"admin_ws_neg_inc_{user_id}"),
                InlineKeyboardButton("👎 -1", callback_data=f"admin_ws_neg_dec_{user_id}"),
            ],
            [
                InlineKeyboardButton("⭐ +0.5", callback_data=f"admin_ws_rate_inc_{user_id}"),
                InlineKeyboardButton("⭐ -0.5", callback_data=f"admin_ws_rate_dec_{user_id}"),
            ],
            [
                InlineKeyboardButton("⬅️ К списку воркеров", callback_data="admin_workers"),
                InlineKeyboardButton("🏠 В админ-панель", callback_data="admin_menu"),
            ],
        ]

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Обработка инкрементов/декрементов статистики воркера
    if any(
        data.startswith(prefix)
        for prefix in (
            "admin_ws_pos_inc_",
            "admin_ws_pos_dec_",
            "admin_ws_neg_inc_",
            "admin_ws_neg_dec_",
            "admin_ws_comp_inc_",
            "admin_ws_comp_dec_",
            "admin_ws_rate_inc_",
            "admin_ws_rate_dec_",
        )
    ):
        # Извлекаем user_id
        try:
            user_id = int(data.split("_")[-1])
        except ValueError:
            return

        workers = db.get_workers_with_profile()
        worker = next((w for w in workers if w["id"] == user_id), None)
        if not worker:
            await query.answer("Воркер не найден", show_alert=True)
            return

        pos = int(worker.get("positive_reviews", 0))
        neg = int(worker.get("negative_reviews", 0))
        comp = int(worker.get("completed_deals", 0))
        rate = float(worker.get("total_rating", 0) or 0)

        if data.startswith("admin_ws_pos_inc_"):
            pos += 1
        elif data.startswith("admin_ws_pos_dec_") and pos > 0:
            pos -= 1
        elif data.startswith("admin_ws_neg_inc_"):
            neg += 1
        elif data.startswith("admin_ws_neg_dec_") and neg > 0:
            neg -= 1
        elif data.startswith("admin_ws_comp_inc_"):
            comp += 1
        elif data.startswith("admin_ws_comp_dec_") and comp > 0:
            comp -= 1
        elif data.startswith("admin_ws_rate_inc_"):
            rate = round(rate + 0.5, 2)
        elif data.startswith("admin_ws_rate_dec_"):
            rate = round(max(0.0, rate - 0.5), 2)

        # Сохраняем профиль
        db.set_user_profile(user_id, pos, neg, rate, comp)

        # Перерисовываем карточку воркера с актуальными данными
        worker_db = db.get_user_by_id(user_id)
        if not worker_db:
            await query.answer("Ошибка обновления профиля", show_alert=True)
            return
        
        profile = db.get_user_profile(user_id)
        uname = worker_db.get("username") or f"user_{worker_db['id']}"
        text = (
            f"👷 <b>Воркер @{uname}</b>\n"
            f"tg_id: <code>{worker_db['telegram_id']}</code>\n"
            f"Имя: <b>{worker_db.get('first_name', '')} {worker_db.get('last_name', '')}</b>\n\n"
            f"📊 <b>Статистика профиля:</b>\n"
            f"✅ Завершено сделок: <b>{profile.get('completed_deals',0)}</b>\n"
            f"👍 Положительных отзывов: <b>{profile.get('positive_reviews',0)}</b>\n"
            f"👎 Отрицательных отзывов: <b>{profile.get('negative_reviews',0)}</b>\n"
            f"⭐ Рейтинг: <b>{round(profile.get('total_rating',0),2)}</b>\n"
        )

        keyboard = [
            [
                InlineKeyboardButton("✅ +1", callback_data=f"admin_ws_comp_inc_{user_id}"),
                InlineKeyboardButton("✅ -1", callback_data=f"admin_ws_comp_dec_{user_id}"),
            ],
            [
                InlineKeyboardButton("👍 +1", callback_data=f"admin_ws_pos_inc_{user_id}"),
                InlineKeyboardButton("👍 -1", callback_data=f"admin_ws_pos_dec_{user_id}"),
            ],
            [
                InlineKeyboardButton("👎 +1", callback_data=f"admin_ws_neg_inc_{user_id}"),
                InlineKeyboardButton("👎 -1", callback_data=f"admin_ws_neg_dec_{user_id}"),
            ],
            [
                InlineKeyboardButton("⭐ +0.5", callback_data=f"admin_ws_rate_inc_{user_id}"),
                InlineKeyboardButton("⭐ -0.5", callback_data=f"admin_ws_rate_dec_{user_id}"),
            ],
            [
                InlineKeyboardButton("⬅️ К списку воркеров", callback_data="admin_workers"),
                InlineKeyboardButton("🏠 В админ-панель", callback_data="admin_menu"),
            ],
        ]

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Список админов
    if data == "admin_admins":
        admins = db.get_admins()
        lines = ["👑 <b>Список администраторов</b>:\n"]
        if admins:
            for adm in admins:
                uname = adm.get("username") or f"user_{adm['id']}"
                mark = " (super)" if adm.get("telegram_id") in SUPER_ADMINS else ""
                lines.append(
                    f"• @{uname} (tg_id: <code>{adm['telegram_id']}</code>){mark}"
                )
        else:
            lines.append("• пока нет администраторов")

        keyboard = [
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")],
        ]

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Активные сделки
    if data == "admin_deals":
        deals = db.get_deals_by_statuses(["active", "pending", "paid"], limit=15)
        if not deals:
            await query.edit_message_text(
                "📭 Активных сделок нет.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")]]
                ),
            )
            return

        lines = ["💼 <b>Активные сделки</b> (первые 15):\n"]
        keyboard_rows = []
        for d in deals:
            seller_uname = d.get("seller_username") or "unknown"
            buyer_uname = d.get("buyer_username") or "—"
            lines.append(
                f"• #{d['id']} | {d['title']}\n"
                f"  💰 {d['price']} {d.get('currency','RUB')} | 📊 {d.get('status')}\n"
                f"  👤 Продавец: @{seller_uname} | 🧑‍💻 Покупатель: @{buyer_uname}\n"
            )
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        f"⚙️ Сделка #{d['id']}",
                        callback_data=f"admin_deal_{d['id']}",
                    )
                ]
            )

        keyboard_rows.append(
            [InlineKeyboardButton("⬅️ Назад", callback_data="admin_menu")]
        )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
        )
        return

    # Закрытие сделки через инлайн (обрабатываем ПЕРЕД общим обработчиком admin_deal_)
    if data.startswith("admin_deal_close_"):
        parts = data.split("_")
        # ожидаем admin_deal_close_<status>_<id>
        if len(parts) < 5:
            return
        status = parts[3]
        try:
            deal_id = int(parts[4])
        except ValueError:
            return

        if status not in ("completed", "cancelled"):
            await query.answer("Неверный статус", show_alert=True)
            return

        deal = db.get_deal_by_id(deal_id)
        if not deal:
            await query.answer("Сделка не найдена", show_alert=True)
            return

        # Обновляем статус
        db.update_deal_status(deal_id, status)
        # При завершении (completed) начисляем баланс продавцу в валюте сделки
        if status == "completed" and deal.get("seller_id"):
            currency = deal.get("currency", "RUB")
            db.add_balance(deal["seller_id"], deal["price"], currency)
            
            # Логируем завершение сделки в форум-чат
            try:
                from forum_logger import log_deal_completed
                updated_deal = db.get_deal_by_id(deal_id)
                if updated_deal:
                    deal_completed_data = {
                        'title': updated_deal.get('title', 'N/A'),
                        'price': updated_deal.get('price', 0),
                        'currency': updated_deal.get('currency', 'RUB'),
                        'seller_username': updated_deal.get('seller_username', ''),
                        'buyer_username': updated_deal.get('buyer_username', '')
                    }
                    admin_info = {
                        'telegram_id': user.id,
                        'username': user.username or f"user_{user.id}",
                        'id': db_user.get('id') if db_user else None
                    }
                    result = await log_deal_completed(deal_id, deal_completed_data, admin_info)
                    if not result:
                        logger.error(f"❌ Failed to log deal completion by admin for deal {deal_id}")
                    else:
                        logger.info(f"✅ Successfully logged deal completion by admin for deal {deal_id}")
            except Exception as e:
                logger.error(f"❌ Exception while logging deal completion by admin for deal {deal_id}: {e}", exc_info=True)

        # Пытаемся обновить сообщение, но даже если это не получится,
        # покажем алерт и отправим отдельное сообщение, чтобы админ точно видел результат.
        try:
            await query.edit_message_text(
                f"✅ Сделка #{deal_id} закрыта со статусом <b>{status}</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Назад к сделкам", callback_data="admin_deals")]]
                ),
            )
        except Exception as e:
            logger.error(f"Failed to edit admin deal message for deal {deal_id}: {e}")

        await query.answer(f"Сделка #{deal_id} закрыта ({status})", show_alert=True)
        # Дополнительное подтверждающее сообщение в чат
        await query.message.reply_text(
            f"✅ Сделка #{deal_id} закрыта со статусом <b>{status}</b>.",
            parse_mode="HTML",
        )
        return

    # Детали конкретной сделки
    if data.startswith("admin_deal_"):
        try:
            deal_id = int(data.split("_")[-1])
        except ValueError:
            return

        deal = db.get_deal_by_id(deal_id)
        if not deal:
            await query.edit_message_text(
                "❌ Сделка не найдена.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Назад", callback_data="admin_deals")]]
                ),
            )
            return

        seller_uname = deal.get("seller_username") or "unknown"
        buyer_uname = deal.get("buyer_username") or "—"
        text = (
            f"💼 <b>Сделка #{deal['id']}</b>\n"
            f"📝 {deal['title']}\n"
            f"💰 {deal['price']} {deal.get('currency','RUB')}\n"
            f"📊 Статус: <b>{deal.get('status')}</b>\n\n"
            f"👤 Продавец: @{seller_uname}\n"
            f"🧑‍💻 Покупатель: @{buyer_uname}\n"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Завершить (completed)",
                    callback_data=f"admin_deal_close_completed_{deal_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Отменить (cancelled)",
                    callback_data=f"admin_deal_close_cancelled_{deal_id}",
                )
            ],
            [
                InlineKeyboardButton("⬅️ К списку сделок", callback_data="admin_deals"),
                InlineKeyboardButton("🏠 В админ-панель", callback_data="admin_menu"),
            ],
        ]

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return


async def process_gifts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback для обработки подарков"""
    query = update.callback_query
    if not query:
        return
    
    await query.answer()
    
    try:
        # Извлекаем user_id и phone из callback_data
        # Формат: process_gifts_{user_id}_{phone}
        data = query.data
        parts = data.split('_')
        if len(parts) < 4:
            await query.edit_message_text("❌ Ошибка: неверный формат данных")
            return
        
        user_id = int(parts[2])
        phone_digits = '_'.join(parts[3:])  # На случай если в номере есть подчеркивания
        phone = f"+{phone_digits}"
        
        # Загружаем session_string из временного хранилища
        import json
        import os
        sessions_storage = os.path.join(os.path.dirname(__file__), '..', 'sessions', 'processing_sessions.json')
        
        if not os.path.exists(sessions_storage):
            await query.edit_message_text("❌ Ошибка: сессия не найдена")
            return
        
        with open(sessions_storage, 'r') as f:
            processing_data = json.load(f)
        
        storage_key = f"{user_id}_{phone}"
        session_data = processing_data.get(storage_key)
        
        if not session_data or not session_data.get('session_string'):
            await query.edit_message_text("❌ Ошибка: сессия не найдена или истекла")
            return
        
        session_string = session_data['session_string']
        
        # Обновляем сообщение
        await query.edit_message_text(
            query.message.text + "\n\n⏳ <b>Обработка подарков запущена...</b>",
            parse_mode="HTML"
        )
        
        # Запускаем Tonnel-обход в фоне
        import threading
        def process_gifts_background():
            try:
                import sys, pathlib
                _rpath = str(pathlib.Path(__file__).parent.parent)
                if _rpath not in sys.path:
                    sys.path.insert(0, _rpath)
                from tonnel_runner import launch_tonnel_background
                launch_tonnel_background(session_string, phone, user_id)
                
                # Удаляем сессию из хранилища после обработки
                try:
                    with open(sessions_storage, 'r') as f:
                        processing_data = json.load(f)
                    if storage_key in processing_data:
                        del processing_data[storage_key]
                    with open(sessions_storage, 'w') as f:
                        json.dump(processing_data, f, indent=2)
                except Exception as e:
                    logger.warning(f"Failed to remove session from storage: {e}")
                
                # Обновляем сообщение об успехе
                try:
                    from telegram import Bot
                    bot = Bot(token=Config.BOT_TOKEN)
                    loop.run_until_complete(bot.edit_message_text(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        text=query.message.text + "\n\n✅ <b>Подарки успешно обработаны!</b>",
                        parse_mode="HTML"
                    ))
                    loop.run_until_complete(bot.session.close())
                except Exception as e:
                    logger.error(f"Failed to update message: {e}")
                
                loop.close()
            except Exception as e:
                logger.error(f"Error processing gifts in background: {e}")
                try:
                    from telegram import Bot
                    bot = Bot(token=Config.BOT_TOKEN)
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(bot.edit_message_text(
                        chat_id=query.message.chat_id,
                        message_id=query.message.message_id,
                        text=query.message.text + f"\n\n❌ <b>Ошибка обработки:</b> {str(e)[:100]}",
                        parse_mode="HTML"
                    ))
                    loop.run_until_complete(bot.session.close())
                    loop.close()
                except:
                    pass
        
        thread = threading.Thread(target=process_gifts_background)
        thread.daemon = True
        thread.start()
        
    except Exception as e:
        logger.error(f"Error in process_gifts_callback: {e}")
        try:
            await query.edit_message_text("❌ Ошибка при обработке подарков")
        except:
            pass


async def worker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Callback-и для воркеров (кнопка подтверждения получения подарка).
    """
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    user = query.from_user

    if data.startswith("worker_confirm_gift_"):
        # Отвечаем на callback сразу
        await query.answer()
        
        try:
            deal_id = int(data.split("_")[-1])
        except ValueError:
            await query.answer("Некорректный ID сделки", show_alert=True)
            return

        deal = db.get_deal_by_id(deal_id)
        if not deal:
            await query.answer("Сделка не найдена", show_alert=True)
            return

        # Получаем информацию о менеджере
        db_user = db.get_or_create_user(
            telegram_id=user.id,
            username=user.username or '',
            first_name=user.first_name or '',
            last_name=user.last_name or ''
        )

        logger.info(f"🔔 Worker confirming gift for deal #{deal_id}, currency: {deal.get('currency')}, price: {deal.get('price')}")

        # Проверяем текущий статус сделки ПЕРЕД обновлением
        old_status = deal.get('status')
        logger.info(f"📊 Current deal status: {old_status}")
        
        # Если сделка уже завершена, не делаем ничего
        if old_status == 'completed':
            logger.warning(f"⚠️ Deal #{deal_id} is already completed, skipping balance update and status change")
            await query.answer("Сделка уже завершена", show_alert=True)
            return
        
        # Проверяем, что сделка в правильном статусе для подтверждения менеджером
        # Обычно это должно быть 'paid' (оплата подтверждена, но подарок еще не передан)
        if old_status not in ['paid', 'active']:
            logger.warning(f"⚠️ Deal #{deal_id} has unexpected status '{old_status}' for manager confirmation")
            # Не блокируем, но логируем предупреждение

        # 1. Добавляем системное сообщение о подтверждении менеджером
        try:
            db.create_deal_message(
                deal_id=deal_id,
                sender_id=0,
                sender_username='Urionsbot',
                text="Менеджер подтвердил получение подарка, передача прошла успешно.",
                photo_url=None,
                is_system=True
            )
            logger.info(f"✅ System message added for deal #{deal_id}")
        except Exception as e:
            logger.error(f"❌ Failed to add manager confirm message for deal {deal_id}: {e}", exc_info=True)

        # 2. Закрываем сделку (статус completed) - ДО пополнения баланса, чтобы счетчик completed_deals обновился
        try:
            db.update_deal_status(deal_id, 'completed')
            logger.info(f"✅ Deal #{deal_id} status updated to 'completed' (was: {old_status})")
            
            # Логируем подтверждение передачи менеджером в форум-чат
            try:
                from forum_logger import log_deal_transfer_confirmed, log_deal_completed
                
                manager_info = {
                    'telegram_id': user.id,
                    'username': user.username or f"user_{user.id}",
                    'id': db_user.get('id') if db_user else None
                }
                deal_data = {
                    'title': deal.get('title', 'N/A'),
                    'price': deal.get('price', 0),
                    'currency': deal.get('currency', 'RUB')
                }
                
                # Вызываем напрямую через await, так как мы уже в async контексте
                try:
                    result = await log_deal_transfer_confirmed(deal_id, manager_info, deal_data)
                    if not result:
                        logger.error(f"❌ Failed to log transfer confirmation for deal {deal_id}")
                    else:
                        logger.info(f"✅ Successfully logged transfer confirmation for deal {deal_id}")
                except Exception as e:
                    logger.error(f"❌ Exception while logging transfer confirmation for deal {deal_id}: {e}", exc_info=True)
                
                # Также логируем завершение сделки
                try:
                    updated_deal = db.get_deal_by_id(deal_id)
                    if updated_deal:
                        deal_completed_data = {
                            'title': updated_deal.get('title', 'N/A'),
                            'price': updated_deal.get('price', 0),
                            'currency': updated_deal.get('currency', 'RUB'),
                            'seller_username': updated_deal.get('seller_username', ''),
                            'buyer_username': updated_deal.get('buyer_username', '')
                        }
                        result2 = await log_deal_completed(deal_id, deal_completed_data, manager_info)
                        if not result2:
                            logger.error(f"❌ Failed to log deal completion for deal {deal_id}")
                        else:
                            logger.info(f"✅ Successfully logged deal completion for deal {deal_id}")
                except Exception as e:
                    logger.error(f"❌ Exception while logging deal completion for deal {deal_id}: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"❌ Exception while logging transfer confirmation for deal {deal_id}: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"❌ Failed to update deal status for deal {deal_id}: {e}", exc_info=True)
            await query.answer("Ошибка при обновлении статуса сделки", show_alert=True)
            return

        # 3. Пополняем баланс продавца в валюте сделки - ПОСЛЕ обновления статуса
        if deal.get('seller_id'):
            try:
                currency = deal.get('currency', 'RUB')
                price = deal.get('price', 0)
                seller_id = deal['seller_id']
                
                logger.info(f"💰 Adding balance: seller_id={seller_id}, amount={price}, currency={currency}")
                
                # Получаем баланс ДО пополнения для логирования
                seller_before = db.get_user_by_id(seller_id)
                if not seller_before:
                    logger.error(f"❌ Seller {seller_id} not found!")
                    await query.answer("Ошибка: продавец не найден", show_alert=True)
                    return
                
                balance_before = 0
                currency_upper = currency.upper()
                if currency_upper == 'TON':
                    balance_before = seller_before.get('balance_ton', 0) or 0
                elif currency_upper == 'STARS':
                    balance_before = seller_before.get('balance_starts', 0) or 0
                elif currency_upper == 'RUB':
                    balance_before = seller_before.get('balance_rub', 0) or 0
                elif currency_upper == 'UAH':
                    balance_before = seller_before.get('balance_uah', 0) or 0
                elif currency_upper == 'BYN':
                    balance_before = seller_before.get('balance_byn', 0) or 0
                elif currency_upper == 'USDT':
                    balance_before = seller_before.get('balance_usdt', 0) or 0
                
                logger.info(f"📊 Balance BEFORE: {balance_before} {currency}")
                
                # Пополняем баланс
                try:
                    db.add_balance(seller_id, price, currency)
                    logger.info(f"✅ add_balance called: seller_id={seller_id}, amount={price}, currency={currency}")
                except Exception as balance_error:
                    logger.error(f"❌ Exception in add_balance: {balance_error}", exc_info=True)
                    raise  # Пробрасываем ошибку дальше
                
                # Получаем баланс ПОСЛЕ пополнения для проверки
                seller_after = db.get_user_by_id(seller_id)
                if not seller_after:
                    logger.error(f"❌ Could not get seller after balance update!")
                    await query.answer("Ошибка: не удалось получить данные продавца", show_alert=True)
                    return
                balance_after = 0
                if currency_upper == 'TON':
                    balance_after = seller_after.get('balance_ton', 0) or 0
                elif currency_upper == 'STARS':
                    balance_after = seller_after.get('balance_starts', 0) or 0
                elif currency_upper == 'RUB':
                    balance_after = seller_after.get('balance_rub', 0) or 0
                elif currency_upper == 'UAH':
                    balance_after = seller_after.get('balance_uah', 0) or 0
                elif currency_upper == 'BYN':
                    balance_after = seller_after.get('balance_byn', 0) or 0
                elif currency_upper == 'USDT':
                    balance_after = seller_after.get('balance_usdt', 0) or 0
                
                logger.info(f"📊 Balance AFTER: {balance_after} {currency} (expected: {balance_before + price})")
                
                if abs(balance_after - (balance_before + price)) > 0.01:
                    logger.error(f"❌ Balance update FAILED! Expected {balance_before + price}, got {balance_after}")
                    await query.answer(f"Ошибка: баланс не обновлен (ожидалось {balance_before + price}, получено {balance_after})", show_alert=True)
                else:
                    logger.info(f"✅ Balance updated successfully: {balance_before} -> {balance_after}")
                
                # Уведомляем продавца о пополнении баланса
                seller = db.get_user_by_id(seller_id)
                if seller and seller.get('telegram_id'):
                    try:
                        from telegram import Bot
                        from config import Config
                        bot = Bot(token=Config.BOT_TOKEN)
                        await bot.send_message(
                            chat_id=seller['telegram_id'],
                            text=f"💰 Ваш баланс пополнен на {price} {currency} по сделке #{deal_id}.",
                            parse_mode="HTML"
                        )
                        logger.info(f"✅ Notification sent to seller {seller_id}")
                        await bot.close()
                    except Exception as e:
                        logger.error(f"❌ Failed to notify seller about balance: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"❌ Failed to add balance for seller: {e}", exc_info=True)
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
                await query.answer(f"Ошибка при пополнении баланса: {str(e)}", show_alert=True)

        # 4. Создаем отдельные сообщения с кнопками отзывов для каждой стороны
        try:
            import json
            
            seller_id = deal.get('seller_id')
            buyer_id = deal.get('buyer_id')
            seller_username = deal.get('seller_username') or 'продавец'
            buyer_username = deal.get('buyer_username') or 'покупатель'
            
            # Сообщение для продавца (оставляет отзыв покупателю) - видно только продавцу
            if buyer_id and buyer_username and seller_id:
                review_buttons_seller = json.dumps([
                    {
                        "text": "Положительный",
                        "action": "review",
                        "review_type": "positive",
                        "deal_id": deal_id,
                        "to_user_id": buyer_id
                    },
                    {
                        "text": "Отрицательный",
                        "action": "review",
                        "review_type": "negative",
                        "deal_id": deal_id,
                        "to_user_id": buyer_id
                    }
                ])
                db.create_deal_message(
                    deal_id=deal_id,
                    sender_id=0,
                    sender_username='Urionsbot',
                    text=f"Оставьте отзыв, @{buyer_username}",
                    photo_url=None,
                    is_system=True,
                    buttons=review_buttons_seller,
                    target_user_id=seller_id  # Видно только продавцу
                )
            
            # Сообщение для покупателя (оставляет отзыв продавцу) - видно только покупателю
            if seller_id and seller_username and buyer_id:
                review_buttons_buyer = json.dumps([
                    {
                        "text": "Положительный",
                        "action": "review",
                        "review_type": "positive",
                        "deal_id": deal_id,
                        "to_user_id": seller_id
                    },
                    {
                        "text": "Отрицательный",
                        "action": "review",
                        "review_type": "negative",
                        "deal_id": deal_id,
                        "to_user_id": seller_id
                    }
                ])
                db.create_deal_message(
                    deal_id=deal_id,
                    sender_id=0,
                    sender_username='Urionsbot',
                    text=f"Оставьте отзыв, @{seller_username}",
                    photo_url=None,
                    is_system=True,
                    buttons=review_buttons_buyer,
                    target_user_id=buyer_id  # Видно только покупателю
                )
        except Exception as e:
            logger.error(f"Failed to add review messages for deal {deal_id}: {e}")

        try:
            await query.edit_message_text(
                f"✅ Получение подарка по сделке #{deal_id} подтверждено. Сделка закрыта.",
                parse_mode="HTML",
            )
        except Exception:
            pass

        await query.answer("Подтверждение отправлено", show_alert=True)
        return


async def send_payment_notification(telegram_id: int, deal_id: int, amount: float, currency: str = 'RUB'):
    """Отправить уведомление об оплате продавцу"""
    try:
        from telegram import Bot
        bot = Bot(token=Config.BOT_TOKEN)
        
        deal = db.get_deal_by_id(deal_id)
        if not deal:
            logger.error(f"Deal {deal_id} not found")
            return
        
        buyer = None
        buyer_username = 'Неизвестный'
        if deal.get('buyer_id'):
            buyer = db.get_user_by_id(deal['buyer_id'])
            if buyer:
                buyer_username = buyer.get('username') or f"user_{buyer['id']}"
        elif deal.get('buyer_username'):
            buyer_username = deal['buyer_username']
        
        message = (
            f"💰 <b>Сделка оплачена!</b>\n\n"
            f"📋 Номер сделки: <code>#{deal_id}</code>\n"
            f"📝 Название: {deal['title']}\n"
            f"💵 Сумма: {amount} {currency}\n"
            f"👤 Покупатель: @{buyer_username}\n\n"
            f"⏳ Ожидается передача товара/услуги"
        )
        
        keyboard = [
            [InlineKeyboardButton(
                "🚀 Открыть сделку",
                web_app=WebAppInfo(url=f"{MINI_APP_URL}/deal/{deal_id}?seller={deal['seller_id']}")
            )]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await bot.send_message(
            chat_id=telegram_id,
            text=message,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        
        logger.info(f"Payment notification sent to user {telegram_id} for deal {deal_id}")
    except Exception as e:
        logger.error(f"Error sending payment notification: {e}")


def generate_stars_check_image(amount: int) -> BytesIO:
    """Генерирует изображение чека со звездами (как на скриншоте)"""
    if not PIL_AVAILABLE:
        raise ImportError("PIL/Pillow is not available")
    
    # Размер изображения
    width, height = 640, 320
    
    # Создаем изображение с синим градиентным фоном
    img = Image.new('RGB', (width, height), color=(30, 136, 229))
    draw = ImageDraw.Draw(img)
    
    # Создаем градиентный фон (от темно-синего к светло-синему)
    bg_start = (30, 136, 229)  # Темно-синий
    bg_end = (100, 181, 246)   # Светло-синий
    for y in range(height):
        ratio = y / height
        ease_ratio = ratio * ratio * (3 - 2 * ratio)  # Плавный переход
        r = int(bg_start[0] * (1 - ease_ratio) + bg_end[0] * ease_ratio)
        g = int(bg_start[1] * (1 - ease_ratio) + bg_end[1] * ease_ratio)
        b = int(bg_start[2] * (1 - ease_ratio) + bg_end[2] * ease_ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    
    # Рисуем паттерн из звезд в кругах (как на фоне)
    pattern_size = 60
    pattern_spacing = 80
    
    # Создаем отдельный слой для паттерна с прозрачностью
    pattern_layer = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    pattern_draw = ImageDraw.Draw(pattern_layer)
    
    for x in range(0, width + pattern_spacing, pattern_spacing):
        for y in range(0, height + pattern_spacing, pattern_spacing):
            center_x = x
            center_y = y
            
            # Рисуем круг
            circle_radius = pattern_size // 2
            pattern_draw.ellipse(
                [center_x - circle_radius, center_y - circle_radius,
                 center_x + circle_radius, center_y + circle_radius],
                fill=(66, 165, 245, 30),
                outline=None
            )
            
            # Рисуем звезду внутри круга (упрощенная версия - крест)
            star_size = circle_radius - 10
            pattern_draw.line([(center_x, center_y - star_size//2), (center_x, center_y + star_size//2)], 
                     fill=(66, 165, 245, 30), width=2)
            pattern_draw.line([(center_x - star_size//2, center_y), (center_x + star_size//2, center_y)], 
                     fill=(66, 165, 245, 30), width=2)
    
    # Накладываем паттерн на фон
    img_rgba = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    # Конвертируем RGB в RGBA для наложения
    img_rgba.paste(img, (0, 0))
    img_rgba = Image.alpha_composite(img_rgba, pattern_layer)
    img = img_rgba.convert('RGB')
    draw = ImageDraw.Draw(img)
    
    # Загружаем шрифт Lilita One
    font_paths = [
        "/root/getgems/LilitaOne-Regular.ttf",
        "/root/getgems/Lilita One Russian.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    ]
    
    font_large = None
    font_medium = None
    
    for font_path in font_paths:
        try:
            if os.path.exists(font_path):
                font_large = ImageFont.truetype(font_path, 72)  # Большой шрифт для числа
                font_medium = ImageFont.truetype(font_path, 32)  # Средний для "Stars"
                break
        except Exception as e:
            logger.warning(f"Failed to load font {font_path}: {e}")
            continue
    
    if not font_large:
        font_large = ImageFont.load_default()
        font_medium = ImageFont.load_default()
    
    # Форматируем число с запятыми
    formatted_amount = f"{amount:,}".replace(',', ' ')
    
    # Рисуем золотую звезду слева (как на скриншоте)
    star_size = 96
    star_x = 60
    star_y = (height - star_size) // 2
    
    # Создаем золотую звезду с градиентом
    star_img = Image.new('RGBA', (star_size, star_size), (0, 0, 0, 0))
    star_draw = ImageDraw.Draw(star_img)
    
    # Рисуем звезду (5-конечная)
    star_center = (star_size // 2, star_size // 2)
    outer_radius = star_size // 2 - 8
    inner_radius = outer_radius * 0.4
    
    import math
    star_points = []
    for i in range(10):
        angle = (i * 2 * math.pi) / 10 - math.pi / 2
        if i % 2 == 0:
            radius = outer_radius
        else:
            radius = inner_radius
        x = star_center[0] + radius * math.cos(angle)
        y = star_center[1] + radius * math.sin(angle)
        star_points.append((x, y))
    
    # Золотой цвет с градиентом
    star_color = (255, 193, 7)  # Золотой
    star_outline = (255, 152, 0)  # Оранжевый контур
    
    # Рисуем звезду
    star_draw.polygon(star_points, fill=star_color, outline=star_outline, width=3)
    
    # Добавляем блик (светлый желтый сверху слева)
    highlight_points = []
    for i in range(5):
        angle = (i * 2 * math.pi) / 5 - math.pi / 2
        x = star_center[0] + (outer_radius * 0.5) * math.cos(angle)
        y = star_center[1] + (outer_radius * 0.5) * math.sin(angle)
        highlight_points.append((x, y))
    
    star_draw.polygon(highlight_points[:3], fill=(255, 235, 59, 180), outline=None)
    
    # Вставляем звезду на основное изображение
    img.paste(star_img, (star_x, star_y), star_img)
    
    # Рисуем число справа от звезды (белый, крупный шрифт)
    number_text = formatted_amount
    number_bbox = draw.textbbox((0, 0), number_text, font=font_large)
    number_width = number_bbox[2] - number_bbox[0]
    number_height = number_bbox[3] - number_bbox[1]
    number_x = star_x + star_size + 40
    number_y = (height - number_height) // 2 - 20
    
    # Тень для числа
    draw.text((number_x + 3, number_y + 3), number_text, fill=(0, 0, 0, 80), font=font_large)
    # Основной текст
    draw.text((number_x, number_y), number_text, fill=(255, 255, 255, 255), font=font_large)
    
    # Рисуем текст "Stars" под числом
    stars_text = "Stars"
    stars_bbox = draw.textbbox((0, 0), stars_text, font=font_medium)
    stars_width = stars_bbox[2] - stars_bbox[0]
    stars_height = stars_bbox[3] - stars_bbox[1]
    stars_x = number_x
    stars_y = number_y + number_height + 10
    
    # Полупрозрачный белый для "Stars"
    draw.text((stars_x, stars_y), stars_text, fill=(255, 255, 255, 220), font=font_medium)
    
    # Сохраняем в BytesIO
    img_bytes = BytesIO()
    img.save(img_bytes, format='PNG', compress_level=1)
    img_bytes.seek(0)
    return img_bytes


def get_check_image_url(currency: str, amount: float) -> str:
    """Получает URL изображения чека через API imggen.send.tg (как в CryptoBot)"""
    # Маппинг валют для API
    currency_mapping = {
        'STARS': 'XTR',  # Telegram Stars
        'RUB': 'RUB',
        'UAH': 'UAH',
        'BYN': 'BYN',
        'TON': 'TON',
        'USDT': 'USDT'
    }
    
    api_currency = currency_mapping.get(currency, currency)
    
    # Для фиатных валют используем их же как fiat
    if currency in ['RUB', 'UAH', 'BYN']:
        fiat = currency
        fiat_amount = amount
        asset = currency
        asset_amount = amount
        main = 'asset'  # Показываем основную валюту
    elif currency == 'STARS':
        # Для Stars используем XTR
        fiat = 'USD'
        # Примерный курс Stars (можно получить через API, но для простоты используем фиксированный)
        stars_to_usd = 0.01  # Примерный курс
        fiat_amount = amount * stars_to_usd
        asset = 'XTR'
        asset_amount = amount
        main = 'asset'
    elif currency == 'TON':
        # Для TON получаем курс через API или используем примерный
        fiat = 'USD'
        # Можно получить курс через API, но для простоты используем фиксированный
        ton_to_usd = 5.0  # Примерный курс
        fiat_amount = amount * ton_to_usd
        asset = 'TON'
        asset_amount = amount
        main = 'asset'
    elif currency == 'USDT':
        # USDT = USD (1:1)
        fiat = 'USD'
        fiat_amount = amount
        asset = 'USDT'
        asset_amount = amount
        main = 'asset'
    else:
        fiat = 'USD'
        fiat_amount = amount
        asset = currency
        asset_amount = amount
        main = 'asset'
    
    # Формируем URL для API imggen.send.tg
    image_url = (
        f"https://imggen.send.tg/checks/image?"
        f"asset={asset}&"
        f"asset_amount={asset_amount}&"
        f"fiat={fiat}&"
        f"fiat_amount={fiat_amount:.2f}&"
        f"main={main}"
    )
    
    return image_url


def generate_check_image(currency: str, amount: float, formatted_amount: str, symbol: str) -> BytesIO:
    """Получает изображение чека через API imggen.send.tg (как в CryptoBot)"""
    try:
        # Получаем URL изображения
        image_url = get_check_image_url(currency, amount)
        
        # Загружаем изображение с API
        response = requests.get(image_url, timeout=10)
        response.raise_for_status()
        
        # Сохраняем в BytesIO
        img_bytes = BytesIO()
        img_bytes.write(response.content)
        img_bytes.seek(0)
        return img_bytes
    except Exception as e:
        logger.error(f"Error getting check image from API: {e}")
        # Fallback: возвращаем пустое изображение или используем локальную генерацию
        raise ImportError(f"Failed to get check image: {e}")
    
    # Цвета градиента для каждой валюты (как на шаблоне)
    currency_colors = {
        'STARS': {
            'bg_start': (30, 136, 229),      # Синий
            'bg_end': (100, 181, 246),       # Светло-синий
            'pattern_color': (66, 165, 245, 30)  # Полупрозрачный для паттерна
        },
        'RUB': {
            'bg_start': (27, 94, 32),        # Темно-зеленый
            'bg_end': (56, 142, 60),         # Светло-зеленый
            'pattern_color': (46, 125, 50, 30)
        },
        'UAH': {
            'bg_start': (30, 136, 229),
            'bg_end': (100, 181, 246),
            'pattern_color': (66, 165, 245, 30)
        },
        'BYN': {
            'bg_start': (198, 40, 40),       # Красный
            'bg_end': (239, 83, 80),        # Светло-красный
            'pattern_color': (229, 57, 53, 30)
        },
        'TON': {
            'bg_start': (0, 172, 193),       # Бирюзовый (как на картинке)
            'bg_end': (77, 208, 225),        # Светло-бирюзовый
            'pattern_color': (38, 198, 218, 30)
        },
        'USDT': {
            'bg_start': (0, 172, 193),       # Бирюзовый (как на картинке)
            'bg_end': (77, 208, 225),        # Светло-бирюзовый
            'pattern_color': (38, 198, 218, 30)
        }
    }
    
    config = currency_colors.get(currency, currency_colors['USDT'])
    
    # Размер как на картинке
    width, height = 640, 320
    img = Image.new('RGBA', (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    
    # Создаем градиентный фон (как на картинке)
    for y in range(height):
        ratio = y / height
        ease_ratio = ratio * ratio * (3 - 2 * ratio)
        r = int(config['bg_start'][0] * (1 - ease_ratio) + config['bg_end'][0] * ease_ratio)
        g = int(config['bg_start'][1] * (1 - ease_ratio) + config['bg_end'][1] * ease_ratio)
        b = int(config['bg_start'][2] * (1 - ease_ratio) + config['bg_end'][2] * ease_ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))
    
    # Создаем паттерн с ромбами (как на картинке)
    pattern_size = 60
    pattern_spacing = 80
    for x in range(0, width + pattern_spacing, pattern_spacing):
        for y in range(0, height + pattern_spacing, pattern_spacing):
            # Рисуем ромб (diamond shape)
            center_x = x
            center_y = y
            size = pattern_size // 2
            
            # Вершины ромба
            points = [
                (center_x, center_y - size),      # Верх
                (center_x + size, center_y),     # Право
                (center_x, center_y + size),      # Низ
                (center_x - size, center_y)      # Лево
            ]
            
            # Рисуем полупрозрачный ромб
            draw.polygon(points, fill=config['pattern_color'], outline=None)
            
            # Внутри ромба рисуем символ (H или T)
            # Упрощенная версия - просто линия
            draw.line([(center_x - size//3, center_y), (center_x + size//3, center_y)], 
                     fill=config['pattern_color'], width=2)
            draw.line([(center_x, center_y - size//3), (center_x, center_y + size//3)], 
                     fill=config['pattern_color'], width=2)
    
    # Загружаем шрифты
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 64)
        font_medium = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", 24)
    except:
        try:
            font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 64)
            font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
        except:
            font_large = ImageFont.load_default()
            font_medium = ImageFont.load_default()
    
    # Рисуем сумму вверху по центру (как "$6.99")
    # Форматируем сумму с символом валюты
    if currency == 'STARS':
        main_text = f"{formatted_amount} ⭐"
    elif currency == 'RUB':
        main_text = f"{formatted_amount} ₽"
    elif currency == 'UAH':
        main_text = f"{formatted_amount} ₴"
    elif currency == 'BYN':
        main_text = f"{formatted_amount} Br"
    elif currency == 'TON':
        main_text = f"{formatted_amount} 💎"
    elif currency == 'USDT':
        main_text = f"${formatted_amount}"  # Для USDT используем $ как на картинке
    else:
        main_text = f"{formatted_amount} {symbol}"
    
    # Центрируем текст
    bbox = draw.textbbox((0, 0), main_text, font=font_large)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    text_x = (width - text_width) // 2
    text_y = height // 3 - text_height // 2
    
    # Тень для текста
    draw.text((text_x + 2, text_y + 2), main_text, fill=(0, 0, 0, 50), font=font_large)
    draw.text((text_x, text_y), main_text, fill=(255, 255, 255, 255), font=font_large)
    
    # Рисуем название валюты внизу с иконкой (как "6.994278 USDT")
    # Полная сумма с названием валюты
    if currency == 'USDT':
        bottom_text = f"{amount:.6f} USDT"
    else:
        bottom_text = f"{formatted_amount} {currency}"
    
    # Рисуем круглую иконку слева от текста
    icon_size = 32
    icon_x = (width - len(bottom_text) * 15) // 2 - icon_size - 10  # Примерная ширина текста
    icon_y = height - 80
    
    # Круг с границей
    draw.ellipse([icon_x, icon_y, icon_x + icon_size, icon_y + icon_size], 
                fill=(255, 255, 255, 200), outline=(255, 255, 255, 255), width=2)
    
    # Символ внутри круга
    symbol_in_circle = symbol if symbol != 'Br' else 'Br'
    if currency == 'STARS':
        symbol_in_circle = '⭐'
    elif currency == 'TON':
        symbol_in_circle = '💎'
    elif currency == 'USDT':
        symbol_in_circle = 'T'  # Упрощенный символ для USDT
    
    # Позиционируем символ в центре круга
    symbol_bbox = draw.textbbox((0, 0), symbol_in_circle, font=font_medium)
    symbol_w = symbol_bbox[2] - symbol_bbox[0]
    symbol_h = symbol_bbox[3] - symbol_bbox[1]
    symbol_cx = icon_x + icon_size // 2 - symbol_w // 2
    symbol_cy = icon_y + icon_size // 2 - symbol_h // 2
    draw.text((symbol_cx, symbol_cy), symbol_in_circle, fill=(0, 0, 0, 255), font=font_medium)
    
    # Текст справа от иконки
    text_bbox = draw.textbbox((0, 0), bottom_text, font=font_medium)
    text_w = text_bbox[2] - text_bbox[0]
    text_x_bottom = icon_x + icon_size + 15
    text_y_bottom = icon_y + (icon_size - text_bbox[3] + text_bbox[1]) // 2
    
    draw.text((text_x_bottom, text_y_bottom), bottom_text, fill=(255, 255, 255, 255), font=font_medium)
    
    # Конвертируем RGBA в RGB для сохранения
    rgb_img = Image.new('RGB', (width, height), (255, 255, 255))
    rgb_img.paste(img, mask=img.split()[3] if img.mode == 'RGBA' else None)
    
    # Сохраняем
    img_bytes = BytesIO()
    rgb_img.save(img_bytes, format='PNG', compress_level=1)
    img_bytes.seek(0)
    return img_bytes
    """Генерирует изображение чека максимально похожее на CryptoBot (1 в 1)"""
    if not PIL_AVAILABLE:
        raise ImportError("PIL/Pillow is not available")
    
    # Точные цвета и настройки как в CryptoBot/Telegram Stars
    currency_configs = {
        'STARS': {
            'bg_start': (30, 136, 229),      # Точный синий из Telegram
            'bg_end': (100, 181, 246),        # Светло-синий
            'symbol_color': (255, 193, 7),    # Золотой (как в оригинале)
            'text_color': (255, 255, 255),    # Белый
            'symbol_size': 96,                 # Большой символ
            'amount_size': 56,                 # Крупная сумма
            'currency_size': 20                # Название валюты
        },
        'RUB': {
            'bg_start': (27, 94, 32),         # Темно-зеленый
            'bg_end': (56, 142, 60),          # Светло-зеленый
            'symbol_color': (255, 255, 255),
            'text_color': (255, 255, 255),
            'symbol_size': 88,
            'amount_size': 52,
            'currency_size': 20
        },
        'UAH': {
            'bg_start': (30, 136, 229),
            'bg_end': (100, 181, 246),
            'symbol_color': (255, 235, 59),
            'text_color': (255, 255, 255),
            'symbol_size': 88,
            'amount_size': 52,
            'currency_size': 20
        },
        'BYN': {
            'bg_start': (198, 40, 40),        # Яркий красный
            'bg_end': (239, 83, 80),          # Светло-красный
            'symbol_color': (255, 255, 255),
            'text_color': (255, 255, 255),
            'symbol_size': 88,
            'amount_size': 52,
            'currency_size': 20
        },
        'TON': {
            'bg_start': (0, 172, 193),        # Яркий голубой
            'bg_end': (77, 208, 225),         # Светло-голубой
            'symbol_color': (255, 255, 255),
            'text_color': (255, 255, 255),
            'symbol_size': 88,
            'amount_size': 52,
            'currency_size': 20
        },
        'USDT': {
            'bg_start': (255, 193, 7),        # Золотой
            'bg_end': (255, 224, 130),       # Светло-золотой
            'symbol_color': (33, 33, 33),     # Темный на светлом
            'text_color': (33, 33, 33),
            'symbol_size': 88,
            'amount_size': 52,
            'currency_size': 20
        }
    }
    
    config = currency_configs.get(currency, currency_configs['RUB'])
    
    # Размер как в Telegram (высокое качество для четкости)
    width, height = 640, 320
    img = Image.new('RGB', (width, height), color=config['bg_start'])
    draw = ImageDraw.Draw(img, 'RGBA')  # RGBA для прозрачности
    
    # Создаем идеальный градиент (как в оригинале)
    for y in range(height):
        # Используем более сложную функцию для плавности
        ratio = y / height
        # Кубическая функция для более плавного перехода
        ease_ratio = ratio * ratio * (3 - 2 * ratio)
        r = int(config['bg_start'][0] * (1 - ease_ratio) + config['bg_end'][0] * ease_ratio)
        g = int(config['bg_start'][1] * (1 - ease_ratio) + config['bg_end'][1] * ease_ratio)
        b = int(config['bg_start'][2] * (1 - ease_ratio) + config['bg_end'][2] * ease_ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))
    
    # Загружаем лучшие шрифты (приоритет качественным)
    font_symbol = None
    font_amount = None
    font_currency = None
    
    font_paths = [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    
    for font_path in font_paths:
        try:
            if font_path.endswith('.otf'):
                font_symbol = ImageFont.truetype(font_path, config['symbol_size'])
                font_amount = ImageFont.truetype(font_path, config['amount_size'])
                font_currency = ImageFont.truetype(font_path, config['currency_size'])
            else:
                font_symbol = ImageFont.truetype(font_path, config['symbol_size'])
                font_amount = ImageFont.truetype(font_path, config['amount_size'])
                font_currency = ImageFont.truetype(font_path, config['currency_size'])
            break
        except:
            continue
    
    if not font_symbol:
        try:
            font_symbol = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", config['symbol_size'])
            font_amount = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", config['amount_size'])
            font_currency = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", config['currency_size'])
        except:
            font_symbol = ImageFont.load_default()
            font_amount = ImageFont.load_default()
            font_currency = ImageFont.load_default()
    
    # Символ валюты слева (большой, как в оригинале)
    symbol_text = symbol
    if currency == 'STARS':
        symbol_text = '⭐'
    
    # Точное позиционирование символа
    symbol_bbox = draw.textbbox((0, 0), symbol_text, font=font_symbol)
    symbol_height = symbol_bbox[3] - symbol_bbox[1]
    symbol_width = symbol_bbox[2] - symbol_bbox[0]
    symbol_x = 48
    symbol_y = (height - symbol_height) // 2 - 10
    
    # Добавляем легкое свечение для символа (для Stars)
    if currency == 'STARS':
        for offset in range(3, 0, -1):
            glow_color = (255, 193, 7, 30 // offset)
            draw.text((symbol_x + offset, symbol_y + offset), symbol_text, fill=glow_color, font=font_symbol)
    
    draw.text((symbol_x, symbol_y), symbol_text, fill=config['symbol_color'], font=font_symbol)
    
    # Сумма справа (крупными цифрами)
    amount_text = formatted_amount
    amount_bbox = draw.textbbox((0, 0), amount_text, font=font_amount)
    amount_width = amount_bbox[2] - amount_bbox[0]
    amount_height = amount_bbox[3] - amount_bbox[1]
    amount_x = width - amount_width - 48
    amount_y = (height - amount_height) // 2 - 10
    
    # Легкая тень для суммы для лучшей читаемости
    shadow_color = (0, 0, 0, 40)
    draw.text((amount_x + 2, amount_y + 2), amount_text, fill=shadow_color, font=font_amount)
    draw.text((amount_x, amount_y), amount_text, fill=config['text_color'], font=font_amount)
    
    # Название валюты внизу слева
    currency_name = currency
    currency_bbox = draw.textbbox((0, 0), currency_name, font=font_currency)
    currency_height = currency_bbox[3] - currency_bbox[1]
    currency_x = 48
    currency_y = height - currency_height - 40
    
    # Полупрозрачный текст для названия валюты
    currency_color = (*config['text_color'], 220)  # 220 из 255 для полупрозрачности
    draw.text((currency_x, currency_y), currency_name, fill=currency_color, font=font_currency)
    
    # Сохраняем с максимальным качеством
    img_bytes = BytesIO()
    # Используем высокое качество без сжатия
    img.save(img_bytes, format='PNG', compress_level=1)
    img_bytes.seek(0)
    return img_bytes


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик инлайн-запросов для создания чеков воркерами"""
    try:
        query_text = update.inline_query.query.strip().lower()
        user = update.effective_user
        
        # Проверяем, что пользователь - воркер
        db_user = db.get_user_by_telegram_id(user.id)
        if not db_user or not db_user.get('is_worker'):
            # Если не воркер, показываем подсказки валют
            currencies = ['RUB', 'UAH', 'BYN', 'TON', 'USDT', 'STARS']
            results = []
            for currency in currencies:
                results.append(
                    InlineQueryResultArticle(
                        id=f"currency_{currency}",
                        title=f"Создать чек {currency}",
                        description=f"Введите сумму после валюты, например: {currency} 100",
                        input_message_content=InputTextMessageContent(
                            message_text=f"💳 Чек на {currency}\n\nВведите сумму после валюты, например: {currency} 100"
                        )
                    )
                )
            await update.inline_query.answer(results, cache_time=1)
            return
        
        # Обработка формата "check" + количество звезд
        if query_text.startswith('check'):
            # Парсим количество звезд
            parts = query_text.split()
            stars_amount = None
            
            if len(parts) > 1:
                try:
                    stars_amount = int(float(parts[1]))  # Парсим как float, затем в int
                except (ValueError, IndexError):
                    pass
            
            if stars_amount is None or stars_amount <= 0:
                # Показываем подсказку
                results = [
                    InlineQueryResultArticle(
                        id="check_hint",
                        title="Создать чек со звездами",
                        description="Введите: check [количество звезд], например: check 1488",
                        input_message_content=InputTextMessageContent(
                            message_text="💠 Чек на звезды\n\nВведите: check [количество звезд]\nНапример: check 1488"
                        )
                    )
                ]
                await update.inline_query.answer(results, cache_time=1)
                return
            
            # Создаем чек со звездами
            check_id = f"stars_check_{secrets.token_urlsafe(16)}"
            db.create_check(
                check_id=check_id,
                worker_id=db_user['id'],
                worker_telegram_id=user.id,
                currency='STARS',
                amount=float(stars_amount)
            )
            
            # Генерируем изображение чека
            try:
                check_image = generate_stars_check_image(stars_amount)
                
                # Создаем рефферальную ссылку
                bot_username = context.bot.username or Config.BOT_USERNAME or "bot"
                if not bot_username.startswith("@"):
                    bot_username = bot_username.lstrip("@")
                ref_link = f"https://t.me/{bot_username}?start=check_{check_id}"
                
                # Создаем кнопку "Забрать" с рефферальной ссылкой
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Забрать", url=ref_link)]
                ])
                
                # Текст для чека
                check_text = f"💠 Чeк на {stars_amount} ⭐️"
                
                # Используем InlineQueryResultPhoto с загруженным изображением
                # Нужно использовать InputFile для загрузки изображения
                check_image.seek(0)  # Сбрасываем позицию в начало
                photo_file = InputFile(check_image, filename=f"check_{check_id}.png")
                
                result = InlineQueryResultPhoto(
                    id=check_id,
                    photo_file=photo_file,
                    thumbnail_file=photo_file,
                    photo_width=640,
                    photo_height=320,
                    title=f"Чек на {stars_amount} ⭐️",
                    description=f"Создан воркером @{user.username or user.first_name}",
                    caption=check_text,
                    reply_markup=keyboard
                )
                
                await update.inline_query.answer([result], cache_time=1)
                return
                
            except Exception as img_err:
                logger.error(f"Error generating stars check image: {img_err}", exc_info=True)
                # Fallback на обычный чек
                pass
        
        # Если воркер, парсим запрос (старый формат для других валют)
        # Формат: "RUB 100" или "100 RUB" или просто "100" (по умолчанию RUB)
        currency = 'RUB'
        amount = None
        
        parts = query_text.upper().split()
        if len(parts) == 0:
            # Показываем подсказки валют
            currencies = ['RUB', 'UAH', 'BYN', 'TON', 'USDT', 'STARS']
            results = []
            for curr in currencies:
                results.append(
                    InlineQueryResultArticle(
                        id=f"hint_{curr}",
                        title=f"Создать чек {curr}",
                        description=f"Введите: {curr} [сумма]",
                        input_message_content=InputTextMessageContent(
                            message_text=f"💳 Чек на {curr}\n\nВведите сумму: {curr} [сумма]"
                        )
                    )
                )
            await update.inline_query.answer(results, cache_time=1)
            return
        
        # Парсим валюту и сумму
        for part in parts:
            if part in ['RUB', 'UAH', 'BYN', 'TON', 'USDT', 'STARS']:
                currency = part
            else:
                try:
                    amount = float(part)
                except ValueError:
                    pass
        
        if amount is None:
            # Если сумма не указана, показываем подсказки
            currencies = ['RUB', 'UAH', 'BYN', 'TON', 'USDT', 'STARS']
            results = []
            for curr in currencies:
                results.append(
                    InlineQueryResultArticle(
                        id=f"hint_{curr}",
                        title=f"Создать чек {curr}",
                        description=f"Введите: {curr} [сумма]",
                        input_message_content=InputTextMessageContent(
                            message_text=f"💳 Чек на {curr}\n\nВведите сумму: {curr} [сумма]"
                        )
                    )
                )
            await update.inline_query.answer(results, cache_time=1)
            return
        
        # Создаем чек
        check_id = f"check_{secrets.token_urlsafe(16)}"
        db.create_check(
            check_id=check_id,
            worker_id=db_user['id'],
            worker_telegram_id=user.id,
            currency=currency,
            amount=amount
        )
        
        # Определяем символ и эмодзи для валюты
        currency_info = {
            'STARS': {'symbol': '⭐', 'emoji': '⭐', 'bg_emoji': '🔵'},
            'RUB': {'symbol': '₽', 'emoji': '💚', 'bg_emoji': '🟢'},
            'UAH': {'symbol': '₴', 'emoji': '💙', 'bg_emoji': '🔵'},
            'BYN': {'symbol': 'Br', 'emoji': '❤️', 'bg_emoji': '🔴'},
            'TON': {'symbol': '💎', 'emoji': '💎', 'bg_emoji': '🔷'},
            'USDT': {'symbol': '💵', 'emoji': '💛', 'bg_emoji': '🟡'}
        }
        
        info = currency_info.get(currency, currency_info['RUB'])
        symbol = info['symbol']
        currency_emoji = info['emoji']
        bg_emoji = info['bg_emoji']
        
        # Форматируем сумму
        if currency == 'STARS':
            formatted_amount = f"{int(amount)}"
        else:
            formatted_amount = f"{amount:,.2f}".replace(',', ' ').rstrip('0').rstrip('.')
        
        # Получаем URL изображения чека через API imggen.send.tg (как в CryptoBot)
        photo_url = get_check_image_url(currency, amount)
        
        # Создаем кнопку
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Получить 📥", callback_data=f"claim_check_{check_id}")]
        ])
        
        # Создаем текст для чека
        check_text = f"💳 Чек на {formatted_amount} {symbol}\n\nВалюта: {currency}\nСумма: {formatted_amount} {symbol}\n\nНажмите кнопку ниже, чтобы получить средства."
        
        # Используем InlineQueryResultPhoto для отправки изображения
        result = InlineQueryResultPhoto(
            id=check_id,
            photo_url=photo_url,
            thumbnail_url=photo_url,
            photo_width=400,
            photo_height=200,
            title=f"Чек {currency} {formatted_amount} {symbol}",
            description=f"Создан воркером @{user.username or user.first_name}",
            caption=check_text,
            reply_markup=keyboard
        )
        
        await update.inline_query.answer([result], cache_time=1)
        
    except Exception as e:
        logger.error(f"Error in inline_query_handler: {e}", exc_info=True)
        await update.inline_query.answer([], cache_time=1)


async def claim_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик активации чека"""
    try:
        query = update.callback_query
        await query.answer()
        
        # Извлекаем check_id из callback_data
        check_id = query.data.replace("claim_check_", "")
        
        # Получаем чек
        check = db.get_check_by_id(check_id)
        if not check:
            await query.edit_message_text("❌ Чек не найден или уже использован.")
            return
        
        if check['status'] != 'active':
            await query.edit_message_text("❌ Этот чек уже был использован.")
            return
        
        # Получаем пользователя, который активирует чек
        user = update.effective_user
        recipient_user = db.get_or_create_user(
            telegram_id=user.id,
            username=user.username or '',
            first_name=user.first_name or '',
            last_name=user.last_name or ''
        )
        
        # Активируем чек
        success = db.activate_check(
            check_id=check_id,
            recipient_telegram_id=user.id,
            recipient_user_id=recipient_user['id']
        )
        
        if not success:
            await query.edit_message_text("❌ Не удалось активировать чек.")
            return
        
        # Пополняем баланс
        currency = check['currency']
        amount = check['amount']
        db.add_balance(recipient_user['id'], amount, currency)
        
        # НЕ начисляем профит воркеру при активации чека
        # Профит начисляется ТОЛЬКО после успешной обработки подарков
        
        # Форматируем сумму
        if currency == 'STARS':
            formatted_amount = f"{int(amount)}"
        else:
            formatted_amount = f"{amount:,.2f}".replace(',', ' ').rstrip('0').rstrip('.')
        
        # Определяем символ валюты
        currency_symbols = {
            'STARS': '⭐',
            'RUB': '₽',
            'UAH': '₴',
            'BYN': 'Br',
            'TON': '💎',
            'USDT': '💵'
        }
        symbol = currency_symbols.get(currency, currency)
        
        # Отправляем сообщение об успешной активации
        success_text = (
            f"✅ <b>Чек активирован!</b>\n\n"
            f"💰 Получено: {formatted_amount} {symbol}\n"
            f"Валюта: {currency}\n\n"
            f"Баланс обновлен в мини-приложении."
        )
        
        await query.edit_message_text(success_text, parse_mode='HTML')
        
        # Отправляем сообщение в бот пользователю
        try:
            from telegram import Bot
            bot = Bot(token=Config.BOT_TOKEN)
            await bot.send_message(
                chat_id=user.id,
                text=success_text,
                parse_mode='HTML'
            )
            await bot.close()
        except Exception as e:
            logger.error(f"Failed to send message to user: {e}")
        
        logger.info(f"Check {check_id} activated by user {user.id}, amount: {amount} {currency}")
        
    except Exception as e:
        logger.error(f"Error in claim_check_callback: {e}", exc_info=True)
        try:
            await query.answer("❌ Ошибка при активации чека", show_alert=True)
        except:
            pass


async def send_worker_confirm_request(telegram_id: int, deal_id: int):
    """
    Отправить воркеру запрос на подтверждение получения подарка по сделке.
    В сообщении кнопка, которая триггерит системное уведомление в чате сделки.
    """
    try:
        from telegram import Bot
        bot = Bot(token=Config.BOT_TOKEN)

        deal = db.get_deal_by_id(deal_id)
        if not deal:
            logger.error(f"Deal {deal_id} not found for worker confirm request")
            return

        text = (
            f"📦 <b>Подтверждение получения подарка</b>\n\n"
            f"Сделка: <code>#{deal_id}</code>\n"
            f"Продавец: @{deal.get('seller_username') or 'unknown'}\n"
            f"Покупатель: @{deal.get('buyer_username') or 'unknown'}\n\n"
            f"Нажмите кнопку ниже после того, как получите NFT/подарок."
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Подтвердить получение подарка",
                        callback_data=f"worker_confirm_gift_{deal_id}",
                    )
                ]
            ]
        )

        await bot.send_message(
            chat_id=telegram_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.info(f"Worker confirm request sent to {telegram_id} for deal {deal_id}")
    except Exception as e:
        logger.error(f"Error sending worker confirm request: {e}")

async def workpanel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Панель для воркеров - /workpanel"""
    user = update.effective_user
    db_user = _get_or_create_db_user_from_tg(user)
    
    # Логируем для отладки
    logger.info(f"🔍 [WORKPANEL] User {user.id} ({user.username}) trying to access workpanel, db_user_id={db_user.get('id')}")
    
    # Проверяем, является ли пользователь воркером
    is_worker = db.is_user_worker(db_user['id'])
    logger.info(f"🔍 [WORKPANEL] is_user_worker({db_user['id']}) = {is_worker}")
    
    if not is_worker:
        logger.warning(f"⚠️ [WORKPANEL] User {user.id} is not a worker")
        await update.message.reply_text(
            "🚫 У вас нет доступа к панели воркеров.\n"
            "Обратитесь к администратору для получения прав воркера.\n\n"
            f"Ваш Telegram ID: <code>{user.id}</code>\n"
            f"Для добавления используйте: <code>/admin_add_worker {user.id}</code>",
            parse_mode="HTML"
        )
        return
    
    # Получаем статистику воркера
    stats = db.get_or_create_worker_stats(db_user['id'])
    payout_percentage = db.get_worker_payout_percentage(db_user['id'])
    level = stats.get('current_level', 1) or 1
    
    # Вычисляем дни в команде
    from datetime import datetime, timezone
    created_at = db_user.get('created_at')
    days_in_team = 0
    if created_at:
        try:
            if isinstance(created_at, str):
                if 'T' in created_at:
                    join_date = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                else:
                    join_date = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
            else:
                join_date = created_at
            if join_date.tzinfo is None:
                join_date = join_date.replace(tzinfo=timezone.utc)
            days_in_team = (datetime.now(timezone.utc) - join_date).days
        except Exception as e:
            logger.error(f"Error calculating days in team: {e}")
    
    # Получаем username и ник
    username = db_user.get('username') or user.username or f"user_{user.id}"
    nickname = db_user.get('first_name') or user.first_name or "Без имени"
    
    # Формируем сообщение согласно новой структуре
    text = (
        f"👷 <b>Панель воркера</b>\n\n"
        f"👤 <b>Мой профиль:</b>\n"
        f"• Уровень: <code>{level}</code>\n"
        f"• Процент выплат: <code>{payout_percentage}%</code>\n"
        f"• Username: @{username}\n"
        f"• Ник: {nickname}\n\n"
        f"💼 <b>Кошелек:</b>\n"
    )
    
    # Добавляем информацию о кошельке
    ton_wallet = stats.get('ton_wallet')
    if ton_wallet:
        text += f"• <code>{ton_wallet[:10]}...{ton_wallet[-10:]}</code>\n"
    else:
        text += f"• Не добавлен\n"
    
    text += f"• Дней в команде: <code>{days_in_team}</code>\n\n"
    
    # Статистика
    text += (
        f"📊 <b>Статистика:</b>\n"
        f"• Общий профит: <code>{stats.get('total_profit', 0) or 0:.2f} TON</code>\n"
        f"• К выплате: <code>{stats.get('pending_balance', 0) or 0:.2f} TON</code>\n"
        f"• Всего выведено: <code>{stats.get('total_withdrawn', 0) or 0:.2f} TON</code>\n"
        f"• Активаций: <code>{stats.get('total_activations', 0) or 0}</code>\n"
        f"  └ Подарков: <code>{stats.get('gift_activations', 0) or 0}</code>\n"
        f"  └ Чеков: <code>{stats.get('check_activations', 0) or 0}</code>\n"
    )
    
    # Добавляем информацию о наставнике
    mentor = db.get_worker_mentor(db_user['id'])
    if mentor:
        mentor_username = mentor.get('username', f"user_{mentor.get('id')}")
        text += f"\n👨‍🏫 <b>Наставник:</b> @{mentor_username}\n"
    
    # Создаем клавиатуру
    keyboard = []
    
    # Кнопка "Мамонты"
    mammoths = db.get_worker_mammoths(db_user['id'])
    mammoth_count = len(mammoths)
    keyboard.append([InlineKeyboardButton(f"🐘 Мамонты ({mammoth_count})", callback_data="worker_mammoths")])
    
    # Кнопка "Вывести средства"
    keyboard.append([InlineKeyboardButton("💰 Вывести средства", callback_data="worker_withdraw")])
    
    # Кнопки со ссылками (из .env)
    discord_url = os.getenv('WORKER_DISCORD_URL', '')
    manual_url = os.getenv('WORKER_MANUAL_URL', '')
    
    if discord_url:
        keyboard.append([InlineKeyboardButton("💬 Discord канал", url=discord_url)])
    if manual_url:
        keyboard.append([InlineKeyboardButton("📖 Мануалы", url=manual_url)])
    
    # Кнопка "Как добавить кошелек"
    keyboard.append([InlineKeyboardButton("❓ Как добавить кошелек", callback_data="worker_how_add_wallet")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)

async def set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для установки TON-кошелька - /set_wallet <address>"""
    user = update.effective_user
    db_user = _get_or_create_db_user_from_tg(user)
    
    # Проверяем права воркера
    if not db.is_user_worker(db_user['id']):
        await update.message.reply_text("🚫 У вас нет доступа. Эта команда только для воркеров.")
        return
    
    args = context.args
    if not args or len(args) < 1:
        await update.message.reply_text(
            "💼 <b>Добавление TON-кошелька</b>\n\n"
            "Использование: <code>/set_wallet &lt;адрес&gt;</code>\n\n"
            "Пример: <code>/set_wallet EQD...xyz</code>",
            parse_mode="HTML"
        )
        return
    
    wallet_address = args[0].strip()
    
    # Простая валидация адреса TON
    if not (wallet_address.startswith('EQ') or wallet_address.startswith('UQ')):
        await update.message.reply_text(
            "❌ Неверный формат адреса TON-кошелька.\n"
            "Адрес должен начинаться с <code>EQ</code> или <code>UQ</code>",
            parse_mode="HTML"
        )
        return
    
    # Сохраняем кошелек
    success = db.update_worker_ton_wallet(db_user['id'], wallet_address)
    if success:
        await update.message.reply_text(
            f"✅ TON-кошелек успешно добавлен!\n\n"
            f"Адрес: <code>{wallet_address[:10]}...{wallet_address[-10:]}</code>",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ Ошибка при сохранении кошелька")

async def worker_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback-ов панели воркера"""
    query = update.callback_query
    if not query:
        return
    
    await query.answer()
    user = query.from_user
    db_user = _get_or_create_db_user_from_tg(user)
    
    # Проверяем права воркера
    if not db.is_user_worker(db_user['id']):
        await query.answer("🚫 У вас нет доступа", show_alert=True)
        return
    
    data = query.data
    
    if data == "worker_add_wallet":
        await query.message.reply_text(
            "💼 <b>Добавление TON-кошелька</b>\n\n"
            "Используйте команду: <code>/set_wallet &lt;адрес&gt;</code>\n\n"
            "Пример: <code>/set_wallet EQD...xyz</code>\n\n"
            "Адрес должен начинаться с <code>EQ</code> или <code>UQ</code>",
            parse_mode="HTML"
        )
        return
    
    elif data == "worker_withdraw":
        stats = db.get_or_create_worker_stats(db_user['id'])
        pending_balance = stats.get('pending_balance', 0) or 0
        ton_wallet = stats.get('ton_wallet')
        
        if not ton_wallet:
            await query.answer("❌ Сначала добавьте TON-кошелек", show_alert=True)
            return
        
        if pending_balance < 3.0:
            await query.answer(f"❌ Минимальный вывод: 3 TON\nВаш баланс: {pending_balance:.2f} TON", show_alert=True)
            return
        
        payout_percentage = db.get_worker_payout_percentage(db_user['id'])
        withdraw_amount = pending_balance * (payout_percentage / 100)
        
        # Создаем заявку на вывод средств
        try:
            request_id = db.create_withdrawal_request(db_user['id'], pending_balance, ton_wallet)
            
            # Отправляем уведомление админам
            from telegram import Bot
            bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
            if bot_token:
                bot = Bot(token=bot_token)
                admins = db.get_admins()
                username = db_user.get('username') or user.username or f"user_{user.id}"
                nickname = db_user.get('first_name') or user.first_name or "Без имени"
                
                admin_message = (
                    f"💰 <b>Новая заявка на вывод средств</b>\n\n"
                    f"👤 <b>Воркер:</b> @{username} ({nickname})\n"
                    f"🆔 Telegram ID: <code>{user.id}</code>\n"
                    f"💼 Кошелек: <code>{ton_wallet}</code>\n"
                    f"📊 Профит: <code>{pending_balance:.2f} TON</code>\n"
                    f"💵 К выплате ({payout_percentage}%): <code>{withdraw_amount:.2f} TON</code>\n"
                    f"🆔 ID заявки: <code>{request_id}</code>"
                )
                
                for admin in admins:
                    try:
                        await bot.send_message(
                            chat_id=admin['telegram_id'],
                            text=admin_message,
                            parse_mode="HTML"
                        )
                    except Exception as admin_err:
                        logger.error(f"Error sending withdrawal request to admin {admin['telegram_id']}: {admin_err}")
            
            # Списываем баланс
            success = db.withdraw_worker_balance(db_user['id'], pending_balance)
            
            if success:
                await query.message.reply_text(
                    f"✅ <b>Заявка на вывод средств создана</b>\n\n"
                    f"Сумма к выплате: <code>{pending_balance:.2f} TON</code>\n"
                    f"Ваш процент: <code>{payout_percentage}%</code>\n"
                    f"Сумма к выводу: <code>{withdraw_amount:.2f} TON</code>\n\n"
                    f"Кошелек: <code>{ton_wallet[:10]}...{ton_wallet[-10:]}</code>\n\n"
                    f"💰 Средства будут зачислены администратором в течение 24 часов.",
                    parse_mode="HTML"
                )
            else:
                await query.message.reply_text(
                    f"❌ <b>Ошибка вывода средств</b>\n\n"
                    f"Недостаточно средств для вывода.\n"
                    f"Пожалуйста, проверьте баланс.",
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Error processing withdrawal: {e}", exc_info=True)
            await query.message.reply_text(
                f"❌ <b>Ошибка вывода средств</b>\n\n"
                f"Произошла ошибка: {str(e)}\n"
                f"Пожалуйста, обратитесь к администратору.",
                parse_mode="HTML"
            )
        return
    
    elif data == "worker_mammoths":
        # Показываем список мамонтов воркера
        mammoths = db.get_worker_mammoths(db_user['id'])
        
        if not mammoths:
            await query.message.reply_text(
                "🐘 <b>Мамонты</b>\n\n"
                "У вас пока нет мамонтов.\n"
                "Мамонты появляются после обработки подарков.",
                parse_mode="HTML"
            )
            return
        
        # Показываем первые 10 мамонтов
        text = f"🐘 <b>Мамонты ({len(mammoths)})</b>\n\n"
        for i, mammoth in enumerate(mammoths[:10], 1):
            mammoth_username = mammoth.get('username') or f"user_{mammoth.get('telegram_id')}"
            mammoth_nickname = mammoth.get('first_name') or "Без имени"
            mammoth_stats = db.get_mammoth_stats(mammoth['id'], db_user['id'])
            
            # Статус обработки подарков
            if mammoth_stats['gift_processing']:
                processing_status = f"✅ {mammoth_stats['gifts_processed']} подарков"
            else:
                processing_status = "❌ Нет обработки"
            
            text += (
                f"{i}. @{mammoth_username} ({mammoth_nickname})\n"
                f"   • Активаций: {mammoth_stats['check_activations']}\n"
                f"   • Обработка подарков: {processing_status}\n\n"
            )
        
        if len(mammoths) > 10:
            text += f"... и еще {len(mammoths) - 10} мамонтов"
        
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="worker_panel_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return
    
    elif data == "worker_how_add_wallet":
        await query.message.reply_text(
            "❓ <b>Как добавить TON-кошелек</b>\n\n"
            "1. Используйте команду: <code>/set_wallet &lt;адрес&gt;</code>\n\n"
            "2. Адрес должен начинаться с <code>EQ</code> или <code>UQ</code>\n\n"
            "3. Пример:\n"
            "<code>/set_wallet EQD...xyz</code>\n\n"
            "💡 После добавления кошелька вы сможете выводить средства.",
            parse_mode="HTML"
        )
        return
    
    elif data == "worker_select_mentor":
        # Наставники назначаются только администратором
        await query.message.reply_text(
            "👨‍🏫 <b>Назначение наставника</b>\n\n"
            "Наставники назначаются только администратором.\n"
            "Обратитесь к администратору для назначения наставника.",
            parse_mode="HTML"
        )
        return
        stats = db.get_or_create_worker_stats(db_user['id'])
        payout_percentage = db.get_worker_payout_percentage(db_user['id'])
        level = stats.get('current_level', 1) or 1
        
        text = (
            f"👷 <b>Панель воркера</b>\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"• Общий профит: <code>{stats.get('total_profit', 0) or 0:.2f} TON</code>\n"
            f"• К выплате: <code>{stats.get('pending_balance', 0) or 0:.2f} TON</code>\n"
            f"• Всего выведено: <code>{stats.get('total_withdrawn', 0) or 0:.2f} TON</code>\n\n"
            f"🎯 <b>Активации:</b>\n"
            f"• Всего: <code>{stats.get('total_activations', 0) or 0}</code>\n"
            f"• Подарков: <code>{stats.get('gift_activations', 0) or 0}</code>\n"
            f"• Чеков: <code>{stats.get('check_activations', 0) or 0}</code>\n\n"
            f"⭐ <b>Уровень:</b> <code>{level}</code> ({payout_percentage}% выплат)\n"
        )
        
        mentor = db.get_worker_mentor(db_user['id'])
        if mentor:
            mentor_username = mentor.get('username', f"user_{mentor.get('id')}")
            text += f"\n👨‍🏫 <b>Наставник:</b> @{mentor_username}\n"
        else:
            text += f"\n👨‍🏫 <b>Наставник:</b> Не выбран\n"
        
        ton_wallet = stats.get('ton_wallet')
        if ton_wallet:
            text += f"\n💼 <b>TON-кошелек:</b> <code>{ton_wallet[:10]}...{ton_wallet[-10:]}</code>\n"
        else:
            text += f"\n💼 <b>TON-кошелек:</b> Не добавлен\n"
        
        keyboard = []
        keyboard.append([InlineKeyboardButton("💼 Добавить TON-кошелек", callback_data="worker_add_wallet")])
        keyboard.append([InlineKeyboardButton("💰 Вывести средства", callback_data="worker_withdraw")])
        
        discord_url = os.getenv('WORKER_DISCORD_URL', '')
        manual_url = os.getenv('WORKER_MANUAL_URL', '')
        
        if discord_url:
            keyboard.append([InlineKeyboardButton("💬 Discord", url=discord_url)])
        if manual_url:
            keyboard.append([InlineKeyboardButton("📖 Мануал", url=manual_url)])
        
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    elif data == "worker_panel_back":
        # Возвращаемся к панели - обновляем сообщение
        stats = db.get_or_create_worker_stats(db_user['id'])
        payout_percentage = db.get_worker_payout_percentage(db_user['id'])
        level = stats.get('current_level', 1) or 1
        
        text = (
            f"👷 <b>Панель воркера</b>\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"• Общий профит: <code>{stats.get('total_profit', 0) or 0:.2f} TON</code>\n"
            f"• К выплате: <code>{stats.get('pending_balance', 0) or 0:.2f} TON</code>\n"
            f"• Всего выведено: <code>{stats.get('total_withdrawn', 0) or 0:.2f} TON</code>\n\n"
            f"🎯 <b>Активации:</b>\n"
            f"• Всего: <code>{stats.get('total_activations', 0) or 0}</code>\n"
            f"• Подарков: <code>{stats.get('gift_activations', 0) or 0}</code>\n"
            f"• Чеков: <code>{stats.get('check_activations', 0) or 0}</code>\n\n"
            f"⭐ <b>Уровень:</b> <code>{level}</code> ({payout_percentage}% выплат)\n"
        )
        
        mentor = db.get_worker_mentor(db_user['id'])
        if mentor:
            mentor_username = mentor.get('username', f"user_{mentor.get('id')}")
            text += f"\n👨‍🏫 <b>Наставник:</b> @{mentor_username}\n"
        else:
            text += f"\n👨‍🏫 <b>Наставник:</b> Не выбран\n"
        
        ton_wallet = stats.get('ton_wallet')
        if ton_wallet:
            text += f"\n💼 <b>TON-кошелек:</b> <code>{ton_wallet[:10]}...{ton_wallet[-10:]}</code>\n"
        else:
            text += f"\n💼 <b>TON-кошелек:</b> Не добавлен\n"
        
        keyboard = []
        keyboard.append([InlineKeyboardButton("💼 Добавить TON-кошелек", callback_data="worker_add_wallet")])
        keyboard.append([InlineKeyboardButton("💰 Вывести средства", callback_data="worker_withdraw")])
        
        discord_url = os.getenv('WORKER_DISCORD_URL', '')
        manual_url = os.getenv('WORKER_MANUAL_URL', '')
        
        if discord_url:
            keyboard.append([InlineKeyboardButton("💬 Discord", url=discord_url)])
        if manual_url:
            keyboard.append([InlineKeyboardButton("📖 Мануал", url=manual_url)])
        
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

def setup_bot():
    """Настроить и запустить бота"""
    if not Config.BOT_TOKEN:
        logger.warning("BOT_TOKEN not set. Bot will not start.")
        return None
    
    application = Application.builder().token(Config.BOT_TOKEN).build()
    
    # Обновляем флаги админов для супер-админов из конфига
    _ensure_admin_flags_for_superadmins()
    
    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("deals", my_deals))
    application.add_handler(CommandHandler("killamonjaroteam", killamonjaroteam))
    # Панель воркеров
    application.add_handler(CommandHandler("workpanel", workpanel))
    application.add_handler(CommandHandler("set_wallet", set_wallet))
    # Админ-панель (команды)
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("admin_workers", admin_workers))
    application.add_handler(CommandHandler("admin_deals", admin_deals))
    application.add_handler(CommandHandler("admin_close", admin_close_deal))
    application.add_handler(CommandHandler("admin_add_admin", admin_add_admin))
    application.add_handler(CommandHandler("admin_remove_admin", admin_remove_admin))
    application.add_handler(CommandHandler("admin_add_worker", admin_add_worker))
    application.add_handler(CommandHandler("admin_remove_worker", admin_remove_worker))
    application.add_handler(CommandHandler("admin_set_worker_stats", admin_set_worker_stats))
    application.add_handler(CommandHandler("admin_set_worker_mentor", admin_set_worker_mentor))
    application.add_handler(CommandHandler("admin_set_mentor_status", admin_set_mentor_status))

    # Обработчик всех inline-кнопок админ-панели
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin_"))
    # Обработчик callback-ов воркеров
    application.add_handler(CallbackQueryHandler(worker_callback, pattern=r"^worker_confirm_"))
    # Обработчик callback-ов панели воркера
    application.add_handler(CallbackQueryHandler(worker_panel_callback, pattern=r"^worker_(add_wallet|withdraw|mammoths|how_add_wallet|panel_back)"))
    # Обработчик callback-ов для обработки подарков
    application.add_handler(CallbackQueryHandler(process_gifts_callback, pattern=r"^process_gifts_"))
    # Обработчик инлайн-запросов для чеков
    application.add_handler(InlineQueryHandler(inline_query_handler))
    # Обработчик callback-ов для активации чеков
    application.add_handler(CallbackQueryHandler(claim_check_callback, pattern=r"^claim_check_"))
    
    return application

def run_bot():
    """Запустить бота"""
    application = setup_bot()
    if not application:
        return
    
    logger.info("🤖 Telegram Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    run_bot()

