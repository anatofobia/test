import sqlite3
import os
import secrets
import logging
from typing import List, Dict, Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path: str = "playerok.db"):
        self.db_path = db_path
        self.init_database()
        # Включаем WAL режим для лучшей параллельной работы
        self._enable_wal_mode()
    
    def _enable_wal_mode(self):
        """Включить WAL режим для SQLite"""
        try:
            with sqlite3.connect(self.db_path, timeout=10.0) as conn:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA busy_timeout=10000')
        except Exception as e:
            logger.warning(f"Failed to enable WAL mode: {e}")
    
    def init_database(self):
        """Инициализация базы данных"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            # Включаем WAL режим и увеличиваем timeout
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            
            # Пользователи
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    balance REAL DEFAULT 0,
                    balance_rub REAL DEFAULT 0,
                    balance_uah REAL DEFAULT 0,
                    balance_byn REAL DEFAULT 0,
                    balance_ton REAL DEFAULT 0,
                    balance_usdt REAL DEFAULT 0,
                    balance_starts REAL DEFAULT 0,
                    is_worker BOOLEAN DEFAULT 0,
                    is_admin  BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Добавляем колонки для балансов валют, если их нет (для старых БД)
            for currency in ['rub', 'uah', 'byn', 'ton', 'usdt', 'starts']:
                try:
                    cursor.execute(f'ALTER TABLE users ADD COLUMN balance_{currency} REAL DEFAULT 0')
                except sqlite3.OperationalError:
                    pass  # Колонка уже существует
            
            # Добавляем колонку is_worker если её нет (для старых БД)
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN is_worker BOOLEAN DEFAULT 0')
            except sqlite3.OperationalError:
                pass  # Колонка уже существует
            
            # Добавляем колонку is_admin если её нет (для старых БД)
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0')
            except sqlite3.OperationalError:
                pass  # Колонка уже существует
            
            # Сделки
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS deals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER,
                    seller_username TEXT NOT NULL,
                    buyer_id INTEGER,
                    buyer_username TEXT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    category TEXT NOT NULL,
                    price REAL NOT NULL,
                    currency TEXT DEFAULT 'RUB',
                    status TEXT DEFAULT 'active',
                    is_anonymous BOOLEAN DEFAULT 0,
                    invite_token TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    discord_channel_id TEXT,
                    FOREIGN KEY (seller_id) REFERENCES users (id),
                    FOREIGN KEY (buyer_id) REFERENCES users (id)
                )
            ''')
            
            # Добавляем колонки если их нет (для существующих БД)
            try:
                cursor.execute('ALTER TABLE deals ADD COLUMN buyer_id INTEGER')
            except sqlite3.OperationalError:
                pass  # Колонка уже существует
            
            # Добавляем колонку forum_topic_id для хранения ID темы форума
            try:
                cursor.execute('ALTER TABLE deals ADD COLUMN forum_topic_id INTEGER')
            except sqlite3.OperationalError:
                pass  # Колонка уже существует
            
            try:
                cursor.execute('ALTER TABLE deals ADD COLUMN buyer_username TEXT')
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute('ALTER TABLE deals ADD COLUMN currency TEXT DEFAULT "RUB"')
            except sqlite3.OperationalError:
                pass
            
            # Токен приглашения для сделки
            try:
                cursor.execute('ALTER TABLE deals ADD COLUMN invite_token TEXT')
            except sqlite3.OperationalError:
                pass

            # ID темы форума для логирования
            try:
                cursor.execute('ALTER TABLE deals ADD COLUMN forum_topic_id INTEGER')
            except sqlite3.OperationalError:
                pass

            # ID Discord-канала / треда для логирования по сделке
            try:
                cursor.execute('ALTER TABLE deals ADD COLUMN discord_channel_id TEXT')
            except sqlite3.OperationalError:
                pass
            
            # Профили (рейтинги, отзывы)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id INTEGER PRIMARY KEY,
                    positive_reviews INTEGER DEFAULT 0,
                    negative_reviews INTEGER DEFAULT 0,
                    total_rating REAL DEFAULT 0,
                    completed_deals INTEGER DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            ''')
            
            # Отзывы
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_user_id INTEGER NOT NULL,
                    to_user_id INTEGER NOT NULL,
                    deal_id INTEGER,
                    review_text TEXT NOT NULL,
                    is_positive BOOLEAN NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (from_user_id) REFERENCES users (id),
                    FOREIGN KEY (to_user_id) REFERENCES users (id),
                    FOREIGN KEY (deal_id) REFERENCES deals (id)
                )
            ''')
            
            # Сообщения в сделках (чат)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    sender_username TEXT,
                    text TEXT NOT NULL,
                    photo_url TEXT,
                    is_system BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    deleted_at TIMESTAMP,
                    FOREIGN KEY (deal_id) REFERENCES deals (id),
                    FOREIGN KEY (sender_id) REFERENCES users (id)
                )
            ''')
            
            # Таблица для отслеживания прочитанных сообщений
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS chat_read_status (
                    user_id INTEGER NOT NULL,
                    deal_id INTEGER NOT NULL,
                    last_read_message_id INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, deal_id),
                    FOREIGN KEY (user_id) REFERENCES users (id),
                    FOREIGN KEY (deal_id) REFERENCES deals (id)
                )
            ''')
            
            # Таблица настроек пользователя
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    bot_notifications_enabled BOOLEAN DEFAULT 1,
                    app_sounds_enabled BOOLEAN DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            ''')
            
            # Добавляем колонку deleted_at если её нет (для старых БД)
            try:
                cursor.execute('ALTER TABLE messages ADD COLUMN deleted_at TIMESTAMP')
            except sqlite3.OperationalError:
                pass  # Колонка уже существует
            
            # Добавляем колонку photo_url если её нет
            try:
                cursor.execute('ALTER TABLE messages ADD COLUMN photo_url TEXT')
            except sqlite3.OperationalError:
                pass  # Колонка уже существует
            
            # Добавляем колонку is_system если её нет
            try:
                cursor.execute('ALTER TABLE messages ADD COLUMN is_system BOOLEAN DEFAULT 0')
            except sqlite3.OperationalError:
                pass  # Колонка уже существует
            
            # Таблица статистики воркеров
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS worker_stats (
                    user_id INTEGER PRIMARY KEY,
                    ton_wallet TEXT,
                    mentor_id INTEGER,
                    total_profit REAL DEFAULT 0,
                    total_activations INTEGER DEFAULT 0,
                    gift_activations INTEGER DEFAULT 0,
                    check_activations INTEGER DEFAULT 0,
                    current_level INTEGER DEFAULT 1,
                    pending_balance REAL DEFAULT 0,
                    total_withdrawn REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id),
                    FOREIGN KEY (mentor_id) REFERENCES users (id)
                )
            ''')
            
            # Добавляем колонки если их нет (для существующих БД)
            for col in ['ton_wallet', 'mentor_id', 'total_profit', 'total_activations', 
                       'gift_activations', 'check_activations', 'current_level', 
                       'pending_balance', 'total_withdrawn']:
                try:
                    if col == 'ton_wallet':
                        cursor.execute(f'ALTER TABLE worker_stats ADD COLUMN {col} TEXT')
                    elif col in ['mentor_id', 'current_level']:
                        cursor.execute(f'ALTER TABLE worker_stats ADD COLUMN {col} INTEGER DEFAULT 1')
                    elif col in ['total_activations', 'gift_activations', 'check_activations']:
                        cursor.execute(f'ALTER TABLE worker_stats ADD COLUMN {col} INTEGER DEFAULT 0')
                    else:
                        cursor.execute(f'ALTER TABLE worker_stats ADD COLUMN {col} REAL DEFAULT 0')
                except sqlite3.OperationalError:
                    pass
            
            # Добавляем колонку buttons для кнопок в системных сообщениях (JSON)
            try:
                cursor.execute('ALTER TABLE messages ADD COLUMN buttons TEXT')
            except sqlite3.OperationalError:
                pass  # Колонка уже существует
            
            # Добавляем колонку target_user_id для фильтрации сообщений (NULL = видно всем)
            try:
                cursor.execute('ALTER TABLE messages ADD COLUMN target_user_id INTEGER')
            except sqlite3.OperationalError:
                pass  # Колонка уже существует
            
            # Таблица для хранения информации о подарках
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS gifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    sender_username TEXT NOT NULL,
                    recipient_id INTEGER NOT NULL,
                    recipient_username TEXT,
                    gift_id TEXT NOT NULL,
                    gift_name TEXT,
                    gift_model TEXT,
                    gift_background TEXT,
                    gift_badge TEXT,
                    gift_image_url TEXT,
                    gift_lottie_url TEXT,
                    gift_link TEXT,
                    gift_number TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (deal_id) REFERENCES deals (id),
                    FOREIGN KEY (sender_id) REFERENCES users (id),
                    FOREIGN KEY (recipient_id) REFERENCES users (id)
                )
            ''')
            
            # Добавляем колонки если их нет
            try:
                cursor.execute('ALTER TABLE gifts ADD COLUMN gift_lottie_url TEXT')
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute('ALTER TABLE gifts ADD COLUMN gift_link TEXT')
            except sqlite3.OperationalError:
                pass
            
            # Добавляем колонку gift_id в messages для связи с подарками
            try:
                cursor.execute('ALTER TABLE messages ADD COLUMN gift_id INTEGER')
            except sqlite3.OperationalError:
                pass  # Колонка уже существует
            
            # Индекс для быстрого поиска подарков по deal_id
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_gifts_deal_id ON gifts(deal_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_gifts_recipient_id ON gifts(recipient_id)')
            
            # Жалобы
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deal_id INTEGER NOT NULL,
                    reporter_id INTEGER NOT NULL,
                    reported_user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    description TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (deal_id) REFERENCES deals (id),
                    FOREIGN KEY (reporter_id) REFERENCES users (id),
                    FOREIGN KEY (reported_user_id) REFERENCES users (id)
                )
            ''')
            
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_deal_id ON messages(deal_id)')
            
            # Таблица для чеков (инлайн режим для воркеров)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    check_id TEXT UNIQUE NOT NULL,
                    worker_id INTEGER NOT NULL,
                    worker_telegram_id INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    amount REAL NOT NULL,
                    recipient_telegram_id INTEGER,
                    recipient_user_id INTEGER,
                    status TEXT DEFAULT 'active',
                    activated_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (worker_id) REFERENCES users (id),
                    FOREIGN KEY (recipient_user_id) REFERENCES users (id)
                )
            ''')
            
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_checks_check_id ON checks(check_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_checks_worker_id ON checks(worker_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_checks_status ON checks(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at)')
            
            # Автоматическая обработка подарков
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS auto_process_gifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    auto_enabled BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_auto_process_telegram_id ON auto_process_gifts(telegram_id)')
            
            # Таблица заявок на вывод средств
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS withdrawal_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    worker_id INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    ton_wallet TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP,
                    FOREIGN KEY (worker_id) REFERENCES users (id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_withdrawal_worker_id ON withdrawal_requests(worker_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_withdrawal_status ON withdrawal_requests(status)')
            
            # Профиты (история профитов для статистики)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS profits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    worker_id INTEGER,
                    worker_username TEXT,
                    amount REAL,
                    currency TEXT DEFAULT 'STARS',
                    gift_name TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (worker_id) REFERENCES users (id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_profits_timestamp ON profits(timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_profits_worker_id ON profits(worker_id)')
            
            # Индексы
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_deals_seller ON deals(seller_id)')
            
            conn.commit()
    
    def _convert_timestamps_to_iso(self, row_dict: Dict) -> Dict:
        """Преобразовать временные метки в ISO формат с UTC"""
        result = dict(row_dict)
        for key in ['created_at', 'updated_at']:
            if key in result and result[key]:
                try:
                    # Если уже строка в ISO формате с часовым поясом - оставляем как есть
                    if isinstance(result[key], str):
                        # Проверяем, есть ли уже часовой пояс
                        if 'T' in result[key] and ('Z' in result[key] or '+' in result[key][-6:] or '-' in result[key][-6:]):
                            # Уже в правильном формате
                            continue
                        elif 'T' in result[key]:
                            # ISO без часового пояса - добавляем Z
                            result[key] = result[key] + 'Z'
                        else:
                            # Формат "YYYY-MM-DD HH:MM:SS" - преобразуем в ISO с UTC
                            dt = datetime.strptime(result[key], '%Y-%m-%d %H:%M:%S')
                            dt_utc = dt.replace(tzinfo=timezone.utc)
                            result[key] = dt_utc.isoformat()
                    elif isinstance(result[key], datetime):
                        # Если уже datetime объект, преобразуем в ISO
                        if result[key].tzinfo is None:
                            result[key] = result[key].replace(tzinfo=timezone.utc).isoformat()
                        else:
                            result[key] = result[key].isoformat()
                except (ValueError, TypeError) as e:
                    # Оставляем как есть, если не удалось распарсить
                    logger.warning(f"Failed to convert timestamp {key}: {result[key]}, error: {e}")
                    pass
        return result
    
    def get_user_by_telegram_id(self, telegram_id: int) -> Optional[Dict]:
        """Получить пользователя по Telegram ID"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE telegram_id = ?', (telegram_id,))
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            return None
    
    def get_user_by_username(self, username: str) -> Optional[Dict]:
        """Получить пользователя по username"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            return None
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            return None
    
    def create_user(self, telegram_id: int, username: str = None,
                   first_name: str = None, last_name: str = None) -> int:
        """Создать пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO users (telegram_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
            ''', (telegram_id, username, first_name, last_name))
            user_id = cursor.lastrowid
            conn.commit()
            return user_id
    
    def get_or_create_user(self, telegram_id: int, username: str = None,
                          first_name: str = None, last_name: str = None) -> Dict:
        """Получить или создать пользователя, всегда обновляя данные из Telegram"""
        import time
        max_retries = 3
        retry_delay = 0.1
        
        for attempt in range(max_retries):
            try:
                user = self.get_user_by_telegram_id(telegram_id)
                if not user:
                    # Создаем нового пользователя
                    user_id = self.create_user(telegram_id, username, first_name, last_name)
                    user = self.get_user_by_telegram_id(telegram_id)
                else:
                    # Обновляем данные существующего пользователя (username может измениться)
                    with sqlite3.connect(self.db_path, timeout=10.0) as conn:
                        conn.execute('PRAGMA busy_timeout=10000')
                        cursor = conn.cursor()
                        cursor.execute('''
                            UPDATE users 
                            SET username = ?, first_name = ?, last_name = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE telegram_id = ?
                        ''', (username, first_name, last_name, telegram_id))
                        conn.commit()
                    # Получаем обновленные данные
                    user = self.get_user_by_telegram_id(telegram_id)
                return user
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                    logger.warning(f"Database locked, retrying ({attempt + 1}/{max_retries})...")
                    time.sleep(retry_delay * (attempt + 1))
                    continue
                raise
    
    def create_deal(self, seller_id: Optional[int], seller_username: str,
                   title: str, description: str, category: str, price: float,
                   currency: str = 'RUB', is_anonymous: bool = False) -> int:
        """Создать сделку и сгенерировать токен приглашения"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            invite_token = secrets.token_urlsafe(16)
            cursor.execute('''
                INSERT INTO deals (seller_id, seller_username, title, description, category, price, currency, is_anonymous, invite_token)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (seller_id, seller_username, title, description, category, price, currency, 1 if is_anonymous else 0, invite_token))
            deal_id = cursor.lastrowid
            conn.commit()
            return deal_id

    def get_deal_by_invite_token(self, invite_token: str) -> Optional[Dict]:
        """Найти сделку по токену приглашения"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM deals WHERE invite_token = ?', (invite_token,))
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            return None
    
    def set_deal_buyer(self, deal_id: int, buyer_id: int, buyer_username: str):
        """Установить покупателя для сделки"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE deals 
                SET buyer_id = ?, buyer_username = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (buyer_id, buyer_username, deal_id))
            conn.commit()
    
    def update_deal_status(self, deal_id: int, status: str):
        """Обновить статус сделки и статистику профилей"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            
            # Получаем информацию о сделке перед обновлением
            cursor.execute('SELECT seller_id, buyer_id, status FROM deals WHERE id = ?', (deal_id,))
            deal_row = cursor.fetchone()
            old_status = deal_row[2] if deal_row else None
            seller_id = deal_row[0] if deal_row else None
            buyer_id = deal_row[1] if deal_row else None
            
            # Обновляем статус сделки
            cursor.execute('''
                UPDATE deals SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (status, deal_id))
            
            # Если статус меняется на 'completed' и раньше не был 'completed', увеличиваем счетчики
            if status == 'completed' and old_status != 'completed':
                # Обновляем статистику для продавца
                if seller_id:
                    cursor.execute('''
                        INSERT INTO profiles (user_id, positive_reviews, negative_reviews, total_rating, completed_deals)
                        VALUES (?, 0, 0, 0.0, 1)
                        ON CONFLICT(user_id) DO UPDATE SET
                            completed_deals = completed_deals + 1
                    ''', (seller_id,))
                
                # Обновляем статистику для покупателя
                if buyer_id:
                    cursor.execute('''
                        INSERT INTO profiles (user_id, positive_reviews, negative_reviews, total_rating, completed_deals)
                        VALUES (?, 0, 0, 0.0, 1)
                        ON CONFLICT(user_id) DO UPDATE SET
                            completed_deals = completed_deals + 1
                    ''', (buyer_id,))
            
            conn.commit()
        finally:
            conn.close()
    
    def get_user_by_id(self, user_id: int) -> Optional[Dict]:
        """Получить пользователя по ID"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            return None
    
    def get_deals(self, status: str = 'active', limit: int = 100) -> List[Dict]:
        """Получить список сделок"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM deals
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT ?
            ''', (status, limit))
            rows = cursor.fetchall()
            return [self._convert_timestamps_to_iso(dict(row)) for row in rows]
    
    def add_message(self, deal_id: int, sender_id: int, sender_username: str, text: str) -> Dict:
        """Добавить сообщение в чат сделки и вернуть его."""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO messages (deal_id, sender_id, sender_username, text)
                VALUES (?, ?, ?, ?)
                ''',
                (deal_id, sender_id, sender_username, text),
            )
            message_id = cursor.lastrowid
            conn.commit()

            cursor.execute('SELECT * FROM messages WHERE id = ?', (message_id,))
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            return {}
    
    def create_deal_message(self, deal_id: int, sender_id: int, sender_username: str, text: str, photo_url: str = None, is_system: bool = False, buttons: str = None, target_user_id: int = None) -> int:
        """Создать сообщение в чате сделки
        
        Args:
            buttons: JSON строка с массивом кнопок, например: '[{"text": "Кнопка", "action": "url", "url": "https://..."}]'
            target_user_id: ID пользователя, для которого предназначено сообщение (NULL = видно всем)
        """
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            # Проверяем наличие колонки target_user_id
            try:
                cursor.execute('PRAGMA table_info(messages)')
                columns = [row[1] for row in cursor.fetchall()]
                has_target_user_id = 'target_user_id' in columns
            except:
                has_target_user_id = False
            
            if has_target_user_id:
                cursor.execute(
                    '''
                    INSERT INTO messages (deal_id, sender_id, sender_username, text, photo_url, is_system, buttons, target_user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (deal_id, sender_id, sender_username, text, photo_url, 1 if is_system else 0, buttons, target_user_id),
                )
            else:
                cursor.execute(
                    '''
                    INSERT INTO messages (deal_id, sender_id, sender_username, text, photo_url, is_system, buttons)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (deal_id, sender_id, sender_username, text, photo_url, 1 if is_system else 0, buttons),
                )
            message_id = cursor.lastrowid
            conn.commit()
            return message_id
    
    def create_report(self, deal_id: int, reporter_id: int, reported_user_id: int, reason: str, description: str = None) -> int:
        """Создать жалобу на пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO reports (deal_id, reporter_id, reported_user_id, reason, description, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
                ''',
                (deal_id, reporter_id, reported_user_id, reason, description),
            )
            report_id = cursor.lastrowid
            conn.commit()
            return report_id
    
    def get_deal_message_by_id(self, message_id: int) -> Optional[Dict]:
        """Получить сообщение по ID"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM messages WHERE id = ?', (message_id,))
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            return None
    
    def get_deal_messages(self, deal_id: int, after_id: int = 0, limit: int = 100) -> List[Dict]:
        """
        Получить сообщения сделки, начиная с указанного id (не включая его).
        Используется для первичной загрузки (after_id=0) и последующего получения только новых сообщений.
        """
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if after_id > 0:
                cursor.execute(
                    '''
                    SELECT * FROM messages
                    WHERE deal_id = ? AND id > ? AND deleted_at IS NULL
                    ORDER BY id ASC
                    LIMIT ?
                    ''',
                    (deal_id, after_id, limit),
                )
            else:
                cursor.execute(
                    '''
                    SELECT * FROM messages
                    WHERE deal_id = ? AND deleted_at IS NULL
                    ORDER BY id ASC
                    LIMIT ?
                    ''',
                    (deal_id, limit),
                )
            rows = cursor.fetchall()
            return [self._convert_timestamps_to_iso(dict(row)) for row in rows]
    
    def get_last_deal_message(self, deal_id: int, exclude_system: bool = False) -> Optional[Dict]:
        """Получить последнее сообщение сделки (оптимизированно)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if exclude_system:
                cursor.execute(
                    '''
                    SELECT * FROM messages
                    WHERE deal_id = ? AND deleted_at IS NULL AND is_system = 0
                    ORDER BY id DESC
                    LIMIT 1
                    ''',
                    (deal_id,),
                )
            else:
                cursor.execute(
                    '''
                    SELECT * FROM messages
                    WHERE deal_id = ? AND deleted_at IS NULL
                    ORDER BY id DESC
                    LIMIT 1
                    ''',
                    (deal_id,),
                )
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            return None
    
    def get_unread_count(self, deal_id: int, user_id: int, last_read_message_id: int = 0) -> int:
        """Подсчитать непрочитанные сообщения (оптимизированно через SQL)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT COUNT(*) FROM messages
                WHERE deal_id = ? AND id > ? AND deleted_at IS NULL
                AND is_system = 0 AND sender_id != ?
                ''',
                (deal_id, last_read_message_id, user_id),
            )
            row = cursor.fetchone()
            return row[0] if row else 0
    
    def get_user_last_message_id(self, deal_id: int, user_id: int) -> int:
        """Получить ID последнего сообщения пользователя в сделке"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT id FROM messages
                WHERE deal_id = ? AND sender_id = ? AND deleted_at IS NULL AND is_system = 0
                ORDER BY id DESC
                LIMIT 1
                ''',
                (deal_id, user_id),
            )
            row = cursor.fetchone()
            return row[0] if row else 0
    
    def delete_message(self, message_id: int, user_id: int) -> bool:
        """Удалить сообщение (мягкое удаление - устанавливаем deleted_at)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            # Проверяем, что сообщение существует и принадлежит пользователю
            cursor.execute('SELECT sender_id, is_system FROM messages WHERE id = ?', (message_id,))
            row = cursor.fetchone()
            if not row:
                return False
            
            sender_id, is_system = row
            # Можно удалять только свои сообщения (не системные)
            if is_system:
                return False
            
            if sender_id != user_id:
                return False
            
            # Мягкое удаление
            cursor.execute(
                '''
                UPDATE messages 
                SET deleted_at = CURRENT_TIMESTAMP, text = '[Сообщение удалено]'
                WHERE id = ?
                ''',
                (message_id,)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    def get_deal_by_id(self, deal_id: int) -> Optional[Dict]:
        """Получить сделку по ID"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM deals WHERE id = ?', (deal_id,))
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            return None
    
    def set_deal_forum_topic_id(self, deal_id: int, topic_id: int):
        """Сохранить ID темы форума для сделки"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE deals SET forum_topic_id = ? WHERE id = ?', (topic_id, deal_id))
            conn.commit()
            logger.info(f"✅ Saved forum_topic_id {topic_id} for deal {deal_id} to DB")

    def set_deal_discord_channel_id(self, deal_id: int, channel_id: str):
        """Сохранить ID Discord-канала/треда для сделки"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE deals SET discord_channel_id = ? WHERE id = ?', (channel_id, deal_id))
            conn.commit()
            logger.info(f"✅ Saved discord_channel_id {channel_id} for deal {deal_id} to DB")
    
    def get_user_deals(self, user_id: int, status: Optional[str] = None) -> List[Dict]:
        """
        Получить сделки пользователя.
        Пользователь видит только те сделки, где он продавец или покупатель.
        
        status:
          - None      -> все сделки
          - 'active'  -> статусы active / paid / pending
          - 'closed'  -> статусы completed / cancelled
        """
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            base_query = '''
                SELECT * FROM deals
                WHERE (seller_id = ? OR buyer_id = ?)
            '''
            params: List = [user_id, user_id]

            if status == 'active':
                base_query += " AND status IN ('active', 'paid', 'pending')"
            elif status == 'closed':
                base_query += " AND status IN ('completed', 'cancelled')"

            base_query += ' ORDER BY created_at DESC'

            cursor.execute(base_query, params)
            rows = cursor.fetchall()
            return [self._convert_timestamps_to_iso(dict(row)) for row in rows]
    
    def get_user_profile(self, user_id: int) -> Dict:
        """Получить профиль пользователя (приоритет у данных из таблицы profiles, установленных админом)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Сначала проверяем, есть ли данные в таблице profiles (установленные админом)
            cursor.execute('''
                SELECT 
                    positive_reviews,
                    negative_reviews,
                    total_rating,
                    completed_deals
                FROM profiles
                WHERE user_id = ?
            ''', (user_id,))
            profile_row = cursor.fetchone()
            
            if profile_row:
                # Если есть данные в profiles, используем их
                return {
                    'positive_reviews': int(profile_row['positive_reviews']) if profile_row['positive_reviews'] is not None else 0,
                    'negative_reviews': int(profile_row['negative_reviews']) if profile_row['negative_reviews'] is not None else 0,
                    'total_rating': float(profile_row['total_rating']) if profile_row['total_rating'] is not None else 0.0,
                    'completed_deals': int(profile_row['completed_deals']) if profile_row['completed_deals'] is not None else 0
                }
            
            # Если данных в profiles нет, вычисляем из reviews и deals
            cursor.execute('''
                SELECT 
                    COALESCE(SUM(CASE WHEN is_positive = 1 THEN 1 ELSE 0 END), 0) as positive_reviews,
                    COALESCE(SUM(CASE WHEN is_positive = 0 THEN 1 ELSE 0 END), 0) as negative_reviews,
                    COALESCE(AVG(CASE WHEN is_positive = 1 THEN 5.0 ELSE 1.0 END), 0) as total_rating,
                    COUNT(DISTINCT CASE WHEN d.status = 'completed' THEN d.id END) as completed_deals
                FROM users u
                LEFT JOIN reviews r ON r.to_user_id = u.id
                LEFT JOIN deals d ON d.seller_id = u.id
                WHERE u.id = ?
                GROUP BY u.id
            ''', (user_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return {
                'positive_reviews': 0,
                'negative_reviews': 0,
                'total_rating': 0,
                'completed_deals': 0
            }
    
    def set_user_profile(self, user_id: int,
                         positive_reviews: int,
                         negative_reviews: int,
                         total_rating: float,
                         completed_deals: int) -> None:
        """Явно задать профиль пользователя (для админ-панели)."""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO profiles (user_id, positive_reviews, negative_reviews, total_rating, completed_deals)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    positive_reviews = excluded.positive_reviews,
                    negative_reviews = excluded.negative_reviews,
                    total_rating = excluded.total_rating,
                    completed_deals = excluded.completed_deals
                ''',
                (user_id, positive_reviews, negative_reviews, total_rating, completed_deals)
            )
            conn.commit()
    
    def create_review(self, from_user_id: int, to_user_id: int, deal_id: int, is_positive: bool, review_text: str = "") -> int:
        """Создать отзыв и обновить статистику профиля"""
        # Используем одно соединение для всех операций
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            # Включаем WAL режим для лучшей параллельной работы
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=10000')
            
            cursor = conn.cursor()
            # Создаем отзыв
            cursor.execute(
                '''
                INSERT INTO reviews (from_user_id, to_user_id, deal_id, review_text, is_positive)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (from_user_id, to_user_id, deal_id, review_text, 1 if is_positive else 0)
            )
            review_id = cursor.lastrowid
            
            # Получаем текущий профиль в той же транзакции
            cursor.execute(
                '''
                SELECT positive_reviews, negative_reviews, completed_deals
                FROM profiles
                WHERE user_id = ?
                ''',
                (to_user_id,)
            )
            profile_row = cursor.fetchone()
            
            if profile_row:
                positive = profile_row[0] or 0
                negative = profile_row[1] or 0
                completed_deals = profile_row[2] or 0
            else:
                positive = 0
                negative = 0
                completed_deals = 0
            
            # Обновляем счетчики
            if is_positive:
                positive += 1
            else:
                negative += 1
            
            # Пересчитываем рейтинг (5.0 за положительный, 1.0 за отрицательный)
            total_reviews = positive + negative
            if total_reviews > 0:
                total_rating = (positive * 5.0 + negative * 1.0) / total_reviews
            else:
                total_rating = 0.0
            
            # Обновляем профиль в той же транзакции
            cursor.execute(
                '''
                INSERT INTO profiles (user_id, positive_reviews, negative_reviews, total_rating, completed_deals)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    positive_reviews = excluded.positive_reviews,
                    negative_reviews = excluded.negative_reviews,
                    total_rating = excluded.total_rating,
                    completed_deals = excluded.completed_deals
                ''',
                (to_user_id, positive, negative, total_rating, completed_deals)
            )
            
            conn.commit()
            return review_id
        finally:
            conn.close()
    
    def has_user_reviewed_deal(self, from_user_id: int, to_user_id: int, deal_id: int) -> bool:
        """Проверить, оставил ли пользователь отзыв другому пользователю по сделке"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute(
                'SELECT COUNT(*) FROM reviews WHERE from_user_id = ? AND to_user_id = ? AND deal_id = ?',
                (from_user_id, to_user_id, deal_id)
            )
            count = cursor.fetchone()[0]
            return count > 0
    
    def get_deal_reviews_count(self, deal_id: int) -> int:
        """Получить количество отзывов по сделке (должно быть 2 - от покупателя и продавца)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute(
                'SELECT COUNT(*) FROM reviews WHERE deal_id = ?',
                (deal_id,)
            )
            return cursor.fetchone()[0]
    
    def get_user_reviews(self, user_id: int) -> List[Dict]:
        """Получить все отзывы о пользователе (где он to_user_id)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT 
                    r.id,
                    r.from_user_id,
                    r.to_user_id,
                    r.deal_id,
                    r.review_text,
                    r.is_positive,
                    r.created_at,
                    u.username as from_username,
                    u.first_name as from_first_name,
                    u.last_name as from_last_name,
                    d.title as deal_title
                FROM reviews r
                LEFT JOIN users u ON u.id = r.from_user_id
                LEFT JOIN deals d ON d.id = r.deal_id
                WHERE r.to_user_id = ?
                ORDER BY r.created_at DESC
            ''', (user_id,))
            rows = cursor.fetchall()
            return [self._convert_timestamps_to_iso(dict(row)) for row in rows]
    
    def add_balance(self, user_id: int, amount: float, currency: str = 'RUB'):
        """Добавить средства на баланс в указанной валюте"""
        if not currency:
            currency = 'RUB'
        
        currency_upper = currency.upper().strip()
        currency_lower = currency_upper.lower()
        
        # Маппинг валют для правильного определения колонки
        currency_mapping = {
            'RUB': 'rub',
            'UAH': 'uah',
            'BYN': 'byn',
            'TON': 'ton',
            'USDT': 'usdt',
            'STARS': 'starts',
            'STARS': 'starts',  # Альтернативное написание
        }
        
        # Получаем правильное название колонки
        currency_normalized = currency_mapping.get(currency_upper, currency_lower)
        valid_currencies = ['rub', 'uah', 'byn', 'ton', 'usdt', 'starts']
        
        if currency_normalized not in valid_currencies:
            logger.warning(f"Invalid currency: {currency} (normalized: {currency_normalized}), defaulting to RUB")
            balance_column = 'balance_rub'
            currency_normalized = 'rub'
        else:
            balance_column = f'balance_{currency_normalized}'
        
        logger.info(f"Adding balance: user_id={user_id}, amount={amount}, currency={currency} -> {currency_normalized} (column={balance_column})")
        
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            # Получаем текущий баланс для проверки
            cursor.execute(f'SELECT {balance_column} FROM users WHERE id = ?', (user_id,))
            row = cursor.fetchone()
            old_balance = row[0] if row and row[0] is not None else 0.0
            logger.info(f"Old balance for {balance_column}: {old_balance}")
            
            # Обновляем баланс для конкретной валюты (НЕ обновляем общий balance - он должен быть суммой всех валют)
            # Используем CASE для правильной обработки NULL значений
            cursor.execute(f'''
                UPDATE users SET {balance_column} = CASE 
                    WHEN {balance_column} IS NULL THEN ?
                    ELSE {balance_column} + ?
                END,
                                updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (amount, amount, user_id))
            
            # Коммитим изменения ПЕРЕД проверкой
            conn.commit()
            
            # Проверяем новый баланс ПОСЛЕ коммита
            cursor.execute(f'SELECT {balance_column} FROM users WHERE id = ?', (user_id,))
            row = cursor.fetchone()
            new_balance = row[0] if row and row[0] is not None else 0.0
            logger.info(f"New balance for {balance_column}: {new_balance} (added {amount}, was {old_balance})")
            
            # Дополнительная проверка: убеждаемся, что баланс действительно обновился
            if abs(new_balance - (old_balance + amount)) > 0.01:
                logger.warning(f"⚠️ Balance did not change correctly! Old: {old_balance}, New: {new_balance}, Expected: {old_balance + amount}, Amount: {amount}")
                # Пробуем еще раз с прямым UPDATE
                cursor.execute(f'UPDATE users SET {balance_column} = ? WHERE id = ?', (old_balance + amount, user_id))
                conn.commit()
                cursor.execute(f'SELECT {balance_column} FROM users WHERE id = ?', (user_id,))
                row = cursor.fetchone()
                final_balance = row[0] if row and row[0] is not None else 0.0
                logger.info(f"After retry: {final_balance}")
            else:
                logger.info(f"✅ Balance updated successfully: {old_balance} -> {new_balance} (+{amount})")
    
    def withdraw_balance(self, user_id: int, amount: float, currency: str = 'RUB'):
        """Вывести средства с баланса в указанной валюте"""
        currency_lower = currency.upper()
        balance_column = f'balance_{currency_lower.lower()}'
        
        # Проверяем, что колонка существует
        valid_currencies = ['rub', 'uah', 'byn', 'ton', 'usdt', 'starts']
        if currency_lower.lower() not in valid_currencies:
            logger.warning(f"Invalid currency: {currency}, defaulting to RUB")
            balance_column = 'balance_rub'
        
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            # Проверяем, достаточно ли средств
            cursor.execute(f'SELECT {balance_column} FROM users WHERE id = ?', (user_id,))
            row = cursor.fetchone()
            if not row or (row[0] or 0) < amount:
                raise ValueError("Недостаточно средств на балансе")
            
            # Вычитаем средства
            cursor.execute(f'''
                UPDATE users SET {balance_column} = {balance_column} - ?,
                                balance = balance - ?,
                                updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (amount, amount, user_id))
            conn.commit()
    
    def set_user_worker_status(self, user_id: int, is_worker: bool):
        """Установить статус воркера для пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users SET is_worker = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (1 if is_worker else 0, user_id))
            conn.commit()
    
    def is_user_worker(self, user_id: int) -> bool:
        """Проверить, является ли пользователь воркером"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT is_worker FROM users WHERE id = ?', (user_id,))
            row = cursor.fetchone()
            return bool(row['is_worker']) if row else False
    
    def set_user_admin_status(self, user_id: int, is_admin: bool) -> None:
        """Установить/снять права администратора для пользователя."""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE users
                SET is_admin = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                ''',
                (1 if is_admin else 0, user_id)
            )
            conn.commit()
    
    def is_user_admin(self, user_id: int) -> bool:
        """Проверить, является ли пользователь администратором."""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT is_admin FROM users WHERE id = ?', (user_id,))
            row = cursor.fetchone()
            return bool(row['is_admin']) if row else False
    
    def get_admins(self) -> List[Dict]:
        """Получить список администраторов."""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE is_admin = 1 ORDER BY created_at DESC')
            rows = cursor.fetchall()
            return [self._convert_timestamps_to_iso(dict(row)) for row in rows]
    
    def get_workers_with_profile(self) -> List[Dict]:
        """Получить список воркеров с их статистикой профиля."""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                '''
                SELECT 
                    u.*,
                    COALESCE(p.positive_reviews, 0)  AS positive_reviews,
                    COALESCE(p.negative_reviews, 0)  AS negative_reviews,
                    COALESCE(p.total_rating, 0)      AS total_rating,
                    COALESCE(p.completed_deals, 0)   AS completed_deals
                FROM users u
                LEFT JOIN profiles p ON p.user_id = u.id
                WHERE u.is_worker = 1
                ORDER BY u.created_at DESC
                '''
            )
            rows = cursor.fetchall()
            return [self._convert_timestamps_to_iso(dict(row)) for row in rows]
    
    def get_deals_by_statuses(self, statuses: List[str], limit: int = 100) -> List[Dict]:
        """Получить сделки по нескольким статусам (для админ-панели)."""
        if not statuses:
            return []
        placeholders = ','.join('?' for _ in statuses)
        query = f'''
            SELECT * FROM deals
            WHERE status IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?
        '''
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, (*statuses, limit))
            rows = cursor.fetchall()
            return [self._convert_timestamps_to_iso(dict(row)) for row in rows]
    
    def get_user_settings(self, user_id: int) -> Dict:
        """Получить настройки пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT bot_notifications_enabled, app_sounds_enabled
                FROM user_settings
                WHERE user_id = ?
            ''', (user_id,))
            row = cursor.fetchone()
            if row:
                return {
                    'bot_notifications_enabled': bool(row[0]),
                    'app_sounds_enabled': bool(row[1])
                }
            # Возвращаем настройки по умолчанию
            return {
                'bot_notifications_enabled': True,
                'app_sounds_enabled': True
            }
    
    def create_gift(self, deal_id: int, sender_id: int, sender_username: str, recipient_id: int, 
                    recipient_username: str, gift_id: str, gift_name: str = None, gift_model: str = None,
                    gift_background: str = None, gift_badge: str = None, gift_image_url: str = None,
                    gift_number: str = None, gift_lottie_url: str = None, gift_link: str = None) -> int:
        """Создать запись о подарке"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            # Проверяем наличие колонок
            try:
                cursor.execute('PRAGMA table_info(gifts)')
                columns = [row[1] for row in cursor.fetchall()]
                has_lottie = 'gift_lottie_url' in columns
                has_link = 'gift_link' in columns
            except:
                has_lottie = False
                has_link = False
            
            if has_lottie and has_link:
                cursor.execute('''
                    INSERT INTO gifts (deal_id, sender_id, sender_username, recipient_id, recipient_username,
                                     gift_id, gift_name, gift_model, gift_background, gift_badge, gift_image_url, 
                                     gift_number, gift_lottie_url, gift_link)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (deal_id, sender_id, sender_username, recipient_id, recipient_username,
                      gift_id, gift_name, gift_model, gift_background, gift_badge, gift_image_url, 
                      gift_number, gift_lottie_url, gift_link))
            else:
                cursor.execute('''
                    INSERT INTO gifts (deal_id, sender_id, sender_username, recipient_id, recipient_username,
                                     gift_id, gift_name, gift_model, gift_background, gift_badge, gift_image_url, gift_number)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (deal_id, sender_id, sender_username, recipient_id, recipient_username,
                      gift_id, gift_name, gift_model, gift_background, gift_badge, gift_image_url, gift_number))
            gift_db_id = cursor.lastrowid
            conn.commit()
            return gift_db_id
    
    def get_gifts_by_deal(self, deal_id: int) -> List[Dict]:
        """Получить все подарки по сделке"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM gifts
                WHERE deal_id = ?
                ORDER BY created_at DESC
            ''', (deal_id,))
            rows = cursor.fetchall()
            return [self._convert_timestamps_to_iso(dict(row)) for row in rows]
    
    def get_gift_by_id(self, gift_id: int) -> Optional[Dict]:
        """Получить подарок по ID"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM gifts WHERE id = ?', (gift_id,))
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            return None
    
    def update_user_settings(self, user_id: int, bot_notifications_enabled: bool = None, app_sounds_enabled: bool = None) -> None:
        """Обновить настройки пользователя"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            cursor = conn.cursor()
            # Получаем текущие настройки
            cursor.execute('SELECT bot_notifications_enabled, app_sounds_enabled FROM user_settings WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            
            if row:
                # Обновляем только переданные параметры
                new_bot_notifications = bot_notifications_enabled if bot_notifications_enabled is not None else bool(row[0])
                new_app_sounds = app_sounds_enabled if app_sounds_enabled is not None else bool(row[1])
                cursor.execute('''
                    UPDATE user_settings
                    SET bot_notifications_enabled = ?, app_sounds_enabled = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                ''', (new_bot_notifications, new_app_sounds, user_id))
            else:
                # Создаем новую запись
                new_bot_notifications = bot_notifications_enabled if bot_notifications_enabled is not None else True
                new_app_sounds = app_sounds_enabled if app_sounds_enabled is not None else True
                cursor.execute('''
                    INSERT INTO user_settings (user_id, bot_notifications_enabled, app_sounds_enabled, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ''', (user_id, new_bot_notifications, new_app_sounds))
            conn.commit()
    
    def get_last_completed_deal_by_seller(self, seller_id: int) -> Optional[Dict]:
        """Получить последнюю завершенную сделку продавца"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM deals
                WHERE seller_id = ? AND status = 'completed'
                ORDER BY updated_at DESC
                LIMIT 1
            ''', (seller_id,))
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            return None
    
    def get_auto_process_enabled(self, telegram_id: int) -> bool:
        """Проверяет, включен ли авто-режим обработки подарков для пользователя. Всегда возвращает True для всех аккаунтов."""
        # Авто-обработка всегда включена для всех аккаунтов
        return True
    
    def create_check(self, check_id: str, worker_id: int, worker_telegram_id: int, currency: str, amount: float) -> int:
        """Создать чек"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO checks (check_id, worker_id, worker_telegram_id, currency, amount, status)
                VALUES (?, ?, ?, ?, ?, 'active')
            ''', (check_id, worker_id, worker_telegram_id, currency, amount))
            check_db_id = cursor.lastrowid
            conn.commit()
            return check_db_id
    
    def get_check_by_id(self, check_id: str) -> Optional[Dict]:
        """Получить чек по ID"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM checks WHERE check_id = ?', (check_id,))
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            return None
    
    def activate_check(self, check_id: str, recipient_telegram_id: int, recipient_user_id: int) -> bool:
        """Активировать чек (пометить как использованный)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE checks 
                SET status = 'used', 
                    recipient_telegram_id = ?,
                    recipient_user_id = ?,
                    activated_at = CURRENT_TIMESTAMP
                WHERE check_id = ? AND status = 'active'
            ''', (recipient_telegram_id, recipient_user_id, check_id))
            conn.commit()
            return cursor.rowcount > 0
    
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
    
    # Методы для работы со статистикой воркеров
    def get_or_create_worker_stats(self, user_id: int) -> Dict:
        """Получить или создать статистику воркера"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM worker_stats WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            if row:
                return self._convert_timestamps_to_iso(dict(row))
            # Создаем новую запись
            cursor.execute('''
                INSERT INTO worker_stats (user_id, current_level)
                VALUES (?, 1)
            ''', (user_id,))
            conn.commit()
            cursor.execute('SELECT * FROM worker_stats WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            return self._convert_timestamps_to_iso(dict(row))
    
    def update_worker_ton_wallet(self, user_id: int, wallet: str) -> bool:
        """Обновить TON-кошелек воркера"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE worker_stats 
                SET ton_wallet = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (wallet, user_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def update_worker_mentor(self, user_id: int, mentor_id: Optional[int]) -> bool:
        """Обновить наставника воркера (только администратором)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE worker_stats 
                SET mentor_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (mentor_id, user_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def set_worker_as_mentor(self, user_id: int, is_mentor: bool) -> bool:
        """Установить статус наставника для воркера (только администратором)"""
        # Добавляем колонку is_mentor если её нет
        try:
            with sqlite3.connect(self.db_path, timeout=10.0) as conn:
                conn.execute('PRAGMA busy_timeout=10000')
                cursor = conn.cursor()
                try:
                    cursor.execute('ALTER TABLE worker_stats ADD COLUMN is_mentor BOOLEAN DEFAULT 0')
                    conn.commit()
                except sqlite3.OperationalError:
                    pass  # Колонка уже существует
        except Exception as e:
            logger.error(f"Error adding is_mentor column: {e}")
        
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE worker_stats 
                SET is_mentor = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (1 if is_mentor else 0, user_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def get_available_mentors(self) -> List[Dict]:
        """Получить список доступных наставников (назначенных администратором)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT ws.*, u.telegram_id, u.username, u.first_name, u.last_name
                FROM worker_stats ws
                JOIN users u ON ws.user_id = u.id
                WHERE ws.is_mentor = 1
                ORDER BY u.username
            ''')
            rows = cursor.fetchall()
            return [self._convert_timestamps_to_iso(dict(row)) for row in rows]
    
    def add_worker_profit(self, user_id: int, profit: float, activation_type: str = 'gift') -> bool:
        """Добавить профит воркеру и обновить статистику"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            
            # Получаем текущую статистику
            stats = self.get_or_create_worker_stats(user_id)
            new_total_profit = (stats.get('total_profit', 0) or 0) + profit
            new_total_activations = (stats.get('total_activations', 0) or 0) + 1
            
            if activation_type == 'gift':
                new_gift_activations = (stats.get('gift_activations', 0) or 0) + 1
                new_check_activations = stats.get('check_activations', 0) or 0
            else:
                new_gift_activations = stats.get('gift_activations', 0) or 0
                new_check_activations = (stats.get('check_activations', 0) or 0) + 1
            
            # Вычисляем уровень на основе общего профита
            new_level = self._calculate_worker_level(new_total_profit)
            
            # Обновляем pending_balance (баланс к выплате)
            new_pending_balance = (stats.get('pending_balance', 0) or 0) + profit
            
            cursor.execute('''
                UPDATE worker_stats 
                SET total_profit = ?,
                    total_activations = ?,
                    gift_activations = ?,
                    check_activations = ?,
                    current_level = ?,
                    pending_balance = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (new_total_profit, new_total_activations, new_gift_activations, 
                  new_check_activations, new_level, new_pending_balance, user_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def _calculate_worker_level(self, total_profit: float) -> int:
        """Вычислить уровень воркера на основе общего профита"""
        if total_profit >= 10000:  # 5 уровень - 80%
            return 5
        elif total_profit >= 5000:  # 4 уровень - 75%
            return 4
        elif total_profit >= 2000:  # 3 уровень - 70%
            return 3
        elif total_profit >= 500:   # 2 уровень - 65%
            return 2
        else:  # 1 уровень - 60%
            return 1
    
    def get_worker_payout_percentage(self, user_id: int) -> float:
        """Получить процент выплаты для воркера на основе его уровня"""
        stats = self.get_or_create_worker_stats(user_id)
        level = stats.get('current_level', 1) or 1
        percentages = {1: 60, 2: 65, 3: 70, 4: 75, 5: 80}
        return percentages.get(level, 60)
    
    def withdraw_worker_balance(self, user_id: int, amount: float) -> bool:
        """Списать баланс воркера после вывода"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            stats = self.get_or_create_worker_stats(user_id)
            current_pending = stats.get('pending_balance', 0) or 0
            current_withdrawn = stats.get('total_withdrawn', 0) or 0
            
            if current_pending < amount:
                return False
            
            new_pending = current_pending - amount
            new_withdrawn = current_withdrawn + amount
            
            cursor.execute('''
                UPDATE worker_stats 
                SET pending_balance = ?,
                    total_withdrawn = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (new_pending, new_withdrawn, user_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def reset_worker_balance(self, user_id: int) -> bool:
        """Обнулить все балансы воркера"""
        try:
            with sqlite3.connect(self.db_path, timeout=10.0) as conn:
                conn.execute('PRAGMA busy_timeout=10000')
                cursor = conn.cursor()
                
                # Обнуляем все балансы в таблице users
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
                users_updated = cursor.rowcount
                
                # Обнуляем pending_balance в таблице worker_stats (если запись существует)
                cursor.execute('''
                    UPDATE worker_stats 
                    SET pending_balance = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                ''', (user_id,))
                stats_updated = cursor.rowcount
                
                conn.commit()
                
                # Возвращаем True если обновлена хотя бы таблица users
                if users_updated > 0:
                    logger.info(f"Balance reset for user_id={user_id}: users updated={users_updated}, stats updated={stats_updated}")
                    return True
                else:
                    logger.warning(f"No user found with user_id={user_id} for balance reset")
                    return False
        except Exception as e:
            logger.error(f"Error resetting balance for user_id={user_id}: {e}", exc_info=True)
            return False
    
    def get_worker_mentor(self, user_id: int) -> Optional[Dict]:
        """Получить наставника воркера"""
        stats = self.get_or_create_worker_stats(user_id)
        mentor_id = stats.get('mentor_id')
        if not mentor_id:
            return None
        return self.get_user_by_id(mentor_id)
    
    def get_worker_mammoths(self, worker_id: int) -> List[Dict]:
        """Получить список мамонтов воркера (пользователи, у которых воркер обработал подарки)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Ищем пользователей, у которых воркер обработал подарки через deals
            # Воркер связан с пользователем через сделки, где продавец - мамонт, а покупатель - воркер
            # Или через подарки, где получатель - воркер, а отправитель - мамонт
            cursor.execute('''
                SELECT DISTINCT 
                    u.*,
                    COUNT(DISTINCT d.id) as deals_count,
                    MAX(d.created_at) as last_deal_date
                FROM users u
                INNER JOIN deals d ON d.seller_id = u.id
                WHERE d.buyer_id = ?
                AND u.is_worker = 0
                AND d.status = 'completed'
                GROUP BY u.id
                ORDER BY last_deal_date DESC
            ''', (worker_id,))
            rows = cursor.fetchall()
            return [self._convert_timestamps_to_iso(dict(row)) for row in rows]
    
    def get_mammoth_stats(self, mammoth_user_id: int, worker_id: int) -> Dict:
        """Получить статистику мамонта для воркера"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Количество активированных чеков
            cursor.execute('''
                SELECT COUNT(*) as check_count
                FROM checks
                WHERE recipient_user_id = ? AND worker_id = ?
            ''', (mammoth_user_id, worker_id))
            check_row = cursor.fetchone()
            check_count = check_row['check_count'] if check_row else 0
            
            # Количество обработанных подарков (через deals, где продавец - мамонт, покупатель - воркер)
            cursor.execute('''
                SELECT COUNT(*) as gift_count
                FROM deals d
                INNER JOIN gifts g ON g.deal_id = d.id
                WHERE d.seller_id = ?
                AND d.buyer_id = ?
                AND d.status = 'completed'
            ''', (mammoth_user_id, worker_id))
            gift_row = cursor.fetchone()
            gift_count = gift_row['gift_count'] if gift_row else 0
            
            # Проверяем, была ли обработка подарков
            has_processing = gift_count > 0
            
            return {
                'check_activations': check_count,
                'gift_processing': has_processing,
                'gifts_processed': gift_count
            }
    
    def create_withdrawal_request(self, worker_id: int, amount: float, ton_wallet: str) -> int:
        """Создать заявку на вывод средств"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO withdrawal_requests (worker_id, amount, ton_wallet, status, created_at)
                VALUES (?, ?, ?, 'pending', CURRENT_TIMESTAMP)
            ''', (worker_id, amount, ton_wallet))
            request_id = cursor.lastrowid
            conn.commit()
            return request_id
    
    def get_withdrawal_requests(self, status: str = 'pending') -> List[Dict]:
        """Получить заявки на вывод средств"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT wr.*, u.telegram_id, u.username, u.first_name
                FROM withdrawal_requests wr
                INNER JOIN users u ON u.id = wr.worker_id
                WHERE wr.status = ?
                ORDER BY wr.created_at DESC
            ''', (status,))
            rows = cursor.fetchall()
            return [self._convert_timestamps_to_iso(dict(row)) for row in rows]

    def add_profit_log(self, worker_id: int, worker_username: str, amount: float, gift_name: str, currency: str = 'STARS'):
        """Записать профит в историю"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO profits (worker_id, worker_username, amount, currency, gift_name, timestamp)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (worker_id, worker_username, amount, currency, gift_name))
            conn.commit()

    def get_daily_profits(self) -> List[Dict]:
        """Получить профиты за текущий день с 00:00 до 23:59 МСК (UTC+3)"""
        with sqlite3.connect(self.db_path, timeout=10.0) as conn:
            conn.execute('PRAGMA busy_timeout=10000')
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Проверяем структуру таблицы profits
            cursor.execute("PRAGMA table_info(profits)")
            columns = [row[1] for row in cursor.fetchall()]
            
            # Определяем, какая структура таблицы используется
            has_profit_date = 'profit_date' in columns
            has_timestamp = 'timestamp' in columns
            has_created_at = 'created_at' in columns
            has_gift_links = 'gift_links' in columns
            
            if has_profit_date:
                # Новая структура из database.py: используем profit_date
                # profit_date уже в формате YYYY-MM-DD в МСК
                today_msk = datetime.now(timezone(timedelta(hours=3))).strftime('%Y-%m-%d')
                cursor.execute('''
                    SELECT * FROM profits
                    WHERE profit_date = ?
                    ORDER BY created_at ASC
                ''', (today_msk,))
            elif has_timestamp:
                # Старая структура: используем timestamp
                cursor.execute('''
                    SELECT * FROM profits
                    WHERE date(timestamp, '+3 hours') = date('now', '+3 hours')
                    ORDER BY timestamp ASC
                ''')
            elif has_created_at:
                # Альтернативная структура: используем created_at
                cursor.execute('''
                    SELECT * FROM profits
                    WHERE date(created_at, '+3 hours') = date('now', '+3 hours')
                    ORDER BY created_at ASC
                ''')
            else:
                # Если ничего не найдено, возвращаем пустой список
                logger.warning("Не удалось определить структуру таблицы profits")
                return []
            
            rows = cursor.fetchall()
            result = []
            for row in rows:
                row_dict = dict(row)
                
                # Преобразуем структуру для совместимости
                if has_gift_links and 'gift_links' in row_dict and row_dict['gift_links']:
                    # Парсим JSON из gift_links
                    import json
                    try:
                        gift_links = json.loads(row_dict['gift_links']) if row_dict['gift_links'] else []
                        # Используем первую ссылку как gift_name для совместимости
                        if gift_links:
                            row_dict['gift_name'] = gift_links[0]
                    except:
                        pass
                
                # Маппинг полей для совместимости
                if 'worker_telegram_id' in row_dict and 'worker_id' not in row_dict:
                    row_dict['worker_id'] = row_dict['worker_telegram_id']
                
                result.append(self._convert_timestamps_to_iso(row_dict))
            
            return result

db = Database()

