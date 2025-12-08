import asyncio
import datetime
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Dict, List

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)
from aiogram.enums import ChatType
from aiogram.enums import ParseMode

import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Проверяем наличие токена
if not config.TOKEN:
    logger.error("Токен бота не найден! Создайте файл .env с BOT_TOKEN=your_token_here")
    exit(1)

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

# --- Константы для очистки ---
GAME_TIMEOUT = 3600  # 1 час в секундах
CLEANUP_INTERVAL = 300  # 5 минут в секундах

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
#   "last_activity": timestamp,  # время последней активности
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

# --- Функции очистки ---
async def cleanup_old_games():
    """Очищает игры, которые неактивны более GAME_TIMEOUT секунд"""
    current_time = time.time()
    removed_games = []
    
    async with GAMES_LOCK:
        games_to_remove = []
        for chat_id, game in GAMES.items():
            last_activity = game.get('last_activity', current_time)
            if current_time - last_activity > GAME_TIMEOUT:
                games_to_remove.append(chat_id)
                removed_games.append(chat_id)
        
        for chat_id in games_to_remove:
            del GAMES[chat_id]
    
    if removed_games:
        logger.info(f"Очищено {len(removed_games)} неактивных игр: {removed_games}")
    
    return len(removed_games)

async def cleanup_task():
    """Периодическая задача очистки неиспользуемых игр"""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                removed_count = await cleanup_old_games()
                if removed_count > 0:
                    logger.info(f"Автоматическая очистка: удалено {removed_count} игр")
            except Exception as e:
                logger.error(f"Ошибка при очистке игр: {e}", exc_info=True)
                # Продолжаем работу даже при ошибке очистки
        except asyncio.CancelledError:
            logger.info("Задача очистки отменена")
            break
        except Exception as e:
            logger.critical(f"Критическая ошибка в cleanup_task: {e}", exc_info=True)
            # Ждем перед следующей попыткой
            await asyncio.sleep(60)

async def heartbeat_task(start_time: datetime.datetime):
    """Периодическая задача для поддержания активности бота"""
    while True:
        try:
            await asyncio.sleep(3600)  # Каждый час
            # Проверяем, что бот все еще работает
            try:
                me = await bot.get_me()
                uptime = datetime.datetime.now() - start_time
                logger.info(f"Heartbeat: бот активен (@{me.username}), работает {uptime}")
            except Exception as e:
                logger.error(f"Heartbeat: ошибка при проверке состояния бота: {e}")
                # Если не можем получить информацию о боте, значит соединение разорвано
                # Это вызовет перезапуск polling в основном цикле
                raise
        except asyncio.CancelledError:
            logger.info("Heartbeat задача отменена")
            break
        except Exception as e:
            logger.error(f"Ошибка в heartbeat_task: {e}", exc_info=True)
            await asyncio.sleep(60)  # Ждем перед следующей попыткой

async def connection_check_task():
    """Периодически проверяет соединение с Telegram API"""
    while True:
        try:
            await asyncio.sleep(1800)  # Каждые 30 минут
            try:
                # Простая проверка - получаем информацию о боте
                me = await bot.get_me()
                logger.debug(f"Проверка соединения: бот активен (@{me.username})")
            except Exception as e:
                logger.warning(f"Проблема с соединением: {e}")
                # Если соединение разорвано, это вызовет перезапуск в основном цикле
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ошибка в connection_check_task: {e}")
            await asyncio.sleep(60)

def update_game_activity(chat_id: int):
    """Обновляет время последней активности игры"""
    if chat_id in GAMES:
        GAMES[chat_id]['last_activity'] = time.time()

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
        "Игроки будут присоединяться через кнопку 'Присоединиться'.\n\n"
        "Команды:\n"
        "/newgame - создать новую игру\n"
        "/status - показать статус игры\n"
        "/cleanup - очистить неактивные игры (только для админов)",
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
            "last_activity": time.time(),
        }

    await message.reply(
        "Новая игра создана! Нажмите «Присоединиться», чтобы вступить в игру.\n"
        f"Минимум игроков: {config.MIN_PLAYERS}. Когда все присоединятся — ведущий (или админ чата) нажмёт «Закончить набор».",
        reply_markup=start_game_kb()
        )

