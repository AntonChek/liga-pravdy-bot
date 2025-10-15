from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
import asyncio
import random
from config import TOKEN

# main.py
import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Dict, List

from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.enums import ChatType
from aiogram.enums import ParseMode

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.TOKEN)
dp = Dispatcher()

# --- Загрузка данных ---
DATA_DIR = Path(config.DATA_DIR)
SITUATIONS_FILE = DATA_DIR / "situations.json"
WITNESSES_FILE = DATA_DIR / "witnesses.json"
CONCLUSIONS_FILE = DATA_DIR / "conclusions.json"

def load_json(path):
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))

SITUATIONS = load_json(SITUATIONS_FILE)
WITNESSES = load_json(WITNESSES_FILE)
CONCLUSIONS = load_json(CONCLUSIONS_FILE)

# --- Хранилище состояний игр в разных чатах (in-memory) ---
# Структура для каждого chat_id:
# {
#   "players": {user_id: {"name": str, "role": str, "is_joined": True}},
#   "order": [user_id, ...],  # порядок игроков
#   "roles_assigned": bool,
#   "current_situation": {title, text},
#   "used_situations": set(),
#   "used_witnesses": set(),
#   "stage": "idle"/"situation"/"witnesses"/"debate"/"verdict",
#   "judge_id": user_id,
#   "defendant_id": user_id,
#   "witness_map": {user_id: witness_text},
# }
GAMES: Dict[int, Dict] = {}
GAMES_LOCK = asyncio.Lock()

# --- Утилиты ---
def pick_random_and_mark(collection: List, used: set):
    if not collection:
        return None
    available = [i for i in range(len(collection)) if i not in used]
    if not available:
        # reset used
        used.clear()
        available = list(range(len(collection)))
    idx = random.choice(available)
    used.add(idx)
    return collection[idx]

def get_mention(user: types.User):
    name = user.full_name
    return f"<a href='tg://user?id={user.id}'>{name}</a>"

# --- Keyboards ---
def start_game_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Присоединиться", callback_data="join")],
        [InlineKeyboardButton(text="Закончить набор", callback_data="stop_join")],
        [InlineKeyboardButton(text="Инструкция", callback_data="instructions")],
        [InlineKeyboardButton(text="Завершить игру", callback_data="end_game")]

    ])
    return kb

def game_control_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Раздать роли", callback_data="assign_roles")],
        [InlineKeyboardButton(text="Завершить игру", callback_data="end_game")]
    ])
    return kb

def situation_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Вытянуть карту свидетеля", callback_data="draw_witness")],
        [InlineKeyboardButton(text="Начать дебаты (прокурор/адвокат)", callback_data="start_debate")],
        [InlineKeyboardButton(text="Призвать судью к вердикту", callback_data="judge_verdict")],
        [InlineKeyboardButton(text="Завершить игру", callback_data="end_game")]

    ])
    return kb

def debate_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Вытянуть карту свидетеля", callback_data="draw_witness")],
        [InlineKeyboardButton(text="Призвать судью к вердикту", callback_data="judge_verdict")],
        [InlineKeyboardButton(text="Завершить игру", callback_data="end_game")]

    ])
    return kb

def roles_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Начать раунд (ситуация)", callback_data="start_round")],
        [InlineKeyboardButton(text="Завершить игру", callback_data="end_game")]

    ])
    return kb

# --- Команды ---
@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.reply(
        "Привет! Я бот для ролевой игры 'Суд'.\n\n"
        "Запусти /newgame в групповом чате, чтобы начать новую игру.\n"
        "Игроки будут присоединяться через кнопку 'Присоединиться'.",
    )

