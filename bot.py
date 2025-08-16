# bot.py
import os  # Импортируем os для работы с переменными окружения
import telebot
from telebot import types
import requests
import json
import logging
import time
import re
from datetime import datetime
import sqlite3
import html

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ===
# Получаем токены из переменных окружения для безопасности
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')

# Проверяем, установлены ли токены
if not TOKEN:
    raise ValueError("Ошибка: TELEGRAM_BOT_TOKEN не установлен в переменных окружения.")
if not OPENROUTER_API_KEY:
    raise ValueError("Ошибка: OPENROUTER_API_KEY не установлен в переменных окружения.")

# Имя канала (убедитесь, что оно начинается с @)
CHANNEL_USERNAME = '@AIwithCoffee'

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
# НОВОЕ: Словарь для отслеживания состояния пользователей (занят/свободен)
user_busy_states = {}

# Инициализация базы данных для хранения истории
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

# Сохранение сообщения в историю
def save_to_history(user_id, role, content):
    conn = sqlite3.connect('bot_history.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_history (user_id, role, content)
        VALUES (?, ?, ?)
    ''', (user_id, role, content))
    conn.commit()
    conn.close()

# Получение истории пользователя
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
    return [(role, content) for role, content in reversed(history)]  # Возвращаем в хронологическом порядке

# Очистка старой истории (оставляем только последние 20 сообщений)
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
    """Очистка ответа от проблемных тегов и форматирование кода"""
    if not text:
        return ""
    
    # Удаляем теги <think> и другие проблемные теги
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<\w+[^>]*>', lambda m: m.group(0) if m.group(0) in ['<b>', '<i>', '<code>', '<pre>'] else '', text)
    text = re.sub(r'</\w+>', lambda m: m.group(0) if m.group(0) in ['</b>', '</i>', '</code>', '</pre>'] else '', text)
    
    # НОВОЕ: Преобразование **текст** в <b>текст</b> (жирный шрифт)
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    
    # НОВОЕ: Преобразование *текст* в <i>текст</i> (курсив)
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    
    # Заменяем ``` на <pre><code> для кода
    def code_block_replace(match):
        code_content = match.group(1).strip()
        # Экранируем HTML символы в коде
        code_content = html.escape(code_content)
        return f'<pre><code>{code_content}</code></pre>'
    
    text = re.sub(r'```([^`]*)```', code_block_replace, text, flags=re.DOTALL)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)  # Одиночные ` для inline кода
    
    return text.strip()

def escape_html(text):
    """Экранирование HTML символов"""
    return html.escape(text)

def is_user_subscribed(user_id):
    """Проверка подписки пользователя с кэшированием"""
    current_time = time.time()
    
    # Проверяем кэш
    if user_id in user_subscriptions:
        cached_time, is_subscribed = user_subscriptions[user_id]
        if current_time - cached_time < 300:  # Кэш на 5 минут
            return is_subscribed
    
    # Проверяем подписку
    try:
        chat_member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        is_subscribed = chat_member.status in ['member', 'administrator', 'creator']
        user_subscriptions[user_id] = (current_time, is_subscribed)
        return is_subscribed
    except Exception as e:
        logger.error(f"Ошибка проверки подписки: {e}")
        return False

# НОВОЕ: Функция для проверки, занят ли пользователь
def is_user_busy(user_id):
    """Проверяет, обрабатывает ли бот уже запрос от этого пользователя."""
    return user_busy_states.get(user_id, False)

# НОВОЕ: Функция для установки состояния пользователя
def set_user_busy(user_id, busy=True):
    """Устанавливает состояние пользователя (занят/свободен)."""
    user_busy_states[user_id] = busy

def get_ai_response(prompt, user_id):
    """Получение ответа от ИИ через OpenRouter"""
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # Получаем историю сообщений пользователя
        history = get_user_history(user_id)
        
        # Определяем модель по типу запроса
        is_code_request = any(keyword in prompt.lower() for keyword in [
            'код', 'script', 'unity', 'c#', 'csharp', 'python', 'javascript', 'js', 
            'java', 'cpp', 'c++', 'php', 'ruby', 'go', 'rust', 'swift', 'kotlin',
            'скрипт', 'программа', 'функция', 'метод', 'класс'
        ])
        
        if is_code_request:
            # Для кода используем специализированную модель
            models_to_try = [
    "meta-llama/llama-3.3-70b-instruct:free", # Очень популярная и мощная бесплатная модель.
    "nousresearch/hermes-3-llama-3.1-405b:free", # Высококачественная модель с улучшенными инструкциями.
    "neversleep/llama-3.1-lumimaid-70b:free", # Альтернатива Llama 3.1 70B.
    "microsoft/wizardlm-2-7b:free", # Хорошая модель от Microsoft, меньший размер.
    "google/gemma-2-27b-it:free", # Мощная модель Gemma 2 от Google.
    "google/gemma-2-9b-it:free", # Более легкая версия Gemma 2.
    "meta-llama/llama-3-70b-instruct:free", # Предыдущая версия Llama 3, 70B.
    "meta-llama/llama-3-8b-instruct:free", # Легкая версия Llama 3, 8B.
    "mistralai/mistral-7b-instruct:free", # Классическая модель Mistral 7B.
    "openchat/openchat-7b:free", # Другая популярная 7B модель.
            ]
            system_prompt = """Ты Qwen Coder, специализированная модель для программирования.
Ты должен писать только рабочий, протестированный код.
Не придумывай код, который не работает.
Объясняй код по шагам.
Используй правильный синтаксис.
Отвечай на русском языке."""
        else:
            # Для обычных запросов используем общую модель
            models_to_try = [
    "meta-llama/llama-3.3-70b-instruct:free", # Очень популярная и мощная бесплатная модель.
    "nousresearch/hermes-3-llama-3.1-405b:free", # Высококачественная модель с улучшенными инструкциями.
    "neversleep/llama-3.1-lumimaid-70b:free", # Альтернатива Llama 3.1 70B.
    "microsoft/wizardlm-2-7b:free", # Хорошая модель от Microsoft, меньший размер.
    "google/gemma-2-27b-it:free", # Мощная модель Gemma 2 от Google.
    "google/gemma-2-9b-it:free", # Более легкая версия Gemma 2.
    "meta-llama/llama-3-70b-instruct:free", # Предыдущая версия Llama 3, 70B.
    "meta-llama/llama-3-8b-instruct:free", # Легкая версия Llama 3, 8B.
    "mistralai/mistral-7b-instruct:free", # Классическая модель Mistral 7B.
    "openchat/openchat-7b:free", # Другая популярная 7B модель.
            ]
            system_prompt = """Ты Qwen, продвинутая языковая модель.
Будь полезным, точным и дружелюбным.
Отвечай на русском языке.
Если не знаешь ответа — скажи честно."""

        # Формируем сообщения для контекста
        messages = [{"role": "system", "content": system_prompt}]
        
        # Добавляем историю сообщений
        for role, content in history:
            messages.append({"role": role, "content": content})
        
        # Добавляем текущий запрос пользователя
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
            
            # ИСПРАВЛЕНО: Убран лишний пробел в конце URL
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
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
                logger.info(f"Успешный ответ от модели {model}")
                
                # Сохраняем в историю
                save_to_history(user_id, "user", prompt)
                save_to_history(user_id, "assistant", clean_answer)
                cleanup_history(user_id)  # Очищаем старую историю
                
                return clean_answer
            else:
                logger.warning(f"Модель {model} не доступна: {response.status_code}")
                # ИЗМЕНЕНО: Добавлена задержка перед следующей попыткой в случае ошибки 429 или других
                if response.status_code == 429 or response.status_code >= 500:
                     time.sleep(1) # Пауза 1 секунда перед следующей попыткой
                continue
        
        logger.error("Все модели недоступны")
        return None
            
    except requests.exceptions.Timeout:
        logger.error("Таймаут запроса к API")
        return "timeout"
    except requests.exceptions.ConnectionError:
        logger.error("Ошибка подключения к API")
        return "connection_error"
    except Exception as e:
        logger.error(f"Ошибка при запросе к OpenRouter: {e}")
        return None

def check_user_limit(user_id):
    """Проверка лимита запросов пользователя"""
    current_time = datetime.now()
    
    if user_id not in user_requests:
        user_requests[user_id] = []
    
    # Удаляем старые запросы (старше 1 часа)
    user_requests[user_id] = [
        req_time for req_time in user_requests[user_id] 
        if (current_time - req_time).seconds < 3600
    ]
    
    # Проверяем лимит (15 запросов в час)
    if len(user_requests[user_id]) >= 15:
        return False, "Вы превысили лимит запросов (15 в час). Попробуйте позже!"
    
    # Добавляем текущий запрос
    user_requests[user_id].append(current_time)
    return True, ""

def get_main_menu_markup():
    """Создание главного меню"""
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
    
    # Проверяем, подписан ли пользователь
    if is_user_subscribed(user_id):
        # Уже подписан - показываем прямой доступ
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
        help_btn = types.InlineKeyboardButton(
            f"{EMOJIS['question']} Помощь", 
            callback_data="help"
        )
        markup.add(help_btn)
        
        # ИЗМЕНЕНО: Убран escape_html, так как parse_mode='HTML' установлен
        bot.send_message(message.chat.id, welcome_text, reply_markup=markup)
        return
    
    # Не подписан - показываем стандартное приветствие
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
    
    # ИЗМЕНЕНО: Убран escape_html
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_menu_markup())

@bot.message_handler(commands=['help'])
def send_help(message):
    help_text = f"""
{EMOJIS['robot']} <b>Помощь по боту</b>

{EMOJIS['question']} <b>Как пользоваться:</b>
• Напиши любой вопрос в чат
• Получи умный ответ от ИИ

{EMOJIS['star']} <b>Примеры запросов:</b>
• "Напиши сочинение на тему дружбы"
• "Объясни теорию относительности"
• "Переведи текст с английского"
• "Придумай 5 идей для подарка"
• "Напиши скрипт для Unity"

{EMOJIS['light']} <b>Советы:</b>
• Чем точнее вопрос, тем лучше ответ
• Для кода используй четкие указания языка

{EMOJIS['warning']} <b>Лимиты:</b>
• 15 запросов в час для каждого пользователя
    """
    
    markup = types.InlineKeyboardMarkup()
    back_btn = types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")
    markup.add(back_btn)
    
    # ИЗМЕНЕНО: Убран escape_html
    bot.send_message(message.chat.id, help_text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "check_subscription")
def check_subscription(call):
    try:
        user_id = call.from_user.id
        user_name = call.from_user.first_name or "Пользователь"
        
        if is_user_subscribed(user_id):
            success_text = f"""
{EMOJIS['party']} <b>Отлично, {user_name}! Доступ открыт!</b>

{EMOJIS['robot']} Теперь ты можешь задавать мне любые вопросы!

<i>{EMOJIS['zap']} Просто напиши свой вопрос в чат!</i>
            """
            
            bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
            # ИЗМЕНЕНО: Убран escape_html
            bot.edit_message_text(success_text, call.message.chat.id, call.message.message_id)
            
            instruction_text = f"""
{EMOJIS['light']} <b>Готов к работе!</b>
Напиши свой вопрос, и я отвечу с помощью ИИ!

{EMOJIS['clock']} <i>Ответ приходит быстро (5-15 секунд)</i>
            """
            # ИЗМЕНЕНО: Убран escape_html
            bot.send_message(call.message.chat.id, instruction_text)
        else:
            bot.answer_callback_query(call.id, "❌ Сначала подпишись на канал")
            
            error_text = f"""
{EMOJIS['warning']} <b>Нужно подписаться на канал</b>

Для использования бота необходимо быть подписчиком нашего канала {CHANNEL_USERNAME}!

{EMOJIS['check']} После подписки нажми кнопку "Проверить подписку" еще раз.
            """
            
            # ИЗМЕНЕНО: Убран escape_html
            bot.edit_message_text(error_text, call.message.chat.id, call.message.message_id, reply_markup=get_main_menu_markup())
            
    except Exception as e:
        logger.error(f"Ошибка проверки подписки: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка проверки. Попробуй позже")

@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def back_to_main(call):
    user_id = call.from_user.id
    user_name = call.from_user.first_name or "Пользователь"
    
    if is_user_subscribed(user_id):
        # Уже подписан
        welcome_text = f"""
{EMOJIS['party']} <b>Привет снова, {user_name}!</b>

{EMOJIS['robot']} Ты уже подписан и можешь задавать мне любые вопросы!

<b>Что я умею:</b>
• {EMOJIS['light']} Отвечать на вопросы
• {EMOJIS['pen']} Писать тексты  
• {EMOJIS['book']} Помогать с учебой
• {EMOJIS['bulb']} Генерировать идеи

<i>{EMOJIS['zap']} Просто напиши свой вопрос!</i>
        """
        
        markup = types.InlineKeyboardMarkup()
        help_btn = types.InlineKeyboardButton(
            f"{EMOJIS['question']} Помощь", 
            callback_data="help"
        )
        markup.add(help_btn)
        
        # ИЗМЕНЕНО: Убран escape_html
        bot.edit_message_text(welcome_text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    else:
        # Не подписан
        welcome_text = f"""
{EMOJIS['robot']} <b>Привет, {user_name}! Добро пожаловать в AI Помощник!</b>

{EMOJIS['brain']} <b>Что я умею:</b>
• {EMOJIS['light']} Отвечать на любые вопросы
• {EMOJIS['pen']} Писать тексты и сочинения  
• {EMOJIS['book']} Помогать с учебой
• {EMOJIS['bulb']} Генерировать идеи
• {EMOJIS['globe']} Переводить тексты
        """
        
        # ИЗМЕНЕНО: Убран escape_html
        bot.edit_message_text(welcome_text, call.message.chat.id, call.message.message_id, reply_markup=get_main_menu_markup())
    
    bot.answer_callback_query(call.id, "")

@bot.callback_query_handler(func=lambda call: call.data == "help")
def show_help(call):
    help_text = f"""
{EMOJIS['robot']} <b>Помощь по боту</b>

{EMOJIS['question']} <b>Как пользоваться:</b>
• Напиши любой вопрос в чат
• Получи умный ответ от ИИ

{EMOJIS['star']} <b>Примеры запросов:</b>
• "Напиши сочинение на тему дружбы"
• "Объясни теорию относительности"
• "Переведи текст с английского"
• "Придумай 5 идей для подарка"
• "Напиши скрипт для Unity на C#"

{EMOJIS['light']} <b>Советы:</b>
• Чем точнее вопрос, тем лучше ответ
• Для кода указывай язык программирования

{EMOJIS['warning']} <b>Лимиты:</b>
• 15 запросов в час для каждого пользователя
    """
    
    markup = types.InlineKeyboardMarkup()
    back_btn = types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_main")
    markup.add(back_btn)
    
    # ИЗМЕНЕНО: Убран escape_html
    bot.edit_message_text(help_text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id, "")

@bot.message_handler(func=lambda message: True)
def handle_question(message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "Пользователь"
    question = message.text
    
    logger.info(f"Вопрос от {user_name} ({user_id}): {question}")
    
    # ИЗМЕНЕНО: Проверка, занят ли пользователь
    if is_user_busy(user_id):
        # Пользователь уже отправил запрос, который обрабатывается
        logger.info(f"Пользователь {user_id} уже занят. Игнорируем новый запрос.")
        # Можно отправить сообщение пользователю, что его запрос в очереди
        # bot.send_message(message.chat.id, f"{EMOJIS['clock']} Ваш предыдущий запрос обрабатывается. Пожалуйста, подождите.")
        return # Игнорируем новый запрос

    # Строгая проверка подписки
    if not is_user_subscribed(user_id):
        # Не подписан - отправляем на подписку
        welcome_text = f"""
{EMOJIS['warning']} <b>Доступ ограничен</b>

Для использования бота необходимо быть подписчиком канала {CHANNEL_USERNAME}

{EMOJIS['check']} Пожалуйста, подпишись и подтверди подписку:
        """
        
        # ИЗМЕНЕНО: Убран escape_html
        bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_menu_markup())
        return
    
    try:
        # Проверяем лимит запросов
        can_proceed, limit_message = check_user_limit(user_id)
        if not can_proceed:
            bot.send_message(message.chat.id, f"{EMOJIS['warning']} {limit_message}")
            return

        # ИЗМЕНЕНО: Устанавливаем состояние пользователя как "занят"
        set_user_busy(user_id, True)
        
        # Отправляем уведомление о обработке
        processing_msg = bot.send_message(
            message.chat.id, 
            f"{EMOJIS['clock']} Обрабатываю запрос...\n{EMOJIS['brain']} Подключаюсь к ИИ..."
        )
        
        # Получаем ответ от ИИ
        answer = get_ai_response(question, user_id)
        
        # Удаляем сообщение о обработке
        try:
            bot.delete_message(message.chat.id, processing_msg.message_id)
        except:
            pass  # Игнорируем ошибки удаления

        # ИЗМЕНЕНО: Сбрасываем состояние пользователя после получения ответа от ИИ
        set_user_busy(user_id, False)
        
        if answer and answer not in ["timeout", "connection_error"]:
            # Отправляем только чистый ответ от ИИ
            # ИЗМЕНЕНО: parse_mode='HTML' уже установлен по умолчанию, но можно оставить для ясности
            try:
                bot.send_message(message.chat.id, answer, parse_mode='HTML')
            except Exception as e:
                # Если есть ошибка форматирования, отправляем как обычный текст
                logger.error(f"Ошибка отправки форматированного сообщения: {e}")
                clean_text = re.sub(r'<[^>]+>', '', answer)  # Удаляем все теги
                bot.send_message(message.chat.id, clean_text)
        elif answer == "timeout":
            error_text = f"""
{EMOJIS['warning']} <b>Время ожидания истекло</b>

Запрос выполнялся слишком долго. Попробуй:
• Сделать вопрос проще
• Запросить меньше информации
• Попробовать позже

{EMOJIS['clock']} Повтори попытку через минуту.
            """
            # ИЗМЕНЕНО: Убран escape_html
            bot.send_message(message.chat.id, error_text)
        elif answer == "connection_error":
            error_text = f"""
{EMOJIS['warning']} <b>Проблемы с подключением</b>

Не удалось подключиться к серверу ИИ. Попробуй позже.

{EMOJIS['clock']} Повтори попытку через несколько минут.
            """
            # ИЗМЕНЕНО: Убран escape_html
            bot.send_message(message.chat.id, error_text)
        else:
            error_text = f"""
{EMOJIS['warning']} <b>Извини, возникла ошибка</b>

Не удалось получить ответ от ИИ. Попробуй:
• Переформулировать вопрос
• Сделать его проще
• Попробовать позже

{EMOJIS['clock']} Повтори попытку через пару минут.
            """
            # ИЗМЕНЕНО: Убран escape_html
            bot.send_message(message.chat.id, error_text)
            
    except Exception as e:
        logger.error(f"Ошибка обработки сообщения: {e}")
        # ИЗМЕНЕНО: Сбрасываем состояние пользователя в случае ошибки
        set_user_busy(user_id, False)
        try:
            bot.delete_message(message.chat.id, processing_msg.message_id)
        except:
            pass
        error_text = f"""
{EMOJIS['warning']} <b>Внутренняя ошибка бота</b>

Что-то пошло не так. Попробуй повторить запрос позже.

{EMOJIS['clock']} Разработчик уже уведомлен о проблеме.
        """
        # ИЗМЕНЕНО: Убран escape_html
        bot.send_message(message.chat.id, error_text)

# === ЗАПУСК БОТА ===

if __name__ == '__main__':
    init_db()  # Инициализируем базу данных
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
