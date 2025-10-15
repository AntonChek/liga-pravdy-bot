import os
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Получаем токен бота
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

BOT_TOKEN = TOKEN
MIN_PLAYERS = int(os.getenv("MIN_PLAYERS", "3"))
DATA_DIR = os.getenv("DATA_DIR", ".")