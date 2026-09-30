import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ---------- Конфиг ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
# Render даёт переменную RENDER_EXTERNAL_URL, например https://mybot.onrender.com
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://your-app.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 10000))

SCHEDULE_FILE = "schedule.csv"
USERS_FILE = "users.json"
ADMIN_IDS = {123456789}  # <-- сюда впиши Telegram ID админов

# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Хранилище пользователей (простое JSON-файло) ----------
def load_users() -> dict:
    if Path(USERS_FILE).exists():
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_users(users: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

users_db = load_users()

# ---------- Загрузка расписания ----------
def load_schedule() -> pd.DataFrame:
    df = pd.read_csv(SCHEDULE_FILE, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df["date_o"] = df["date_o"].astype(str).str.strip()
    return df

def format_date(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")

def get_schedule_for_date(date_str: str) -> pd.DataFrame:
    df = load_schedule()
    return df[df["date_o"] == date_str].reset_index(drop=True)

def render_schedule(date_str: str, weekday: str, df: pd.DataFrame, title: str) -> str:
    if df.empty:
        return f"{title} ({date_str} - {weekday}):\n\nНа этот день пар нет."
    lines = [f"{title} ({date_str} - {weekday}):", "", f"Всего пар сегодня: {len(df)}", ""]
    for i, row in df.iterrows():
        lines.append(f"{i+1}. {row['discipline']} / {row['type_disc']} - {row['audience']}")
        lines.append(f"{row['teacher']}")
        lines.append(f"{row['timestamp_lesson']}")
        lines.append("")
    return "\n".join(lines).strip()

# ---------- Клавиатуры ----------
def group_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ИОЗ-221", callback_data="set_group:group221")],
        [InlineKeyboardButton(text="ИОЗ-222", callback_data="set_group:group222")],
    ])

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Сегодня", callback_data="day:today"),
            InlineKeyboardButton(text="Завтра", callback_data="day:tomorrow"),
        ],
        [InlineKeyboardButton(text="Указать свою дату", callback_data="day:custom")],
    ])

# ---------- FSM ----------
class UserStates(StatesGroup):
    choosing_group = State()
    custom_date = State()

# ---------- Инициализация ----------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

START_TEXT = "{group}. Расписание нужно на...:"

# ---------- /start ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    uid = str(message.from_user.id)
    if uid not in users_db:
        await state.set_state(UserStates.choosing_group)
        await message.answer(
            "Привет! 👋\nК какой группе ты относишься?",
            reply_markup=group_keyboard(),
        )
    else:
        group = users_db[uid]["group"]
        await message.answer(
            START_TEXT.format(group=group),
            reply_markup=main_menu_keyboard(),
        )

# ---------- Выбор группы ----------
@dp.callback_query(F.data.startswith("set_group:"))
async def set_group(cb: CallbackQuery, state: FSMContext):
    tag = cb.data.split(":")[1]  # group221 / group222
    display = "ИОЗ-221" if tag == "group221" else "ИОЗ-222"
    uid = str(cb.from_user.id)
    users_db[uid] = {"group": display, "tag": tag}
    save_users(users_db)
    await state.clear()
    await cb.message.edit_text(f"✅ Твоя группа: <b>{display}</b> (тег: <code>{tag}</code>)")
    await cb.message.answer(
        START_TEXT.format(group=display),
        reply_markup=main_menu_keyboard(),
    )
    await cb.answer()

# ---------- Обработка кнопок дней ----------
@dp.callback_query(F.data == "day:today")
async def day_today(cb: CallbackQuery, state: FSMContext):
    uid = str(cb.from_user.id)
    if uid not in users_db:
        await cb.answer("Сначала выбери группу через /start", show_alert=True)
        return
    dt = datetime.now()
    date_str = format_date(dt)
    weekday = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"][dt.weekday()]
    df = get_schedule_for_date(date_str)
    text = render_schedule(date_str, weekday, df, "Расписание на сегодня")
    await cb.message.edit_text(text)
    await cb.message.answer("Что-нибудь ещё?", reply_markup=main_menu_keyboard())
    await cb.answer()

@dp.callback_query(F.data == "day:tomorrow")
async def day_tomorrow(cb: CallbackQuery, state: FSMContext):
    uid = str(cb.from_user.id)
    if uid not in users_db:
        await cb.answer("Сначала выбери группу через /start", show_alert=True)
        return
    dt = datetime.now() + timedelta(days=1)
    date_str = format_date(dt)
    weekday = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"][dt.weekday()]
    df = get_schedule_for_date(date_str)
    text = render_schedule(date_str, weekday, df, "Расписание на завтра")
    await cb.message.edit_text(text)
    await cb.message.answer("Что-нибудь ещё?", reply_markup=main_menu_keyboard())
    await cb.answer()

@dp.callback_query(F.data == "day:custom")
async def day_custom(cb: CallbackQuery, state: FSMContext):
    uid = str(cb.from_user.id)
    if uid not in users_db:
        await cb.answer("Сначала выбери группу через /start", show_alert=True)
        return
    await state.set_state(UserStates.custom_date)
    await cb.message.answer("Введи дату в формате <b>ДД.ММ.ГГГГ</b>, например 05.11.2025")
    await cb.answer()

@dp.message(UserStates.custom_date)
async def process_custom_date(message: Message, state: FSMContext):
    txt = message.text.strip()
    try:
        dt = datetime.strptime(txt, "%d.%m.%Y")
    except ValueError:
        await message.answer("❌ Неверный формат. Попробуй ещё раз: ДД.ММ.ГГГГ")
        return
    date_str = format_date(dt)
    weekday = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"][dt.weekday()]
    df = get_schedule_for_date(date_str)
    text = render_schedule(date_str, weekday, df, "Расписание")
    await state.clear()
    await message.answer(text)
    await message.answer("Что-нибудь ещё?", reply_markup=main_menu_keyboard())

# ---------- Админ: смена группы ----------
@dp.message(F.text.startswith("/setgroup"))
async def admin_setgroup(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет прав.")
        return
    # /setgroup <user_id> <group221|group222>
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Использование: /setgroup <user_id> <group221|group222>")
        return
    target_id, tag = parts[1], parts[2]
    if tag not in ("group221", "group222"):
        await message.answer("Тег должен быть group221 или group222")
        return
    display = "ИОЗ-221" if tag == "group221" else "ИОЗ-222"
    users_db[target_id] = {"group": display, "tag": tag}
    save_users(users_db)
    await message.answer(f"✅ Пользователю {target_id} установлена группа {display}")

# ---------- Webhook-сервер ----------
async def on_startup(bot: Bot):
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

async def on_shutdown(bot: Bot):
    await bot.delete_webhook()

def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    # health-check для Render
    async def health(request):
        return web.Response(text="OK")
    app.router.add_get("/", health)

    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)

if __name__ == "__main__":
    main()