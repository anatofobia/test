"""
Модуль для ручного получения сессии через Telethon
Используется для получения сессии аккаунта авто докида подарков
"""
import asyncio
import json
import os
from typing import Optional

from dotenv import load_dotenv
import sqlite3
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberInvalidError

load_dotenv()

from config import Config
API_ID = Config.TELEGRAM_API_ID
API_HASH = Config.TELEGRAM_API_HASH


async def send_code(phone: str) -> Optional[str]:
    """Send login code to the given phone. Returns phone_code_hash or None on error."""
    sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    phone_digits = phone.replace("+", "")
    session_path = os.path.join(sessions_dir, f"{phone_digits}.session")

    # Create client, if sqlite schema is broken delete file and retry
    try:
        client = TelegramClient(session_path, API_ID, API_HASH)
    except sqlite3.OperationalError:
        try:
            if os.path.exists(session_path):
                os.remove(session_path)
            jpath = session_path + "-journal"
            if os.path.exists(jpath):
                os.remove(jpath)
            client = TelegramClient(session_path, API_ID, API_HASH)
            print("⚠️ Broken session file removed and recreated.")
        except Exception:
            # Fallback: recreate in fresh path
            client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()
    try:
        result = await client.send_code_request(phone)
        # Persist helper file to allow resume
        code_info_path = os.path.join(sessions_dir, f"{phone_digits}.phone_code.json")
        with open(code_info_path, "w") as f:
            json.dump({"phone": phone, "phone_code_hash": result.phone_code_hash}, f)
        print(f"✓ Code sent to {phone}. phone_code_hash saved.")
        return result.phone_code_hash
    except PhoneNumberInvalidError:
        print("❌ Invalid phone number. Use format like +1234567890")
        return None
    except Exception as e:
        print(f"❌ Failed to send code: {e}")
        return None
    finally:
        await client.disconnect()


async def confirm_code(phone: str, code: str, twofa_password: Optional[str] = None) -> bool:
    """Confirm the code and finish sign-in. Supports 2FA password."""
    sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
    phone_digits = phone.replace("+", "")
    session_path = os.path.join(sessions_dir, f"{phone_digits}.session")

    # Create client, handle broken sqlite session
    try:
        client = TelegramClient(session_path, API_ID, API_HASH)
    except sqlite3.OperationalError:
        try:
            if os.path.exists(session_path):
                os.remove(session_path)
            jpath = session_path + "-journal"
            if os.path.exists(jpath):
                os.remove(jpath)
            client = TelegramClient(session_path, API_ID, API_HASH)
            print("⚠️ Broken session file removed and recreated.")
        except Exception:
            client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()
    # Read saved phone_code_hash
    code_info_path = os.path.join(sessions_dir, f"{phone_digits}.phone_code.json")
    phone_code_hash = None
    try:
        with open(code_info_path, "r") as f:
            phone_code_hash = json.load(f).get("phone_code_hash")
    except Exception:
        pass

    try:
        sign_in = await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except PhoneCodeInvalidError:
        print("❌ Invalid code. Please resend and try again.")
        await client.disconnect()
        return False
    except SessionPasswordNeededError:
        if not twofa_password:
            print("❌ 2FA enabled and password not provided.")
            await client.disconnect()
            return False
        try:
            sign_in = await client.sign_in(password=twofa_password)
        except Exception as e:
            print(f"❌ 2FA sign-in failed: {e}")
            await client.disconnect()
            return False
    except Exception as e:
        print(f"❌ Sign-in failed: {e}")
        await client.disconnect()
        return False

    me = await client.get_me()
    print(f"✅ Authorized as id={me.id} username=@{getattr(me, 'username', None)}")
    await client.disconnect()
    return True


