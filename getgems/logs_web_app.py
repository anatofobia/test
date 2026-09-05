#!/usr/bin/env python3
"""
Веб-приложение для отправки сообщений в темы Telegram от лица бота логов
"""
import os
import asyncio
import logging
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from dotenv import load_dotenv
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
import hashlib
import secrets

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
CORS(app)

# Токен бота для логов
LOGS_BOT_TOKEN = os.getenv("LOGS_BOT_TOKEN") or os.getenv("TELEGRAM_LOGS_BOT_TOKEN", "")
FORUM_CHAT_ID = os.getenv("FORUM_CHAT_ID") or os.getenv("PROFIT_CHAT_ID", "")
LOGS_TOPIC_ID = os.getenv("LOGS_TOPIC_ID") or os.getenv("LOGS_FORUM_TOPIC_ID") or os.getenv("FORUM_TOPIC_ID", "")
PROFIT_TOPIC_ID = os.getenv("PROFIT_TOPIC_ID") or os.getenv("PROFIT_FORUM_TOPIC_ID", "")

# Получаем список админов из env
ADMIN_IDS = [
    int(admin_id.strip()) 
    for admin_id in os.getenv("ADMIN_IDS", "").split(",") 
    if admin_id.strip().isdigit()
]

# Простой токен для авторизации (можно сделать более сложным)
AUTH_TOKEN = os.getenv("LOGS_WEB_AUTH_TOKEN", secrets.token_hex(16))

def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    return user_id in ADMIN_IDS if ADMIN_IDS else False

def check_auth(require_admin=True):
    """Проверяет авторизацию через токен или Telegram auth"""
    # Проверяем токен в заголовке (для API)
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header.replace('Bearer ', '')
        if token == AUTH_TOKEN:
            return True
    
    # Проверяем токен в сессии
    if session.get('authenticated'):
        if require_admin:
            user_id = session.get('user_id')
            if user_id and is_admin(user_id):
                return True
        else:
            return True
    
    # Проверяем Telegram auth_data если есть (для веб-интерфейса)
    init_data = request.form.get('init_data') or request.args.get('init_data') or request.cookies.get('init_data')
    if init_data:
        try:
            user_id = extract_user_id_from_init_data(init_data)
            if user_id:
                if require_admin:
                    if is_admin(user_id):
                        session['authenticated'] = True
                        session['user_id'] = user_id
                        return True
                else:
                    session['authenticated'] = True
                    session['user_id'] = user_id
                    return True
        except Exception as e:
            logger.warning(f"Ошибка проверки Telegram auth: {e}")
    
    # Для веб-интерфейса разрешаем доступ без авторизации (можно отключить для безопасности)
    # Для API требуется авторизация
    if not require_admin and request.path.startswith('/'):
        # Разрешаем доступ к веб-интерфейсу без строгой авторизации (можно настроить через env)
        allow_web_without_auth = os.getenv("LOGS_WEB_ALLOW_WITHOUT_AUTH", "false").lower() == "true"
        if allow_web_without_auth:
            return True
    
    return False

def extract_user_id_from_init_data(init_data: str) -> int:
    """Извлекает user_id из Telegram init_data"""
    try:
        from urllib.parse import unquote, parse_qs
        import hmac
        import hashlib
        import json
        
        # Парсим init_data
        data = parse_qs(unquote(init_data))
        user_str = data.get('user', [None])[0]
        if not user_str:
            return None
        
        user_data = json.loads(user_str)
        return int(user_data.get('id'))
    except Exception as e:
        logger.error(f"Ошибка извлечения user_id: {e}")
        return None

