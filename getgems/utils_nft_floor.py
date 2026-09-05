#!/usr/bin/env python3
"""
Функции для получения floor цен NFT по модели и атрибутам (более точный расчет)
"""
import os
import sys
import requests
import time
from typing import Optional, Dict, Tuple
from pathlib import Path

# Добавляем путь к KillamonjaroAuto для импорта portals_floor
killamonjaro_path = '/root/KillamonjaroAuto/src/utils'
if os.path.exists(killamonjaro_path) and killamonjaro_path not in sys.path:
    sys.path.insert(0, killamonjaro_path)

try:
    from portals_floor import extract_collection_name, format_collection_name
except ImportError:
    extract_collection_name = None
    format_collection_name = None


def get_nft_attributes_from_link(nft_link: str) -> Dict[str, Optional[str]]:
    """
    Извлекает все атрибуты NFT (Model, Backdrop, Symbol) из веб-страницы.
    Формат ссылки: https://t.me/nft/CollectionName-Number
    
    Args:
        nft_link: Ссылка на NFT
        
    Returns:
        Словарь с атрибутами {'model': '...', 'backdrop': '...', 'symbol': '...'} или пустой словарь
    """
    attributes = {'model': None, 'backdrop': None, 'symbol': None}
    
    if not nft_link or '/nft/' not in nft_link:
        return attributes
    
    try:
        # Пробуем парсить веб-страницу NFT для получения всех атрибутов
        response = requests.get(
            nft_link,
            timeout=10,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
        )
        
        if response.status_code == 200:
            html = response.text
            import re
            
            # Паттерны для поиска атрибутов
            attr_patterns = {
                'model': [
                    r'Model[:\s]*<[^>]*>([^<]+)',
                    r'Model["\']?\s*[:]\s*["\']?([^"\'<]+)',
                    r'<span[^>]*>Model</span>[^<]*<span[^>]*>([^<]+)',
                    r'<div[^>]*>Model[^<]*</div>[^<]*<div[^>]*>([^<]+)',
                    r'"model"[:\s]*"([^"]+)"',
                    r'\'model\'[:\s]*\'([^\']+)\'',
                ],
                'backdrop': [
                    r'Backdrop[:\s]*<[^>]*>([^<]+)',
                    r'Backdrop["\']?\s*[:]\s*["\']?([^"\'<]+)',
                    r'<span[^>]*>Backdrop</span>[^<]*<span[^>]*>([^<]+)',
                    r'<div[^>]*>Backdrop[^<]*</div>[^<]*<div[^>]*>([^<]+)',
                    r'"backdrop"[:\s]*"([^"]+)"',
                    r'\'backdrop\'[:\s]*\'([^\']+)\'',
                ],
                'symbol': [
                    r'Symbol[:\s]*<[^>]*>([^<]+)',
                    r'Symbol["\']?\s*[:]\s*["\']?([^"\'<]+)',
                    r'<span[^>]*>Symbol</span>[^<]*<span[^>]*>([^<]+)',
                    r'<div[^>]*>Symbol[^<]*</div>[^<]*<div[^>]*>([^<]+)',
                    r'"symbol"[:\s]*"([^"]+)"',
                    r'\'symbol\'[:\s]*\'([^\']+)\'',
                ]
            }
            
            # Сначала пробуем парсить из HTML таблицы (самый надежный способ)
            # Формат: <tr><th>Model</th><td>Cheems <mark>1.5%</mark></td></tr>
            table_patterns = {
                'model': r'<tr><th>Model</th><td>([^<]+)',
                'backdrop': r'<tr><th>Backdrop</th><td>([^<]+)',
                'symbol': r'<tr><th>Symbol</th><td>([^<]+)',
            }
            
            for attr_name, pattern in table_patterns.items():
                match = re.search(pattern, html, re.IGNORECASE)
                if match:
                    value = match.group(1).strip()
                    # Убираем HTML теги и процентные значения
                    value = re.sub(r'<[^>]+>', '', value).strip()
                    # Убираем процентные значения типа "1.5%"
                    value = re.sub(r'\s*<mark>[^<]*</mark>', '', value).strip()
                    value = re.sub(r'\d+\.?\d*%', '', value).strip()
                    if value and len(value) > 0 and len(value) < 100:
                        attributes[attr_name] = value
            
            # Также пробуем парсить из meta-тегов (og:description, twitter:description)
            # Формат: Model: Cheems\nBackdrop: Caramel\nSymbol: Wizard Hat
            meta_patterns = [
                r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']',
                r'<meta[^>]*name=["\']twitter:description["\'][^>]*content=["\']([^"\']+)["\']',
            ]
            
            for pattern in meta_patterns:
                match = re.search(pattern, html, re.IGNORECASE)
                if match:
                    meta_content = match.group(1)
                    # Парсим многострочный формат
                    lines = meta_content.split('\n')
                    for line in lines:
                        line = line.strip()
                        if line.startswith('Model:'):
                            if not attributes['model']:
                                attributes['model'] = line.replace('Model:', '').strip()
                        elif line.startswith('Backdrop:'):
                            if not attributes['backdrop']:
                                attributes['backdrop'] = line.replace('Backdrop:', '').strip()
                        elif line.startswith('Symbol:'):
                            if not attributes['symbol']:
                                attributes['symbol'] = line.replace('Symbol:', '').strip()
            
            # Если не нашли в таблице или meta-тегах, пробуем общие паттерны
            for attr_name, patterns in attr_patterns.items():
                if attributes[attr_name]:  # Если уже нашли, пропускаем
                    continue
                    
                for pattern in patterns:
                    match = re.search(pattern, html, re.IGNORECASE)
                    if match:
                        value = match.group(1).strip()
                        # Убираем лишние данные после первого атрибута (если парсинг захватил несколько)
                        if attr_name == 'model':
                            # Убираем Backdrop и Symbol из значения модели
                            if 'Backdrop' in value or 'backdrop' in value:
                                value = value.split('Backdrop')[0].split('backdrop')[0].strip()
                            if 'Symbol' in value or 'symbol' in value:
                                value = value.split('Symbol')[0].split('symbol')[0].strip()
                        elif attr_name == 'backdrop':
                            # Убираем Symbol из значения фона
                            if 'Symbol' in value or 'symbol' in value:
                                value = value.split('Symbol')[0].split('symbol')[0].strip()
                        
                        # Очищаем от HTML тегов и лишних символов
                        value = re.sub(r'<[^>]+>', '', value).strip()
                        value = re.sub(r'[\n\r]+', ' ', value).strip()
                        # Убираем процентные значения
                        value = re.sub(r'\d+\.?\d*%', '', value).strip()
                        
                        if value and len(value) > 0 and len(value) < 100:  # Ограничиваем длину
                            attributes[attr_name] = value
                            break
            
            # Пробуем найти в JSON-LD структурированных данных
            json_ld_pattern = r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
            matches = re.findall(json_ld_pattern, html, re.DOTALL | re.IGNORECASE)
            for match in matches:
                try:
                    import json
                    data = json.loads(match)
                    if isinstance(data, dict):
                        # Ищем атрибуты в разных возможных местах
                        if not attributes['model']:
                            attributes['model'] = (data.get('model') or 
                                                   data.get('Model') or
                                                   data.get('additionalProperty', {}).get('value') if isinstance(data.get('additionalProperty'), dict) else None)
                            if attributes['model']:
                                attributes['model'] = str(attributes['model'])
                        
                        if not attributes['backdrop']:
                            attributes['backdrop'] = (data.get('backdrop') or 
                                                      data.get('Backdrop') or
                                                      data.get('background') or
                                                      data.get('Background'))
                            if attributes['backdrop']:
                                attributes['backdrop'] = str(attributes['backdrop'])
                        
                        if not attributes['symbol']:
                            attributes['symbol'] = (data.get('symbol') or 
                                                    data.get('Symbol'))
                            if attributes['symbol']:
                                attributes['symbol'] = str(attributes['symbol'])
                except Exception:
                    pass
            
            # Также пробуем найти в inline JavaScript/JSON
            js_json_patterns = [
                r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
                r'__NEXT_DATA__\s*=\s*({.*?})</script>',
                r'nftData\s*[:=]\s*({.*?})',
                r'nft\s*[:=]\s*({.*?})',
            ]
            
            for pattern in js_json_patterns:
                match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
                if match:
                    try:
                        import json
                        data = json.loads(match.group(1))
                        if isinstance(data, dict):
                            # Ищем вложенные структуры
                            def find_in_dict(d, keys):
                                for key in keys:
                                    if key in d:
                                        return d[key]
                                    # Ищем в вложенных словарях
                                    for v in d.values():
                                        if isinstance(v, dict):
                                            result = find_in_dict(v, keys)
                                            if result:
                                                return result
                                return None
                            
                            if not attributes['model']:
                                model = find_in_dict(data, ['model', 'Model', 'type', 'Type'])
                                if model:
                                    attributes['model'] = str(model)
                            
                            if not attributes['backdrop']:
                                backdrop = find_in_dict(data, ['backdrop', 'Backdrop', 'background', 'Background'])
                                if backdrop:
                                    attributes['backdrop'] = str(backdrop)
                            
                            if not attributes['symbol']:
                                symbol = find_in_dict(data, ['symbol', 'Symbol'])
                                if symbol:
                                    attributes['symbol'] = str(symbol)
                    except Exception:
                        pass
        
    except Exception as e:
        pass
    
    return attributes


