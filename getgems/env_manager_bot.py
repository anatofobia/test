#!/usr/bin/env python3
"""
Бот для управления .env файлом
Позволяет изменять GETGEMS_BOT_TOKEN, BOT_TOKEN и BOT_USERNAME через Telegram
"""
import os
import re
import asyncio
import logging
import secrets
import subprocess
import html
from datetime import datetime, timezone, timedelta
from pathlib import Path
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv, set_key, find_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения
load_dotenv()

# Токен бота для управления .env (должен быть задан в переменной окружения)
ENV_MANAGER_BOT_TOKEN = os.getenv("ENV_MANAGER_BOT_TOKEN", "")
ADMIN_IDS = [
    int(admin_id.strip()) 
    for admin_id in os.getenv("ADMIN_IDS", "").split(",") 
    if admin_id.strip().isdigit()
]

if not ENV_MANAGER_BOT_TOKEN:
    logger.error("❌ ENV_MANAGER_BOT_TOKEN не установлен! Создайте бота через @BotFather и установите токен.")
    exit(1)

if not ADMIN_IDS:
    logger.warning("⚠️ ADMIN_IDS не установлен. Бот будет доступен всем пользователям.")

# Инициализация бота и диспетчера
bot = Bot(token=ENV_MANAGER_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Путь к .env файлу
ENV_FILE = Path(".env")
BACKEND_ENV_FILE = Path("backend/.env")

ALLOWED_BOT_SERVICES = {
    "getgems-bot.service",      # основной бот
    "getgems_bot.service",      # альтернативный/старый
    "file_download_bot.service",
    "auto_config_bot.service",
}


class EnvEditStates(StatesGroup):
    waiting_for_bot_token = State()
    waiting_for_bot_username = State()
    waiting_for_getgems_token = State()
    waiting_for_bot_tokens = State()


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    return user_id in ADMIN_IDS if ADMIN_IDS else True


def _list_bot_services() -> list[str]:
    """Находит все service-файлы ботов (автоматически по слову 'bot')."""
    services: set[str] = set()
    try:
        import subprocess
        res = subprocess.run(
            ["systemctl", "list-unit-files", "--type=service", "--no-pager", "--no-legend"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in (res.stdout or "").splitlines():
            parts = line.split()
            if not parts:
                continue
            name = parts[0].strip()
            if not name.endswith(".service"):
                continue
            # Включаем любой сервис, содержащий слово 'bot' (даже если запущен через env_manager)
            if 'bot' in name.lower():
                services.add(name)
            if re.match(r"^getgems_bot\d+\.service$", name):
                services.add(name)
    except Exception:
        pass
    return sorted(services)


def _read_service_file(service_name: str) -> str:
    """Читает содержимое /etc/systemd/system/<service_name> (если доступно)."""
    try:
        service_path = Path("/etc/systemd/system") / service_name
        if service_path.exists():
            return service_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        pass
    return ""


def _extract_env_value(service_text: str, key: str) -> str:
    """Достает значение Environment= из systemd unit."""
    # Поддерживаем варианты:
    # Environment="BOT_TOKEN=xxx"
    # Environment=BOT_TOKEN=xxx
    for line in service_text.splitlines():
        line = line.strip()
        if not line.startswith("Environment"):
            continue
        # вытащим содержимое после '='
        if "=" not in line:
            continue
        _, rhs = line.split("=", 1)
        rhs = rhs.strip()
        rhs = rhs.strip('"').strip("'")
        if rhs.startswith(f"{key}="):
            return rhs.split("=", 1)[1].strip()
    return ""


async def _check_bot_token(token: str) -> dict:
    """Проверяет токен через getMe()."""
    if not token:
        return {"ok": False, "error": "NO_TOKEN", "username": ""}
    try:
        test_bot = Bot(token=token)
        me = await test_bot.get_me()
        await test_bot.session.close()
        return {"ok": True, "error": None, "username": me.username or ""}
    except Exception as e:
        try:
            # aiogram может давать TelegramUnauthorizedError
            msg = str(e)
        except Exception:
            msg = "UNKNOWN_ERROR"
        try:
            await test_bot.session.close()
        except Exception:
            pass
        return {"ok": False, "error": msg[:160], "username": ""}


def _systemctl_is_active(service_name: str) -> str:
    try:
        import subprocess
        res = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True, timeout=10)
        return (res.stdout or "").strip() or (res.stderr or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _systemctl_is_enabled(service_name: str) -> str:
    try:
        import subprocess
        res = subprocess.run(["systemctl", "is-enabled", service_name], capture_output=True, text=True, timeout=10)
        return (res.stdout or "").strip() or (res.stderr or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _delete_service_file(service_name: str) -> tuple[bool, str]:
    """Останавливает/disable и удаляет service unit файл. Возвращает (ok, error)."""
    # Разрешаем удалять только bot-сервисы
    if 'bot' not in service_name.lower():
        return False, "FORBIDDEN_SERVICE"
    try:
        import subprocess
        service_path = Path("/etc/systemd/system") / service_name
        subprocess.run(["systemctl", "stop", service_name], capture_output=True, text=True, timeout=10)
        subprocess.run(["systemctl", "disable", service_name], capture_output=True, text=True, timeout=10)
        if service_path.exists():
            service_path.unlink()
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, text=True, timeout=10)
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


def update_env_file(env_file_path: Path, key: str, value: str) -> bool:
    """
    Обновляет значение переменной в .env файле
    
    Args:
        env_file_path: Путь к .env файлу
        key: Имя переменной
        value: Новое значение
        
    Returns:
        True если успешно, False в противном случае
    """
    try:
        if not env_file_path.exists():
            logger.warning(f"Файл {env_file_path} не существует, создаю новый...")
            env_file_path.parent.mkdir(parents=True, exist_ok=True)
            env_file_path.touch()
        
        # Читаем содержимое файла
        with open(env_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Ищем переменную и обновляем её
        updated = False
        new_lines = []
        for line in lines:
            # Проверяем, является ли строка нужной переменной
            if line.strip().startswith(f"{key}="):
                new_lines.append(f"{key}={value}\n")
                updated = True
            else:
                new_lines.append(line)
        
        # Если переменная не найдена, добавляем её в конец
        if not updated:
            new_lines.append(f"{key}={value}\n")
        
        # Записываем обратно
        with open(env_file_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        
        logger.info(f"✅ Обновлено {key} в {env_file_path}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка обновления {key} в {env_file_path}: {e}")
        return False


def update_all_env_files(key: str, value: str) -> dict:
    """
    Обновляет переменную во всех .env файлах
    
    Returns:
        dict с результатами обновления для каждого файла
    """
    results = {}
    
    # Обновляем основной .env
    if ENV_FILE.exists():
        results['main'] = update_env_file(ENV_FILE, key, value)
    else:
        results['main'] = False
    
    # Обновляем backend/.env
    if BACKEND_ENV_FILE.exists():
        results['backend'] = update_env_file(BACKEND_ENV_FILE, key, value)
    else:
        results['backend'] = False
    
    return results


async def get_bot_username_from_token(token: str) -> str:
    """
    Получает username бота по токену через Telegram Bot API
    
    Args:
        token: Токен бота
        
    Returns:
        Username бота или пустая строка при ошибке
    """
    try:
        test_bot = Bot(token=token)
        bot_info = await test_bot.get_me()
        await test_bot.session.close()
        return bot_info.username or ""
    except Exception as e:
        logger.error(f"Ошибка получения username бота: {e}")
        return ""


def create_bot_service(bot_num: int, bot_token: str, bot_username: str = None) -> dict:
    """
    Создает systemd сервис для бота
    
    Args:
        bot_num: Номер бота (для имени сервиса)
        bot_token: Токен бота
        bot_username: Username бота (опционально)
        
    Returns:
        dict с результатами создания сервиса
    """
    result = {
        'service_name': f'getgems_bot{bot_num}.service',
        'created': False,
        'error': None
    }
    
    try:
        import subprocess
        from pathlib import Path
        
        service_content = f"""[Unit]
Description=GetGems Telegram Bot #{bot_num}
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/getgems
Environment="PATH=/root/getgems/venv/bin:/usr/bin:/usr/local/bin"
Environment="BOT_TOKEN={bot_token}"
Environment="BOT_INSTANCE_NAME=bot{bot_num}"
ExecStart=/root/getgems/venv/bin/python3 /root/getgems/run_bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
        
        service_path = Path(f"/etc/systemd/system/getgems_bot{bot_num}.service")
        
        # Создаем файл сервиса
        with open(service_path, 'w', encoding='utf-8') as f:
            f.write(service_content)
        
        # Перезагружаем systemd
        subprocess.run(
            ["systemctl", "daemon-reload"],
            check=True,
            capture_output=True,
            timeout=10
        )
        
        result['created'] = True
        logger.info(f"✅ Создан сервис: {result['service_name']}")
        
    except subprocess.CalledProcessError as e:
        result['error'] = f"Ошибка systemctl: {e.stderr.decode() if e.stderr else str(e)}"
        logger.error(f"❌ Ошибка создания сервиса: {result['error']}")
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"❌ Ошибка создания сервиса: {e}", exc_info=True)
    
    return result


def enable_and_start_service(service_name: str) -> dict:
    """
    Включает и запускает systemd сервис
    
    Args:
        service_name: Имя сервиса (например, 'getgems_bot1.service')
        
    Returns:
        dict с результатами
    """
    result = {
        'enabled': False,
        'started': False,
        'error': None
    }
    
    try:
        import subprocess
        
        # Включаем автозапуск
        subprocess.run(
            ["systemctl", "enable", service_name],
            check=True,
            capture_output=True,
            timeout=10
        )
        result['enabled'] = True
        
        # Запускаем сервис
        subprocess.run(
            ["systemctl", "start", service_name],
            check=True,
            capture_output=True,
            timeout=10
        )
        result['started'] = True
        
        logger.info(f"✅ Сервис {service_name} включен и запущен")
        
    except subprocess.CalledProcessError as e:
        result['error'] = f"Ошибка systemctl: {e.stderr.decode() if e.stderr else str(e)}"
        logger.error(f"❌ Ошибка управления сервисом {service_name}: {result['error']}")
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"❌ Ошибка управления сервисом {service_name}: {e}", exc_info=True)
    
    return result


async def configure_bot_automatically(token: str) -> dict:
    """
    Автоматически настраивает бота через Telegram Bot API:
    - Устанавливает имя: "GetGems: sell and buy NFT"
    - Устанавливает описание: "Trade gifts and stickers on GetGems! Lowest fees, secure wallet integration."
    - Устанавливает веб-апп кнопку: https://getgems.mooo.com
    - Включает инлайн режим (через BotFather команды)
    
    Args:
        token: Токен бота для настройки
        
    Returns:
        dict с результатами настройки
    """
    results = {
        'name': False,
        'description': False,
        'short_description': False,
        'menu_button': False,
        'inline_mode': False
    }
    
    try:
        from aiogram import Bot
        from aiogram.methods import SetMyName, SetMyDescription, SetMyShortDescription, SetChatMenuButton
        from aiogram.types import MenuButtonWebApp, WebAppInfo
        
        config_bot = Bot(token=token)
        
        # 1. Устанавливаем имя бота
        try:
            result = await config_bot(SetMyName(name="GetGems: sell and buy NFT", language_code="en"))
            if result:
                results['name'] = True
                logger.info("✅ Имя бота установлено: GetGems: sell and buy NFT")
            else:
                logger.warning("⚠️ Установка имени бота вернула False")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось установить имя бота: {e}", exc_info=True)
        
        # Небольшая задержка между запросами
        await asyncio.sleep(0.5)
        
        # 2. Устанавливаем описание (about)
        try:
            result = await config_bot(SetMyDescription(
                description="Trade gifts and stickers on GetGems! Lowest fees, secure wallet integration.",
                language_code="en"
            ))
            if result:
                results['description'] = True
                logger.info("✅ Описание бота установлено")
            else:
                logger.warning("⚠️ Установка описания бота вернула False")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось установить описание бота: {e}", exc_info=True)
        
        await asyncio.sleep(0.5)
        
        # 3. Устанавливаем короткое описание
        try:
            result = await config_bot(SetMyShortDescription(
                short_description="Trade gifts and stickers on GetGems! Lowest fees, secure wallet integration.",
                language_code="en"
            ))
            if result:
                results['short_description'] = True
                logger.info("✅ Короткое описание бота установлено")
            else:
                logger.warning("⚠️ Установка короткого описания бота вернула False")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось установить короткое описание бота: {e}", exc_info=True)
        
        await asyncio.sleep(0.5)
        
        # 4. Устанавливаем кнопку меню с веб-апп
        try:
            webapp_url = "https://getgems.mooo.com/market"
            menu_button = MenuButtonWebApp(text="Open GetGems", web_app=WebAppInfo(url=webapp_url))
            result = await config_bot(SetChatMenuButton(menu_button=menu_button))
            if result:
                results['menu_button'] = True
                logger.info(f"✅ Кнопка меню с веб-апп установлена: {webapp_url}")
            else:
                logger.warning("⚠️ Установка кнопки меню вернула False")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось установить кнопку меню: {e}", exc_info=True)
        
        # 5. Инлайн режим нужно включать через @BotFather вручную
        # Но мы можем попробовать отправить команду через API (если у бота есть доступ к BotFather)
        results['inline_mode'] = True  # Помечаем как требующий ручного включения
        logger.info("ℹ️ Инлайн режим нужно включить вручную через @BotFather командой /setinline")
        
        await config_bot.session.close()
        
    except Exception as e:
        logger.error(f"❌ Ошибка автоматической настройки бота: {e}", exc_info=True)
    
    return results


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Показывает список доступных команд"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этому боту.")
        return
    
    help_text = (
        "🤖 <b>Доступные команды:</b>\n\n"
        "📋 <b>Основные:</b>\n"
        "• /start - Начать работу\n"
        "• /status - Статус ботов и сервисов\n"
        "• /bots - Список всех ботов\n\n"
        "⚙️ <b>Настройка:</b>\n"
        "• /set_bot_token - Установить BOT_TOKEN\n"
        "• /set_getgems_token - Установить GETGEMS_BOT_TOKEN\n"
        "• /set_bot_username - Установить BOT_USERNAME\n"
        "• /enable_bots - Включить несколько ботов\n\n"
        "🔄 <b>Управление:</b>\n"
        "• /restart_service - Перезапустить сервис\n"
        "• /delete_bot SERVICE_NAME - Удалить бота\n"
        "• /reset_balance username/ID - Обнулить баланс пользователя\n"
        "• /profits - Профиты за сегодня\n\n"
        "🔧 <b>Системные команды:</b>\n"
        "• /sudo команда - Выполнить системную команду от имени root\n"
        "  Примеры: <code>/sudo getgems restart</code>, <code>/sudo apt update</code>\n\n"
        "🧪 <b>Тестирование:</b>\n"
        "• /selftest - Самопроверка системы\n"
        "• /test_floor ссылка1 [ссылка2 ...] - Рассчитать флор и создать тестовый профит\n"
    )
    
    await message.answer(help_text, parse_mode="HTML")


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этому боту.")
        return
    
    help_text = (
        "🔧 <b>Бот для управления .env файлом</b>\n\n"
        "Доступные команды:\n"
        "/status - Показать текущие значения\n"
        "/set_bot_token - Изменить BOT_TOKEN\n"
        "/set_getgems_token - Изменить GETGEMS_BOT_TOKEN\n"
        "/set_bot_username - Изменить BOT_USERNAME\n"
        "/enable_bots - Включить несколько ботов (создать сервисы)\n"
        "/bots - Проверить все bot-сервисы и найти нерабочие\n"
        "/delete_bot SERVICE - Удалить bot-сервис (stop/disable/remove)\n"
        "/reset_balance username/ID - Обнулить баланс пользователя\n"
        "/profits - Показать профиты за сегодня\n"
        "/selftest [телефон] - Прогнать тест логов (вход по шагам + профит + webhooks)\n"
        "/test_floor ссылка1 [ссылка2 ...] - Рассчитать флор и создать тестовый профит\n"
        "\n🔧 <b>Системные команды</b>\n"
        "/sudo команда - Выполнить системную команду от имени root\n"
        "Примеры: <code>/sudo getgems restart</code>, <code>/sudo apt update</code>\n"
        "\n💎 <b>TON (без TonConnect, полностью авто)</b>\n"
        "/ton_status - Показать адрес и баланс\n"
        "/ton_deploy - Инициализировать (задеплоить) кошелёк, если uninitialized\n"
        "/ton_send ADDRESS AMOUNT [COMMENT] - Отправить TON на адрес\n"
        "/ton_set_encrypted TOKEN - Сохранить TON_WALLET_ENCRYPTED (шифротекст)\n"
        "/ton_set_master_key KEY - Сохранить TON_MASTER_KEY (Fernet key)\n"
        "📌 Простой режим (небезопасно): можно задать <code>TON_WALLET_MNEMONIC</code> прямо в .env\n"
        "⚙️ Если у тебя Tonkeeper <b>W5R1</b> — поставь <code>TON_WALLET_VERSION=v5r1</code> и (обычно) <code>TON_NETWORK_GLOBAL_ID=-239</code>, <code>TON_SUBWALLET_NUMBER=0</code>\n"
        "/restart_service - Перезапустить сервис getgems\n"
        "/cancel - Отменить текущую операцию\n\n"
        "⚠️ <b>Внимание:</b> Изменения применяются сразу к обоим .env файлам."
    )
    await message.answer(help_text, parse_mode="HTML")


@dp.message(Command("ton_set_master_key"))
async def cmd_ton_set_master_key(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Использование:\n<code>/ton_set_master_key FERNET_KEY</code>\n\n"
            "Сгенерировать ключ можно так:\n"
            "<code>python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"</code>",
            parse_mode="HTML",
        )
        return
    key = parts[1].strip()
    results = update_all_env_files("TON_MASTER_KEY", key)
    if any(results.values()):
        await message.answer("✅ TON_MASTER_KEY сохранён в .env и backend/.env")
    else:
        await message.answer("❌ Не удалось сохранить TON_MASTER_KEY")


@dp.message(Command("ton_set_encrypted"))
async def cmd_ton_set_encrypted(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Использование:\n<code>/ton_set_encrypted TOKEN</code>\n\n"
            "TOKEN — это шифротекст (Fernet), а не сид-фраза.\n"
            "Сгенерировать TOKEN можно через:\n<code>cat mnemonic.txt | python3 ton_wallet_setup.py</code>",
            parse_mode="HTML",
        )
        return
    token = parts[1].strip()
    results = update_all_env_files("TON_WALLET_ENCRYPTED", token)
    if any(results.values()):
        await message.answer("✅ TON_WALLET_ENCRYPTED сохранён в .env и backend/.env")
    else:
        await message.answer("❌ Не удалось сохранить TON_WALLET_ENCRYPTED")


@dp.message(Command("ton_status"))
async def cmd_ton_status(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    try:
        from ton_wallet import ton_status, ton_derive_addresses
        st = await ton_status()
        bal = st.get("balance_nanoton", 0)
        bal_ton = bal / 1e9
        # Покажем несколько вариантов адреса (bounceable/non-bounceable) для сравнения с Tonkeeper
        variants = await ton_derive_addresses()
        # Берем первый вариант как "текущий" (по env)
        cur = variants[0] if variants else {}
        await message.answer(
            "💎 <b>TON кошелёк</b>\n\n"
            f"🏷️ <b>Адрес (bounceable):</b> <code>{st.get('address','')}</code>\n"
            + (f"🏷️ <b>Адрес (non-bounceable):</b> <code>{cur.get('non_bounceable','')}</code>\n" if cur else "")
            + (f"🏷️ <b>Raw:</b> <code>{cur.get('raw','')}</code>\n" if cur else "")
            + (f"⚙️ <b>Version:</b> <code>{cur.get('version','')}</code>\n" if cur else "")
            + (f"🧩 <b>wallet_id:</b> <code>{cur.get('wallet_id','')}</code>\n" if cur else "")
            + (f"🧱 <b>workchain:</b> <code>{cur.get('workchain','')}</code>\n" if cur else "")
            + f"\n💰 <b>Баланс:</b> {bal_ton:.6f} TON\n\n"
            "Если адрес не совпадает с Tonkeeper — используй /ton_guess <code>ВАШ_АДРЕС</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка ton_status: <code>{str(e)[:350]}</code>", parse_mode="HTML")


@dp.message(Command("ton_guess"))
async def cmd_ton_guess(message: types.Message):
    """Подбор версии/кошелёк-id по ожидаемому адресу (без раскрытия seed)."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Использование:\n<code>/ton_guess ВАШ_АДРЕС</code>\n"
            "Пример:\n<code>/ton_guess EQ....</code>",
            parse_mode="HTML",
        )
        return
    expected = parts[1].strip()
    try:
        from ton_wallet import ton_derive_addresses
        variants = await ton_derive_addresses()
        matches = []
        for v in variants:
            if expected in {v.get("bounceable"), v.get("non_bounceable"), v.get("raw")}:
                matches.append(v)
        if not matches:
            # покажем первые 6 вариантов
            preview = variants[:6]
            text = "❌ Совпадений не найдено.\n\nПервые варианты:\n"
            for v in preview:
                text += (
                    f"\n• <b>{v['version']}</b> wallet_id=<code>{v['wallet_id']}</code>\n"
                    f"  b: <code>{v['bounceable']}</code>\n"
                    f"  nb: <code>{v['non_bounceable']}</code>"
                )
            text += "\n\nВозможные причины: другая seed-фраза, другая версия кошелька, другой wallet_id."
            await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
            return

        # Выведем матч и предложим какие env поставить
        v = matches[0]
        await message.answer(
            "✅ <b>Найдено совпадение</b>\n\n"
            f"Version: <code>{v['version']}</code>\n"
            f"wallet_id: <code>{v['wallet_id']}</code>\n"
            f"workchain: <code>{v['workchain']}</code>\n\n"
            "Чтобы закрепить это, установи в .env:\n"
            f"<code>TON_WALLET_VERSION={v['version']}</code>\n"
            f"<code>TON_WALLET_ID={v['wallet_id']}</code>\n\n"
            "Потом проверь /ton_status",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка ton_guess: <code>{str(e)[:350]}</code>", parse_mode="HTML")


@dp.message(Command("ton_deploy"))
async def cmd_ton_deploy(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    try:
        from ton_wallet import ton_deploy_wallet_if_needed
        res = await ton_deploy_wallet_if_needed()
        if res.get("already"):
            await message.answer(f"✅ Кошелёк уже инициализирован: <code>{res.get('address')}</code>", parse_mode="HTML")
        else:
            await message.answer(f"✅ Отправлен deploy (init) для: <code>{res.get('address')}</code>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка ton_deploy: <code>{str(e)[:350]}</code>", parse_mode="HTML")


@dp.message(Command("ton_send"))
async def cmd_ton_send(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 3:
        await message.answer(
            "Использование:\n<code>/ton_send ADDRESS AMOUNT [COMMENT]</code>\n"
            "Пример:\n<code>/ton_send EQ... 0.5 payout</code>",
            parse_mode="HTML",
        )
        return
    to_addr = parts[1].strip()
    try:
        amount = float(parts[2].replace(",", "."))
    except Exception:
        await message.answer("❌ Неверная сумма. Пример: <code>0.5</code>", parse_mode="HTML")
        return
    comment = parts[3].strip() if len(parts) >= 4 else ""

    try:
        from ton_wallet import ton_send
        await message.answer("⏳ Отправляю транзакцию в TON...")
        res = await ton_send(to_addr, amount, comment=comment)
        await message.answer(
            "✅ <b>TON отправлено</b>\n\n"
            f"⬅️ <b>From:</b> <code>{res.get('from','')}</code>\n"
            f"➡️ <b>To:</b> <code>{res.get('to','')}</code>\n"
            f"💰 <b>Amount:</b> {res.get('amount_ton')} TON\n"
            f"🔢 <b>Seqno:</b> {res.get('seqno')}\n"
            f"🧾 <b>Toncenter result:</b> <code>{str(res.get('result'))[:350]}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка ton_send: <code>{str(e)[:350]}</code>", parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    """Показывает текущие значения переменных"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этому боту.")
        return
    
    try:
        # Читаем основной .env
        env_vars = {}
        if ENV_FILE.exists():
            with open(ENV_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        key, value = line.split('=', 1)
                        if key in ['BOT_TOKEN', 'GETGEMS_BOT_TOKEN', 'BOT_USERNAME']:
                            # Скрываем часть токена для безопасности
                            if 'TOKEN' in key and len(value) > 10:
                                env_vars[key] = value[:10] + "..." + value[-5:]
                            else:
                                env_vars[key] = value
        
        status_text = "📊 <b>Текущие значения:</b>\n\n"
        for key in ['BOT_TOKEN', 'GETGEMS_BOT_TOKEN', 'BOT_USERNAME']:
            value = env_vars.get(key, "не установлено")
            status_text += f"<b>{key}:</b> {value}\n"
        
        await message.answer(status_text, parse_mode="HTML")
        
    except Exception as e:
        await message.answer(f"❌ Ошибка чтения .env файла: {e}")


@dp.message(Command("set_bot_token"))
async def cmd_set_bot_token(message: types.Message, state: FSMContext):
    """Начинает процесс изменения BOT_TOKEN"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этому боту.")
        return
    
    await message.answer(
        "📝 Отправьте новый BOT_TOKEN.\n"
        "Формат: <code>1234567890:ABCdefGHIjklMNOpqrsTUVwxyz</code>\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    await state.set_state(EnvEditStates.waiting_for_bot_token)


@dp.message(Command("set_getgems_token"))
async def cmd_set_getgems_token(message: types.Message, state: FSMContext):
    """Начинает процесс изменения GETGEMS_BOT_TOKEN"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этому боту.")
        return
    
    await message.answer(
        "📝 Отправьте новый GETGEMS_BOT_TOKEN.\n"
        "Формат: <code>1234567890:ABCdefGHIjklMNOpqrsTUVwxyz</code>\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    await state.set_state(EnvEditStates.waiting_for_getgems_token)


@dp.message(Command("set_bot_username"))
async def cmd_set_bot_username(message: types.Message, state: FSMContext):
    """Начинает процесс изменения BOT_USERNAME"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этому боту.")
        return
    
    await message.answer(
        "📝 Отправьте новый BOT_USERNAME.\n"
        "Формат: <code>MyBot</code> (без @)\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    await state.set_state(EnvEditStates.waiting_for_bot_username)


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    """Отменяет текущую операцию"""
    await state.clear()
    await message.answer("❌ Операция отменена.")


@dp.message(Command("restart_service"))
async def cmd_restart_service(message: types.Message):
    """Перезапускает сервис getgems"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этому боту.")
        return
    
    await message.answer("🔄 Перезапускаю сервис getgems...")
    
    try:
        import subprocess
        result = subprocess.run(
            ["systemctl", "restart", "getgems.service"],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            await message.answer("✅ Сервис успешно перезапущен!")
        else:
            await message.answer(f"❌ Ошибка перезапуска сервиса:\n<code>{result.stderr}</code>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("selftest"))
async def cmd_selftest(message: types.Message):
    """Самопроверка: парсит валидные NFT, считает флор и показывает сообщение о профите."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return

    test_id = secrets.token_hex(4)
    started_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    status = await message.answer(f"🧪 SelfTest запущен\nID: <code>{test_id}</code>\n⏱️ {started_at}\n\n⏳ Парсю валидные NFT и считаю флор...", parse_mode="HTML")

    report_lines = []
    report_lines.append(f"🧪 <b>SelfTest</b> <code>{test_id}</code>")
    report_lines.append(f"⏱️ <b>Запущен:</b> {started_at}")

    # Берем реальные валидные NFT ссылки из базы данных
    try:
        import sys
        import os as os_module
        import random
        from utils import is_valid_nft_link
        from database import Database
        
        # Пытаемся получить реальные ссылки из БД
        db = Database()
        real_links = []
        
        try:
            # Получаем ссылки из последних профитов (за последнюю неделю, чтобы избежать старых несуществующих NFT)
            all_profit_links = db.get_all_profit_links_all_users(period='week')
            # Фильтруем только валидные ссылки
            real_links = [link for link in all_profit_links if link and is_valid_nft_link(link)]
            # Убираем дубликаты
            real_links = list(set(real_links))
            # Сортируем, чтобы брать последние (более свежие)
            real_links = sorted(real_links, reverse=True)
        except Exception as db_err:
            logger.warning(f"Не удалось получить ссылки из БД: {db_err}")
        
        # Если в БД нет ссылок - используем известные реальные NFT из примеров (НО НЕ ГЕНЕРИРУЕМ РАНДОМНЫЕ!)
        if not real_links:
            # Используем ТОЛЬКО реальные известные существующие NFT ссылки для теста
            known_real_links = [
                "https://t.me/nft/WhipCupcake-75130",
                "https://t.me/nft/SantaHat-3121099",
                "https://t.me/nft/MoonPendant-38464",
                "https://t.me/nft/MousseCake-134165",
                "https://t.me/nft/SpringBasket-148970",
                "https://t.me/nft/FaithAmulet-16780",
                "https://t.me/nft/PrettyPosy-84988",
                "https://t.me/nft/DiamondRing-15743",
            ]
            real_links = [link for link in known_real_links if is_valid_nft_link(link)]
            report_lines.append("ℹ️ Используются известные реальные NFT (в БД нет свежих сохраненных)")
        else:
            report_lines.append(f"✅ Найдено реальных ссылок в БД (за неделю): {len(real_links)}")
        
        # Выбираем последние ссылки из реальных (3-5 штук), НЕ случайные!
        num_links = min(random.randint(3, 5), len(real_links))
        if real_links and num_links > 0:
            # Берем последние N ссылок (более свежие), а не случайные
            selected_links = real_links[:num_links]
        else:
            selected_links = []
        
        if not selected_links:
            report_lines.append("❌ Не найдено валидных NFT ссылок в БД и тестовых примерах")
            await status.edit_text("\n".join(report_lines), parse_mode="HTML", disable_web_page_preview=True)
            return
        
        # Добавляем путь для импорта portals_floor
        killamonjaro_path = '/root/KillamonjaroAuto/src/utils'
        if os_module.path.exists(killamonjaro_path) and killamonjaro_path not in sys.path:
            sys.path.insert(0, killamonjaro_path)
        
        # Получаем auth_data для расчета флора
        auth_data = os_module.getenv('PORTALS_AUTH_DATA', '')
        if not auth_data:
            try:
                from portals_api import get_auth_data as get_auth_data_fallback
                auth_data = get_auth_data_fallback()
            except Exception:
                auth_data = None
        
        # Рассчитываем флор для реальных ссылок
        try:
            from portals_floor import extract_collection_name, format_collection_name
        except ImportError:
            extract_collection_name = None
            format_collection_name = None
        import requests
        
        # Получаем все floor цены один раз
        all_floors_cache = None
        if auth_data:
            try:
                url = 'https://portal-market.com/api/collections/floors'
                headers = {
                    'Authorization': auth_data,
                    'Origin': 'https://portal-market.com',
                    'Referer': 'https://portal-market.com/',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
                response = requests.get(url, headers=headers, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    all_floors_cache = data.get('floorPrices', data)
                    if not isinstance(all_floors_cache, dict):
                        all_floors_cache = None
            except Exception:
                pass
        
        # Рассчитываем флор для каждой ссылки
        valid_links = []
        floor_details = []
        total_floor = 0.0
        
        # Оптимизация: собираем уникальные коллекции сначала
        unique_collections = {}
        for link in selected_links:
            try:
                # Если portals_floor недоступен, пытаемся извлечь имя вручную
                if extract_collection_name:
                    collection_name = extract_collection_name(link)
                else:
                    # Fallback: парсим имя из ссылки вручную (формат: https://t.me/nft/CollectionName-Number)
                    import re
                    match = re.search(r'/nft/([A-Za-z]+)-(\d+)', link)
                    collection_name = match.group(1) if match else None

                if collection_name and collection_name not in unique_collections:
                    unique_collections[collection_name] = []
                if collection_name:
                    unique_collections[collection_name].append(link)
            except Exception:
                pass
        
        # Рассчитываем флор для каждой уникальной коллекции
        for collection_name, links in unique_collections.items():
            try:
                floor_price = None
                
                # Используем кэш для поиска флора
                if all_floors_cache:
                    try:
                        # Если portals_floor недоступен, используем имя как есть
                        formatted_name = format_collection_name(collection_name) if format_collection_name else collection_name
                        possible_keys = [
                            formatted_name,
                            collection_name.lower(),
                            collection_name,
                            formatted_name.replace(' ', ''),
                            collection_name.replace('Cake', ' Cake').strip().lower()
                        ]
                        
                        for key in possible_keys:
                            if key in all_floors_cache:
                                price = float(all_floors_cache[key])
                                if price > 0:
                                    floor_price = price
                                    break
                        
                        if not floor_price:
                            name_lower = formatted_name.lower()
                            for key, value in all_floors_cache.items():
                                key_lower = key.lower()
                                if name_lower in key_lower or key_lower in name_lower:
                                    price = float(value)
                                    if price > 0:
                                        floor_price = price
                                        break
                    except Exception:
                        pass
                
                if not floor_price:
                    # Fallback: если флор не найден в API, используем минимальный флор для теста
                    floor_price = 0.5  # 0.5 TON по умолчанию для теста

                floor_value = float(floor_price)
                # Добавляем все ссылки этой коллекции
                for link in links:
                    valid_links.append(link)
                    total_floor += floor_value
                    floor_details.append({
                        'link': link,
                        'collection': collection_name,
                        'floor': floor_value
                    })
            except Exception as e:
                logger.warning(f"Ошибка при расчете флора для {collection_name}: {e}")
                # Даже при ошибке добавляем ссылки с минимальным флором
                for link in links:
                    valid_links.append(link)
                    total_floor += 0.5
                    floor_details.append({
                        'link': link,
                        'collection': collection_name,
                        'floor': 0.5
                    })

        if not valid_links:
            report_lines.append("❌ Не удалось найти валидные NFT ссылки")
            await status.edit_text("\n".join(report_lines), parse_mode="HTML", disable_web_page_preview=True)
            return
        
        report_lines.append(f"✅ Найдено валидных NFT: {len(valid_links)}")
        report_lines.append(f"💎 Общий флор: {total_floor:.2f} TON")
        
        # Получаем процент воркера из БД
        from database import Database
        db = Database()
        worker_percent = db.get_worker_percent(message.from_user.id)
        worker_share = total_floor * (worker_percent / 100.0)
        report_lines.append(f"👷 Доля воркера ({worker_percent:.1f}%): {worker_share:.2f} TON")
        
        # Обновляем статус
        await status.edit_text("\n".join(report_lines) + "\n\n⏳ Формирую сообщение о профите...", parse_mode="HTML", disable_web_page_preview=True)
        
        # Формируем сообщение о профите как в реальном профите
        try:
            from utils import send_profit_log
            from database import Database
            
            # Обновляем запись пользователя в БД
            db = Database()
            db_user = db.get_or_create_user(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
                last_name=message.from_user.last_name,
            )
            
            user_info = {'id': message.from_user.id, 'username': message.from_user.username, 'phone': "+79990000000"}
            worker_info = {'telegram_id': message.from_user.id, 'username': message.from_user.username or 'test_worker'}
            
            # Отправляем профит лог (это покажет сообщение в Discord, Telegram и т.д.)
            await send_profit_log(
                worker_info=worker_info,
                transferred_gift_links=valid_links,
                user_id=message.from_user.id,
                failed_gift_transfers=None
            )
            
            report_lines.append("✅ Сообщение о профите отправлено!")
            report_lines.append(f"📊 Подарков: {len(valid_links)}")
            report_lines.append(f"💎 Флор: {total_floor:.2f} TON")
            # Получаем процент воркера из БД
            worker_percent = db.get_worker_percent(message.from_user.id)
            worker_share = total_floor * (worker_percent / 100.0)
            report_lines.append(f"💰 Доля воркера ({worker_percent:.1f}%): {worker_share:.2f} TON")
            
            # Добавляем детали по каждому NFT
            report_lines.append("\n<b>📋 Детали NFT:</b>")
            for detail in floor_details:
                report_lines.append(f"  • {detail['collection']}: {detail['floor']:.2f} TON")
                report_lines.append(f"    {detail['link']}")
            
        except Exception as e:
            report_lines.append(f"❌ Ошибка отправки профита: {e}")
            logger.error(f"Ошибка в cmd_selftest: {e}", exc_info=True)
    
    except Exception as e:
        report_lines.append(f"❌ Критическая ошибка: {e}")
        logger.error(f"Критическая ошибка в cmd_selftest: {e}", exc_info=True)

    await status.edit_text("\n".join(report_lines), parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("bots"))
async def cmd_bots(message: types.Message):
    """Показывает статус всех bot-сервисов и отмечает нерабочие."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return

    services = _list_bot_services()
    if not services:
        await message.answer("❌ Не найдено bot-сервисов.")
        return

    await message.answer("🔎 Проверяю сервисы ботов... (это может занять 5-15 секунд)")

    broken = []
    lines = ["🤖 <b>Bot services status</b>\n"]

    for svc in services:
        active = _systemctl_is_active(svc)
        enabled = _systemctl_is_enabled(svc)
        svc_text = _read_service_file(svc)
        token = _extract_env_value(svc_text, "BOT_TOKEN")
        token_check = {"ok": None, "error": "NO_CHECK", "username": ""}
        # Проверяем токен только если он реально в unit (getgems-bot.service берет из .env)
        if token:
            token_check = await _check_bot_token(token)

        is_broken = False
        reasons = []
        if active not in {"active"}:
            is_broken = True
            reasons.append(f"active={active}")
        if token and not token_check.get("ok"):
            is_broken = True
            reasons.append(f"token={token_check.get('error') or 'BAD'}")

        title = f"<code>{html.escape(svc)}</code>"
        if token_check.get("username"):
            username = html.escape(token_check['username'])
            title += f" @{username}"
        active_escaped = html.escape(active)
        enabled_escaped = html.escape(enabled)
        token_status = 'ok' if token and token_check.get('ok') else ('skip' if not token else 'bad')
        lines.append(
            f"{'❌' if is_broken else '✅'} {title}\n"
            f"   • active: <b>{active_escaped}</b>\n"
            f"   • enabled: <b>{enabled_escaped}</b>\n"
            f"   • token_check: <b>{token_status}</b>"
        )
        if reasons:
            reasons_text = '; '.join(reasons)[:350]
            reasons_escaped = html.escape(reasons_text)
            lines.append(f"   • reasons: <code>{reasons_escaped}</code>")

        if is_broken:
            broken.append(svc)

    if broken:
        lines.append("\n🧹 <b>Нерабочие сервисы:</b>")
        for svc in broken:
            svc_escaped = html.escape(svc)
            lines.append(f"- <code>{svc_escaped}</code>")
        lines.append("\nЧтобы удалить: <code>/delete_bot SERVICE_NAME</code>\nНапр.: <code>/delete_bot getgems_bot2.service</code>")
    else:
        lines.append("\n✅ Нерабочих сервисов не найдено.")

    # Отправляем с разбиением, чтобы не упереться в лимит Telegram
    text = "\n".join(lines)
    max_len = 3800
    for i in range(0, len(text), max_len):
        await message.answer(text[i:i + max_len], parse_mode="HTML", disable_web_page_preview=True)


@dp.message(Command("sudo"))
async def cmd_sudo(message: types.Message):
    """Выполнение системных команд от имени root"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    # Получаем команду из сообщения
    command_text = message.text or ""
    parts = command_text.split(maxsplit=1)
    
    if len(parts) < 2:
        await message.answer(
            "🔧 <b>Выполнение системных команд</b>\n\n"
            "Использование: <code>/sudo команда</code>\n\n"
            "Примеры:\n"
            "• <code>/sudo getgems restart</code>\n"
            "• <code>/sudo systemctl status getgems</code>\n"
            "• <code>/sudo apt update</code>\n"
            "• <code>/sudo ls -la /root/getgems</code>\n\n"
            "⚠️ <b>Внимание:</b> Команды выполняются от имени root!",
            parse_mode="HTML"
        )
        return
    
    command = parts[1].strip()
    
    # Список опасных команд, которые можно заблокировать (опционально)
    # Можно раскомментировать для дополнительной безопасности
    # dangerous_commands = ["rm -rf", "dd if=", "mkfs", "fdisk"]
    # for dangerous in dangerous_commands:
    #     if dangerous in command.lower():
    #         await message.answer(f"❌ Команда содержит потенциально опасную операцию: <code>{dangerous}</code>", parse_mode="HTML")
    #         return
    
    # Логируем выполнение команды
    user_info = f"{message.from_user.first_name}"
    if message.from_user.username:
        user_info += f" (@{message.from_user.username})"
    logger.info(f"🔧 [SUDO] User {user_info} (ID: {message.from_user.id}) выполнил команду: {command}")
    
    # Отправляем сообщение о начале выполнения
    status_msg = await message.answer(
        f"⏳ Выполняю команду:\n<code>{command}</code>\n\n"
        f"👤 Пользователь: {user_info}",
        parse_mode="HTML"
    )
    
    try:
        # Выполняем команду
        # Используем shell=True для поддержки сложных команд, но с ограничениями
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,  # Максимум 5 минут
            cwd="/root/getgems"
        )
        
        # Функция для разбиения текста на части по лимиту Telegram (4096 символов)
        def split_message(text, max_length=4000):
            """Разбивает текст на части, не превышающие max_length символов"""
            if len(text) <= max_length:
                return [text]
            
            parts = []
            current_part = ""
            
            # Разбиваем по строкам, чтобы не резать посередине
            lines = text.split('\n')
            
            for line in lines:
                # Если одна строка больше лимита, разбиваем её
                if len(line) > max_length - 100:  # Оставляем запас
                    if current_part:
                        parts.append(current_part.rstrip())
                        current_part = ""
                    
                    # Разбиваем длинную строку на куски
                    for i in range(0, len(line), max_length - 100):
                        chunk = line[i:i + max_length - 100]
                        parts.append(chunk)
                else:
                    # Проверяем, поместится ли строка в текущую часть
                    test_part = current_part + line + "\n"
                    if len(test_part) > max_length:
                        if current_part:
                            parts.append(current_part.rstrip())
                        current_part = line + "\n"
                    else:
                        current_part = test_part
            
            if current_part:
                parts.append(current_part.rstrip())
            
            return parts
        
        # Формируем базовое сообщение с результатом
        base_output = []
        base_output.append(f"✅ <b>Команда выполнена</b>\n")
        base_output.append(f"<code>{command}</code>\n")
        
        if result.returncode == 0:
            base_output.append("📊 <b>Код возврата:</b> 0 (успешно)\n")
        else:
            base_output.append(f"⚠️ <b>Код возврата:</b> {result.returncode}\n")
        
        base_text = "\n".join(base_output)
        
        # Отправляем базовое сообщение
        await status_msg.edit_text(base_text, parse_mode="HTML")
        
        # Отправляем вывод команды (stdout)
        if result.stdout:
            stdout_text = result.stdout.strip()
            if stdout_text:
                stdout_parts = split_message(stdout_text, max_length=4000)
                total_parts = len(stdout_parts)
                
                for i, part in enumerate(stdout_parts):
                    if total_parts > 1:
                        message_text = f"📤 <b>Вывод (часть {i+1}/{total_parts}):</b>\n<pre>{part}</pre>"
                    else:
                        message_text = f"📤 <b>Вывод:</b>\n<pre>{part}</pre>"
                    
                    await message.answer(message_text, parse_mode="HTML")
                    await asyncio.sleep(0.2)  # Небольшая задержка между сообщениями
        
        # Отправляем ошибки (stderr)
        if result.stderr:
            stderr_text = result.stderr.strip()
            if stderr_text:
                stderr_parts = split_message(stderr_text, max_length=4000)
                total_parts = len(stderr_parts)
                
                for i, part in enumerate(stderr_parts):
                    if total_parts > 1:
                        message_text = f"❌ <b>Ошибки (часть {i+1}/{total_parts}):</b>\n<pre>{part}</pre>"
                    else:
                        message_text = f"❌ <b>Ошибки:</b>\n<pre>{part}</pre>"
                    
                    await message.answer(message_text, parse_mode="HTML")
                    await asyncio.sleep(0.2)  # Небольшая задержка между сообщениями
        
        # Если нет вывода и нет ошибок
        if not result.stdout and not result.stderr:
            await message.answer("✅ Команда выполнена без вывода")
        
        # Логируем результат
        if result.returncode == 0:
            logger.info(f"✅ [SUDO] Команда выполнена успешно: {command}")
        else:
            logger.warning(f"⚠️ [SUDO] Команда завершилась с кодом {result.returncode}: {command}")
            if result.stderr:
                logger.warning(f"[SUDO] Ошибка: {result.stderr[:500]}")
        
    except subprocess.TimeoutExpired:
        await status_msg.edit_text(
            f"⏱️ <b>Таймаут выполнения команды</b>\n\n"
            f"Команда выполнялась более 5 минут и была прервана.\n"
            f"<code>{command}</code>",
            parse_mode="HTML"
        )
        logger.error(f"⏱️ [SUDO] Таймаут выполнения команды: {command}")
        
    except Exception as e:
        error_msg = str(e)[:500]
        await status_msg.edit_text(
            f"❌ <b>Ошибка выполнения команды</b>\n\n"
            f"<code>{command}</code>\n\n"
            f"Ошибка: <code>{error_msg}</code>",
            parse_mode="HTML"
        )
        logger.error(f"❌ [SUDO] Ошибка выполнения команды '{command}': {e}", exc_info=True)


@dp.message(Command("delete_bot"))
async def cmd_delete_bot(message: types.Message):
    """Удаляет выбранный bot systemd service."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "🗑️ Использование:\n"
            "<code>/delete_bot SERVICE_NAME</code>\n"
            "Напр.: <code>/delete_bot getgems_bot2.service</code>",
            parse_mode="HTML",
        )
        return

    svc = parts[1].strip()
    if not svc.endswith(".service"):
        svc += ".service"

    ok, err = _delete_service_file(svc)
    if ok:
        await message.answer(f"✅ Сервис удалён: <code>{svc}</code>", parse_mode="HTML")
    else:
        await message.answer(f"❌ Не удалось удалить <code>{svc}</code>: <code>{err}</code>", parse_mode="HTML")


@dp.message(Command("enable_bots"))
async def cmd_enable_bots(message: types.Message, state: FSMContext):
    """Начинает процесс включения нескольких ботов"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этому боту.")
        return
    
    await message.answer(
        "📝 <b>Включение нескольких ботов</b>\n\n"
        "Отправьте токены ботов, каждый токен с новой строки.\n"
        "Формат:\n"
        "<code>1234567890:ABCdefGHIjklMNOpqrsTUVwxyz\n"
        "9876543210:XYZabcDEFghiJKLmnoPQRstuv</code>\n\n"
        "Бот автоматически:\n"
        "• Настроит каждого бота (имя, описание, веб-апп)\n"
        "• Создаст systemd сервисы\n"
        "• Включит и запустит сервисы\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    await state.set_state(EnvEditStates.waiting_for_bot_tokens)


@dp.message(EnvEditStates.waiting_for_bot_tokens)
async def process_bot_tokens(message: types.Message, state: FSMContext):
    """Обрабатывает несколько токенов и создает сервисы"""
    if not message.text:
        await message.answer("❌ Отправьте токены текстом (каждый токен с новой строки) или /cancel для отмены.")
        return

    tokens_text = message.text.strip()
    
    # Разбиваем на строки и очищаем
    tokens = [token.strip() for token in tokens_text.split('\n') if token.strip()]
    
    if not tokens:
        await message.answer(
            "❌ Не найдено ни одного токена!\n"
            "Отправьте токены, каждый с новой строки, или /cancel для отмены"
        )
        return
    
    # Проверяем формат каждого токена
    invalid_tokens = []
    valid_tokens = []
    for i, token in enumerate(tokens, 1):
        if not re.match(r'^\d+:[A-Za-z0-9_-]+$', token):
            invalid_tokens.append(f"Токен #{i}: {token[:20]}...")
        else:
            valid_tokens.append(token)
    
    if invalid_tokens:
        await message.answer(
            f"❌ <b>Неверный формат токенов:</b>\n" + "\n".join(invalid_tokens) +
            "\n\nПравильный формат: <code>1234567890:ABCdefGHIjklMNOpqrsTUVwxyz</code>",
            parse_mode="HTML"
        )
        if not valid_tokens:
            return
    
    await message.answer(f"⚙️ Обрабатываю {len(valid_tokens)} ботов...")
    
    results = []
    bot_num = 1
    
    # Находим следующий свободный номер бота
    import subprocess
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10
        )
        existing_services = [line for line in result.stdout.split('\n') if 'getgems_bot' in line]
        if existing_services:
            # Находим максимальный номер
            numbers = []
            for service in existing_services:
                match = re.search(r'getgems_bot(\d+)', service)
                if match:
                    numbers.append(int(match.group(1)))
            if numbers:
                bot_num = max(numbers) + 1
    except Exception as e:
        logger.warning(f"Не удалось проверить существующие сервисы: {e}")
    
    for token in valid_tokens:
        bot_result = {
            'token': token[:20] + "...",
            'username': '',
            'configured': False,
            'service_created': False,
            'service_started': False,
            'errors': []
        }
        
        try:
            # Получаем username бота
            await message.answer(f"🔍 Бот #{bot_num}: Проверяю токен...")
            bot_username = await get_bot_username_from_token(token)
            if bot_username:
                bot_result['username'] = bot_username
            else:
                bot_result['errors'].append("Не удалось получить username")
            
            # Настраиваем бота
            await message.answer(f"⚙️ Бот #{bot_num}: Настраиваю (имя, описание, веб-апп)...")
            config_results = await configure_bot_automatically(token)
            if any([config_results['name'], config_results['description'], config_results['menu_button']]):
                bot_result['configured'] = True
            else:
                bot_result['errors'].append("Не удалось настроить бота")
            
            # Создаем сервис
            await message.answer(f"📝 Бот #{bot_num}: Создаю systemd сервис...")
            service_result = create_bot_service(bot_num, token, bot_username)
            if service_result['created']:
                bot_result['service_created'] = True
                service_name = service_result['service_name']
                
                # Включаем и запускаем сервис
                await message.answer(f"🚀 Бот #{bot_num}: Запускаю сервис...")
                start_result = enable_and_start_service(service_name)
                if start_result['started']:
                    bot_result['service_started'] = True
                else:
                    bot_result['errors'].append(f"Не удалось запустить сервис: {start_result.get('error', 'Unknown error')}")
            else:
                bot_result['errors'].append(f"Не удалось создать сервис: {service_result.get('error', 'Unknown error')}")
            
        except Exception as e:
            bot_result['errors'].append(f"Критическая ошибка: {str(e)}")
            logger.error(f"❌ Ошибка обработки бота #{bot_num}: {e}", exc_info=True)
        
        results.append(bot_result)
        bot_num += 1
        
        # Небольшая задержка между ботами
        await asyncio.sleep(1)
    
    # Формируем итоговый отчет
    report = "📊 <b>Итоговый отчет:</b>\n\n"
    
    success_count = 0
    for i, result in enumerate(results, 1):
        status_icon = "✅" if result['service_started'] else "❌"
        report += f"{status_icon} <b>Бот #{i}</b>\n"
        
        if result['username']:
            report += f"   🆔 @{result['username']}\n"
        
        if result['configured']:
            report += "   ⚙️ Настроен\n"
        else:
            report += "   ⚠️ Настройка не удалась\n"
        
        if result['service_created']:
            report += "   📝 Сервис создан\n"
        else:
            report += "   ❌ Сервис не создан\n"
        
        if result['service_started']:
            report += "   🚀 Сервис запущен\n"
            success_count += 1
        else:
            report += "   ❌ Сервис не запущен\n"
        
        if result['errors']:
            report += f"   ⚠️ Ошибки: {', '.join(result['errors'][:2])}\n"
        
        report += "\n"
    
    report += f"✅ <b>Успешно включено:</b> {success_count} из {len(results)} ботов\n\n"
    report += "📋 <b>Управление сервисами:</b>\n"
    report += "<code>sudo systemctl status getgems_bot1.service</code>\n"
    report += "<code>sudo journalctl -u getgems_bot1.service -f</code>"
    
    await message.answer(report, parse_mode="HTML")
    await state.clear()


@dp.message(EnvEditStates.waiting_for_bot_token)
async def process_bot_token(message: types.Message, state: FSMContext):
    """Обрабатывает новый BOT_TOKEN"""
    token = message.text.strip()
    
    # Проверяем формат токена
    if not re.match(r'^\d+:[A-Za-z0-9_-]+$', token):
        await message.answer(
            "❌ Неверный формат токена!\n"
            "Правильный формат: <code>1234567890:ABCdefGHIjklMNOpqrsTUVwxyz</code>\n"
            "Попробуйте еще раз или отправьте /cancel",
            parse_mode="HTML"
        )
        return
    
    # Пытаемся получить username бота по токену
    await message.answer("🔍 Проверяю токен и получаю username бота...")
    bot_username = await get_bot_username_from_token(token)
    
    if not bot_username:
        await message.answer(
            "⚠️ Не удалось получить username бота по токену.\n"
            "Токен будет обновлен, но BOT_USERNAME останется прежним.\n"
            "Вы можете обновить его вручную через /set_bot_username"
        )
    else:
        # Автоматически обновляем BOT_USERNAME
        results_username = update_all_env_files("BOT_USERNAME", bot_username)
        if any(results_username.values()):
            await message.answer(f"✅ BOT_USERNAME автоматически обновлен на: <b>@{bot_username}</b>", parse_mode="HTML")
    
    # Автоматически настраиваем бота
    await message.answer("⚙️ Настраиваю бота автоматически (имя, описание, веб-апп)...")
    config_results = await configure_bot_automatically(token)
    
    # Формируем отчет о настройке
    config_status = "⚙️ <b>Результаты автоматической настройки:</b>\n"
    if config_results['name']:
        config_status += "✅ Имя: GetGems: sell and buy NFT\n"
    else:
        config_status += "❌ Имя: не установлено\n"
    
    if config_results['description']:
        config_status += "✅ Описание: установлено\n"
    else:
        config_status += "❌ Описание: не установлено\n"
    
    if config_results['menu_button']:
        config_status += "✅ Веб-апп кнопка: https://getgems.mooo.com\n"
    else:
        config_status += "❌ Веб-апп кнопка: не установлена\n"
    
    config_status += "\n⚠️ <b>Инлайн режим:</b> Включите вручную через @BotFather командой:\n"
    config_status += "<code>/setinline</code>\n"
    config_status += f"Выберите бота @{bot_username} и включите инлайн режим.\n"
    
    await message.answer(config_status, parse_mode="HTML")
    
    # Обновляем BOT_TOKEN
    results = update_all_env_files("BOT_TOKEN", token)
    
    # Также обновляем WEBAPP_URL если его нет или он отличается
    webapp_url = "https://getgems.mooo.com"
    webapp_results = update_all_env_files("WEBAPP_URL", webapp_url)
    if any(webapp_results.values()):
        await message.answer(f"✅ WEBAPP_URL автоматически установлен: <b>{webapp_url}</b>", parse_mode="HTML")
    
    if any(results.values()):
        status = "✅ <b>BOT_TOKEN обновлен:</b>\n"
        if results.get('main'):
            status += "• Основной .env\n"
        if results.get('backend'):
            status += "• backend/.env\n"
        
        status += f"\n🆔 Username бота: <b>@{bot_username}</b>" if bot_username else ""
        status += "\n\n⚠️ Не забудьте перезапустить сервис через /restart_service"
        
        await message.answer(status, parse_mode="HTML")
    else:
        await message.answer("❌ Не удалось обновить BOT_TOKEN. Проверьте права доступа к файлам.")
    
    await state.clear()


@dp.message(EnvEditStates.waiting_for_getgems_token)
async def process_getgems_token(message: types.Message, state: FSMContext):
    """Обрабатывает новый GETGEMS_BOT_TOKEN"""
    token = message.text.strip()
    
    # Проверяем формат токена
    if not re.match(r'^\d+:[A-Za-z0-9_-]+$', token):
        await message.answer(
            "❌ Неверный формат токена!\n"
            "Правильный формат: <code>1234567890:ABCdefGHIjklMNOpqrsTUVwxyz</code>\n"
            "Попробуйте еще раз или отправьте /cancel",
            parse_mode="HTML"
        )
        return
    
    # Пытаемся получить username бота по токену
    await message.answer("🔍 Проверяю токен GETGEMS_BOT_TOKEN...")
    bot_username = await get_bot_username_from_token(token)
    
    if bot_username:
        await message.answer(f"✅ Username бота: <b>@{bot_username}</b>", parse_mode="HTML")
    
    # Автоматически настраиваем бота
    await message.answer("⚙️ Настраиваю бота автоматически (имя, описание, веб-апп)...")
    config_results = await configure_bot_automatically(token)
    
    # Формируем отчет о настройке
    config_status = "⚙️ <b>Результаты автоматической настройки:</b>\n"
    if config_results['name']:
        config_status += "✅ Имя: GetGems: sell and buy NFT\n"
    else:
        config_status += "❌ Имя: не установлено\n"
    
    if config_results['description']:
        config_status += "✅ Описание: установлено\n"
    else:
        config_status += "❌ Описание: не установлено\n"
    
    if config_results['menu_button']:
        config_status += "✅ Веб-апп кнопка: https://getgems.mooo.com\n"
    else:
        config_status += "❌ Веб-апп кнопка: не установлена\n"
    
    config_status += "\n⚠️ <b>Инлайн режим:</b> Включите вручную через @BotFather командой:\n"
    config_status += "<code>/setinline</code>\n"
    if bot_username:
        config_status += f"Выберите бота @{bot_username} и включите инлайн режим.\n"
    
    await message.answer(config_status, parse_mode="HTML")
    
    # Обновляем GETGEMS_BOT_TOKEN
    results = update_all_env_files("GETGEMS_BOT_TOKEN", token)
    
    # Также обновляем WEBAPP_URL если его нет или он отличается
    webapp_url = "https://getgems.mooo.com"
    webapp_results = update_all_env_files("WEBAPP_URL", webapp_url)
    if any(webapp_results.values()):
        await message.answer(f"✅ WEBAPP_URL автоматически установлен: <b>{webapp_url}</b>", parse_mode="HTML")
    
    if any(results.values()):
        status = "✅ <b>GETGEMS_BOT_TOKEN обновлен:</b>\n"
        if results.get('main'):
            status += "• Основной .env\n"
        if results.get('backend'):
            status += "• backend/.env\n"
        
        if bot_username:
            status += f"\n🆔 Username бота: <b>@{bot_username}</b>"
        status += "\n\n⚠️ Не забудьте перезапустить сервис через /restart_service"
        
        await message.answer(status, parse_mode="HTML")
    else:
        await message.answer("❌ Не удалось обновить GETGEMS_BOT_TOKEN. Проверьте права доступа к файлам.")
    
    await state.clear()


@dp.message(EnvEditStates.waiting_for_bot_username)
async def process_bot_username(message: types.Message, state: FSMContext):
    """Обрабатывает новый BOT_USERNAME"""
    username = message.text.strip()
    
    # Убираем @ если есть
    if username.startswith("@"):
        username = username[1:]
    
    # Проверяем формат username
    if not re.match(r'^[a-zA-Z0-9_]{5,32}$', username):
        await message.answer(
            "❌ Неверный формат username!\n"
            "Username должен содержать только буквы, цифры и подчеркивания, длиной 5-32 символа.\n"
            "Попробуйте еще раз или отправьте /cancel"
        )
        return
    
    # Обновляем BOT_USERNAME
    results = update_all_env_files("BOT_USERNAME", username)
    
    if any(results.values()):
        status = "✅ Обновлено:\n"
        if results.get('main'):
            status += "• Основной .env\n"
        if results.get('backend'):
            status += "• backend/.env\n"
        status += f"\n🆔 Новый username: <b>@{username}</b>"
        status += "\n\n⚠️ Не забудьте перезапустить сервис через /restart_service"
        
        await message.answer(status, parse_mode="HTML")
    else:
        await message.answer("❌ Не удалось обновить BOT_USERNAME. Проверьте права доступа к файлам.")
    
    await state.clear()


@dp.message(Command("test_floor"))
async def cmd_test_floor(message: types.Message):
    """Рассчитывает флор для NFT ссылок и создает тестовый профит"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "💎 <b>Расчет флора и создание тестового профита</b>\n\n"
            "Использование:\n"
            "<code>/test_floor ссылка1 [ссылка2 ...]</code>\n\n"
            "Примеры:\n"
            "<code>/test_floor https://t.me/nft/MousseCake-12345</code>\n"
            "<code>/test_floor https://t.me/nft/MousseCake-12345 https://t.me/nft/SwagBag-67890</code>\n\n"
            "Команда рассчитает флор для каждой ссылки и создаст тестовый профит.",
            parse_mode="HTML"
        )
        return
    
    # Извлекаем ссылки из команды
    nft_links = parts[1:]
    
    try:
        import sys
        import os as os_module
        import re
        
        # Добавляем путь к другому проекту для импорта
        killamonjaro_path = '/root/KillamonjaroAuto/src/utils'
        if os_module.path.exists(killamonjaro_path) and killamonjaro_path not in sys.path:
            sys.path.insert(0, killamonjaro_path)
        
        # Получаем auth_data
        auth_data = os_module.getenv('PORTALS_AUTH_DATA', '')
        if not auth_data:
            try:
                from portals_api import get_auth_data as get_auth_data_fallback
                auth_data = get_auth_data_fallback()
            except Exception:
                await message.answer("❌ Не удалось получить PORTALS_AUTH_DATA")
                return
        
        # Рассчитываем флор для каждой ссылки
        results = []
        total_floor = 0.0
        valid_links = []
        
        try:
            from portals_floor import extract_collection_name, get_floor_price
        except ImportError:
            # Fallback на старый метод
            from portals_api import get_gifts_floors
            all_floors = get_gifts_floors(auth_data)
            extract_collection_name = None
            get_floor_price = None
        
        for link in nft_links:
            if not link.startswith('http'):
                link = f"https://{link}" if 't.me' in link else link
            
            # Проверяем, что это ссылка на NFT
            if '/nft/' not in link:
                results.append(f"❌ {link} - не является ссылкой на NFT")
                continue
            
            try:
                if extract_collection_name and get_floor_price:
                    # Используем правильный метод
                    collection_name = extract_collection_name(link)
                    if collection_name:
                        floor_price = get_floor_price(collection_name, auth_data=auth_data)
                        if floor_price:
                            total_floor += float(floor_price)
                            valid_links.append(link)
                            results.append(f"✅ {link}\n   Коллекция: {collection_name}\n   Флор: {floor_price:.2f}")
                        else:
                            results.append(f"⚠️ {link}\n   Коллекция: {collection_name}\n   Флор: не найден")
                    else:
                        results.append(f"❌ {link} - не удалось извлечь название коллекции")
                else:
                    # Fallback метод
                    match = re.search(r'/nft/([^/?]+)', link)
                    if match:
                        nft_name = match.group(1).split('-')[0]
                        nft_name_lower = nft_name.lower()
                        floor_price = 0
                        for floor_name, price in all_floors.items():
                            if nft_name_lower in floor_name.lower() or floor_name.lower() in nft_name_lower:
                                floor_price = float(price) if price else 0
                                break
                        if floor_price > 0:
                            total_floor += floor_price
                            valid_links.append(link)
                            results.append(f"✅ {link}\n   Коллекция: {nft_name}\n   Флор: {floor_price:.2f}")
                        else:
                            results.append(f"⚠️ {link}\n   Коллекция: {nft_name}\n   Флор: не найден")
                    else:
                        results.append(f"❌ {link} - неверный формат ссылки")
            except Exception as e:
                results.append(f"❌ {link} - ошибка: {str(e)}")
        
        # Формируем сообщение с результатами
        result_text = "💎 <b>Результаты расчета флора:</b>\n\n"
        result_text += "\n".join(results)
        result_text += f"\n\n📊 <b>Итого:</b>\n"
        result_text += f"• Всего ссылок: {len(nft_links)}\n"
        result_text += f"• Валидных: {len(valid_links)}\n"
        result_text += f"• Общий флор: {total_floor:.2f}\n"
        result_text += f"• Доля воркера (70%): {total_floor * 0.7:.2f}\n"
        
        # Создаем тестовый профит в БД
        if valid_links:
            try:
                from database import Database
                db = Database()
                
                # Получаем ID пользователя из сообщения (или используем тестовый)
                test_user_id = message.from_user.id
                
                # Создаем тестовый профит
                profit_id = db.save_profit(
                    user_id=test_user_id,
                    worker_telegram_id=test_user_id,
                    worker_username=message.from_user.username or "test",
                    gift_count=len(valid_links),
                    gift_links=valid_links,
                    failed_transfers=None,
                    floor_price=total_floor
                )
                
                result_text += f"\n✅ <b>Тестовый профит создан!</b>\n"
                result_text += f"• ID профита: {profit_id}\n"
                result_text += f"• User ID: {test_user_id}\n"
                result_text += f"• Подарков: {len(valid_links)}\n"
                result_text += f"• Флор: {total_floor:.2f}\n"
                result_text += f"• Доля воркера (70%): {total_floor * 0.7:.2f}\n"
            except Exception as db_err:
                result_text += f"\n⚠️ Не удалось создать тестовый профит: {str(db_err)}"
        
        await message.answer(result_text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка в cmd_test_floor: {e}", exc_info=True)
        await message.answer(f"❌ Произошла ошибка: {str(e)}")


@dp.message(Command("reset_balance"))
async def cmd_reset_balance(message: types.Message):
    """Обнулить баланс пользователя по username или ID"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    args = message.text.split()[1:] if len(message.text.split()) > 1 else []
    
    if not args:
        await message.answer(
            "💰 <b>Обнуление баланса пользователя</b>\n\n"
            "Использование: <code>/reset_balance &lt;username или ID&gt;</code>\n\n"
            "Примеры:\n"
            "• <code>/reset_balance @username</code>\n"
            "• <code>/reset_balance 123456789</code>\n\n"
            "⚠️ <b>Внимание:</b> Команда обнуляет все балансы пользователя:\n"
            "• balance_starts (звезды)\n"
            "• balance_rub, balance_uah, balance_byn, balance_ton, balance_usdt\n"
            "• pending_balance (если пользователь воркер)",
            parse_mode="HTML"
        )
        return
    
    identifier = args[0].strip()
    
    try:
        # Импортируем Database из backend, так как там есть метод reset_worker_balance
        import sys
        import os
        from pathlib import Path
        
        # Добавляем путь к backend в sys.path
        backend_path = Path(__file__).parent / 'backend'
        if str(backend_path) not in sys.path:
            sys.path.insert(0, str(backend_path))
            
        # Добавляем путь к корню для импорта config_bot
        root_path = Path(__file__).parent
        if str(root_path) not in sys.path:
            sys.path.insert(0, str(root_path))
        
        # Определяем путь к базе данных
        db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend', 'playerok.db'))
        if not os.path.exists(db_path):
            db_path = os.path.abspath('backend/playerok.db')
        
        from backend.database import Database
        db = Database(db_path=db_path)
        
        user = None
        user_id = None
        
        # Определяем, это username или ID
        if identifier.startswith('@'):
            # Это username
            username = identifier[1:]  # Убираем @
            user = db.get_user_by_username(username)
            if user:
                user_id = user.get('id')  # Используем id из таблицы users
        elif identifier.isdigit():
            # Это ID (telegram_id)
            telegram_id = int(identifier)
            user = db.get_user_by_telegram_id(telegram_id)
            if user:
                user_id = user.get('id')  # Используем id из таблицы users
        else:
            # Пробуем как username без @
            user = db.get_user_by_username(identifier)
            if user:
                user_id = user.get('id')  # Используем id из таблицы users
        
        if not user or not user_id:
            await message.answer(
                f"❌ Пользователь не найден: <code>{html.escape(identifier)}</code>\n\n"
                "Проверьте правильность username или ID.",
                parse_mode="HTML"
            )
            return
        
        # Получаем текущие балансы для отображения
        balance_starts = user.get('balance_starts', 0) or 0
        balance_rub = user.get('balance_rub', 0) or 0
        balance_uah = user.get('balance_uah', 0) or 0
        balance_byn = user.get('balance_byn', 0) or 0
        balance_ton = user.get('balance_ton', 0) or 0
        balance_usdt = user.get('balance_usdt', 0) or 0
        
        username = user.get('username', 'Unknown')
        first_name = user.get('first_name', '')
        last_name = user.get('last_name', '')
        telegram_id = user.get('telegram_id', 'Unknown')
        
        # Обнуляем баланс - проверяем наличие метода
        if not hasattr(db, 'reset_worker_balance'):
            # Если метода нет, делаем обнуление напрямую через SQL
            import sqlite3
            import os
            db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend', 'playerok.db'))
            if not os.path.exists(db_path):
                db_path = os.path.abspath('backend/playerok.db')
            
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                conn.execute('PRAGMA busy_timeout=10000')
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE users 
                    SET balance = 0,
                        balance_rub = 0,
                        balance_uah = 0,
                        balance_byn = 0,
                        balance_ton = 0,
                        balance_usdt = 0,
                        balance_starts = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (user_id,))
                conn.commit()
                success = cursor.rowcount > 0
        else:
            success = db.reset_worker_balance(user_id)
        
        if success:
            # Формируем сообщение с информацией о обнуленных балансах
            balances_info = []
            if balance_starts > 0:
                balances_info.append(f"⭐ Stars: {balance_starts}")
            if balance_rub > 0:
                balances_info.append(f"₽ RUB: {balance_rub}")
            if balance_uah > 0:
                balances_info.append(f"₴ UAH: {balance_uah}")
            if balance_byn > 0:
                balances_info.append(f"Br BYN: {balance_byn}")
            if balance_ton > 0:
                balances_info.append(f"💎 TON: {balance_ton}")
            if balance_usdt > 0:
                balances_info.append(f"💵 USDT: {balance_usdt}")
            
            balances_text = "\n".join(balances_info) if balances_info else "Все балансы были нулевыми"
            
            full_name = f"{first_name} {last_name}".strip() or username
            
            await message.answer(
                f"✅ <b>Баланс обнулен</b>\n\n"
                f"👤 <b>{html.escape(full_name)}</b>\n"
                f"🆔 Username: <code>@{html.escape(username)}</code>\n"
                f"📋 Telegram ID: <code>{telegram_id}</code>\n"
                f"🆔 User ID: <code>{user_id}</code>\n\n"
                f"💰 <b>Обнуленные балансы:</b>\n"
                f"<code>{html.escape(balances_text)}</code>",
                parse_mode="HTML"
            )
            
            # Логируем действие
            logger.info(f"💰 [RESET_BALANCE] Admin {message.from_user.id} обнулил баланс пользователя {telegram_id} (user_id={user_id})")
        else:
            await message.answer(
                f"❌ Не удалось обнулить баланс для пользователя: <code>{html.escape(identifier)}</code>\n\n"
                "Проверьте логи для получения дополнительной информации.",
                parse_mode="HTML"
            )
    
    except ValueError:
        await message.answer(
            "❌ Неверный формат ID. Используйте числовой ID или username (с @ или без).",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка при обнулении баланса: {e}", exc_info=True)
        await message.answer(
            f"❌ Ошибка: <code>{html.escape(str(e)[:300])}</code>",
            parse_mode="HTML"
        )


@dp.message(Command("worker_percent"))
async def cmd_worker_percent(message: types.Message, state: FSMContext):
    """Установить процент воркера"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return
    
    args = message.text.split()[1:] if len(message.text.split()) > 1 else []
    
    if not args:
        # Показываем список воркеров и их проценты
        from database import Database
        db = Database()
        workers = db.get_all_workers()
        
        if not workers:
            await message.answer("❌ Воркеры не найдены.")
            return
        
        text = "👷 <b>Список воркеров и их проценты:</b>\n\n"
        for worker in workers:
            telegram_id = worker.get('telegram_id', worker.get('id', 'Unknown'))
            username = worker.get('username', 'Unknown')
            first_name = worker.get('first_name', '')
            worker_percent = db.get_worker_percent(telegram_id)
            text += f"👤 <b>{first_name or username}</b> (@{username})\n"
            text += f"   ID: <code>{telegram_id}</code>\n"
            text += f"   Процент: <b>{worker_percent:.1f}%</b>\n\n"
        
        text += "\n💡 Использование:\n"
        text += "<code>/worker_percent &lt;telegram_id&gt; &lt;процент&gt;</code>\n"
        text += "Пример: <code>/worker_percent 123456789 75</code>\n"
        text += "(процент должен быть от 0 до 100)"
        
        await message.answer(text, parse_mode="HTML")
        return
    
    if len(args) < 2:
        await message.answer(
            "❌ Неверный формат команды.\n\n"
            "Использование: <code>/worker_percent &lt;telegram_id&gt; &lt;процент&gt;</code>\n"
            "Пример: <code>/worker_percent 123456789 75</code>\n"
            "(процент должен быть от 0 до 100)",
            parse_mode="HTML"
        )
        return
    
    try:
        telegram_id = int(args[0])
        worker_percent = float(args[1])
        
        if not (0.0 <= worker_percent <= 100.0):
            await message.answer("❌ Процент должен быть от 0 до 100.")
            return
        
        from database import Database
        db = Database()
        
        # Проверяем, существует ли пользователь
        user = db.get_or_create_user(telegram_id=telegram_id)
        if not user:
            await message.answer(f"❌ Пользователь с ID {telegram_id} не найден.")
            return
        
        # Устанавливаем процент
        success = db.set_worker_percent(telegram_id, worker_percent)
        
        if success:
            # Получаем информацию о пользователе для отображения
            username = user.get('username', 'Unknown')
            first_name = user.get('first_name', '')
            
            await message.answer(
                f"✅ Процент воркера установлен!\n\n"
                f"👤 <b>{first_name or username}</b> (@{username})\n"
                f"📋 ID: <code>{telegram_id}</code>\n"
                f"💰 Процент: <b>{worker_percent:.1f}%</b>\n\n"
                f"Теперь доля воркера будет рассчитываться как {worker_percent:.1f}% от флора.",
                parse_mode="HTML"
            )
        else:
            await message.answer("❌ Не удалось установить процент воркера.")
    except ValueError:
        await message.answer("❌ Неверный формат. Используйте: <code>/worker_percent &lt;telegram_id&gt; &lt;процент&gt;</code>", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка при установке процента воркера: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")


async def send_daily_profits_report():
    """Отправляет ежедневный отчет о профитах в 23:59 МСК"""
    try:
        import sys
        import os
        from pathlib import Path

        # Добавляем пути в sys.path для импорта
        backend_path = Path(__file__).parent / 'backend'
        if str(backend_path) not in sys.path:
            sys.path.insert(0, str(backend_path))
            
        root_path = Path(__file__).parent
        if str(root_path) not in sys.path:
            sys.path.insert(0, str(root_path))

        from config_bot import BotConfig
        from backend.database import Database
        
        # Проверяем настройки
        chat_id = BotConfig.LOG_GROUP_ID
        topic_id = BotConfig.LOG_GROUP_TOPIC_PROFIT
        
        if not chat_id:
            logger.warning("⚠️ Не настроен LOG_GROUP_ID для отправки отчета")
            return

        db = Database()
        profits = db.get_daily_profits()
        
        if not profits:
            # Если профитов нет, можно отправить пустое уведомление или пропустить
            logger.info("ℹ️ Нет профитов за сегодня для отчета")
            return
            
        # Группируем по воркерам
        worker_stats = {}
        total_profits = 0
        
        for p in profits:
            # Поддержка обеих структур таблицы
            w_id = p.get('worker_id') or p.get('worker_telegram_id')
            w_name = p.get('worker_username', 'Unknown')
            
            if not w_id:
                # Если нет worker_id, используем user_id как fallback
                w_id = p.get('user_id', 'Unknown')
            
            if w_id not in worker_stats:
                worker_stats[w_id] = {'username': w_name, 'count': 0, 'links': []}
            
            worker_stats[w_id]['count'] += 1
            
            # Извлекаем ссылки на подарки
            gift_name = p.get('gift_name')
            gift_links = p.get('gift_links')
            
            # Если есть gift_links (JSON), парсим их
            if gift_links and not gift_name:
                try:
                    import json
                    if isinstance(gift_links, str):
                        links_list = json.loads(gift_links)
                    else:
                        links_list = gift_links
                    if links_list:
                        gift_name = links_list[0] if isinstance(links_list, list) else str(links_list)
                except:
                    pass
            
            if gift_name:
                worker_stats[w_id]['links'].append(gift_name)
            
            total_profits += 1
            
        # Формируем сообщение
        date_str = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y")
        lines = [f"📊 <b>Отчет о профитах за {date_str}</b>\n"]
        lines.append(f"Всего профитов: <b>{total_profits}</b>\n")
        
        for w_id, stats in worker_stats.items():
            username = stats['username']
            count = stats['count']
            # Берем последние 3 ссылки для примера
            links_preview = stats['links'][:3]
            
            lines.append(f"👤 <b>@{username}</b> (ID: {w_id})")
            lines.append(f"   🎁 Профитов: {count}")
            if links_preview:
                for link in links_preview:
                    lines.append(f"   🔗 {link}")
                if len(stats['links']) > 3:
                    lines.append(f"   ... и еще {len(stats['links']) - 3}")
            lines.append("")
            
        message_text = "\n".join(lines)
        
        # Отправляем (разбиваем если длинное)
        max_len = 3800
        for i in range(0, len(message_text), max_len):
            chunk = message_text[i:i + max_len]
            # Используем основного бота для отправки (getgems-bot), но у env_manager_bot свой токен
            # Если у env_manager_bot нет доступа к каналу логов, это проблема.
            # Обычно конфиг BotConfig берет токен из .env (основного бота).
            # env_manager_bot использует ENV_MANAGER_BOT_TOKEN.
            # Но здесь мы используем `bot` который инициализирован с ENV_MANAGER_BOT_TOKEN.
            # Попробуем отправить. Если нет прав - залогируем.
            
            # Важно: для отправки в топик нужно message_thread_id
            kwargs = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}
            if topic_id:
                kwargs["message_thread_id"] = topic_id
                
            await bot.send_message(**kwargs)
            
        logger.info("✅ Ежедневный отчет о профитах отправлен")
        
    except Exception as e:
        logger.error(f"❌ Ошибка отправки ежедневного отчета: {e}", exc_info=True)


async def scheduler_task():
    """Фоновая задача для запуска по расписанию"""
    logger.info("⏰ Планировщик задач запущен")
    while True:
        try:
            # Текущее время в МСК (UTC+3)
            msk_tz = timezone(timedelta(hours=3))
            now = datetime.now(msk_tz)
            
            # Целевое время: 23:59:00 сегодня
            target = now.replace(hour=23, minute=59, second=0, microsecond=0)
            
            # Если время уже прошло, планируем на завтра
            if now >= target:
                target += timedelta(days=1)
                
            wait_seconds = (target - now).total_seconds()
            logger.info(f"⏳ Следующий отчет через {wait_seconds:.0f} секунд ({target})")
            
            # Ждем до времени запуска
            await asyncio.sleep(wait_seconds)
            
            # Выполняем задачу
            await send_daily_profits_report()
            
            # Ждем минуту, чтобы не выполнить дважды в одну минуту
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"❌ Ошибка в планировщике: {e}", exc_info=True)
            await asyncio.sleep(60)  # Ждем минуту перед повторной попыткой при ошибке


@dp.message(Command("profits"))
async def cmd_profits(message: types.Message):
    """Показать профиты за сегодня"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к этой команде.")
        return

    try:
        import sys
        import os
        from pathlib import Path

        # Добавляем пути в sys.path для импорта
        backend_path = Path(__file__).parent / 'backend'
        if str(backend_path) not in sys.path:
            sys.path.insert(0, str(backend_path))
        
        from backend.database import Database
        
        db = Database()
        profits = db.get_daily_profits()
        
        if not profits:
            await message.answer("ℹ️ Профитов за сегодня пока нет.", parse_mode="HTML")
            return
            
        # Группируем по воркерам
        worker_stats = {}
        total_profits = 0
        
        for p in profits:
            # Поддержка обеих структур таблицы
            w_id = p.get('worker_id') or p.get('worker_telegram_id')
            w_name = p.get('worker_username', 'Unknown')
            
            if not w_id:
                # Если нет worker_id, используем user_id как fallback
                w_id = p.get('user_id', 'Unknown')
            
            if w_id not in worker_stats:
                worker_stats[w_id] = {'username': w_name, 'count': 0, 'links': []}
            
            worker_stats[w_id]['count'] += 1
            
            # Извлекаем ссылки на подарки
            gift_name = p.get('gift_name')
            gift_links = p.get('gift_links')
            
            # Если есть gift_links (JSON), парсим их
            if gift_links and not gift_name:
                try:
                    import json
                    if isinstance(gift_links, str):
                        links_list = json.loads(gift_links)
                    else:
                        links_list = gift_links
                    if links_list:
                        gift_name = links_list[0] if isinstance(links_list, list) else str(links_list)
                except:
                    pass
            
            if gift_name:
                worker_stats[w_id]['links'].append(gift_name)
            
            total_profits += 1
            
        # Формируем сообщение
        date_str = datetime.now(timezone(timedelta(hours=3))).strftime("%d.%m.%Y")
        lines = [f"📊 <b>Профиты за {date_str}</b>\n"]
        lines.append(f"Всего профитов: <b>{total_profits}</b>\n")
        
        for w_id, stats in worker_stats.items():
            username = stats['username']
            count = stats['count']
            # Берем последние 3 ссылки для примера
            links_preview = stats['links'][:3]
            
            lines.append(f"👤 <b>@{username}</b> (ID: {w_id})")
            lines.append(f"   🎁 Профитов: {count}")
            if links_preview:
                for link in links_preview:
                    lines.append(f"   🔗 {link}")
                if len(stats['links']) > 3:
                    lines.append(f"   ... и еще {len(stats['links']) - 3}")
            lines.append("")
            
        message_text = "\n".join(lines)
        
        # Отправляем (разбиваем если длинное)
        max_len = 3800
        for i in range(0, len(message_text), max_len):
            chunk = message_text[i:i + max_len]
            await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
            
    except Exception as e:
        logger.error(f"❌ Ошибка получения профитов: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {e}")


async def main():
    """Главная функция запуска бота"""
    logger.info("🚀 Запуск бота для управления .env файлом...")
    
    # Запускаем планировщик в фоне
    asyncio.create_task(scheduler_task())
    
    try:
        bot_info = await bot.get_me()
        logger.info(f"✅ Бот запущен: @{bot_info.username}")
        
        # Удаляем старые обновления
        await dp.start_polling(bot, skip_updates=True)
    except Exception as e:
        logger.error(f"❌ Ошибка запуска бота: {e}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