# --- Callbacks: join / stop_join / assign roles / start round / draw witness / etc. ---
@dp.callback_query(lambda c: c.data == "join")
async def cb_join(callback_query: CallbackQuery):
    try:
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
            game["last_activity"] = time.time()

        await bot.answer_callback_query(callback_query.id, "Вы присоединились к игре.")
        await bot.send_message(chat_id, f"{get_mention(user)} присоединился к игре.", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Ошибка в cb_join: {e}", exc_info=True)
        try:
            await bot.answer_callback_query(callback_query.id, "Произошла ошибка. Попробуйте позже.")
        except:
            pass

@dp.callback_query(lambda c: c.data == "stop_join")
async def cb_stop_join(callback_query: CallbackQuery):
    try:
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

            game["last_activity"] = time.time()
            names = [p["name"] for p in game["players"].values()]
        
        txt = "Набор окончен. Игроки:\n" + "\n".join(f"- {n}" for n in names)
        await bot.answer_callback_query(callback_query.id, "Набор окончен. Можете раздать роли.")
        await bot.send_message(chat_id, txt, reply_markup=game_control_kb())
    except Exception as e:
        logger.error(f"Ошибка в cb_stop_join: {e}", exc_info=True)
        try:
            await bot.answer_callback_query(callback_query.id, "Произошла ошибка. Попробуйте позже.")
        except:
            pass

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
        game["last_activity"] = time.time()

    # Отправить приватные сообщения с ролью каждому игроку
    failed_users = []
    for uid, pdata in game["players"].items():
        try:
            await bot.send_message(uid, f"Вам назначена роль: {pdata['role']}", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning(f"Не удалось отправить приватное сообщение {uid}: {e}")
            failed_users.append(pdata['name'])
    
    # Если не удалось отправить некоторым пользователям, сообщим об этом
    if failed_users:
        await bot.send_message(chat_id, f"Не удалось отправить роли в ЛС следующим пользователям: {', '.join(failed_users)}. Они могут узнать свою роль через команду /status.")

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
        game["last_activity"] = time.time()
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
        # Проверяем существование файла
        from pathlib import Path
        instructions_path = Path("./instructions.jpg")
        
        if not instructions_path.exists():
            await bot.answer_callback_query(callback_query.id, "Файл инструкции не найден.")
            return
            
        # Отправка файла
        from aiogram.types import FSInputFile
        pdf_file = FSInputFile(instructions_path)
        await bot.send_photo(chat_id=chat_id, photo=pdf_file, caption="Инструкция по игре")
        await bot.answer_callback_query(callback_query.id, "Инструкция отправлена.")
    except Exception as e:
        logger.error(f"Ошибка при отправке инструкции: {e}")
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
        game["last_activity"] = time.time()

    # отправить приватно текст свидетелю
    try:
        witness_title = witness.get("title", "Карта свидетеля")
        witness_text = witness.get("text", "")
        await bot.send_message(user.id, f"Ваша кураторская карта свидетеля:\n\n<b>{witness_title}</b>\n\n{witness_text}", parse_mode=ParseMode.HTML)
        await bot.answer_callback_query(callback_query.id, "Карта отправлена вам в личные сообщения.")
    except Exception as e:
        logger.warning(f"Не удалось отправить свидетелю приватную карту {user.id}: {e}")
        # если нельзя писать приватно, отправим в чат с упоминанием (без раскрытия всей карты)
        await bot.send_message(chat_id, f"{get_mention(user)} вытянул(а) карту свидетеля (карта отправлена в ЛС или недоступна).", parse_mode=ParseMode.HTML)
        await bot.answer_callback_query(callback_query.id, "Карта отправлена в чат (ЛС недоступны).")

@dp.callback_query(lambda c: c.data == "start_debate")
async def cb_start_debate(callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    async with GAMES_LOCK:
        game = GAMES.get(chat_id)
        if not game or not game.get("current_situation"):
            await bot.answer_callback_query(callback_query.id, "Нет активного раунда.")
            return
        game["stage"] = "debate"
        game["last_activity"] = time.time()
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
        game["last_activity"] = time.time()
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

# --- Команда для очистки неактивных игр (только для админов) ---
@dp.message(Command("cleanup"))
async def cmd_cleanup(message: Message):
    # Проверяем, что команда отправлена в приватном чате или от админа
    if message.chat.type != ChatType.PRIVATE:
        # В групповом чате проверяем права админа
        try:
            chat_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
            if chat_member.status not in ['creator', 'administrator']:
                await message.reply("Только администраторы могут использовать эту команду.")
                return
        except:
            await message.reply("Не удалось проверить права администратора.")
            return
    
    removed_count = await cleanup_old_games()
    await message.reply(f"Очистка завершена. Удалено {removed_count} неактивных игр.")

# --- Обработка ошибок (логирование) ---
@dp.error()
async def handle_errors(update: types.Update, exception: Exception):
    """Глобальный обработчик ошибок"""
    try:
        # Логируем ошибку
        logger.error(f"Ошибка при обработке обновления {update}: {exception}", exc_info=True)
        
        # Обрабатываем специфичные ошибки Telegram
        if isinstance(exception, Exception):
            error_str = str(exception)
            
            # FloodWait - слишком много запросов
            if "FloodWait" in error_str or "429" in error_str:
                logger.warning(f"FloodWait обнаружен: {exception}")
                return True  # Игнорируем, Telegram сам обработает
            
            # ChatNotFound - чат не найден
            if "ChatNotFound" in error_str or "chat not found" in error_str.lower():
                logger.warning(f"Чат не найден: {exception}")
                return True
            
            # RetryAfter - нужно подождать
            if "RetryAfter" in error_str:
                logger.warning(f"RetryAfter: {exception}")
                return True
            
            # Timeout - таймаут соединения
            if "timeout" in error_str.lower() or "timed out" in error_str.lower():
                logger.warning(f"Таймаут: {exception}")
                return True
        
        # Для всех остальных ошибок возвращаем True, чтобы бот продолжал работать
        return True
    except Exception as e:
        logger.critical(f"Критическая ошибка в обработчике ошибок: {e}")
        return True

async def main():
    start_time = datetime.datetime.now()
    logger.info(f"Бот запущен в {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Запускаем health check сервер в фоне (для Render и Railway)
    health_server_thread = None
    if os.environ.get('RENDER') or os.environ.get('PORT'):
        try:
            import threading
            from health_check import start_health_server
            health_server_thread = threading.Thread(target=start_health_server, daemon=True)
            health_server_thread.start()
            platform = "Render" if os.environ.get('RENDER') else "Railway"
            logger.info(f"Health check server started for {platform}")
        except Exception as e:
            logger.warning(f"Failed to start health check server: {e}")
    
    # Запускаем задачу очистки в фоне
    cleanup_task_handle = asyncio.create_task(cleanup_task())
    
    # Запускаем heartbeat задачу
    heartbeat_task_handle = asyncio.create_task(heartbeat_task(start_time))
    
    # Запускаем задачу проверки соединения
    connection_check_handle = asyncio.create_task(connection_check_task())
    
    # Бесконечный цикл перезапуска при ошибках
    retry_delay = 30  # секунд
    consecutive_errors = 0
    max_consecutive_errors = 5
    is_first_start = True  # Флаг для первого запуска

    try:
        while True:
            try:
                logger.info("Запуск бота...")
                # При первом запуске очищаем старые обновления, при перезапуске - не сбрасываем
                # close_bot_session=False - не закрываем сессию при перезапуске
                await dp.start_polling(
                    bot,
                    drop_pending_updates=is_first_start,  # Очищаем только при первом запуске
                    allowed_updates=["message", "callback_query"],
                    close_bot_session=False,  # Важно для перезапуска
                    polling_timeout=30,  # Таймаут для каждого запроса getUpdates
                    request_timeout=30,   # Таймаут для HTTP запросов
                )
                # Если polling завершился без ошибки (например, KeyboardInterrupt)
                break
            except KeyboardInterrupt:
                logger.info("Бот остановлен пользователем")
                break
            except Exception as e:
                error_msg = str(e)
                consecutive_errors += 1
                is_first_start = False  # После первой ошибки это уже не первый запуск

                # Логируем ошибку
                logger.error(f"Ошибка при работе бота (ошибка #{consecutive_errors}): {e}", exc_info=True)
                
                # Проверяем тип ошибки
                is_critical = False
                
                # Специальная обработка различных типов ошибок
                if "Conflict" in error_msg or "getUpdates" in error_msg:
                    logger.warning("Обнаружен конфликт с другим экземпляром бота")
                    logger.warning("Убедитесь, что запущен только один экземпляр бота на Render!")
                    is_critical = True
                elif "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                    logger.warning("Таймаут соединения - это нормально при долгом простое")
                    consecutive_errors = 0  # Таймауты не считаем критическими
                elif "Connection" in error_msg or "network" in error_msg.lower():
                    logger.warning("Проблема с сетью - переподключение...")
                    consecutive_errors = 0  # Сетевые ошибки не критичны
                elif consecutive_errors >= max_consecutive_errors:
                    logger.critical(f"Слишком много последовательных ошибок ({consecutive_errors})")
                    is_critical = True
                
                # Если критическая ошибка или слишком много ошибок подряд
                if is_critical and consecutive_errors >= max_consecutive_errors:
                    logger.error("Критическая ошибка - увеличенная задержка перед перезапуском")
                    retry_delay = min(retry_delay * 2, 600)  # До 10 минут
                else:
                    # Для обычных ошибок используем стандартную задержку
                    retry_delay = 30
                
                # Ждем перед перезапуском
                logger.info(f"Перезапуск через {retry_delay} секунд...")
                await asyncio.sleep(retry_delay)
                
                # Сбрасываем счетчик при успешном перезапуске
                if consecutive_errors < max_consecutive_errors:
                    consecutive_errors = 0
    finally:
        # Останавливаем все задачи
        cleanup_task_handle.cancel()
        heartbeat_task_handle.cancel()
        connection_check_handle.cancel()
        try:
            await cleanup_task_handle
            await heartbeat_task_handle
            await connection_check_handle
        except asyncio.CancelledError:
            pass
        
        # Закрываем сессию бота
        try:
            await bot.session.close()
        except:
            pass
        
        logger.info("Бот остановлен.")

if __name__ == "__main__":
    asyncio.run(main())