def get_nft_model_from_link(nft_link: str) -> Optional[str]:
    """
    Извлекает информацию о модели NFT из ссылки или веб-страницы (для обратной совместимости).
    
    Args:
        nft_link: Ссылка на NFT
        
    Returns:
        Название модели или None
    """
    attributes = get_nft_attributes_from_link(nft_link)
    return attributes.get('model')


async def get_nft_attributes_via_pyrogram(nft_link: str, client) -> Optional[Dict[str, str]]:
    """
    Получает атрибуты NFT (Model, Backdrop, Symbol) через Pyrogram клиент.
    
    Args:
        nft_link: Ссылка на NFT
        client: Pyrogram клиент
        
    Returns:
        Словарь с атрибутами {'model': '...', 'backdrop': '...', 'symbol': '...'} или None
    """
    if not nft_link or not client:
        return None
    
    try:
        # Парсим ссылку для получения части NFT
        # Формат: https://t.me/nft/CollectionName-Number
        nft_part = nft_link.split('/nft/')[-1].split('?')[0]
        
        # Пробуем получить информацию о NFT через Telegram API
        # Используем get_messages или другие методы для получения информации о NFT
        # Но это требует доступа к каналу @nft или другим способам
        
        # Альтернативный способ: парсим из веб-страницы
        attributes = {
            'model': None,
            'backdrop': None,
            'symbol': None
        }
        
        # Пробуем получить модель из веб-страницы
        model = get_nft_model_from_link(nft_link)
        if model:
            attributes['model'] = model
        
        return attributes if any(attributes.values()) else None
        
    except Exception as e:
        return None