async def send_message_to_topic(
    message_text: str,
    topic_id: int = None,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = True
) -> dict:
    """
    Отправляет сообщение в тему Telegram через бота логов
    
    Args:
        message_text: Текст сообщения
        topic_id: ID темы (message_thread_id), если None - используется LOGS_TOPIC_ID
        parse_mode: Режим парсинга (HTML, Markdown, None)
        disable_web_page_preview: Отключить превью ссылок
    
    Returns:
        dict с результатом отправки
    """
    if not LOGS_BOT_TOKEN:
        return {"success": False, "error": "LOGS_BOT_TOKEN не установлен"}
    
    if not FORUM_CHAT_ID:
        return {"success": False, "error": "FORUM_CHAT_ID не установлен"}
    
    try:
        bot = Bot(token=LOGS_BOT_TOKEN)
        
        # Используем переданный topic_id или дефолтный
        message_thread_id = topic_id
        if message_thread_id is None:
            if LOGS_TOPIC_ID and str(LOGS_TOPIC_ID).isdigit():
                message_thread_id = int(LOGS_TOPIC_ID)
            else:
                logger.warning("Topic ID не указан, отправка без темы может не работать в форумных группах")
        
        # Отправляем сообщение
        result = await bot.send_message(
            chat_id=int(FORUM_CHAT_ID),
            text=message_text,
            message_thread_id=message_thread_id,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview
        )
        
        await bot.session.close()
        
        return {
            "success": True,
            "message_id": result.message_id,
            "chat_id": result.chat.id,
            "topic_id": message_thread_id
        }
    
    except TelegramBadRequest as e:
        error_msg = str(e)
        logger.error(f"Ошибка Telegram API: {error_msg}")
        return {"success": False, "error": f"Ошибка Telegram API: {error_msg}"}
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Ошибка отправки сообщения: {error_msg}", exc_info=True)
        return {"success": False, "error": f"Ошибка: {error_msg}"}

