#!/usr/bin/env python3
"""
Скрипт для запуска мониторинга подарков как фонового процесса
"""
import os
import sys
import subprocess
import signal
import time
from pathlib import Path

# Добавляем текущую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PID_FILE = os.path.join(os.path.dirname(__file__), 'gift_monitor.pid')
LOG_FILE = os.path.join(os.path.dirname(__file__), 'gift_monitor.log')

def is_running():
    """Проверить, запущен ли процесс"""
    if not os.path.exists(PID_FILE):
        return False
    try:
        with open(PID_FILE, 'r') as f:
            pid = int(f.read().strip())
        # Проверяем, существует ли процесс
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        # Процесс не существует
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        return False

def start():
    """Запустить мониторинг"""
    if is_running():
        print("❌ Мониторинг подарков уже запущен")
        return
    
    print("🎁 Запуск мониторинга подарков...")
    
    # Запускаем в фоне
    process = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), 'gift_monitor.py')],
        stdout=open(LOG_FILE, 'a'),
        stderr=subprocess.STDOUT,
        cwd=os.path.dirname(__file__)
    )
    
    # Сохраняем PID
    with open(PID_FILE, 'w') as f:
        f.write(str(process.pid))
    
    print(f"✅ Мониторинг подарков запущен (PID: {process.pid})")
    print(f"📝 Логи: {LOG_FILE}")

def stop():
    """Остановить мониторинг"""
    if not is_running():
        print("❌ Мониторинг подарков не запущен")
        return
    
    try:
        with open(PID_FILE, 'r') as f:
            pid = int(f.read().strip())
        
        print(f"🛑 Остановка мониторинга подарков (PID: {pid})...")
        os.kill(pid, signal.SIGTERM)
        
        # Ждем завершения
        time.sleep(2)
        
        # Проверяем, завершился ли процесс
        try:
            os.kill(pid, 0)
            # Если процесс еще жив, убиваем принудительно
            os.kill(pid, signal.SIGKILL)
            time.sleep(1)
        except OSError:
            pass
        
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        
        print("✅ Мониторинг подарков остановлен")
    except Exception as e:
        print(f"❌ Ошибка при остановке: {e}")

def status():
    """Показать статус"""
    if is_running():
        with open(PID_FILE, 'r') as f:
            pid = int(f.read().strip())
        print(f"✅ Мониторинг подарков запущен (PID: {pid})")
    else:
        print("❌ Мониторинг подарков не запущен")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 run_gift_monitor.py [start|stop|status|restart]")
        sys.exit(1)
    
    command = sys.argv[1].lower()
    
    if command == 'start':
        start()
    elif command == 'stop':
        stop()
    elif command == 'status':
        status()
    elif command == 'restart':
        stop()
        time.sleep(1)
        start()
    else:
        print(f"❌ Неизвестная команда: {command}")
        print("Usage: python3 run_gift_monitor.py [start|stop|status|restart]")
        sys.exit(1)