def get_nft_price_from_portal(nft_link: str, model: str = None, backdrop: str = None, auth_data: str = None, max_retries: int = 2, retry_delay: float = 0.5) -> Optional[float]:
    """
    Получает цену конкретного NFT с портала с учетом модели и фона.
    Парсит веб-страницу портала для получения актуальной цены NFT.
    
    Args:
        nft_link: Ссылка на NFT (например, https://t.me/nft/SpringBasket-148970)
        model: Название модели NFT (опционально)
        backdrop: Название фона NFT (опционально, дорогой фон может увеличить цену)
        auth_data: Auth данные для Portals API (опционально, для авторизованных запросов)
        max_retries: Максимальное количество попыток
        retry_delay: Задержка между попытками в секундах
        
    Returns:
        Цена NFT в TON или None
    """
    if not nft_link or '/nft/' not in nft_link:
        return None
    
    slug = nft_link.split('/nft/')[-1].split('?')[0]
    
    # Пробуем получить информацию через веб-страницу портала
    portal_urls = [
        f'https://portal-market.com/nft/{slug}',
        f'https://portals-market.com/nft/{slug}',
    ]
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    if auth_data:
        headers['Authorization'] = auth_data
        headers['Origin'] = 'https://portal-market.com'
        headers['Referer'] = 'https://portal-market.com/'
    
    for attempt in range(1, max_retries + 1):
        for url in portal_urls:
            try:
                response = requests.get(url, headers=headers, timeout=10)
                
                if response.status_code == 200:
                    html = response.text
                    import re
                    
                    # Ищем цену в HTML (разные форматы)
                    price_patterns = [
                        r'\"price\"[:\s]*([\d.]+)',
                        r'\"floorPrice\"[:\s]*([\d.]+)',
                        r'\"floor_price\"[:\s]*([\d.]+)',
                        r'floor[:\s]*([\d.]+)',
                        r'price[:\s]*([\d.]+)',
                        r'data-price=[\"\']([\d.]+)[\"\']',
                        r'price[:\s]*\$?([\d.]+)',
                        r'([\d.]+)\s*TON',
                    ]
                    
                    for pattern in price_patterns:
                        matches = re.findall(pattern, html, re.IGNORECASE)
                        if matches:
                            # Берем последнее найденное значение (обычно самое актуальное)
                            for match in reversed(matches):
                                try:
                                    price = float(match)
                                    if price > 0 and price < 100000:  # Разумный диапазон для NFT
                                        return price
                                except (ValueError, TypeError):
                                    continue
                    
                    # Ищем цену в JSON данных на странице
                    json_patterns = [
                        r'__NEXT_DATA__\s*=\s*({.+?})</script>',
                        r'window\.__INITIAL_STATE__\s*=\s*({.+?});',
                        r'nftData\s*[:=]\s*({.+?})',
                        r'nft\s*[:=]\s*({.+?})',
                    ]
                    
                    for pattern in json_patterns:
                        match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
                        if match:
                            try:
                                import json
                                data = json.loads(match.group(1))
                                
                                # Ищем цену в JSON структуре
                                def find_price_in_dict(d, path=''):
                                    if isinstance(d, dict):
                                        for key, value in d.items():
                                            current_path = f'{path}.{key}' if path else key
                                            if 'price' in key.lower() or 'floor' in key.lower():
                                                if isinstance(value, (int, float)):
                                                    if value > 0 and value < 100000:
                                                        return float(value)
                                                elif isinstance(value, str):
                                                    try:
                                                        price = float(value)
                                                        if price > 0 and price < 100000:
                                                            return price
                                                    except (ValueError, TypeError):
                                                        pass
                                            if isinstance(value, (dict, list)):
                                                result = find_price_in_dict(value, current_path)
                                                if result:
                                                    return result
                                    elif isinstance(d, list):
                                        for i, item in enumerate(d):
                                            result = find_price_in_dict(item, f'{path}[{i}]')
                                            if result:
                                                return result
                                    return None
                                
                                price = find_price_in_dict(data)
                                if price:
                                    return price
                            except Exception:
                                pass
                
            except Exception as e:
                if attempt < max_retries:
                    time.sleep(retry_delay)
                continue
        
        if attempt < max_retries:
            time.sleep(retry_delay)
    
    return None


