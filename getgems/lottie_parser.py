import requests
import json
import os
from urllib.parse import urlparse
from typing import Optional, Dict, Tuple
import logging
logger = logging.getLogger(__name__)
class LottieParser:
    def __init__(self):
        self.base_url = "https://nft.fragment.com/gift"
    def generate_lottie_url(self, gift_name: str, gift_id: str) -> str:
        """Генерирует URL для Lottie-анимации по шаблону"""
        formatted_name = self.format_gift_name(gift_name)
        return f"{self.base_url}/{formatted_name}-{gift_id}.lottie.json"
    def format_gift_name(self, gift_name: str) -> str:
        if gift_name is None:
            return ""
        formatted = ''.join(word.capitalize() for word in gift_name.split())
        return formatted
    def parse_link(self, gift_link: str) -> Optional[Tuple[str, str]]:
        """Парсит gift-ссылку вида https://t.me/nft/SwagBag-98364 -> (SwagBag, 98364)"""
        try:
            if not gift_link:
                return None
            parsed = urlparse(gift_link)
            path = parsed.path or ''
            segment = path.split('/')[-1]
            if not segment:
                return None
            parts = segment.split('-')
            if len(parts) < 2:
                return None
            gift_name = parts[0]
            gift_id = parts[-1]
            return gift_name, gift_id
        except Exception:
            return None
    def get_animation_from_link(self, gift_link: str) -> Dict:
        """Получает Lottie-анимацию напрямую по ссылке на подарок"""
        parsed = self.parse_link(gift_link)
        if not parsed:
            return self.create_fallback_animation("Unknown", "0")
        gift_name, gift_id = parsed
        return self.get_or_download_animation(gift_name, gift_id)
    def download_lottie_animation(self, gift_name: str, gift_id: str, lottie_url: str) -> Optional[Dict]:
        """Загружает Lottie-анимацию с Fragment"""
        try:
            logger.info(f"Attempting to download Lottie animation from: {lottie_url}")
            response = requests.get(lottie_url, timeout=5)
            response.raise_for_status()
            animation_data = response.json()
            logger.info(f"Successfully downloaded Lottie animation for {gift_name}")
            return {
                'url': lottie_url,
                'data': animation_data,
                'success': True
            }
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to download Lottie animation from {lottie_url}: {e}")
            return self.create_fallback_animation(gift_name, gift_id)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in Lottie animation from {lottie_url}: {e}")
            return self.create_fallback_animation(gift_name, gift_id)
        except Exception as e:
            logger.error(f"Unexpected error downloading Lottie animation: {e}")
            return self.create_fallback_animation(gift_name, gift_id)
    def create_fallback_animation(self, gift_name: str, gift_id: str) -> Dict:
        """Создает заглушку анимации если загрузка не удалась"""
        logger.warning(f"Creating fallback animation for {gift_name}")
        fallback_data = {
            "v": "5.7.4",
            "fr": 30,
            "ip": 0,
            "op": 60,
            "w": 200,
            "h": 200,
            "nm": f"Fallback_{gift_name}",
            "ddd": 0,
            "assets": [],
            "layers": [
                {
                    "ddd": 0,
                    "ind": 1,
                    "ty": 4,
                    "nm": "Gift Icon",
                    "sr": 1,
                    "ks": {
                        "o": {"a": 0, "k": 100},
                        "r": {"a": 1, "k": [
                            {"i": {"x": [0.833], "y": [0.833]}, "o": {"x": [0.167], "y": [0.167]}, "t": 0, "s": [0]},
                            {"t": 60, "s": [360]}
                        ]},
                        "p": {"a": 0, "k": [100, 100, 0]},
                        "a": {"a": 0, "k": [0, 0, 0]},
                        "s": {"a": 1, "k": [
                            {"i": {"x": [0.833, 0.833, 0.833], "y": [0.833, 0.833, 0.833]}, "o": {"x": [0.167, 0.167, 0.167], "y": [0.167, 0.167, 0.167]}, "t": 0, "s": [100, 100, 100]},
                            {"i": {"x": [0.833, 0.833, 0.833], "y": [0.833, 0.833, 0.833]}, "o": {"x": [0.167, 0.167, 0.167], "y": [0.167, 0.167, 0.167]}, "t": 30, "s": [120, 120, 100]},
                            {"t": 60, "s": [100, 100, 100]}
                        ]}
                    },
                    "ao": 0,
                    "shapes": [
                        {
                            "ty": "el",
                            "p": {"a": 0, "k": [0, 0]},
                            "s": {"a": 0, "k": [60, 60]},
                            "nm": "Circle"
                        },
                        {
                            "ty": "fl",
                            "c": {"a": 0, "k": [0.4, 0.6, 1, 1]},
                            "o": {"a": 0, "k": 100},
                            "nm": "Fill"
                        }
                    ],
                    "ip": 0,
                    "op": 60,
                    "st": 0,
                    "bm": 0
                }
            ]
        }
        return {
            'url': None,
            'data': fallback_data,
            'success': False,
            'fallback': True
        }
    def get_or_download_animation(self, gift_name: str, gift_id: str) -> Dict:
        """Получает анимацию из локального кэша или загружает новую"""
        lottie_url = self.generate_lottie_url(gift_name, gift_id)
        result = self.download_lottie_animation(gift_name, gift_id, lottie_url)
        if result and result.get('data'):
            return result['data']
        fallback = self.create_fallback_animation(gift_name, gift_id)
        return fallback['data']
lottie_parser = LottieParser()