async def convert_to_pyrogram_session_string(phone: str) -> Optional[str]:
    """Convert Telethon .session to Pyrogram session_string using existing conversion function."""
    try:
        session_path = os.path.join("sessions", f"{phone.replace('+', '')}.session")
        if not os.path.exists(session_path):
            print("❌ Session file not found.")
            return None
        
        # Проверяем авторизацию через Telethon
        telethon_client = TelegramClient(session_path, API_ID, API_HASH)
        await telethon_client.connect()
        if not await telethon_client.is_user_authorized():
            print("❌ Session not authorized.")
            await telethon_client.disconnect()
            return None
        
        await telethon_client.disconnect()
        
        # Используем функцию конвертации из gift_processor
        try:
            from gift_processor import convert_telethon_to_pyrogram
            string_session = await convert_telethon_to_pyrogram(session_path)
            
            if string_session:
                # Сохраняем в JSON
                sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
                json_path = os.path.join(sessions_dir, f"{phone.replace('+', '')}.json")
                with open(json_path, "w") as f:
                    json.dump({
                        "phone": phone,
                        "session_string": string_session,
                        "twoFA": False
                    }, f, indent=2)
                
                print("✓ Pyrogram session_string created and saved.")
                return string_session
            else:
                print("❌ Conversion returned None.")
                return None
        except ImportError:
            print("⚠️ gift_processor.convert_telethon_to_pyrogram not available, using fallback method...")
            # Fallback: создаем Pyrogram клиент и экспортируем session_string
            # Но для этого нужна авторизация через Pyrogram
            from pyrogram import Client
            
            temp_session_name = f"pyrogram_{phone.replace('+', '')}"
            pyrogram_client = Client(
                name=temp_session_name,
                api_id=API_ID,
                api_hash=API_HASH,
                workdir="sessions"
            )
            
            try:
                await pyrogram_client.start()
                # Проверяем авторизацию
                try:
                    await pyrogram_client.get_me()
                except:
                    print("❌ Pyrogram session not authorized. Cannot convert without re-authentication.")
                    await pyrogram_client.stop()
                    return None
                
                string_session = await pyrogram_client.export_session_string()
                await pyrogram_client.stop()
                
                # Сохраняем в JSON
                sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
                json_path = os.path.join(sessions_dir, f"{phone.replace('+', '')}.json")
                with open(json_path, "w") as f:
                    json.dump({
                        "phone": phone,
                        "session_string": string_session,
                        "twoFA": False
                    }, f, indent=2)
                
                print("✓ Pyrogram session_string created and saved (fallback method).")
                return string_session
            except Exception as e:
                print(f"❌ Fallback conversion failed: {e}")
                return None
    except Exception as e:
        print(f"❌ Conversion failed: {e}")
        import traceback
        traceback.print_exc()
        return None


async def interactive_login(phone: str) -> None:
    """
    Интерактивный вход: отправляет код, ждёт ввода из терминала,
    при необходимости запрашивает пароль 2FA, затем конвертирует
    Telethon-сессию в Pyrogram session_string.
    """
    print(f"📨 Отправляю код входа на {phone}...")
    await send_code(phone)
    try:
        code = input("🔢 Введите SMS-код: ").strip()
    except EOFError:
        print("❌ Код не введён (EOF). Выход.")
        return
    if not code:
        print("❌ Пустой код. Выход.")
        return

    ok = await confirm_code(phone, code)
    if not ok:
        # Возможно включена 2FA — предложим ввести пароль
        try:
            password = input("🔐 Введите пароль 2FA (если включён, иначе оставьте пустым): ").strip()
        except EOFError:
            password = ""
        if password:
            ok = await confirm_code(phone, code, password)

    if ok:
        await convert_to_pyrogram_session_string(phone)
    else:
        print("⚠️ Вход не выполнен. Попробуйте снова.")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage:\n  python3 auth_sender.py send +<phone>\n  python3 auth_sender.py confirm +<phone> <code> [twofa_password]\n  python3 auth_sender.py convert +<phone>\n  python3 auth_sender.py login +<phone>")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "send" and len(sys.argv) >= 3:
        phone = sys.argv[2]
        asyncio.run(send_code(phone))
    elif cmd == "confirm" and len(sys.argv) >= 4:
        phone = sys.argv[2]
        code = sys.argv[3]
        twofa = sys.argv[4] if len(sys.argv) >= 5 else None
        ok = asyncio.run(confirm_code(phone, code, twofa))
        if ok:
            # auto convert after successful auth
            asyncio.run(convert_to_pyrogram_session_string(phone))
    elif cmd == "convert" and len(sys.argv) >= 3:
        phone = sys.argv[2]
        asyncio.run(convert_to_pyrogram_session_string(phone))
    elif cmd == "login" and len(sys.argv) >= 3:
        phone = sys.argv[2]
        # Интерактивный режим: отправка кода + ввод в терминале
        # ВНИМАНИЕ: команда интерактивная, не предназначена для автоматизации.
        asyncio.run(interactive_login(phone))
    else:
        print("Invalid arguments.")