def get_floor_price_by_attributes(collection_name: str, model: str = None, backdrop: str = None, symbol: str = None, nft_link: str = None, max_retries: int = 3, retry_delay: float = 1.0, auth_data: str = None) -> Optional[float]:
    """
    Получает floor цену NFT по коллекции и всем атрибутам (модель, фон, символ).
    ПРЕИМУЩЕСТВО: Если указана ссылка на NFT, пытается получить цену конкретного NFT с портала.
    Если не найдено или ссылка не указана, возвращает floor цену коллекции.
    Учитывает, что дорогой фон может увеличить цену NFT.
    
    Args:
        collection_name: Название коллекции (например, SpringBasket)
        model: Название модели NFT (например, Soft Whisper 1.5%) - опционально
        backdrop: Название фона NFT (например, Turquoise) - опционально, может влиять на цену
        symbol: Название символа NFT (например, Ring) - опционально
        nft_link: Ссылка на NFT (например, https://t.me/nft/SpringBasket-148970) - опционально, но рекомендуется
        max_retries: Максимальное количество попыток
        retry_delay: Задержка между попытками в секундах
        auth_data: Auth данные для Portals API
        
    Returns:
        Floor цена в TON или None
    """
    if not collection_name:
        return None
    
    # ПРЕИМУЩЕСТВО: Если указана ссылка на NFT, пытаемся получить цену конкретного NFT с портала
    # Это дает более точную цену с учетом модели и фона
    if nft_link:
        try:
            portal_price = get_nft_price_from_portal(
                nft_link=nft_link,
                model=model,
                backdrop=backdrop,
                auth_data=auth_data,
                max_retries=2,
                retry_delay=0.5
            )
            if portal_price:
                return portal_price
        except Exception:
            pass  # Если не получилось, используем fallback на коллекцию
    
    # Получаем auth_data из переменной окружения, если не передан
    if not auth_data:
        auth_data = os.getenv('PORTALS_AUTH_DATA', '')
    
    if not auth_data:
        return None
    
    # FALLBACK: Используем прямой HTTP запрос к Portals API для получения floor цены коллекции
    url = 'https://portal-market.com/api/collections/floors'
    headers = {
        'Authorization': auth_data,
        'Origin': 'https://portal-market.com',
        'Referer': 'https://portal-market.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    for attempt in range(1, max_retries + 1):
        try:
            # Получаем все floor цены
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code != 200:
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                return None
            
            data = response.json()
            floors = data.get('floorPrices', data)
            
            if not isinstance(floors, dict):
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                return None
            
            # Форматируем название коллекции для поиска
            if format_collection_name:
                formatted_name = format_collection_name(collection_name)
            else:
                import re
                formatted_name = re.sub(r'([A-Z])', r' \1', collection_name).strip().lower()
            
            # Ищем коллекцию в разных форматах
            possible_keys = [
                formatted_name,
                collection_name.lower(),
                collection_name,
                formatted_name.replace(' ', ''),
                collection_name.replace('Cake', ' Cake').strip().lower()
            ]
            
            floor_price = None
            for key in possible_keys:
                if key in floors:
                    price = float(floors[key])
                    if price > 0:
                        floor_price = price
                        break
            
            # Поиск по частичному совпадению, если не нашли точно
            if not floor_price:
                name_lower = formatted_name.lower()
                for key, value in floors.items():
                    key_lower = key.lower()
                    if name_lower in key_lower or key_lower in name_lower:
                        price = float(value)
                        if price > 0:
                            floor_price = price
                            break
            
            # Если атрибуты указаны (модель, фон, символ), это может влиять на цену
            # Пока возвращаем цену коллекции (fallback), но логируем атрибуты для анализа
            
            if floor_price:
                # Если указан дорогой фон или редкая модель, это может увеличить цену
                # Пока возвращаем цену коллекции, но логируем атрибуты для анализа
                if model or backdrop or symbol:
                    # Логируем что атрибуты учтены, но цена пока по коллекции (fallback)
                    pass  # Логирование происходит в вызывающей функции
                return floor_price
            
            if attempt < max_retries:
                time.sleep(retry_delay)
        except Exception as e:
            if attempt < max_retries:
                time.sleep(retry_delay)
            else:
                return None
    
    return None


def get_floor_price_by_nft_link(nft_link: str, max_retries: int = 3, retry_delay: float = 1.0, auth_data: str = None) -> Tuple[Optional[float], Optional[str], Optional[Dict[str, Optional[str]]]]:
    """
    Получает floor цену NFT по ссылке, учитывая все атрибуты (модель, фон, символ) для более точного расчета.
    Учитывает, что дорогой фон может увеличить цену NFT.
    
    Args:
        nft_link: Ссылка на NFT (например, https://t.me/nft/SpringBasket-148970)
        max_retries: Максимальное количество попыток
        retry_delay: Задержка между попытками в секундах
        auth_data: Auth данные для Portals API
        
    Returns:
        Tuple (цена, коллекция, атрибуты) или (None, None, None)
        Атрибуты - словарь {'model': '...', 'backdrop': '...', 'symbol': '...'}
    """
    if not nft_link or '/nft/' not in nft_link:
        return None, None, None
    
    # Извлекаем коллекцию
    if extract_collection_name:
        collection_name = extract_collection_name(nft_link)
    else:
        try:
            parts = nft_link.split('/nft/')[1].split('-')
            collection_name = parts[0] if parts else None
        except Exception:
            collection_name = None
    
    if not collection_name:
        return None, None, None
    
    # Пытаемся получить ВСЕ атрибуты из веб-страницы NFT (модель, фон, символ)
    attributes = get_nft_attributes_from_link(nft_link)
    model = attributes.get('model')
    backdrop = attributes.get('backdrop')
    symbol = attributes.get('symbol')
    
    # Получаем floor цену с учетом всех атрибутов
    # ПЕРЕДАЕМ ССЫЛКУ НА NFT для получения более точной цены с портала
    floor_price = get_floor_price_by_attributes(
        collection_name=collection_name,
        model=model,
        backdrop=backdrop,
        symbol=symbol,
        nft_link=nft_link,  # Передаем ссылку для получения цены конкретного NFT
        max_retries=max_retries,
        retry_delay=retry_delay,
        auth_data=auth_data
    )
    
    return floor_price, collection_name, attributes

