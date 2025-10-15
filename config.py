import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "7397883294:AAEm9CTdQyko44aBFUA2wQ7A3HpobM2bzkw")
BOT_TOKEN = TOKEN
MIN_PLAYERS = int(os.getenv("MIN_PLAYERS", "3"))
DATA_DIR = os.getenv("DATA_DIR", ".")