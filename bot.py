import telebot
from telebot import types
import requests
import logging
import time
import re
from datetime import datetime
import sqlite3
import html
import os  # Добавлено: для переменных окружения

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ЧЕРЕЗ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ===
TOKEN = os.getenv("BOT_TOKEN")  # Берётся из Railway
CHANNEL_USERNAME = "@AIwithCoffee"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")  # Берётся из Railway

EMOJIS = {
    'robot': '🤖', 'star': '⭐', 'check': '✅', 'subscribe': '📢',
    'question': '❓', 'light': '💡', 'warning': '⚠️', 'party': '🎉',
    'fire': '🔥', 'clock': '⏰', 'brain': '🧠', 'zap': '⚡',
    'pen': '✍️', 'book': '📚', 'bulb': '💡', 'globe': '🌍'
}

# ИЗМЕНЕНО: Установка parse_mode='HTML'
bot = telebot.TeleBot(TOKEN, parse_mode='HTML')
user_requests = {}
user_subscriptions = {}
user_busy_states = {}

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect('bot_history.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def save_to_history(user_id, role, content):
    conn = sqlite3.connect('bot_history.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_history (user_id, role, content)
        VALUES (?, ?, ?)
    ''', (user_id, role, content))
    conn.commit()
    conn.close()

def get_user_history(user_id, limit=10):
    conn = sqlite3.connect('bot_history.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT role, content FROM user_history
        WHERE user_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    ''', (user_id, limit))
    history = cursor.fetchall()
    conn.close()
    return [(role, content) for role, content in reversed(history)]

def cleanup_history(user_id):
    conn = sqlite3.connect('bot_history.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        DELETE FROM user_history WHERE id NOT IN (
            SELECT id FROM user_history
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT 20
        ) AND user_id = ?
    ''', (user_id, user_id))
    conn.commit()
    conn.close()

def clean_response(text):
    if not text:
        return ""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<\w+[^>]*>', lambda m: m.group(0) if m.group(0) in ['<b>', '<i>', '<code>', '<pre>'] else '', text)
    text = re.sub(r'</\w+>', lambda m: m.group(0) if m.group(0) in ['</b>', '</i>', '</code>', '</pre>'] else '', text)
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    
    def code_block_replace(match):
        code_content = match.group(1).strip()
        code_content = html.escape(code_content)
        return f'<pre><code>{code_content}</code></pre>'
    
    text = re.sub(r'```([^`]*)```', code_block_replace, text, flags=re.DOTALL)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    return text.strip()

def is_user_subscribed(user_id):
    current_time = time.time()
    if user_id in user_subscriptions:
        cached_time, is_subscribed = user_subscriptions[user_id]
        if current_time - cached_time < 300:
            return is_subscribed
    try:
        chat_member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        is_subscribed = chat_member.status in ['member', 'administrator', 'creator']
        user_subscriptions[user_id] = (current_time, is_subscribed)
        return is_subscribed
    except Exception as e:
        logger.error(f"Ошибка проверки подписки: {e}")
        return False

def is_user_busy(user_id):
    return user_busy_states.get(user_id, False)

def set_user_busy(user_id, busy=True):
    user_busy_states[user_id] = busy

def get_ai_response(prompt, user_id):
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
        
        history = get_user_history(user_id)
        
        is_code_request = any(keyword in prompt.lower() for keyword in [
            'код', 'script', 'unity', 'c#', 'csharp', 'python', 'javascript', 'js', 
            'java', 'cpp', 'c++', 'php', 'ruby', 'go', 'rust', 'swift', 'kotlin',
            'скрипт', 'программа', 'функция', 'метод', 'класс'
        ])
        
        if is_code_request:
            models_to_try = [
                "qwen/qwen-coder",
                "deepseek/deepseek-coder",
                "google/gemma-7b-it"
            ]
            system_prompt = """Ты Qwen Coder, специализированная модель для программирования.
Ты должен писать только рабочий, протестированный код.
Не придумывай код, который не работает.
Объясняй код по шагам.
Используй правильный синтаксис.
Отвечай на русском языке."""
        else:
            models_to_try = [
                "mistralai/mistral-7b-instruct",
                "google/gemma-7b-it",
                "openchat/openchat-7b"
            ]
            system_prompt = """Ты Qwen, продвинутая языковая модель.
Будь полезным, точным и дружелюбным.
Отвечай на русском языке.
Если не знаешь ответа — скажи честно."""

        messages = [{"role": "system", "content": system_prompt}]
        for role, content in history:
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": prompt})
        
        for model in models_to_try:
            data = {
                "model": model,
                "messages": messages,
                "temperature": 0.5,
                "max_tokens": 1000,
                "top_p": 0.9,
                "frequency_penalty": 0.1
            }
            
            logger.info(f"Запрос к модели: {model}")
            start_time = time.time()
            
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",  # ✅ Без пробелов!
                headers=headers,
                json=data,
                timeout=25
            )
            
            end_time = time.time()
            logger.info(f"Время ответа: {end_time - start_time:.2f} сек")
            
            if response.status_code == 200:
                result = response.json()
                answer = result['choices'][0]['message']['content']
                clean_answer = clean_response(answer)
                
                save_to_history(user_id, "user", prompt)
                save_to_history(user_id, "assistant", clean_answer)
                cleanup_history(user_id)
                
                return clean_answer
            else:
                logger.warning(f"Модель {model} не доступна: {response.status_code}")
                if response.status_code == 429 or response.status_code >= 500:
                    time.sleep(1)
                continue
        
        logger.error("Все модели недоступны")
        return None
            
    except requests.exceptions.Timeout:
        logger.error("Таймаус запроса к API")
        return "timeout"
    except requests.exceptions.ConnectionError:
        logger.error("Ошибка подключения к API")
        return "connection_error"
    except Exception as e:
        logger.error(f"Ошибка при запросе к OpenRouter: {e}")
        return None

def check_user_limit(user_id):
    current_time = datetime.now()
    if user_id not in user_requests:
        user_requests[user_id] = []
    user_requests[user_id] = [
        req_time for req_time in user_requests[user_id] 
        if (current_time - req_time).seconds < 3600
    ]
    if len(user_requests[user_id]) >= 15:
        return False, "Вы превысили лимит запросов (15 в час). Попробуйте позже!"
    user_requests[user_id].append(current_time)
    return True, ""

def get_main_menu_markup():
    markup = types.InlineKeyboardMarkup(row_width=1)
    channel_btn = types.InlineKeyboardButton(
        f"{EMOJIS['subscribe']} Подписаться на канал", 
        url=f"https://t.me/{CHANNEL_USERNAME[1:]}"
    )
    check_btn = types.InlineKeyboardButton(
        f"{EMOJIS['check']} Проверить подписку", 
        callback_data="check_subscription"
    )
    help_btn = types.InlineKeyboardButton(
        f"{EMOJIS['question']} Помощь", 
        callback_data="help"
    )
    markup.add(channel_btn, check_btn, help_btn)
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "Пользователь"
    
    if is_user_subscribed(user_id):
        welcome_text = f"""
{EMOJIS['party']} <b>Привет снова, {user_name}!</b>

{EMOJIS['robot']} Ты уже подписан на канал и можешь задавать мне любые вопросы!

<b>Что я умею:</b>
• {EMOJIS['light']} Отвечать на вопросы
• {EMOJIS['pen']} Писать тексты  
• {EMOJIS['book']} Помогать с учебой
• {EMOJIS['bulb']} Генерировать идеи
• {EMOJIS['globe']} Переводить тексты

<i>{EMOJIS['zap']} Просто напиши свой вопрос в чат!</i>
<i>{EMOJIS['warning']} Лимит: 15 запросов в час</i>
        """
        markup = types.InlineKeyboardMarkup()
        help_btn = types.InlineKeyboardButton(f"{EMOJIS['question']} Помощь", callback_data="help")
        markup.add(help_btn)
        bot.send_message(message.chat.id, welcome_text, reply_markup=markup)
        return
    
    welcome_text = f"""
{EMOJIS['robot']} <b>Привет, {user_name}! Добро пожаловать в AI Помощник!</b>

{EMOJIS['brain']} <b>Что я умею:</b>
• {EMOJIS['light']} Отвечать на любые вопросы
• {EMOJIS['pen']} Писать тексты и сочинения  
• {EMOJIS['book']} Помогать с учебой
• {EMOJIS['bulb']} Генерировать идеи
• {EMOJIS['globe']} Переводить тексты

{EMOJIS['warning']} <b>Для начала нужно:</b>
1. {EMOJIS['subscribe']} Подписаться на наш канал {CHANNEL_USERNAME}
2. {EMOJIS['check']} Подтвердить подписку

<i>{EMOJIS['zap']} Это бесплатно! Лимит: 15 запросов в час</i>
    """
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_menu_markup())

# ... остальные обработчики (не менялись) ...

if __name__ == '__main__':
    init_db()
    logger.info("Бот запускается...")
    try:
        bot_info = bot.get_me()
        logger.info(f"✅ Бот @{bot_info.username} успешно запущен!")
        print(f"🤖 Бот @{bot_info.username} готов к работе!")
        print("Нажми Ctrl+C для остановки")
        bot.polling(none_stop=True)
    except Exception as e:
        logger.error(f"❌ Ошибка запуска бота: {e}")
        print(f"❌ Ошибка: {e}")