#!/usr/bin/env python3
"""
Бизнес-бот для передачи NFT подарков
Добавь бота в бизнес-аккаунт через @BotFather команду /setdefaultadministratorrights
"""
import asyncio
import logging
import os
from datetime import datetime
from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфиг
BOT_TOKEN = os.getenv("BUSINESS_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    logger.error("❌ BUSINESS_BOT_TOKEN не установлен!")
    exit(1)

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# FSM состояния
class GiftTransfer(StatesGroup):
    selecting_gift = State()
    entering_recipient = State()
    confirming = State()

# Хранилище данных пользователя (в реальной жизни - БД)
user_sessions = {}

# Хранилище business_connection_id (когда бизнес-аккаунт подключит бота)
BUSINESS_CONNECTION_FILE = 'business_connection.txt'

def get_business_connection_id():
    """Получить сохраненный business_connection_id"""
    if os.path.exists(BUSINESS_CONNECTION_FILE):
        with open(BUSINESS_CONNECTION_FILE, 'r') as f:
            return f.read().strip()
    return None

def save_business_connection_id(connection_id):
    """Сохранить business_connection_id"""
    with open(BUSINESS_CONNECTION_FILE, 'w') as f:
        f.write(connection_id)
    logger.info(f"✅ Бизнес-аккаунт подключен: {connection_id}")

class GiftManager:
    """Управление подарками и передачами"""

    @staticmethod
    async def get_business_account_gifts():
        """Получить список реальных подарков, принадлежащих бизнес-аккаунту"""
        try:
            # Получаем сохраненный business_connection_id
            business_connection_id = get_business_connection_id()

            if not business_connection_id:
                logger.warning("business_connection_id не установлен. Бизнес-аккаунт не подключен.")
                return None, "Бизнес-аккаунт не подключен к боту"

            logger.info(f"Получаем подарки для connection: {business_connection_id}")

            # Получаем подарки, которые принадлежат бизнес-аккаунту
            owned_gifts = await bot.get_business_account_gifts(business_connection_id)

            # owned_gifts.gifts содержит список OwnedGift объектов
            gifts_list = owned_gifts.gifts if hasattr(owned_gifts, 'gifts') else []

            if not gifts_list:
                logger.warning("Подарки бизнес-аккаунта не найдены")
                return None, "У аккаунта нет подарков"

            logger.info(f"Получено {len(gifts_list)} подарков бизнес-аккаунта")

            # Преобразуем в удобный формат
            gift_list = []
            for i, owned_gift in enumerate(gifts_list):
                # Получаем информацию о подарке
                gift = owned_gift.gift if hasattr(owned_gift, 'gift') else None
                if not gift:
                    continue

                gift_name = gift.sticker.emoji if hasattr(gift, 'sticker') and gift.sticker else '🎁'

                gift_data = {
                    'id': gift.id,
                    'name': gift_name,
                    'count': getattr(owned_gift, 'count', 1),
                    'object': owned_gift
                }
                gift_list.append(gift_data)
                logger.info(f"Подарок {gift.id}: {gift_name} (всего: {gift_data['count']})")

            return gift_list, None

        except Exception as e:
            logger.error(f"Ошибка получения подарков: {e}", exc_info=True)
            return None, f"Ошибка получения подарков: {str(e)}"

    @staticmethod
    async def get_user_gifts(user_id: int):
        """Получить список подарков (перенаправляет на get_business_account_gifts)"""
        return await GiftManager.get_business_account_gifts()

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    """Стартовое сообщение"""
    text = """
🎁 <b>Бизнес-бот для передачи NFT</b>

Этот бот позволяет быстро передавать подарки из твоего бизнес-аккаунта.

<b>Команды:</b>
/gifts - Показать список твоих подарков
/help - Справка

<b>Как использовать:</b>
1. Добавь бота в бизнес-аккаунт
2. Используй /gifts чтобы увидеть подарки
3. Выбери подарок
4. Введи юзернейм получателя
5. Подтверди передачу

<i>⚠️ Убедись что твоя сессия активна перед использованием бота</i>
    """
    await message.answer(text, parse_mode="HTML")

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    """Справка"""
    text = """
<b>📖 Справка</b>

<b>Основные команды:</b>
/gifts - Показать подарки
/cancel - Отменить операцию
/auth - Авторизоваться (если нужно)

<b>Как работает передача:</b>
1️⃣ Нажми /gifts
2️⃣ Выбери подарок из списка
3️⃣ Введи юзернейм получателя
4️⃣ Нажми кнопку "Отправить"

<b>Поддерживаемые форматы юзернейма:</b>
- @username
- username

<b>Статусы передачи:</b>
✅ Успешно - подарок передан
❌ Ошибка - проверь юзернейм
⏳ Обработка - ждем ответ сервера
    """
    await message.answer(text, parse_mode="HTML")

@router.message(Command("gifts"))
async def cmd_gifts(message: types.Message, state: FSMContext):
    """Показать доступные подарки"""
    user_id = message.from_user.id

    # Показываем что загружаем
    loading_msg = await message.answer("⏳ Загружаю список подарков...")

    try:
        gifts, error = await GiftManager.get_user_gifts(user_id)

        if error:
            await loading_msg.delete()
            await message.answer(f"❌ {error}", parse_mode="HTML")
            return

        if not gifts:
            await loading_msg.delete()
            await message.answer("❌ У тебя нет доступных подарков", parse_mode="HTML")
            return

        # Сохраняем подарки в FSM
        await state.update_data(gifts=gifts, selected_gift=None)

        # Строим клавиатуру с подарками
        builder = InlineKeyboardBuilder()

        for i, gift in enumerate(gifts[:20]):  # Максимум 20 подарков на экран
            gift_emoji = gift.get('name', '🎁')
            count = gift.get('count', 1)

            # Кнопка с подарком
            builder.button(
                text=f"{gift_emoji} ({count})",
                callback_data=f"gift_{i}"
            )

        builder.adjust(2)  # По две кнопки в ряд

        await loading_msg.delete()
        await message.answer(
            f"🎁 <b>Твои подарки ({len(gifts)})</b>\n\nВыбери подарок для передачи:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )

        await state.set_state(GiftTransfer.selecting_gift)

    except Exception as e:
        logger.error(f"Ошибка в cmd_gifts: {e}", exc_info=True)
        await loading_msg.delete()
        await message.answer(f"❌ Ошибка: {str(e)}", parse_mode="HTML")

@router.callback_query(GiftTransfer.selecting_gift, F.data.startswith("gift_"))
async def process_gift_select(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора подарка"""
    try:
        gift_index = int(callback.data.split("_")[1])

        # Получаем подарки из FSM
        data = await state.get_data()
        gifts = data.get('gifts', [])

        if gift_index >= len(gifts):
            await callback.answer("❌ Подарок не найден", show_alert=True)
            return

        selected_gift = gifts[gift_index]
        gift_name = getattr(selected_gift, 'sticker', {}).get('name', 'Unknown') if hasattr(selected_gift, 'sticker') else 'NFT'

        # Сохраняем выбранный подарок
        await state.update_data(selected_gift_index=gift_index, selected_gift_name=gift_name)

        # Просим юзернейм получателя
        await callback.message.edit_text(
            f"🎁 <b>Выбран подарок:</b> {gift_name}\n\n"
            f"<b>Введи юзернейм получателя:</b>\n"
            f"(например: username или @username)",
            parse_mode="HTML"
        )

        await state.set_state(GiftTransfer.entering_recipient)
        await callback.answer()

    except Exception as e:
        logger.error(f"Ошибка в process_gift_select: {e}")
        await callback.answer(f"❌ Ошибка: {str(e)}", show_alert=True)

@router.message(GiftTransfer.entering_recipient)
async def process_recipient_input(message: types.Message, state: FSMContext):
    """Обработка ввода юзернейма получателя"""
    try:
        username = message.text.strip().lstrip("@")

        # Валидация юзернейма
        if not username or len(username) < 3:
            await message.answer("❌ Юзернейм должен быть минимум 3 символа")
            return

        # Получаем данные
        data = await state.get_data()
        gift_index = data.get('selected_gift_index')
        gift_name = data.get('selected_gift_name', 'NFT')
        gifts = data.get('gifts', [])

        if gift_index is None or gift_index >= len(gifts):
            await message.answer("❌ Ошибка: подарок не найден")
            await state.clear()
            return

        # Сохраняем юзернейм и показываем подтверждение
        await state.update_data(recipient_username=username)

        # Клавиатура подтверждения
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Отправить", callback_data="confirm_transfer")
        builder.button(text="❌ Отмена", callback_data="cancel_transfer")

        await message.answer(
            f"📤 <b>Подтверждение передачи</b>\n\n"
            f"<b>Подарок:</b> {gift_name}\n"
            f"<b>Получатель:</b> @{username}\n\n"
            f"Все верно?",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )

        await state.set_state(GiftTransfer.confirming)

    except Exception as e:
        logger.error(f"Ошибка в process_recipient_input: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")

@router.callback_query(GiftTransfer.confirming, F.data == "confirm_transfer")
async def confirm_transfer(callback: CallbackQuery, state: FSMContext):
    """Подтвердить и отправить подарок"""
    try:
        user_id = callback.from_user.id
        processing_msg = await callback.message.edit_text(
            "⏳ <b>Отправляю подарок...</b>\n\n"
            "Пожалуйста, подожди...",
            parse_mode="HTML"
        )

        # Получаем данные
        data = await state.get_data()
        gift_index = data.get('selected_gift_index')
        gifts = data.get('gifts', [])
        recipient_username = data.get('recipient_username', '').lstrip("@")
        gift_name = data.get('selected_gift_name', 'NFT')

        if not recipient_username or gift_index is None:
            await processing_msg.edit_text("❌ Ошибка: неполные данные")
            await state.clear()
            return

        # Получаем business_connection_id
        business_connection_id = get_business_connection_id()
        if not business_connection_id:
            await processing_msg.edit_text("❌ Бизнес-аккаунт не подключен")
            await state.clear()
            return

        # Отправляем подарок через Bot API
        try:
            selected_gift = gifts[gift_index]
            gift_id = selected_gift.get('id')

            # Отправляем подарок от бизнес-аккаунта
            result = await bot.send_gift(
                user_id=int(recipient_username) if recipient_username.isdigit() else None,
                gift_id=gift_id,
                text="",
                text_parse_mode=None,
                business_connection_id=business_connection_id
            )

            if result:
                await processing_msg.edit_text(
                    f"✅ <b>Успешно!</b>\n\n"
                    f"🎁 Подарок: {gift_name}\n"
                    f"👤 Получатель: @{recipient_username}\n"
                    f"⏰ Время: {datetime.now().strftime('%H:%M:%S')}",
                    parse_mode="HTML"
                )
            else:
                await processing_msg.edit_text("❌ Ошибка при отправке подарка")

        except Exception as e:
            logger.error(f"Ошибка отправки подарка: {e}")
            await processing_msg.edit_text(
                f"❌ <b>Ошибка</b>\n\n"
                f"<i>{str(e)[:200]}</i>",
                parse_mode="HTML"
            )

        await state.clear()

    except Exception as e:
        logger.error(f"Ошибка в confirm_transfer: {e}", exc_info=True)
        try:
            await callback.message.edit_text(f"❌ Ошибка: {str(e)[:200]}")
        except:
            pass
        await state.clear()

@router.callback_query(F.data == "cancel_transfer")
async def cancel_transfer(callback: CallbackQuery, state: FSMContext):
    """Отмена операции"""
    await callback.message.edit_text(
        "❌ Отменено\n\n"
        "Используй /gifts чтобы начать заново"
    )
    await state.clear()
    await callback.answer()

@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    """Отмена операции"""
    await state.clear()
    await message.answer(
        "❌ Отменено\n\n"
        "Используй /gifts чтобы начать заново"
    )

@router.message()
async def echo(message: types.Message):
    """Неизвестная команда"""
    await message.answer(
        "❓ Неизвестная команда\n\n"
        "Используй:\n"
        "/gifts - Начать передачу подарка\n"
        "/help - Справка\n"
        "/cancel - Отмена"
    )

@router.my_chat_member()
async def process_business_connection(update: types.ChatMemberUpdated):
    """Обработка подключения/отключения бизнес-аккаунта"""
    try:
        # Когда бизнес-аккаунт подключает бота, приходит business_connection_id
        if hasattr(update, 'business_connection_id') and update.business_connection_id:
            business_connection_id = update.business_connection_id
            save_business_connection_id(business_connection_id)
            logger.info(f"✅ Бизнес-аккаунт подключен: {business_connection_id}")
        else:
            logger.info(f"Обновление чата: {update}")
    except Exception as e:
        logger.error(f"Ошибка в обработке business connection: {e}")

async def main():
    """Запуск бота"""
    dp.include_router(router)

    logger.info("🚀 Бизнес-бот запущен")
    logger.info(f"Бот: @{(await bot.get_me()).username}")

    try:
        await dp.start_polling(bot, allowed_updates=['message', 'callback_query', 'my_chat_member', 'business_connection'])
    except KeyboardInterrupt:
        logger.info("❌ Бот остановлен")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
