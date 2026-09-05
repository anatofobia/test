"""
Модуль для работы с Portals Marketplace API
Использует домен https://portal-market.com/
"""
import requests
import os
from functools import lru_cache
from datetime import datetime, timedelta
import json
import time
from dotenv import load_dotenv

load_dotenv()

# Базовый URL API Portals (используется portal-market.com без s)
PORTALS_API_BASE = "https://portal-market.com/api"

# Кэш для floor цен (в памяти)
_floor_prices_cache = {}
_cache_timestamps = {}
CACHE_DURATION = 300  # 5 минут кэширования

def get_auth_data():
    """
    Получает authData из переменной окружения PORTALS_AUTH_DATA
    """
    auth_data = os.getenv("PORTALS_AUTH_DATA", "")
    if not auth_data:
        raise ValueError("PORTALS_AUTH_DATA не установлен в .env файле")
    return auth_data

def format_gift_name(gift_name):
    """
    Форматирует имя подарка для API Portals
    Пример: PreciousPeach -> precious peach
    """
    # Разделяем CamelCase на слова
    import re
    words = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)', gift_name)
    return ' '.join(word.lower() for word in words)

def get_gifts_floors(auth_data=None):
    """
    Получает floor цены для всех подарков
    Returns: dict с floor ценами {gift_name: floor_price}
    """
    if auth_data is None:
        auth_data = get_auth_data()
    
    # Проверяем кэш
    cache_key = "all_floors"
    if cache_key in _floor_prices_cache:
        if time.time() - _cache_timestamps.get(cache_key, 0) < CACHE_DURATION:
            return _floor_prices_cache[cache_key]
    
    try:
        # Используем правильный endpoint из Portals API
        # На основе библиотеки portalsmp используется endpoint collections/floors
        url = f"{PORTALS_API_BASE}/collections/floors"
        
        # Формат заголовка Authorization для Portals API
        # Токен уже содержит "tma " в начале, передаем как есть
        headers = {
            "Authorization": auth_data,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "Origin": "https://portals-market.com",
            "Referer": "https://portals-market.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        }
        
        try:
            response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code != 200:
                print(f"Ошибка получения floors: HTTP {response.status_code}: {response.text[:200]}")
                return {}
        except Exception as e:
            print(f"Ошибка получения floors: {e}")
            return {}
        
        # Обрабатываем успешный ответ
        try:
            data = response.json()
            floors = {}
            
            # API возвращает {"floorPrices": {"giftname": "price", ...}}
            if isinstance(data, dict):
                # Проверяем наличие ключа floorPrices
                if 'floorPrices' in data:
                    floor_prices = data['floorPrices']
                    for gift_name, price in floor_prices.items():
                        try:
                            floors[gift_name.lower()] = float(price) if price else 0
                        except (ValueError, TypeError):
                            floors[gift_name.lower()] = 0
                else:
                    # Если формат другой, пробуем обработать напрямую
                    for gift_name, floor_data in data.items():
                        if isinstance(floor_data, dict):
                            floor_price = floor_data.get('floor', floor_data.get('floor_price', 0))
                        else:
                            floor_price = float(floor_data) if floor_data else 0
                        floors[gift_name.lower()] = floor_price
            
            # Сохраняем в кэш
            _floor_prices_cache[cache_key] = floors
            _cache_timestamps[cache_key] = time.time()
            
            return floors
        except Exception as parse_error:
            print(f"Ошибка парсинга ответа floors: {parse_error}")
            return {}
            
    except Exception as e:
        print(f"Ошибка при получении floors: {e}")
        import traceback
        traceback.print_exc()
        return {}

def get_gift_floor_price(gift_name, auth_data=None):
    """
    Получает floor цену для конкретного подарка
    Args:
        gift_name: имя подарка (например, "PreciousPeach")
    Returns:
        float: floor цена или 0 если не найдено
    """
    if auth_data is None:
        auth_data = get_auth_data()
    
    # Форматируем имя для поиска
    formatted_name = format_gift_name(gift_name)
    
    # Проверяем кэш для конкретного подарка
    cache_key = f"floor_{gift_name.lower()}"
    if cache_key in _floor_prices_cache:
        if time.time() - _cache_timestamps.get(cache_key, 0) < CACHE_DURATION:
            return _floor_prices_cache[cache_key]
    
    try:
        # Получаем все floors
        all_floors = get_gifts_floors(auth_data)
        
        # Ищем нужный подарок
        gift_name_lower = gift_name.lower()
        formatted_lower = formatted_name.lower()
        
        # Пробуем разные варианты имени
        for key, price in all_floors.items():
            if (gift_name_lower in key or 
                key in gift_name_lower or 
                formatted_lower in key or 
                key in formatted_lower):
                # Сохраняем в кэш
                _floor_prices_cache[cache_key] = price
                _cache_timestamps[cache_key] = time.time()
                return float(price) if price else 0
        
        return 0
        
    except Exception as e:
        print(f"Ошибка при получении floor цены для {gift_name}: {e}")
        return 0

def get_multiple_gift_prices(gift_names, auth_data=None):
    """
    Получает floor цены для нескольких подарков за один запрос
    Args:
        gift_names: список имен подарков
    Returns:
        dict: {gift_name: floor_price}
    """
    if auth_data is None:
        auth_data = get_auth_data()
    
    # Получаем все floors
    all_floors = get_gifts_floors(auth_data)
    
    result = {}
    for gift_name in gift_names:
        formatted_name = format_gift_name(gift_name)
        gift_name_lower = gift_name.lower()
        formatted_lower = formatted_name.lower()
        
        price = 0
        for key, floor_price in all_floors.items():
            if (gift_name_lower in key or 
                key in gift_name_lower or 
                formatted_lower in key or 
                key in formatted_lower):
                price = float(floor_price) if floor_price else 0
                break
        
        result[gift_name] = price
    
    return result

def clear_cache():
    """Очищает кэш цен"""
    global _floor_prices_cache, _cache_timestamps
    _floor_prices_cache = {}
    _cache_timestamps = {}