@dp.message(Command("newgame"))
async def cmd_newgame(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.reply("Запускай игру в групповом чате, чтобы приглашать игроков.")
        return

    chat_id = message.chat.id
    async with GAMES_LOCK:
        GAMES[chat_id] = {
            "players": {},
            "order": [],
            "roles_assigned": False,
            "current_situation": None,
            "used_situations": set(),
            "used_witnesses": set(),
            "stage": "joining",
            "judge_id": None,
            "defendant_id": None,
            "witness_map": {},
        }

    await message.reply(
        "Новая игра создана! Нажмите «Присоединиться», чтобы вступить в игру.\n"
        f"Минимум игроков: {config.MIN_PLAYERS}. Когда все присоединятся — ведущий (или админ чата) нажмёт «Закончить набор».",
        reply_markup=start_game_kb()
        )

# --- Callbacks: join / stop_join / assign roles / start round / draw witness / etc. ---
@dp.callback_query(lambda c: c.data == "join")
async def cb_join(callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    user = callback_query.from_user

    async with GAMES_LOCK:
        game = GAMES.get(chat_id)
        if not game:
            await bot.answer_callback_query(callback_query.id, "Игра не найдена. Запустите /newgame.")
            return

        if user.id in game["players"]:
            await bot.answer_callback_query(callback_query.id, "Вы уже в игре.")
            return

        game["players"][user.id] = {"name": user.full_name, "role": None}
        game["order"].append(user.id)

    await bot.answer_callback_query(callback_query.id, "Вы присоединились к игре.")
    await bot.send_message(chat_id, f"{get_mention(user)} присоединился к игре.", parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data == "stop_join")
async def cb_stop_join(callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    caller = callback_query.from_user

    async with GAMES_LOCK:
        game = GAMES.get(chat_id)
        if not game:
            await bot.answer_callback_query(callback_query.id, "Игра не найдена.")
            return

        player_count = len(game["players"])
    if player_count < config.MIN_PLAYERS:
        await bot.answer_callback_query(callback_query.id, f"Слишком мало игроков ({player_count}). Нужны минимум {config.MIN_PLAYERS}.")
        return

    # show list and control keyboard
    async with GAMES_LOCK:
        game = GAMES[chat_id]
        names = [p["name"] for p in game["players"].values()]
    txt = "Набор окончен. Игроки:\n" + "\n".join(f"- {n}" for n in names)
    await bot.answer_callback_query(callback_query.id, "Набор окончен. Можете раздать роли.")
    await bot.send_message(chat_id, txt, reply_markup=game_control_kb())

@dp.callback_query(lambda c: c.data == "assign_roles")
async def cb_assign_roles(callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id

    async with GAMES_LOCK:
        game = GAMES.get(chat_id)
        if not game:
            await bot.answer_callback_query(callback_query.id, "Игра не найдена.")
            return

        players = list(game["players"].keys())
        random.shuffle(players)
        n = len(players)

        # Базовые роли: судья, прокурор, адвокат, подсудимый, остальные — свидетели/присяжные
        roles = ["Судья", "Прокурор", "Адвокат", "Подсудимый"] #["Судья", "Прокурор", "Адвокат", "Подсудимый"]
        # если мало игроков — подгоняем
        if n < len(roles):
            roles = roles[:n]

        assigned = {}
        for i, uid in enumerate(players):
            role = roles[i] if i < len(roles) else "Свидетель"
            assigned[uid] = role
            game["players"][uid]["role"] = role

        # определим id судьи и подсудимого для быстрых ссылок
        judge_id = next((uid for uid, r in assigned.items() if r == "Судья"), None)
        defendant_id = next((uid for uid, r in assigned.items() if r == "Подсудимый"), None)
        game["judge_id"] = judge_id
        game["defendant_id"] = defendant_id
        game["roles_assigned"] = True

    # Отправить приватные сообщения с ролью каждому игроку
    for uid, pdata in game["players"].items():
        try:
            await bot.send_message(uid, f"Вам назначена роль: {pdata['role']}", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning(f"Не удалось отправить приватное сообщение {uid}: {e}")

    # Объявление в чате
    lines = []
    for uid, pdata in game["players"].items():
        role = pdata["role"]
        mention = f"<a href='tg://user?id={uid}'>{pdata['name']}</a>"
        lines.append(f"{mention} — {role}")
    msg = "Роли распределены:\n" + "\n".join(lines)
    await bot.answer_callback_query(callback_query.id, "Роли разданы (личные сообщения отправлены).")
    await bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML, reply_markup=roles_kb())

@dp.callback_query(lambda c: c.data == "start_round")
async def cb_start_round(callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    async with GAMES_LOCK:
        game = GAMES.get(chat_id)
        if not game or not game.get("roles_assigned"):
            await bot.answer_callback_query(callback_query.id, "Роли не распределены или игра не найдена.")
            return

        # выбрать ситуацию случайно и отметить использованную
        situation = pick_random_and_mark(SITUATIONS, game["used_situations"])
        if not situation:
            await bot.answer_callback_query(callback_query.id, "Нет доступных ситуаций.")
            return

        game["current_situation"] = situation
        game["stage"] = "situation"
        # очистим карты свидетелей на новый раунд
        game["witness_map"] = {}

    title = situation.get("title", "Ситуация")
    text = situation.get("text", "")
    article = situation.get("article", "")
    consequence = situation.get("consequence", "")
    await bot.answer_callback_query(callback_query.id, "Новая ситуация выдана.")
    await bot.send_message(chat_id, f"<b>СИТУАЦИЯ:</b> {title}\n\n{text}\n\n{article}\n\n{consequence}", parse_mode=ParseMode.HTML, reply_markup=situation_kb())
@dp.callback_query(lambda c: c.data == "instructions")
async def cb_instructions(callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    
    try:
        # Отправка PDF файла
        from aiogram.types import FSInputFile
        pdf_file = FSInputFile("./instructions.jpg")
        await bot.send_photo(chat_id=chat_id, photo=pdf_file, caption="Инструкция по игре")
        await bot.answer_callback_query(callback_query.id, "Инструкция отправлена.")
    except Exception as e:
        logger.error(f"Ошибка при отправке PDF: {e}")
        await bot.answer_callback_query(callback_query.id, "Ошибка при отправке файла.")

@dp.callback_query(lambda c: c.data == "draw_witness")
async def cb_draw_witness(callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    user = callback_query.from_user

    async with GAMES_LOCK:
        game = GAMES.get(chat_id)
        if not game or not game.get("current_situation"):
            await bot.answer_callback_query(callback_query.id, "Нет активной ситуации. Запустите раунд.")
            return

        # у свидетеля может быть максимум одна карта; если уже есть — покажем
        if user.id in game["witness_map"]:
            await bot.answer_callback_query(callback_query.id, "Вы уже вытянули свою карту свидетеля.")
            return

        witness = pick_random_and_mark(WITNESSES, game["used_witnesses"])
        if not witness:
            await bot.answer_callback_query(callback_query.id, "Нет доступных карт свидетелей.")
            return

        game["witness_map"][user.id] = witness

    # отправить приватно текст свидетелю
    try:
        await bot.send_message(user.id, f"Ваша кураторская карта свидетеля:\n\n<b>{witness.title}</b>\n\n{witness.text}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Не удалось отправить свидетелю приватную карту {user.id}: {e}")
        # если нельзя писать приватно, отправим в чат с упоминанием (без раскрытия всей карты)
        await bot.send_message(chat_id, f"{get_mention(user)} вытянул(а) карту свидетеля (карта отправлена в ЛС или недоступна).", parse_mode=ParseMode.HTML)

    await bot.answer_callback_query(callback_query.id, "Карта отправлена вам в личные сообщения (если это возможно).")

@dp.callback_query(lambda c: c.data == "start_debate")
async def cb_start_debate(callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    async with GAMES_LOCK:
        game = GAMES.get(chat_id)
        if not game or not game.get("current_situation"):
            await bot.answer_callback_query(callback_query.id, "Нет активного раунда.")
            return
        game["stage"] = "debate"
    await bot.answer_callback_query(callback_query.id, "Стадия дебатов началась.")
    await bot.send_message(chat_id, "Начинаются дебаты: прокурор и адвокат представляют свои аргументы. Судья может объявить перерыв или перейти к вердикту.", reply_markup=debate_kb())

@dp.callback_query(lambda c: c.data == "judge_verdict")
async def cb_judge_verdict(callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    caller = callback_query.from_user
    async with GAMES_LOCK:
        game = GAMES.get(chat_id)
        if not game:
            await bot.answer_callback_query(callback_query.id, "Игра не найдена.")
            return
        judge_id = game.get("judge_id")
        if judge_id is None:
            await bot.answer_callback_query(callback_query.id, "Судья не назначен.")
            return
        # только судья может выносить формальный вердикт (по правилам)
        if caller.id != judge_id:
            await bot.answer_callback_query(callback_query.id, "Только судья может вызвать вердикт.")
            return

        game["stage"] = "verdict"
        # случайный вывод из conclusions (если есть)
        conclusion = pick_random_and_mark(CONCLUSIONS, set()) if CONCLUSIONS else None

    # показываем судье варианты: Оправдать / Осудить
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оправдать", callback_data="verdict_acquit")],
        [InlineKeyboardButton(text="Осудить", callback_data="verdict_convict")]
    ])
    await bot.answer_callback_query(callback_query.id, "Судья готов выносить вердикт.")
    await bot.send_message(chat_id, f"Судья {get_mention(caller)} готов вынести решение. Если хотите — судья может выбрать один из вариантов ниже.", parse_mode=ParseMode.HTML, reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("verdict_"))
async def cb_verdict(callback_query: CallbackQuery):
    verdict_choice = callback_query.data.split("_", 1)[1]  # convict / acquit
    chat_id = callback_query.message.chat.id
    caller = callback_query.from_user

    async with GAMES_LOCK:
        game = GAMES.get(chat_id)
        if not game:
            await bot.answer_callback_query(callback_query.id, "Игра не найдена.")
            return
        judge_id = game.get("judge_id")
        if caller.id != judge_id:
            await bot.answer_callback_query(callback_query.id, "Только судья может подтверждать вердикт.")
            return

        # формируем текст вердикта
        if verdict_choice == "acquit":
            result_text = "Судья решил: Оправдать подсудимого."
        else:
            result_text = "Судья решил: Осудить подсудимого."

        game["stage"] = "finished"

    await bot.answer_callback_query(callback_query.id, "Вердикт записан.")
    await bot.send_message(chat_id, result_text, parse_mode=ParseMode.MARKDOWN)
    # после вердикта предложим начать новый раунд или завершить игру
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Новый раунд", callback_data="start_round")],
        [InlineKeyboardButton(text="Завершить игру", callback_data="end_game")]
    ])
    await bot.send_message(chat_id, "Дальше: выбрать один из вариантов.", reply_markup=kb)

@dp.callback_query(lambda c: c.data == "end_game")
async def cb_end_game(callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    async with GAMES_LOCK:
        if chat_id in GAMES:
            del GAMES[chat_id]
    await bot.answer_callback_query(callback_query.id, "Игра завершена и состояние удалено.")
    await bot.send_message(chat_id, "Игра завершена. Спасибо за участие! Для новой игры используйте /newgame")

# --- Команда для показа статуса (опционально) ---
@dp.message(Command("status"))
async def cmd_status(message: Message):
    chat_id = message.chat.id
    async with GAMES_LOCK:
        game = GAMES.get(chat_id)
        if not game:
            await message.reply("Игра в этом чате не запущена.")
            return
        players = game["players"]
        lines = [f"{pdata['name']} — {pdata.get('role','(не назначена)')}" for uid,pdata in players.items()]
        await message.reply("Текущие игроки:\n" + "\n".join(lines))

# --- Обработка ошибок (логирование) ---
@dp.error()
async def handle_errors(event, exception):
    logger.exception("Ошибка: %s", exception)
    return True

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())