@app.route('/', methods=['GET'])
def index():
    """Главная страница с интерфейсом отправки сообщений"""
    html_template = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Отправка сообщений в темы Telegram</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        
        .container {
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            max-width: 800px;
            width: 100%;
            padding: 40px;
        }
        
        h1 {
            color: #333;
            margin-bottom: 30px;
            text-align: center;
            font-size: 28px;
        }
        
        .form-group {
            margin-bottom: 25px;
        }
        
        label {
            display: block;
            margin-bottom: 8px;
            color: #555;
            font-weight: 600;
            font-size: 14px;
        }
        
        input, textarea, select {
            width: 100%;
            padding: 12px;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            font-size: 14px;
            transition: border-color 0.3s;
            font-family: inherit;
        }
        
        input:focus, textarea:focus, select:focus {
            outline: none;
            border-color: #667eea;
        }
        
        textarea {
            min-height: 200px;
            resize: vertical;
        }
        
        .topic-selector {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }
        
        .topic-option {
            display: flex;
            align-items: center;
            padding: 15px;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.3s;
        }
        
        .topic-option input[type="radio"] {
            width: auto;
            margin-right: 10px;
        }
        
        .topic-option:hover {
            border-color: #667eea;
            background: #f5f7ff;
        }
        
        .topic-option input[type="radio"]:checked + label {
            color: #667eea;
        }
        
        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 15px 30px;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 20px rgba(102, 126, 234, 0.4);
        }
        
        .btn:active {
            transform: translateY(0);
        }
        
        .btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }
        
        .message {
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
            display: none;
        }
        
        .message.success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        
        .message.error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        
        .message.show {
            display: block;
        }
        
        .preview {
            margin-top: 20px;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 10px;
            border: 2px dashed #e0e0e0;
        }
        
        .preview h3 {
            margin-bottom: 15px;
            color: #333;
        }
        
        .preview-content {
            color: #555;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        
        .info {
            background: #e7f3ff;
            border: 1px solid #b3d9ff;
            color: #004085;
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
            font-size: 14px;
        }
        
        .info strong {
            display: block;
            margin-bottom: 5px;
        }
    </style>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
</head>
<body>
    <div class="container">
        <h1>📤 Отправка сообщений в темы Telegram</h1>
        
        <div class="info">
            <strong>ℹ️ Информация:</strong>
            <div>Чат ID: {{ chat_id }}</div>
            <div>Тема логов: {{ logs_topic_id or 'Не указана' }}</div>
            <div>Тема профитов: {{ profit_topic_id or 'Не указана' }}</div>
        </div>
        
        <div id="message" class="message"></div>
        
        <form id="sendForm">
            <div class="form-group">
                <label for="message_text">Сообщение:</label>
                <textarea id="message_text" name="message_text" required placeholder="Введите текст сообщения..."></textarea>
            </div>
            
            <div class="form-group">
                <label>Выберите тему:</label>
                <div class="topic-selector">
                    <div class="topic-option">
                        <input type="radio" id="topic_logs" name="topic" value="logs" checked>
                        <label for="topic_logs">📋 Тема логов ({{ logs_topic_id or 'ID не указан' }})</label>
                    </div>
                    <div class="topic-option">
                        <input type="radio" id="topic_profit" name="topic" value="profit">
                        <label for="topic_profit">💰 Тема профитов ({{ profit_topic_id or 'ID не указан' }})</label>
                    </div>
                    <div class="topic-option">
                        <input type="radio" id="topic_custom" name="topic" value="custom">
                        <label for="topic_custom">🔧 Своя тема</label>
                    </div>
                </div>
            </div>
            
            <div class="form-group" id="custom_topic_group" style="display: none;">
                <label for="custom_topic_id">ID темы:</label>
                <input type="number" id="custom_topic_id" name="custom_topic_id" placeholder="Введите ID темы...">
            </div>
            
            <div class="form-group">
                <label for="parse_mode">Режим парсинга:</label>
                <select id="parse_mode" name="parse_mode">
                    <option value="HTML">HTML</option>
                    <option value="Markdown">Markdown</option>
                    <option value="None">Без форматирования</option>
                </select>
            </div>
            
            <button type="submit" class="btn" id="sendBtn">📤 Отправить сообщение</button>
        </form>
        
        <div class="preview" id="preview" style="display: none;">
            <h3>Предпросмотр:</h3>
            <div class="preview-content" id="preview_content"></div>
        </div>
    </div>
    
    <script>
        // Получаем init_data из Telegram WebApp
        let tg = window.Telegram?.WebApp;
        let initData = null;
        
        if (tg) {
            tg.ready();
            tg.expand();
            initData = tg.initData || tg.initDataUnsafe?.query_id ? tg.initData : null;
            
            // Сохраняем init_data в cookie для авторизации
            if (initData) {
                document.cookie = `init_data=${encodeURIComponent(initData)}; path=/; max-age=3600`;
            }
        }
        
        const chatId = {{ chat_id|tojson }};
        const logsTopicId = {{ logs_topic_id|tojson }};
        const profitTopicId = {{ profit_topic_id|tojson }};
        
        // Переключение видимости поля для своей темы
        document.querySelectorAll('input[name="topic"]').forEach(radio => {
            radio.addEventListener('change', function() {
                document.getElementById('custom_topic_group').style.display = 
                    this.value === 'custom' ? 'block' : 'none';
            });
        });
        
        // Предпросмотр при вводе
        document.getElementById('message_text').addEventListener('input', function() {
            const preview = document.getElementById('preview');
            const previewContent = document.getElementById('preview_content');
            if (this.value.trim()) {
                preview.style.display = 'block';
                previewContent.textContent = this.value;
            } else {
                preview.style.display = 'none';
            }
        });
        
        // Отправка формы
        document.getElementById('sendForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const messageText = document.getElementById('message_text').value.trim();
            if (!messageText) {
                showMessage('Пожалуйста, введите текст сообщения', 'error');
                return;
            }
            
            const selectedTopic = document.querySelector('input[name="topic"]:checked').value;
            let topicId = null;
            
            if (selectedTopic === 'logs') {
                topicId = logsTopicId ? parseInt(logsTopicId) : null;
            } else if (selectedTopic === 'profit') {
                topicId = profitTopicId ? parseInt(profitTopicId) : null;
            } else if (selectedTopic === 'custom') {
                const customTopicId = document.getElementById('custom_topic_id').value;
                if (!customTopicId) {
                    showMessage('Пожалуйста, введите ID темы', 'error');
                    return;
                }
                topicId = parseInt(customTopicId);
            }
            
            const parseMode = document.getElementById('parse_mode').value;
            
            const sendBtn = document.getElementById('sendBtn');
            sendBtn.disabled = true;
            sendBtn.textContent = '⏳ Отправка...';
            
            try {
                // Включаем init_data в запрос, если доступен
                const requestBody = {
                    message_text: messageText,
                    topic_id: topicId,
                    parse_mode: parseMode === 'None' ? null : parseMode,
                    disable_web_page_preview: true
                };
                
                // Добавляем init_data, если доступен
                if (initData) {
                    requestBody.init_data = initData;
                }
                
                const response = await fetch('/api/send', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(requestBody)
                });
                
                const data = await response.json();
                
                if (data.success) {
                    showMessage('✅ Сообщение успешно отправлено!', 'success');
                    document.getElementById('message_text').value = '';
                    document.getElementById('preview').style.display = 'none';
                } else {
                    showMessage('❌ Ошибка: ' + (data.error || 'Неизвестная ошибка'), 'error');
                }
            } catch (error) {
                showMessage('❌ Ошибка при отправке: ' + error.message, 'error');
            } finally {
                sendBtn.disabled = false;
                sendBtn.textContent = '📤 Отправить сообщение';
            }
        });
        
        function showMessage(text, type) {
            const messageDiv = document.getElementById('message');
            messageDiv.textContent = text;
            messageDiv.className = 'message ' + type + ' show';
            setTimeout(() => {
                messageDiv.classList.remove('show');
            }, 5000);
        }
    </script>
