import os
import json
import csv
import io
import logging
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# ---------- Конфиг ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://your-app.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 10000))

SCHEDULE_FILE = "schedule.csv"
USERS_FILE = "users.json"

# Bootstrap-админы: эти ID всегда считаются админами, независимо от users.json.
# Впиши свой Telegram ID (узнать можно командой /getid).
ADMIN_IDS = {123456789}

WEEKDAYS_RU = [
    "Понедельник", "Вторник", "Среда",
    "Четверг", "Пятница", "Суббота", "Воскресенье",
]

# Кодировки, которые пробуем по очереди при чтении CSV
ENCODINGS_TO_TRY = ("utf-8-sig", "cp1251", "utf-8")

# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Хранилище пользователей ----------
def load_users() -> dict:
    if Path(USERS_FILE).exists():
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось прочитать {USERS_FILE}: {e}")
    return {}

def save_users(users: dict):
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Не удалось сохранить {USERS_FILE}: {e}")

users_db = load_users()

# ---------- Роли ----------
def is_admin(user_id: int) -> bool:
    """Админ = либо в ADMIN_IDS (bootstrap), либо role='admin' в users.json."""
    if user_id in ADMIN_IDS:
        return True
    rec = users_db.get(str(user_id))
    return bool(rec and rec.get("role") == "admin")

def ensure_admin_registered(user_id: int):
    """Если ID в ADMIN_IDS — гарантированно записать его в users_db как админа."""
    if user_id not in ADMIN_IDS:
        return
    uid = str(user_id)
    rec = users_db.get(uid, {})
    rec.setdefault("group", "—")
    rec.setdefault("tag", "admin")
    rec["role"] = "admin"
    users_db[uid] = rec
    save_users(users_db)

# ---------- Работа с расписанием ----------
def format_date(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")

def _read_schedule_rows() -> list[dict]:
    """Читает CSV с автоопределением кодировки и разделителя."""
    path = Path(SCHEDULE_FILE)
    if not path.exists():
        logger.error(f"Файл {SCHEDULE_FILE} не найден")
        return []

    raw = path.read_bytes()

    text = None
    used_encoding = None
    for enc in ENCODINGS_TO_TRY:
        try:
            text = raw.decode(enc)
            used_encoding = enc
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        logger.error(
            f"Не удалось декодировать {SCHEDULE_FILE} ни одной из кодировок {ENCODINGS_TO_TRY}"
        )
        return []

    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")

    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delimiter = dialect.delimiter
    except csv.Error:
        first_line = sample.splitlines()[0] if sample else ""
        delimiter = ";" if ";" in first_line else ","

    logger.info(f"CSV {SCHEDULE_FILE}: encoding={used_encoding}, delimiter='{delimiter}'")

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames:
        reader.fieldnames = [h.strip().lstrip("\ufeff") for h in reader.fieldnames]

    rows: list[dict] = []
    for row in reader:
        clean = {
            (k.strip() if k else ""): (v or "").strip()
            for k, v in row.items()
            if k is not None
        }
        if not any(clean.values()):
            continue
        rows.append(clean)
    return rows

def get_schedule_for_date(date_str: str) -> list[dict]:
    result = []
    for r in _read_schedule_rows():
        if r.get("date_o") != date_str:
            continue
        disc = (r.get("discipline") or "").strip()
        if disc in ("", "—", "-"):
            continue
        result.append(r)
    return result

def render_schedule(date_str: str, weekday: str, rows: list[dict], title: str) -> str:
    if not rows:
        return f"{title} ({date_str} - {weekday}):\n\nНа этот день пар нет."
    lines = [
        f"{title} ({date_str} - {weekday}):",
        "",
        f"Всего пар сегодня: {len(rows)}",
        "",
    ]
    for i, row in enumerate(rows, start=1):
        discipline = row.get("discipline", "—") or "—"
        type_disc = row.get("type_disc", "—") or "—"
        audience = row.get("audience", "—") or "—"
        teacher = row.get("teacher", "—") or "—"
        ts = row.get("timestamp_lesson", "—") or "—"
        lines.append(f"{i}. {discipline} / {type_disc} - {audience}")
        lines.append(teacher)
        lines.append(ts)
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

# ---------- Инициализация бота ----------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

START_TEXT = "{group}. Расписание нужно на...:"

# ---------- /start ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    uid = str(message.from_user.id)
    ensure_admin_registered(message.from_user.id)

    if uid not in users_db:
        await state.set_state(UserStates.choosing_group)
        await message.answer(
            "Привет! 👋\nК какой группе ты относишься?",
            reply_markup=group_keyboard(),
        )
    else:
        group = users_db[uid].get("group", "—")
        await message.answer(
            START_TEXT.format(group=group),
            reply_markup=main_menu_keyboard(),
        )

# ---------- Выбор группы ----------
@dp.callback_query(F.data.startswith("set_group:"))
async def set_group(cb: CallbackQuery, state: FSMContext):
    tag = cb.data.split(":", 1)[1]
    display = "ИОЗ-221" if tag == "group221" else "ИОЗ-222"
    uid = str(cb.from_user.id)
    rec = users_db.get(uid, {})
    rec["group"] = display
    rec["tag"] = tag
    rec.setdefault("role", "user")
    users_db[uid] = rec
    save_users(users_db)
    await state.clear()
    try:
        await cb.message.edit_text(
            f"✅ Твоя группа: <b>{display}</b> (тег: <code>{tag}</code>)"
        )
    except Exception:
        pass
    await cb.message.answer(
        START_TEXT.format(group=display),
        reply_markup=main_menu_keyboard(),
    )
    await cb.answer()

# ---------- /getid (для всех) ----------
@dp.message(Command("getid"))
async def cmd_getid(message: Message):
    uid = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else "—"
    rec = users_db.get(str(uid), {})
    group = rec.get("group", "не выбрана")
    role = rec.get("role") or ("admin" if is_admin(uid) else "user")
    await message.answer(
        f"🆔 Твой Telegram ID: <code>{uid}</code>\n"
        f"👤 Username: {username}\n"
        f"👥 Группа: {group}\n"
        f"🎭 Роль: {role}"
    )

# ---------- /getid_all (только админ) ----------
@dp.message(Command("getid_all"))
async def cmd_getid_all(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Команда доступна только администратору.")
        return

    if not users_db:
        await message.answer("Пока никто не зарегистрировался.")
        return

    lines = [f"👥 Всего пользователей: {len(users_db)}", ""]
    for uid, rec in users_db.items():
        group = rec.get("group", "—")
        tag = rec.get("tag", "—")
        role = rec.get("role") or ("admin" if int(uid) in ADMIN_IDS else "user")
        lines.append(f"<code>{uid}</code> — {group} ({tag}) [{role}]")

    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000])

