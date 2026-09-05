import json
import os
from datetime import datetime
from config import Config
from telegram_client import TelegramAuth, run_async

SESSION_DIR = Config.SESSION_DIR
SESSION_DATA_FILE = Config.SESSION_DATA_FILE
PHONE_FILE = Config.PHONE_FILE

# Создаем директорию сессий если её нет
if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

def save_session_data(user_id, data):
    """Сохранить данные сессии"""
    try:
        if os.path.exists(SESSION_DATA_FILE):
            with open(SESSION_DATA_FILE, 'r') as f:
                session_data = json.load(f)
        else:
            session_data = {}
        
        session_data[str(user_id)] = {
            **data,
            'last_updated': datetime.now().isoformat()
        }
        
        with open(SESSION_DATA_FILE, 'w') as f:
            json.dump(session_data, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving session data: {e}")
        return False

def load_session_data(user_id):
    """Загрузить данные сессии"""
    try:
        if os.path.exists(SESSION_DATA_FILE):
            with open(SESSION_DATA_FILE, 'r') as f:
                session_data = json.load(f)
                return session_data.get(str(user_id), {})
        return {}
    except Exception as e:
        print(f"Error loading session data: {e}")
        return {}

def clear_session_data(user_id):
    """Очистить данные сессии"""
    try:
        if os.path.exists(SESSION_DATA_FILE):
            with open(SESSION_DATA_FILE, 'r') as f:
                session_data = json.load(f)
            if str(user_id) in session_data:
                del session_data[str(user_id)]
            with open(SESSION_DATA_FILE, 'w') as f:
                json.dump(session_data, f, indent=2)
        return True
    except Exception as e:
        print(f"Error clearing session data: {e}")
        return False

def get_phone_from_json(user_id):
    """Получить номер телефона из JSON"""
    try:
        if os.path.exists(PHONE_FILE):
            with open(PHONE_FILE, 'r') as f:
                phones = json.load(f)
                return phones.get(str(user_id), {}).get('phone_number')
    except Exception as e:
        print(f"Error getting phone from JSON: {e}")
    return None

def create_session_json(phone, twoFA=False, user_id=None, session_string=None):
    """Создать JSON файл сессии"""
    session_data = {
        'app_id': Config.TELEGRAM_API_ID,
        'app_hash': Config.TELEGRAM_API_HASH,
        'twoFA': twoFA,
        'session_file': f"{phone.replace('+', '')}.session",
        'phone': phone,
        'user_id': user_id,
        'last_update': datetime.now().isoformat(),
        'status': 'authorized'
    }
    
    # Добавляем session_string если передан
    if session_string:
        session_data['session_string'] = session_string
    
    if user_id:
        phones = {}
        if os.path.exists(PHONE_FILE):
            with open(PHONE_FILE, 'r') as f:
                phones = json.load(f)
        phones[str(user_id)] = {
            'phone_number': phone,
            'last_updated': datetime.now().isoformat()
        }
        with open(PHONE_FILE, 'w') as f:
            json.dump(phones, f, indent=2)
    
    session_json_path = os.path.join(SESSION_DIR, f"{phone.replace('+', '')}.json")
    with open(session_json_path, 'w') as f:
        json.dump(session_data, f, indent=2)
    
    return session_data

def check_session_exists(phone):
    """Проверить существование сессии"""
    session_file = os.path.join(SESSION_DIR, f"{phone.replace('+', '')}.session")
    json_file = os.path.join(SESSION_DIR, f"{phone.replace('+', '')}.json")
    return os.path.exists(session_file) and os.path.exists(json_file)

def validate_session(phone):
    """Проверить валидность сессии"""
    if not check_session_exists(phone):
        return False
    
    session_file = os.path.join(SESSION_DIR, f"{phone.replace('+', '')}.session")
    try:
        auth = TelegramAuth(session_file)
        is_valid = run_async(auth.check_connection())
        return is_valid
    except Exception as e:
        print(f"Error validating session: {e}")
        # Удаляем невалидные файлы
        try:
            if os.path.exists(session_file):
                os.remove(session_file)
            json_file = os.path.join(SESSION_DIR, f"{phone.replace('+', '')}.json")
            if os.path.exists(json_file):
                os.remove(json_file)
        except Exception:
            pass
        return False