</body>
</html>
    """
    
    return render_template_string(
        html_template,
        chat_id=FORUM_CHAT_ID or 'Не указан',
        logs_topic_id=LOGS_TOPIC_ID,
        profit_topic_id=PROFIT_TOPIC_ID
    )

@app.route('/api/send', methods=['POST'])
def api_send():
    """API endpoint для отправки сообщения в тему"""
    try:
        data = request.get_json() or {}
        
        # Проверяем авторизацию через init_data из WebApp
        init_data = data.get('init_data') or request.form.get('init_data') or request.args.get('init_data') or request.cookies.get('init_data')
        
        # Если есть init_data, проверяем через него
        if init_data:
            try:
                user_id = extract_user_id_from_init_data(init_data)
                if user_id and is_admin(user_id):
                    session['authenticated'] = True
                    session['user_id'] = user_id
                else:
                    return jsonify({"success": False, "error": "Недостаточно прав (требуется админ)"}), 403
            except Exception as e:
                logger.warning(f"Ошибка проверки init_data: {e}")
                return jsonify({"success": False, "error": "Ошибка авторизации"}), 401
        elif not check_auth(require_admin=True):
            return jsonify({"success": False, "error": "Не авторизован или недостаточно прав"}), 401
        
        message_text = data.get('message_text', '').strip()
        topic_id = data.get('topic_id')
        parse_mode = data.get('parse_mode', 'HTML')
        disable_web_page_preview = data.get('disable_web_page_preview', True)
        
        if not message_text:
            return jsonify({"success": False, "error": "Текст сообщения не может быть пустым"}), 400
        
        # Преобразуем None в None для parse_mode
        if parse_mode == 'None' or parse_mode is None:
            parse_mode = None
        
        # Отправляем сообщение асинхронно
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(
            send_message_to_topic(
                message_text=message_text,
                topic_id=topic_id,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview
            )
        )
        loop.close()
        
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"Ошибка в API endpoint: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/auth', methods=['POST'])
def api_auth():
    """API endpoint для авторизации через токен"""
    try:
        data = request.get_json()
        token = data.get('token', '')
        
        if token == AUTH_TOKEN:
            session['authenticated'] = True
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Неверный токен"}), 401
    
    except Exception as e:
        logger.error(f"Ошибка авторизации: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv("LOGS_WEB_PORT", 5001))
    host = os.getenv("LOGS_WEB_HOST", "127.0.0.1")
    
    logger.info(f"🚀 Запуск веб-приложения для отправки сообщений в темы")
    logger.info(f"📡 Порт: {port}, Хост: {host}")
    logger.info(f"🔑 Токен авторизации: {AUTH_TOKEN[:10]}...")
    
    app.run(host=host, port=port, debug=False)