# ---------- /setgroup (только админ) ----------
@dp.message(Command("setgroup"))
async def admin_setgroup(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Использование: /setgroup <user_id> <group221|group222>")
        return
    target_id, tag = parts[1], parts[2]
    if tag not in ("group221", "group222"):
        await message.answer("Тег должен быть group221 или group222")
        return
    display = "ИОЗ-221" if tag == "group221" else "ИОЗ-222"
    rec = users_db.get(target_id, {})
    rec["group"] = display
    rec["tag"] = tag
    rec.setdefault("role", "user")
    users_db[target_id] = rec
    save_users(users_db)
    await message.answer(f"✅ Пользователю <code>{target_id}</code> установлена группа {display}")

# ---------- /grant_admin (только админ) ----------
@dp.message(Command("grant_admin"))
async def cmd_grant_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /grant_admin <user_id>")
        return
    target = parts[1]
    rec = users_db.get(target, {})
    rec["role"] = "admin"
    rec.setdefault("group", "—")
    rec.setdefault("tag", "admin")
    users_db[target] = rec
    save_users(users_db)
    await message.answer(f"✅ Пользователь <code>{target}</code> теперь админ.")

# ---------- /revoke_admin (только админ) ----------
@dp.message(Command("revoke_admin"))
async def cmd_revoke_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Использование: /revoke_admin <user_id>")
        return
    target = parts[1]
    if target.isdigit() and int(target) in ADMIN_IDS:
        await message.answer(
            "⚠️ Этот ID в ADMIN_IDS (bootstrap). Убери его из переменных окружения/кода."
        )
        return
    rec = users_db.get(target)
    if rec:
        rec["role"] = "user"
        users_db[target] = rec
        save_users(users_db)
    await message.answer(f"✅ Права админа у <code>{target}</code> сняты.")

# ---------- Хелпер: показ расписания ----------
async def show_schedule(cb: CallbackQuery, dt: datetime, title: str):
    date_str = format_date(dt)
    weekday = WEEKDAYS_RU[dt.weekday()]
    rows = get_schedule_for_date(date_str)
    text = render_schedule(date_str, weekday, rows, title)
    try:
        await cb.message.edit_text(text)
    except Exception:
        await cb.message.answer(text)
    await cb.message.answer("Что-нибудь ещё?", reply_markup=main_menu_keyboard())

# ---------- Сегодня / Завтра ----------
@dp.callback_query(F.data == "day:today")
async def day_today(cb: CallbackQuery, state: FSMContext):
    uid = str(cb.from_user.id)
    if uid not in users_db:
        await cb.answer("Сначала выбери группу через /start", show_alert=True)
        return
    await show_schedule(cb, datetime.now(), "Расписание на сегодня")
    await cb.answer()

@dp.callback_query(F.data == "day:tomorrow")
async def day_tomorrow(cb: CallbackQuery, state: FSMContext):
    uid = str(cb.from_user.id)
    if uid not in users_db:
        await cb.answer("Сначала выбери группу через /start", show_alert=True)
        return
    await show_schedule(cb, datetime.now() + timedelta(days=1), "Расписание на завтра")
    await cb.answer()

# ---------- Своя дата ----------
@dp.callback_query(F.data == "day:custom")
async def day_custom(cb: CallbackQuery, state: FSMContext):
    uid = str(cb.from_user.id)
    if uid not in users_db:
        await cb.answer("Сначала выбери группу через /start", show_alert=True)
        return
    await state.set_state(UserStates.custom_date)
    await cb.message.answer(
        "Введи дату в формате <b>ДД.ММ.ГГГГ</b>, например 05.11.2025"
    )
    await cb.answer()

@dp.message(UserStates.custom_date)
async def process_custom_date(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    try:
        dt = datetime.strptime(txt, "%d.%m.%Y")
    except ValueError:
        await message.answer("❌ Неверный формат. Попробуй ещё раз: ДД.ММ.ГГГГ")
        return
    date_str = format_date(dt)
    weekday = WEEKDAYS_RU[dt.weekday()]
    rows = get_schedule_for_date(date_str)
    text = render_schedule(date_str, weekday, rows, "Расписание")
    await state.clear()
    await message.answer(text)
    await message.answer("Что-нибудь ещё?", reply_markup=main_menu_keyboard())

# ---------- Webhook ----------
async def on_startup(bot: Bot):
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

async def on_shutdown(bot: Bot):
    try:
        await bot.delete_webhook()
    except Exception:
        pass

def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    async def health(request):
        return web.Response(text="OK")
    app.router.add_get("/", health)

    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)

if __name__ == "__main__":
    main()
