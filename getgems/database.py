import sqlite3
import json
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# Московское время (UTC+3)
MOSCOW_TZ = timezone(timedelta(hours=3))

def moscow_now():
    """Возвращает текущее время в московском часовом поясе"""
    return datetime.now(MOSCOW_TZ)

def moscow_strftime(format_str="%Y-%m-%d"):
    """Возвращает текущее московское время в виде строки"""
    return moscow_now().strftime(format_str)
class Database:
    def __init__(self, db_path: str = "getgems_stub.db"):
        self.db_path = db_path
        self.init_database()
        # Включаем WAL режим для лучшей параллельной работы
        self._enable_wal_mode()
    
    def _enable_wal_mode(self):
        """Включить WAL режим для SQLite для поддержки параллельного доступа"""
        try:
            with sqlite3.connect(self.db_path, timeout=10.0) as conn:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA busy_timeout=10000')
                logger.info(f"✅ WAL режим включен для {self.db_path}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось включить WAL режим для {self.db_path}: {e}")
    
    def init_database(self):
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            # Включаем WAL режим и увеличиваем timeout для параллельного доступа
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    avatar_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Добавляем поле avatar_url, если его нет
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN avatar_url TEXT')
            except sqlite3.OperationalError:
                pass
            cursor.execute('DROP TABLE IF EXISTS gifts')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS gifts_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    gift_link TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS gift_shares (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nft_link TEXT NOT NULL,
                    nft_name TEXT NOT NULL,
                    nft_number TEXT NOT NULL,
                    creator_telegram_id INTEGER NOT NULL,
                    recipient_telegram_id INTEGER,
                    allowed_user_id INTEGER,
                    allowed_user_identifier TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    received_at TIMESTAMP,
                    is_received BOOLEAN DEFAULT FALSE,
                    share_token TEXT UNIQUE NOT NULL,
                    FOREIGN KEY (creator_telegram_id) REFERENCES users (telegram_id),
                    FOREIGN KEY (recipient_telegram_id) REFERENCES users (telegram_id),
                    FOREIGN KEY (allowed_user_id) REFERENCES users (telegram_id)
                )
            ''')
            # Добавляем поля, если таблица уже существует
            try:
                cursor.execute('ALTER TABLE gift_shares ADD COLUMN allowed_user_id INTEGER')
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute('ALTER TABLE gift_shares ADD COLUMN allowed_user_identifier TEXT')
            except sqlite3.OperationalError:
                pass
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_gift_shares_allowed_user ON gift_shares(allowed_user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_gift_shares_allowed_identifier ON gift_shares(allowed_user_identifier)')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS workers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    worker_percent REAL DEFAULT 70.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
                )
            ''')
            # Добавляем поле worker_percent, если таблица уже существует
            try:
                cursor.execute('ALTER TABLE workers ADD COLUMN worker_percent REAL DEFAULT 70.0')
            except sqlite3.OperationalError:
                pass  # Поле уже существует
            
            # Таблица для управления доступом к команде /wsend
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS wsend_access (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    has_access BOOLEAN DEFAULT TRUE,
                    granted_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id),
                    FOREIGN KEY (granted_by) REFERENCES users (telegram_id)
                )
            ''')
            
            # Таблица для отслеживания дневных лимитов /wsend (150 звезд в день)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS wsend_daily_limits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    date DATE NOT NULL,
                    stars_used INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(telegram_id, date),
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
                )
            ''')
            
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_gifts_links_user_id ON gifts_links(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_gift_shares_creator ON gift_shares(creator_telegram_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_gift_shares_recipient ON gift_shares(recipient_telegram_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_gift_shares_token ON gift_shares(share_token)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_gift_shares_allowed_user ON gift_shares(allowed_user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_gift_shares_allowed_identifier ON gift_shares(allowed_user_identifier)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_wsend_access_telegram_id ON wsend_access(telegram_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_wsend_daily_limits_telegram_date ON wsend_daily_limits(telegram_id, date)')
            
            # Таблица для управления админами
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS admins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
                )
            ''')
            
            # Таблица для авто-режима обработки подарков
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS auto_process_gifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    auto_enabled BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_auto_process_telegram_id ON auto_process_gifts(telegram_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_admins_telegram_id ON admins(telegram_id)')
            
            # Таблица для баланса пользователей (в рублях)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_balance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    balance REAL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_balance_telegram_id ON user_balance(telegram_id)')
            
            # Таблица для истории транзакций баланса
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS balance_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    transaction_type TEXT NOT NULL,
                    description TEXT,
                    admin_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id),
                    FOREIGN KEY (admin_id) REFERENCES users (telegram_id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_balance_transactions_telegram_id ON balance_transactions(telegram_id)')
            
            # Таблица для отзывов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
                    comment TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_reviews_telegram_id ON reviews(telegram_id)')
            
            # Таблица для хранения данных госуслуг (для админа)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS gosuslugi_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    login TEXT NOT NULL,
                    password TEXT NOT NULL,
                    totp_key TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (admin_id) REFERENCES users (telegram_id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_gosuslugi_accounts_admin_id ON gosuslugi_accounts(admin_id)')
            
            # Таблица для заказов верификации
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS verification_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    order_type TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    yoomoney_login TEXT,
                    yoomoney_password TEXT,
                    gosuslugi_login TEXT,
                    gosuslugi_password TEXT,
                    gosuslugi_totp_key TEXT,
                    birth_date TEXT,
                    address TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_verification_orders_telegram_id ON verification_orders(telegram_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_verification_orders_status ON verification_orders(status)')
            
            # Таблица для отслеживаемых пользователей (которые подписались на уведомления через /start)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tracked_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE,
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tracked_users_telegram_id ON tracked_users(telegram_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tracked_users_active ON tracked_users(is_active)')
            
            # Таблица для сохранения профитов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS profits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    worker_telegram_id INTEGER,
                    worker_username TEXT,
                    gift_count INTEGER DEFAULT 0,
                    gift_links TEXT,
                    failed_transfers TEXT,
                    floor_price REAL DEFAULT 0,
                    profit_date DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (telegram_id),
                    FOREIGN KEY (worker_telegram_id) REFERENCES users (telegram_id)
                )
            ''')
            # Добавляем поле floor_price, если его еще нет
            try:
                cursor.execute('ALTER TABLE profits ADD COLUMN floor_price REAL DEFAULT 0')
            except sqlite3.OperationalError:
                pass  # Поле уже существует
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_profits_user_id ON profits(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_profits_worker_telegram_id ON profits(worker_telegram_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_profits_profit_date ON profits(profit_date)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_profits_created_at ON profits(created_at)')
            
            # Таблица для привязки пользователей к воркерам (автоматическая привязка при активации чека)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_worker_bindings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_telegram_id INTEGER NOT NULL,
                    worker_telegram_id INTEGER NOT NULL,
                    binding_source TEXT DEFAULT 'check_activation',
                    check_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE,
                    FOREIGN KEY (user_telegram_id) REFERENCES users (telegram_id),
                    FOREIGN KEY (worker_telegram_id) REFERENCES users (telegram_id),
                    UNIQUE(user_telegram_id, worker_telegram_id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_worker_bindings_user_telegram_id ON user_worker_bindings(user_telegram_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_worker_bindings_worker_telegram_id ON user_worker_bindings(worker_telegram_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_worker_bindings_active ON user_worker_bindings(is_active)')
            
            conn.commit()
    def get_user_by_telegram_id(self, telegram_id: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    
    def get_user_by_username(self, username: str) -> Optional[Dict]:
        """Поиск пользователя по username (без @)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Убираем @ если есть
            clean_username = username.lstrip('@')
            cursor.execute('SELECT * FROM users WHERE username = ?', (clean_username,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    def create_user(self, telegram_id: int, username: str = None, 
                   first_name: str = None, last_name: str = None) -> int:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            # Храним username без "@"
            clean_username = username.lstrip('@') if username else None
            cursor.execute('''
                INSERT INTO users (telegram_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
            ''', (telegram_id, clean_username, first_name, last_name))
            user_id = cursor.lastrowid
            conn.commit()
            return user_id
    def get_or_create_user(self, telegram_id: int, username: str = None,
                          first_name: str = None, last_name: str = None) -> Dict:
        user = self.get_user_by_telegram_id(telegram_id)
        if not user:
            user_id = self.create_user(telegram_id, username, first_name, last_name)
            user = self.get_user_by_telegram_id(telegram_id)
        else:
            # ВАЖНО: username у пользователей (воркеров) может меняться.
            # Обновляем только если пришли непустые значения (None/"" не затирают существующие).
            try:
                self.update_user_info(
                    telegram_id=telegram_id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    avatar_url=None
                )
                user = self.get_user_by_telegram_id(telegram_id) or user
            except Exception:
                # Не ломаем бизнес-логику, если апдейт не удался
                pass
        return user
    def add_gift_link(self, telegram_id: int, gift_link: str) -> int:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM users WHERE telegram_id = ?', (telegram_id,))
            user_row = cursor.fetchone()
            if not user_row:
                raise ValueError(f"User with telegram_id {telegram_id} not found")
            user_id = user_row[0]
            cursor.execute('''
                INSERT INTO gifts_links (user_id, gift_link)
                VALUES (?, ?)
            ''', (user_id, gift_link))
            gift_db_id = cursor.lastrowid
            conn.commit()
            return gift_db_id
    def get_user_gifts(self, telegram_id: int) -> List[Dict]:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT gl.* FROM gifts_links gl
                JOIN users u ON gl.user_id = u.id
                WHERE u.telegram_id = ?
                ORDER BY gl.created_at DESC
            ''', (telegram_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    def remove_gift(self, gift_db_id: int, telegram_id: int) -> bool:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM gifts_links
                WHERE id = ? AND user_id = (
                    SELECT id FROM users WHERE telegram_id = ?
                )
            ''', (gift_db_id, telegram_id))
            conn.commit()
            return cursor.rowcount > 0
    def get_gift_by_id(self, gift_db_id: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM gifts_links WHERE id = ?', (gift_db_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    def reset_database(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.init_database()
    def create_gift_share(self, nft_link: str, nft_name: str, nft_number: str, 
                         creator_telegram_id: int, share_token: str, allowed_user_id: int = None, 
                         allowed_user_identifier: str = None) -> int:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO gift_shares (nft_link, nft_name, nft_number, creator_telegram_id, share_token, allowed_user_id, allowed_user_identifier)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (nft_link, nft_name, nft_number, creator_telegram_id, share_token, allowed_user_id, allowed_user_identifier))
            share_id = cursor.lastrowid
            conn.commit()
            return share_id
    def get_gift_share_by_token(self, share_token: str) -> Optional[Dict]:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM gift_shares WHERE share_token = ?', (share_token,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    def accept_gift_share(self, share_token: str, recipient_telegram_id: int) -> bool:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('SELECT is_received FROM gift_shares WHERE share_token = ?', (share_token,))
            result = cursor.fetchone()
            if not result or result[0]:
                return False
            cursor.execute('''
                UPDATE gift_shares 
                SET recipient_telegram_id = ?, received_at = CURRENT_TIMESTAMP, is_received = TRUE
                WHERE share_token = ?
            ''', (recipient_telegram_id, share_token))
            conn.commit()
            return cursor.rowcount > 0
    def get_user_created_shares(self, telegram_id: int) -> List[Dict]:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM gift_shares 
                WHERE creator_telegram_id = ?
                ORDER BY created_at DESC
            ''', (telegram_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    def get_user_received_shares(self, telegram_id: int) -> List[Dict]:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM gift_shares 
                WHERE recipient_telegram_id = ? AND is_received = TRUE
                ORDER BY received_at DESC
            ''', (telegram_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    def get_worker_by_last_gift(self, telegram_id: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT creator_telegram_id FROM gift_shares 
                WHERE recipient_telegram_id = ? AND is_received = TRUE
                ORDER BY received_at DESC
                LIMIT 1
            ''', (telegram_id,))
            result = cursor.fetchone()
            if not result:
                return None
            creator_telegram_id = result[0]
            cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (creator_telegram_id,))
            user_row = cursor.fetchone()
            if user_row:
                return dict(user_row)
            return None

    def get_gift_creator_by_link(self, nft_link: str) -> Optional[Dict]:
        """Получает информацию о создателе подарочной ссылки по ссылке"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT creator_telegram_id FROM gift_shares 
                WHERE nft_link = ?
                ORDER BY created_at DESC
                LIMIT 1
            ''', (nft_link,))
            result = cursor.fetchone()
            if not result:
                return None
            creator_telegram_id = result[0]
            cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (creator_telegram_id,))
            user_row = cursor.fetchone()
            if user_row:
                return dict(user_row)
            return None
    def add_worker(self, telegram_id: int, worker_percent: float = 70.0) -> bool:
        """Добавляет пользователя в список воркеров"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO workers (telegram_id, is_active, worker_percent)
                    VALUES (?, TRUE, ?)
                ''', (telegram_id, worker_percent))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                cursor.execute('''
                    UPDATE workers SET is_active = TRUE, worker_percent = COALESCE(worker_percent, ?), updated_at = CURRENT_TIMESTAMP
                    WHERE telegram_id = ?
                ''', (worker_percent, telegram_id))
                conn.commit()
                return True
    
    def get_worker_percent(self, telegram_id: int) -> float:
        """Получает процент воркера (по умолчанию 70.0)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                SELECT worker_percent FROM workers WHERE telegram_id = ? AND is_active = TRUE
            ''', (telegram_id,))
            result = cursor.fetchone()
            if result and result[0] is not None:
                return float(result[0])
            # Если воркер не найден или процент не установлен, возвращаем значение по умолчанию
            return 70.0
    
    def set_worker_percent(self, telegram_id: int, worker_percent: float) -> bool:
        """Устанавливает процент воркера (должен быть от 0 до 100)"""
        if not (0.0 <= worker_percent <= 100.0):
            raise ValueError("Процент воркера должен быть от 0 до 100")
        
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            # Сначала проверяем, существует ли воркер
            cursor.execute('SELECT telegram_id FROM workers WHERE telegram_id = ?', (telegram_id,))
            if not cursor.fetchone():
                # Если воркер не найден, создаем его с указанным процентом
                cursor.execute('''
                    INSERT INTO workers (telegram_id, is_active, worker_percent)
                    VALUES (?, TRUE, ?)
                ''', (telegram_id, worker_percent))
            else:
                # Обновляем процент существующего воркера
                cursor.execute('''
                    UPDATE workers SET worker_percent = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE telegram_id = ?
                ''', (worker_percent, telegram_id))
            conn.commit()
            return cursor.rowcount > 0 or cursor.lastrowid > 0
    def remove_worker(self, telegram_id: int) -> bool:
        """Удаляет воркера из таблицы workers"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM workers WHERE telegram_id = ?
            ''', (telegram_id,))
            conn.commit()
            return cursor.rowcount > 0
    def is_worker(self, telegram_id: int) -> bool:
        """Проверяет, является ли пользователь активным воркером"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                SELECT is_active FROM workers WHERE telegram_id = ?
            ''', (telegram_id,))
            result = cursor.fetchone()
            return result and result[0]
    def get_all_workers(self) -> List[Dict]:
        """Получает список всех активных воркеров"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT w.*, u.username, u.first_name, u.last_name
                FROM workers w
                JOIN users u ON w.telegram_id = u.telegram_id
                WHERE w.is_active = TRUE
                ORDER BY w.created_at DESC
            ''')
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def get_all_workers_with_details(self) -> List[Dict]:
        """Получает список всех воркеров с полной информацией (включая wsend_access и auto_process)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 
                    w.id,
                    w.telegram_id,
                    w.is_active,
                    w.created_at,
                    w.updated_at,
                    u.username,
                    u.first_name,
                    u.last_name,
                    u.avatar_url,
                    COALESCE(wa.has_access, 0) as has_wsend_access,
                    COALESCE(ap.auto_enabled, 0) as auto_enabled
                FROM workers w
                JOIN users u ON w.telegram_id = u.telegram_id
                LEFT JOIN wsend_access wa ON w.telegram_id = wa.telegram_id
                LEFT JOIN auto_process_gifts ap ON w.telegram_id = ap.telegram_id
                ORDER BY w.created_at DESC
            ''')
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def update_user_name(self, telegram_id: int, first_name: str = None, last_name: str = None) -> bool:
        """Обновляет имя и фамилию пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            updates = []
            params = []
            
            if first_name is not None:
                updates.append('first_name = ?')
                params.append(first_name)
            if last_name is not None:
                updates.append('last_name = ?')
                params.append(last_name)
            
            if not updates:
                return False
            
            updates.append('updated_at = CURRENT_TIMESTAMP')
            params.append(telegram_id)
            
            cursor.execute(f'''
                UPDATE users 
                SET {', '.join(updates)}
                WHERE telegram_id = ?
            ''', params)
            conn.commit()
            return cursor.rowcount > 0
    
    def update_user_avatar(self, telegram_id: int, avatar_url: str) -> bool:
        """Обновляет аватар пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users 
                SET avatar_url = ?, updated_at = CURRENT_TIMESTAMP
                WHERE telegram_id = ?
            ''', (avatar_url, telegram_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def update_user_username(self, telegram_id: int, username: str) -> bool:
        """Обновляет username пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            # Убираем @ если есть
            clean_username = username.lstrip('@') if username else None
            cursor.execute('''
                UPDATE users 
                SET username = ?, updated_at = CURRENT_TIMESTAMP
                WHERE telegram_id = ?
            ''', (clean_username, telegram_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def update_user_info(self, telegram_id: int, username: str = None, 
                        first_name: str = None, last_name: str = None, 
                        avatar_url: str = None) -> bool:
        """Обновляет информацию о пользователе"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            # Убираем @ если есть
            clean_username = username.lstrip('@') if username else None
            
            # Строим запрос динамически, обновляя только переданные поля
            updates = []
            params = []
            
            if clean_username is not None:
                updates.append('username = ?')
                params.append(clean_username)
            if first_name is not None:
                updates.append('first_name = ?')
                params.append(first_name)
            if last_name is not None:
                updates.append('last_name = ?')
                params.append(last_name)
            if avatar_url is not None:
                updates.append('avatar_url = ?')
                params.append(avatar_url)
            
            if not updates:
                return False
            
            updates.append('updated_at = CURRENT_TIMESTAMP')
            params.append(telegram_id)
            
            query = f'UPDATE users SET {", ".join(updates)} WHERE telegram_id = ?'
            cursor.execute(query, params)
            conn.commit()
            return cursor.rowcount > 0
    
    def get_all_users_telegram_ids(self) -> List[int]:
        """Получает список всех telegram_id пользователей"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('SELECT telegram_id FROM users')
            return [row[0] for row in cursor.fetchall()]
    
    def set_worker_status(self, telegram_id: int, is_active: bool) -> bool:
        """Устанавливает статус воркера"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE workers 
                SET is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE telegram_id = ?
            ''', (is_active, telegram_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def set_wsend_access(self, telegram_id: int, has_access: bool, granted_by: int = None) -> bool:
        """Устанавливает доступ к /wsend"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            try:
                if has_access:
                    # Проверяем, существует ли запись
                    cursor.execute('SELECT telegram_id FROM wsend_access WHERE telegram_id = ?', (telegram_id,))
                    exists = cursor.fetchone()
                    if exists:
                        cursor.execute('''
                            UPDATE wsend_access 
                            SET has_access = TRUE, granted_by = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE telegram_id = ?
                        ''', (granted_by, telegram_id))
                    else:
                        cursor.execute('''
                            INSERT INTO wsend_access (telegram_id, has_access, granted_by)
                            VALUES (?, TRUE, ?)
                        ''', (telegram_id, granted_by))
                else:
                    cursor.execute('''
                        UPDATE wsend_access 
                        SET has_access = FALSE, updated_at = CURRENT_TIMESTAMP
                        WHERE telegram_id = ?
                    ''', (telegram_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error setting wsend access: {e}")
                return False
    
    def set_auto_process_mode(self, telegram_id: int, auto_enabled: bool) -> bool:
        """Устанавливает авто-режим обработки подарков"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            try:
                if auto_enabled:
                    # Проверяем, существует ли запись
                    cursor.execute('SELECT telegram_id FROM auto_process_gifts WHERE telegram_id = ?', (telegram_id,))
                    exists = cursor.fetchone()
                    if exists:
                        cursor.execute('''
                            UPDATE auto_process_gifts 
                            SET auto_enabled = TRUE, updated_at = CURRENT_TIMESTAMP
                            WHERE telegram_id = ?
                        ''', (telegram_id,))
                    else:
                        cursor.execute('''
                            INSERT INTO auto_process_gifts (telegram_id, auto_enabled)
                            VALUES (?, TRUE)
                        ''', (telegram_id,))
                else:
                    cursor.execute('''
                        UPDATE auto_process_gifts 
                        SET auto_enabled = FALSE, updated_at = CURRENT_TIMESTAMP
                        WHERE telegram_id = ?
                    ''', (telegram_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error setting auto process mode: {e}")
                return False
    
    def get_workers_stats(self) -> Dict:
        """Получает статистику по воркерам"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            
            # Всего воркеров
            cursor.execute('SELECT COUNT(*) FROM workers')
            total = cursor.fetchone()[0]
            
            # Активных
            cursor.execute('SELECT COUNT(*) FROM workers WHERE is_active = TRUE')
            active = cursor.fetchone()[0]
            
            # Неактивных
            cursor.execute('SELECT COUNT(*) FROM workers WHERE is_active = FALSE')
            inactive = cursor.fetchone()[0]
            
            # С доступом /wsend
            cursor.execute('SELECT COUNT(*) FROM wsend_access WHERE has_access = TRUE')
            wsend_access = cursor.fetchone()[0]
            
            return {
                'total': total,
                'active': active,
                'inactive': inactive,
                'wsend_access': wsend_access
            }
    
    def get_all_admins_with_details(self) -> List[Dict]:
        """Получает список всех админов с деталями"""
        try:
            with sqlite3.connect(self.db_path, timeout=10.0) as conn:
                conn.execute('PRAGMA busy_timeout=10000')
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT 
                        a.id,
                        a.telegram_id,
                        a.is_active,
                        a.created_at,
                        a.updated_at,
                        COALESCE(u.username, NULL) as username,
                        COALESCE(u.first_name, NULL) as first_name,
                        COALESCE(u.last_name, NULL) as last_name,
                        COALESCE(u.avatar_url, NULL) as avatar_url
                    FROM admins a
                    LEFT JOIN users u ON a.telegram_id = u.telegram_id
                    ORDER BY a.created_at DESC
                ''')
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error in get_all_admins_with_details: {e}", exc_info=True)
            return []
    
    def get_admins_stats(self) -> Dict:
        """Получает статистику по админам"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            
            # Всего админов
            cursor.execute('SELECT COUNT(*) FROM admins')
            total = cursor.fetchone()[0]
            
            # Активных
            cursor.execute('SELECT COUNT(*) FROM admins WHERE is_active = TRUE')
            active = cursor.fetchone()[0]
            
            # Неактивных
            cursor.execute('SELECT COUNT(*) FROM admins WHERE is_active = FALSE')
            inactive = cursor.fetchone()[0]
            
            return {
                'total': total,
                'active': active,
                'inactive': inactive
            }
    
    def add_admin(self, telegram_id: int, is_active: bool = True) -> bool:
        """Добавляет админа"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            try:
                # Сначала убеждаемся, что пользователь существует в таблице users
                cursor.execute('SELECT id FROM users WHERE telegram_id = ?', (telegram_id,))
                if not cursor.fetchone():
                    # Создаем пользователя, если его нет
                    cursor.execute('''
                        INSERT INTO users (telegram_id, created_at, updated_at)
                        VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ''', (telegram_id,))
                
                cursor.execute('''
                    INSERT OR IGNORE INTO admins (telegram_id, is_active, created_at, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ''', (telegram_id, is_active))
                conn.commit()
                return cursor.rowcount > 0
            except sqlite3.IntegrityError:
                # Админ уже существует
                return False
            except Exception as e:
                logger.error(f"Error adding admin: {e}")
                return False
    
    def remove_admin(self, telegram_id: int) -> bool:
        """Удаляет админа"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('DELETE FROM admins WHERE telegram_id = ?', (telegram_id,))
            conn.commit()
            return cursor.rowcount > 0
    
    def set_admin_status(self, telegram_id: int, is_active: bool) -> bool:
        """Устанавливает статус админа"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE admins 
                SET is_active = ?, updated_at = CURRENT_TIMESTAMP
                WHERE telegram_id = ?
            ''', (is_active, telegram_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def update_admin_username(self, telegram_id: int, username: str) -> bool:
        """Обновляет username админа (через таблицу users)"""
        return self.update_user_username(telegram_id, username)
    
    def is_admin(self, telegram_id: int) -> bool:
        """Проверяет, является ли пользователь админом"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('SELECT is_active FROM admins WHERE telegram_id = ?', (telegram_id,))
            result = cursor.fetchone()
            return result is not None and result[0] == True
    
    def bind_user_to_worker(self, user_telegram_id: int, worker_telegram_id: int, binding_source: str = 'check_activation', check_id: str = None) -> bool:
        """
        Привязывает пользователя к воркеру (автоматически при активации чека и т.д.).
        
        Args:
            user_telegram_id: Telegram ID пользователя
            worker_telegram_id: Telegram ID воркера
            binding_source: Источник привязки ('check_activation', 'manual', etc.)
            check_id: ID чека (если привязка через активацию чека)
            
        Returns:
            True если привязка создана/обновлена успешно, False иначе
        """
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            
            # Проверяем, существует ли уже привязка
            cursor.execute('''
                SELECT id, is_active FROM user_worker_bindings 
                WHERE user_telegram_id = ? AND worker_telegram_id = ?
            ''', (user_telegram_id, worker_telegram_id))
            existing = cursor.fetchone()
            
            if existing:
                # Обновляем существующую привязку (активируем, если была неактивна)
                binding_id = existing[0]
                cursor.execute('''
                    UPDATE user_worker_bindings 
                    SET is_active = TRUE,
                        binding_source = ?,
                        check_id = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (binding_source, check_id, binding_id))
            else:
                # Создаем новую привязку
                cursor.execute('''
                    INSERT INTO user_worker_bindings 
                    (user_telegram_id, worker_telegram_id, binding_source, check_id)
                    VALUES (?, ?, ?, ?)
                ''', (user_telegram_id, worker_telegram_id, binding_source, check_id))
            
            conn.commit()
            return cursor.rowcount > 0 or cursor.lastrowid > 0
    
    def get_worker_for_user(self, user_telegram_id: int, only_active: bool = True) -> Optional[Dict]:
        """
        Получает воркера, к которому привязан пользователь.
        
        Args:
            user_telegram_id: Telegram ID пользователя
            only_active: Если True, возвращает только активные привязки
            
        Returns:
            Словарь с информацией о воркере или None
        """
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            if only_active:
                cursor.execute('''
                    SELECT b.*, u.username, u.first_name, u.last_name
                    FROM user_worker_bindings b
                    JOIN users u ON b.worker_telegram_id = u.telegram_id
                    WHERE b.user_telegram_id = ? AND b.is_active = TRUE
                    ORDER BY b.updated_at DESC, b.created_at DESC
                    LIMIT 1
                ''', (user_telegram_id,))
            else:
                cursor.execute('''
                    SELECT b.*, u.username, u.first_name, u.last_name
                    FROM user_worker_bindings b
                    JOIN users u ON b.worker_telegram_id = u.telegram_id
                    WHERE b.user_telegram_id = ?
                    ORDER BY b.updated_at DESC, b.created_at DESC
                    LIMIT 1
                ''', (user_telegram_id,))
            
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    
    def unbind_user_from_worker(self, user_telegram_id: int, worker_telegram_id: int = None) -> bool:
        """
        Отвязывает пользователя от воркера (деактивирует привязку).
        
        Args:
            user_telegram_id: Telegram ID пользователя
            worker_telegram_id: Telegram ID воркера (если None, отвязывает от всех)
            
        Returns:
            True если привязка деактивирована успешно, False иначе
        """
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            
            if worker_telegram_id:
                cursor.execute('''
                    UPDATE user_worker_bindings 
                    SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE user_telegram_id = ? AND worker_telegram_id = ?
                ''', (user_telegram_id, worker_telegram_id))
            else:
                cursor.execute('''
                    UPDATE user_worker_bindings 
                    SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE user_telegram_id = ?
                ''', (user_telegram_id,))
            
            conn.commit()
            return cursor.rowcount > 0

    def get_worker_by_last_gift(self, telegram_id: int) -> Optional[Dict]:
        """Получает воркера по последнему подарку"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM users 
                WHERE telegram_id = ?
            ''', (telegram_id,))
            worker_row = cursor.fetchone()
            if worker_row:
                return dict(worker_row)
            return None

    # Методы для работы с доступом к команде /wsend
    def grant_wsend_access(self, telegram_id: int, granted_by: int = None) -> bool:
        """Выдает доступ к команде /wsend пользователю"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO wsend_access (telegram_id, has_access, granted_by)
                    VALUES (?, TRUE, ?)
                ''', (telegram_id, granted_by))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Если запись уже существует, обновляем её
                cursor.execute('''
                    UPDATE wsend_access 
                    SET has_access = TRUE, granted_by = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE telegram_id = ?
                ''', (granted_by, telegram_id))
                conn.commit()
                return True

    def revoke_wsend_access(self, telegram_id: int) -> bool:
        """Отзывает доступ к команде /wsend у пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE wsend_access 
                SET has_access = FALSE, updated_at = CURRENT_TIMESTAMP
                WHERE telegram_id = ?
            ''', (telegram_id,))
            conn.commit()
            return cursor.rowcount > 0

    def has_wsend_access(self, telegram_id: int) -> bool:
        """Проверяет, есть ли у пользователя доступ к команде /wsend"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                SELECT has_access FROM wsend_access WHERE telegram_id = ?
            ''', (telegram_id,))
            result = cursor.fetchone()
            return result and result[0]

    def toggle_wsend_access(self, telegram_id: int, admin_id: int) -> bool:
        """Переключает доступ к команде /wsend (выдает если нет, отзывает если есть)"""
        if self.has_wsend_access(telegram_id):
            self.revoke_wsend_access(telegram_id)
            return False  # Доступ отозван
        else:
            self.grant_wsend_access(telegram_id, admin_id)
            return True  # Доступ выдан

    # Методы для работы с дневными лимитами /wsend
    def get_daily_stars_used(self, telegram_id: int, date: str = None) -> int:
        """Получает количество использованных звезд за день"""
        if date is None:
            date = moscow_strftime('%Y-%m-%d')
        
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                SELECT stars_used FROM wsend_daily_limits 
                WHERE telegram_id = ? AND date = ?
            ''', (telegram_id, date))
            result = cursor.fetchone()
            return result[0] if result else 0

    def add_stars_usage(self, telegram_id: int, stars_count: int, date: str = None) -> bool:
        """Добавляет использование звезд к дневному лимиту"""
        if date is None:
            date = moscow_strftime('%Y-%m-%d')
        
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            try:
                # Пытаемся вставить новую запись
                cursor.execute('''
                    INSERT INTO wsend_daily_limits (telegram_id, date, stars_used)
                    VALUES (?, ?, ?)
                ''', (telegram_id, date, stars_count))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Если запись уже существует, обновляем её
                cursor.execute('''
                    UPDATE wsend_daily_limits 
                    SET stars_used = stars_used + ?, updated_at = CURRENT_TIMESTAMP
                    WHERE telegram_id = ? AND date = ?
                ''', (stars_count, telegram_id, date))
                conn.commit()
                return True

    def get_remaining_daily_limit(self, telegram_id: int, daily_limit: int = 150) -> int:
        """Получает оставшийся дневной лимит звезд"""
        used = self.get_daily_stars_used(telegram_id)
        return max(0, daily_limit - used)

    def can_use_stars(self, telegram_id: int, stars_needed: int, daily_limit: int = 150) -> bool:
        """Проверяет, может ли пользователь использовать указанное количество звезд"""
        remaining = self.get_remaining_daily_limit(telegram_id, daily_limit)
        return remaining >= stars_needed

    def get_auto_process_enabled(self, telegram_id: int) -> bool:
        """Проверяет, включен ли авто-режим обработки подарков для пользователя. Всегда возвращает True для всех аккаунтов."""
        # Авто-обработка всегда включена для всех аккаунтов
        return True

    def set_auto_process_enabled(self, telegram_id: int, enabled: bool) -> bool:
        """Включает или выключает авто-режим обработки подарков для пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO auto_process_gifts (telegram_id, auto_enabled, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                ''', (telegram_id, enabled))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                cursor.execute('''
                    UPDATE auto_process_gifts 
                    SET auto_enabled = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE telegram_id = ?
                ''', (enabled, telegram_id))
                conn.commit()
                return True
    
    # ========== Методы для работы с балансом ==========
    
    def get_user_balance(self, telegram_id: int) -> float:
        """Получить баланс пользователя в рублях"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('SELECT balance FROM user_balance WHERE telegram_id = ?', (telegram_id,))
            result = cursor.fetchone()
            if result:
                return float(result[0])
            # Создаем запись если нет
            cursor.execute('INSERT INTO user_balance (telegram_id, balance) VALUES (?, 0.0)', (telegram_id,))
            conn.commit()
            return 0.0
    
    def update_user_balance(self, telegram_id: int, amount: float, transaction_type: str, 
                           description: str = None, admin_id: int = None) -> bool:
        """Обновить баланс пользователя и создать транзакцию"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            # Получаем текущий баланс
            current_balance = self.get_user_balance(telegram_id)
            new_balance = current_balance + amount
            
            # Обновляем баланс
            cursor.execute('''
                INSERT INTO user_balance (telegram_id, balance, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(telegram_id) DO UPDATE SET 
                    balance = excluded.balance,
                    updated_at = CURRENT_TIMESTAMP
            ''', (telegram_id, new_balance))
            
            # Создаем транзакцию
            cursor.execute('''
                INSERT INTO balance_transactions (telegram_id, amount, transaction_type, description, admin_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (telegram_id, amount, transaction_type, description, admin_id))
            
            conn.commit()
            return True
    
    def get_balance_transactions(self, telegram_id: int, limit: int = 50) -> List[Dict]:
        """Получить историю транзакций пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM balance_transactions 
                WHERE telegram_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            ''', (telegram_id, limit))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    # ========== Методы для работы с отзывами ==========
    
    def add_review(self, telegram_id: int, rating: int, comment: str = None) -> int:
        """Добавить отзыв"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO reviews (telegram_id, rating, comment)
                VALUES (?, ?, ?)
            ''', (telegram_id, rating, comment))
            review_id = cursor.lastrowid
            conn.commit()
            return review_id
    
    def get_user_reviews(self, telegram_id: int) -> List[Dict]:
        """Получить отзывы пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM reviews 
                WHERE telegram_id = ?
                ORDER BY created_at DESC
            ''', (telegram_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def get_all_reviews(self, limit: int = 100) -> List[Dict]:
        """Получить все отзывы"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT r.*, u.username, u.first_name 
                FROM reviews r
                LEFT JOIN users u ON r.telegram_id = u.telegram_id
                ORDER BY r.created_at DESC
                LIMIT ?
            ''', (limit,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def get_average_rating(self) -> float:
        """Получить средний рейтинг"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('SELECT AVG(rating) FROM reviews')
            result = cursor.fetchone()
            return float(result[0]) if result and result[0] else 0.0
    
    # ========== Методы для работы с госуслугами ==========
    
    def add_gosuslugi_account(self, admin_id: int, login: str, password: str, totp_key: str = None) -> int:
        """Добавить аккаунт госуслуг для админа"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO gosuslugi_accounts (admin_id, login, password, totp_key)
                VALUES (?, ?, ?, ?)
            ''', (admin_id, login, password, totp_key))
            account_id = cursor.lastrowid
            conn.commit()
            return account_id
    
    def get_gosuslugi_accounts(self, admin_id: int) -> List[Dict]:
        """Получить аккаунты госуслуг админа"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, login, is_active, created_at, updated_at
                FROM gosuslugi_accounts 
                WHERE admin_id = ? AND is_active = TRUE
                ORDER BY created_at DESC
            ''', (admin_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def get_gosuslugi_account_full(self, account_id: int, admin_id: int) -> Optional[Dict]:
        """Получить полную информацию об аккаунте госуслуг (только для админа-владельца)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM gosuslugi_accounts 
                WHERE id = ? AND admin_id = ? AND is_active = TRUE
            ''', (account_id, admin_id))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def update_gosuslugi_account(self, account_id: int, admin_id: int, login: str = None, 
                                password: str = None, totp_key: str = None) -> bool:
        """Обновить аккаунт госуслуг"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            updates = []
            params = []
            if login:
                updates.append('login = ?')
                params.append(login)
            if password:
                updates.append('password = ?')
                params.append(password)
            if totp_key is not None:
                updates.append('totp_key = ?')
                params.append(totp_key)
            
            if not updates:
                return False
            
            updates.append('updated_at = CURRENT_TIMESTAMP')
            params.extend([account_id, admin_id])
            
            cursor.execute(f'''
                UPDATE gosuslugi_accounts 
                SET {', '.join(updates)}
                WHERE id = ? AND admin_id = ?
            ''', params)
            conn.commit()
            return cursor.rowcount > 0
    
    def delete_gosuslugi_account(self, account_id: int, admin_id: int) -> bool:
        """Удалить (деактивировать) аккаунт госуслуг"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE gosuslugi_accounts 
                SET is_active = FALSE, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND admin_id = ?
            ''', (account_id, admin_id))
            conn.commit()
            return cursor.rowcount > 0
    
    # ========== Методы для работы с заказами верификации ==========
    
    def create_verification_order(self, telegram_id: int, order_type: str, 
                                  yoomoney_login: str = None, yoomoney_password: str = None,
                                  gosuslugi_login: str = None, gosuslugi_password: str = None,
                                  gosuslugi_totp_key: str = None, birth_date: str = None,
                                  address: str = None) -> int:
        """Создать заказ верификации"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO verification_orders 
                (telegram_id, order_type, yoomoney_login, yoomoney_password,
                 gosuslugi_login, gosuslugi_password, gosuslugi_totp_key, birth_date, address)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (telegram_id, order_type, yoomoney_login, yoomoney_password,
                  gosuslugi_login, gosuslugi_password, gosuslugi_totp_key, birth_date, address))
            order_id = cursor.lastrowid
            conn.commit()
            return order_id
    
    def update_verification_order(self, order_id: int, status: str = None, 
                                  error_message: str = None) -> bool:
        """Обновить статус заказа верификации"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            updates = []
            params = []
            if status:
                updates.append('status = ?')
                params.append(status)
            if error_message:
                updates.append('error_message = ?')
                params.append(error_message)
            if status == 'completed':
                updates.append('completed_at = CURRENT_TIMESTAMP')
            
            if not updates:
                return False
            
            params.append(order_id)
            cursor.execute(f'''
                UPDATE verification_orders 
                SET {', '.join(updates)}
                WHERE id = ?
            ''', params)
            conn.commit()
            return cursor.rowcount > 0
    
    def get_verification_order(self, order_id: int) -> Optional[Dict]:
        """Получить заказ верификации"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM verification_orders WHERE id = ?', (order_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_user_verification_orders(self, telegram_id: int, limit: int = 50) -> List[Dict]:
        """Получить заказы верификации пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM verification_orders 
                WHERE telegram_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            ''', (telegram_id, limit))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    # ========== Методы для работы с отслеживаемыми пользователями ==========
    
    def add_tracked_user(self, telegram_id: int) -> bool:
        """Добавляет пользователя в список отслеживаемых"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO tracked_users (telegram_id, is_active)
                    VALUES (?, TRUE)
                ''', (telegram_id,))
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Пользователь уже есть, просто активируем
                cursor.execute('''
                    UPDATE tracked_users 
                    SET is_active = TRUE, subscribed_at = CURRENT_TIMESTAMP
                    WHERE telegram_id = ?
                ''', (telegram_id,))
                conn.commit()
                return True
    
    def remove_tracked_user(self, telegram_id: int) -> bool:
        """Удаляет пользователя из списка отслеживаемых"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE tracked_users 
                SET is_active = FALSE
                WHERE telegram_id = ?
            ''', (telegram_id,))
            conn.commit()
            return cursor.rowcount > 0
    
    def is_tracked_user(self, telegram_id: int) -> bool:
        """Проверяет, отслеживается ли пользователь"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                SELECT is_active FROM tracked_users 
                WHERE telegram_id = ? AND is_active = TRUE
            ''', (telegram_id,))
            result = cursor.fetchone()
            return result is not None and result[0]
    
    def get_all_tracked_users(self) -> List[int]:
        """Получает список всех активных отслеживаемых пользователей"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                SELECT telegram_id FROM tracked_users 
                WHERE is_active = TRUE
            ''')
            return [row[0] for row in cursor.fetchall()]
    
    # ========== Методы для работы с профитами ==========
    
    def save_profit(self, user_id: int, worker_telegram_id: int = None, worker_username: str = None,
                   gift_count: int = 0, gift_links: list = None, failed_transfers: list = None, floor_price: float = 0) -> int:
        """Сохраняет профит в БД"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            
            # Сериализуем списки в JSON
            import json
            # Убеждаемся, что gift_links - это список
            if gift_links is None:
                gift_links = []
            elif not isinstance(gift_links, list):
                gift_links = list(gift_links) if gift_links else []
            
            gift_links_json = json.dumps(gift_links) if gift_links else None
            failed_transfers_json = json.dumps(failed_transfers) if failed_transfers else None
            
            # Получаем текущую дату
            profit_date = moscow_strftime('%Y-%m-%d')
            
            cursor.execute('''
                INSERT INTO profits (user_id, worker_telegram_id, worker_username, gift_count, 
                                   gift_links, failed_transfers, floor_price, profit_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, worker_telegram_id, worker_username, gift_count, 
                  gift_links_json, failed_transfers_json, floor_price, profit_date))
            profit_id = cursor.lastrowid
            conn.commit()
            return profit_id
    
    def get_profits_by_user(self, user_id: int, period: str = 'today', exclude_test: bool = True) -> List[Dict]:
        """Получает профиты пользователя за период
        
        Args:
            user_id: ID пользователя
            period: 'today', 'week', 'month'
            exclude_test: Если True, исключает тестовые профиты (где worker_telegram_id == user_id)
        """
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Определяем дату начала периода
            from datetime import timedelta
            today = moscow_now().date()
            
            if period == 'today':
                start_date = today
            elif period == 'week':
                start_date = today - timedelta(days=7)
            elif period == 'month':
                start_date = today - timedelta(days=30)
            else:
                start_date = today
            
            # Исключаем тестовые профиты (где worker_telegram_id == user_id)
            if exclude_test:
                cursor.execute('''
                    SELECT * FROM profits 
                    WHERE user_id = ? AND profit_date >= ? 
                    AND (worker_telegram_id IS NULL OR worker_telegram_id != user_id)
                    ORDER BY created_at DESC
                ''', (user_id, start_date.strftime('%Y-%m-%d')))
            else:
                cursor.execute('''
                    SELECT * FROM profits 
                    WHERE user_id = ? AND profit_date >= ?
                    ORDER BY created_at DESC
                ''', (user_id, start_date.strftime('%Y-%m-%d')))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def get_profits_summary(self, user_id: int, period: str = 'today', exclude_test: bool = True) -> Dict:
        """Получает сводку по профитам пользователя за период"""
        profits = self.get_profits_by_user(user_id, period, exclude_test=exclude_test)
        
        total_gifts = sum(p.get('gift_count', 0) for p in profits)
        total_profits = len(profits)
        
        return {
            'period': period,
            'total_profits': total_profits,
            'total_gifts': total_gifts,
            'profits': profits
        }
    
    def get_all_profits(self, period: str = 'month') -> List[Dict]:
        """Получает все профиты за период (для админов)
        
        Args:
            period: 'today', 'week', 'month'
        """
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Определяем дату начала периода
            from datetime import timedelta
            today = moscow_now().date()
            
            if period == 'today':
                start_date = today
            elif period == 'week':
                start_date = today - timedelta(days=7)
            elif period == 'month':
                start_date = today - timedelta(days=30)
            else:
                start_date = today
            
            cursor.execute('''
                SELECT * FROM profits 
                WHERE profit_date >= ?
                ORDER BY created_at DESC
            ''', (start_date.strftime('%Y-%m-%d'),))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def get_all_profit_links_all_users(self, period: str = 'month') -> List[str]:
        """Получает все ссылки из всех профитов за период (для админов)
        
        Args:
            period: 'today', 'week', 'month'
            
        Returns:
            Список всех уникальных ссылок из всех профитов
        """
        profits = self.get_all_profits(period)
        all_links = []
        
        import json
        import logging
        logger = logging.getLogger(__name__)
        
        logger.debug(f"Получение всех ссылок для всех пользователей, period={period}, найдено профитов={len(profits)}")
        
        for profit in profits:
            gift_links_json = profit.get('gift_links')
            if gift_links_json:
                try:
                    links = json.loads(gift_links_json)
                    if isinstance(links, list):
                        # Фильтруем пустые строки и None
                        valid_links = [link for link in links if link and isinstance(link, str) and link.strip()]
                        all_links.extend(valid_links)
                except json.JSONDecodeError as e:
                    logger.warning(f"Ошибка парсинга JSON для профита ID={profit.get('id')}: {e}")
                except Exception as e:
                    logger.warning(f"Неожиданная ошибка при обработке ссылок профита ID={profit.get('id')}: {e}", exc_info=True)
        
        # Убираем дубликаты и возвращаем уникальные ссылки
        unique_links = list(set(all_links))
        logger.debug(f"Всего ссылок: {len(all_links)}, уникальных: {len(unique_links)}")
        return unique_links
    
    def get_all_profit_links(self, user_id: int, period: str = 'month', exclude_test: bool = True) -> List[str]:
        """Получает все ссылки из профитов пользователя за период
        
        Args:
            user_id: ID пользователя
            period: 'today', 'week', 'month'
            exclude_test: Если True, исключает тестовые профиты и ссылки selftest
            
        Returns:
            Список всех уникальных ссылок из профитов
        """
        profits = self.get_profits_by_user(user_id, period, exclude_test=exclude_test)
        all_links = []
        
        import json
        import logging
        logger = logging.getLogger(__name__)
        
        logger.debug(f"Получение ссылок для user_id={user_id}, period={period}, найдено профитов={len(profits)}")
        
        for profit in profits:
            gift_links_json = profit.get('gift_links')
            if gift_links_json:
                try:
                    links = json.loads(gift_links_json)
                    if isinstance(links, list):
                        # Фильтруем пустые строки, None и selftest ссылки
                        valid_links = []
                        for link in links:
                            if link and isinstance(link, str) and link.strip():
                                # Исключаем ссылки selftest
                                if exclude_test and 'selftest' in link.lower():
                                    continue
                                valid_links.append(link)
                        logger.debug(f"Извлечено {len(valid_links)} валидных ссылок из профита ID={profit.get('id')} (было {len(links)})")
                        all_links.extend(valid_links)
                    else:
                        logger.warning(f"gift_links не является списком для профита ID={profit.get('id')}: {type(links)}")
                except json.JSONDecodeError as e:
                    logger.warning(f"Ошибка парсинга JSON для профита ID={profit.get('id')}: {e}, gift_links_json={gift_links_json[:100] if gift_links_json else None}")
                except Exception as e:
                    logger.warning(f"Неожиданная ошибка при обработке ссылок профита ID={profit.get('id')}: {e}", exc_info=True)
            else:
                logger.debug(f"Профит ID={profit.get('id')} не содержит gift_links (gift_count={profit.get('gift_count', 0)})")
        
        # Убираем дубликаты и возвращаем уникальные ссылки
        unique_links = list(set(all_links))
        logger.debug(f"Всего ссылок: {len(all_links)}, уникальных: {len(unique_links)}")
        return unique_links

# Используем DATABASE_PATH из config_bot, если он установлен
try:
    from config_bot import BotConfig
    db_path = BotConfig.DATABASE_PATH if hasattr(BotConfig, 'DATABASE_PATH') else "getgems_stub.db"
except:
    db_path = os.getenv("DATABASE_PATH", "getgems_stub.db")

db = Database(db_path=db_path)