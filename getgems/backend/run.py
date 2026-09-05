#!/usr/bin/env python3
"""
Скрипт запуска приложения PlayerOK
"""
import os
import sys

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Config
from app import app

if __name__ == '__main__':
    # Создаем необходимые директории
    Config.ensure_directories()
    
    # Запускаем приложение
    print(f"🚀 Запуск PlayerOK на {Config.FLASK_HOST}:{Config.FLASK_PORT}")
    print(f"📊 База данных: {Config.DATABASE_PATH}")
    print(f"📁 Сессии: {Config.SESSION_DIR}")
    
    # Используем waitress для production, если доступен
    try:
        from waitress import serve
        print("✅ Используется Waitress WSGI server")
        serve(
            app,
            host=Config.FLASK_HOST,
            port=Config.FLASK_PORT,
            threads=4
        )
    except ImportError:
        # Fallback на встроенный сервер Flask (для разработки)
        print("⚠️ Waitress не установлен, используется встроенный сервер Flask")
        app.run(
            debug=Config.FLASK_DEBUG,
            host=Config.FLASK_HOST,
            port=Config.FLASK_PORT,
            threaded=True
        )

