import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, BotCommand
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiohttp import web

# ================= SETTINGS =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "BOT_TOKEN_BU_YERGA")
DB_PATH = "anime_bot.db"

ANNOUNCE_CHANNEL_ID = -1004371894042
ANNOUNCE_CHANNEL_LINK = "https://t.me/An1Zen_an1me"
BOT_USERNAME = "An1Zen_bot"
BOT_ID = 8904140298
OWNER_ID = 8470314807
OWNER_USERNAME = "@anituz_org"
INITIAL_ADMIN_IDS = [OWNER_ID]
PORT = int(os.environ.get("PORT", 10000))
ONLINE_WINDOW_MIN = 5

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)


# ================= STATES =================
class SearchStates(StatesGroup):
    waiting_query = State()


class AdminFSM(StatesGroup):
    waiting_admin_add_id = State()
    waiting_admin_remove_id = State()
    waiting_delete_anime = State()
    waiting_delete_episode = State()
    waiting_edit_old_code = State()
    waiting_edit_new_code = State()
    waiting_force_channel = State()
    waiting_force_remove = State()
    waiting_post_video = State()
    waiting_post_meta = State()


# ================= DATABASE =================
def db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS animes (
        code TEXT PRIMARY KEY, title TEXT, total_seasons INTEGER DEFAULT 1,
        quality TEXT, genre TEXT, rating TEXT,
        views INTEGER DEFAULT 0, downloads INTEGER DEFAULT 0,
        poster_file_id TEXT, added_date TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS episodes (
        anime_code TEXT, season_number INTEGER, episode_number INTEGER,
        file_id TEXT, caption TEXT DEFAULT '',
        PRIMARY KEY (anime_code, season_number, episode_number)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, joined_date TEXT,
        last_seen TEXT, blocked INTEGER DEFAULT 0
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS history (
        user_id INTEGER, anime_code TEXT, season_number INTEGER,
        episode_number INTEGER, watched_date TEXT,
        PRIMARY KEY (user_id, anime_code, season_number, episode_number)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY, username TEXT, added_date TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS required_channels (
        channel_id TEXT PRIMARY KEY, title TEXT, link TEXT,
        added_date TEXT
    )""")

    # Safe migrations for an existing database.
    for sql in [
        "ALTER TABLE episodes ADD COLUMN caption TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0",
    ]:
        try:
            cur.execute(sql)
        except sqlite3.OperationalError:
            pass

    cur.execute("SELECT COUNT(*) FROM admins")
    if cur.fetchone()[0] == 0:
        for aid in INITIAL_ADMIN_IDS:
            cur.execute(
                "INSERT OR IGNORE INTO admins (user_id, username, added_date) VALUES (?,?,?)",
                (aid, OWNER_USERNAME, datetime.now().isoformat()),
            )
    else:
        # Always protect/restore the owner account.
        cur.execute(
            "INSERT OR IGNORE INTO admins (user_id, username, added_date) VALUES (?,?,?)",
            (OWNER_ID, OWNER_USERNAME, datetime.now().isoformat()),
        )

    conn.commit()
    conn.close()


def db(query, params=(), fetch=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    result = None
    if fetch == "one":
        result = cur.fetchone()
    elif fetch == "all":
        result = cur.fetchall()
    conn.commit()
    conn.close()
    return result


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or bool(
        db("SELECT 1 FROM admins WHERE user_id=?", (user_id,), "one")
    )


def ensure_user(user_id: int, username: str):
    now = datetime.now().isoformat()
    row = db("SELECT 1 FROM users WHERE user_id=?", (user_id,), "one")
    if not row:
        db(
            "INSERT INTO users (user_id, username, joined_date, last_seen, blocked) VALUES (?,?,?,?,0)",
            (user_id, username or "", now),
        )
    else:
        db(
            "UPDATE users SET username=?, last_seen=?, blocked=0 WHERE user_id=?",
            (username or "", now, user_id),
        )


def mark_blocked(user_id: int):
    db("UPDATE users SET blocked=1 WHERE user_id=?", (user_id,))


# ================= FORCED SUBSCRIPTION =================
def required_channels():
    return db(
        "SELECT channel_id, title, link FROM required_channels ORDER BY added_date",
        (), "all"
    )


async def subscription_keyboard():
    rows = []
    for channel_id, title, link in required_channels():
        rows.append([InlineKeyboardButton(text="✅ Kanal obuna ✅", url=link)])
    rows.append([InlineKeyboardButton(text="🔎 Tekshirish", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def is_subscribed_to_all(user_id: int) -> bool:
    channels = required_channels()
    if not channels:
        return True
    for channel_id, _, _ in channels:
        try:
            member = await bot.get_chat_member(int(channel_id), user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception as e:
            logging.warning("Subscription check failed for %s: %s", channel_id, e)
            # If Telegram cannot check a configured channel, fail closed for safety.
            return False
    return True


async def require_subscription(message: Message) -> bool:
    if is_admin(message.from_user.id):
        return True
    if await is_subscribed_to_all(message.from_user.id):
        return True
    kb = await subscription_keyboard()
    await message.answer(
        "⚠️ <b>Botdan foydalanish uchun quyidagi kanallarga obuna bo‘ling:</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return False


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery):
    ensure_user(callback.from_user.id, callback.from_user.username)
    if await is_subscribed_to_all(callback.from_user.id):
        await callback.message.answer("✅ Barcha kanallarga obuna bo‘lgansiz. Endi anime kodini yuborishingiz mumkin.")
        await callback.answer("✅ Tekshirildi")
    else:
        kb = await subscription_keyboard()
        await callback.message.answer(
            "❌ Hali barcha kanallarga obuna bo‘lmagansiz.", reply_markup=kb
        )
        await callback.answer("❌ Obuna yetarli emas", show_alert=True)


# ================= KEYBOARDS =================
def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👑 Admin qo‘shish/olish"), KeyboardButton(text="👥 Foydalanuvchilar")],
            [KeyboardButton(text="🎬 Anime qo‘shish"), KeyboardButton(text="🗑 Anime olish")],
            [KeyboardButton(text="➕ Qism qo‘shish"), KeyboardButton(text="🗑 Qism olish")],
            [KeyboardButton(text="🔄 Kodni olish/o‘zgartirish"), KeyboardButton(text="📋 Kodlar ro‘yxati")],
            [KeyboardButton(text="📢 Qism post qilish"), KeyboardButton(text="🔔 Majburiy obuna")],
        ], resize_keyboard=True
    )


def admin_manage_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Admin qo‘shish", callback_data="adm:add")],
        [InlineKeyboardButton(text="➖ Admin olish", callback_data="adm:remove")],
        [InlineKeyboardButton(text="📋 Adminlar ro‘yxati", callback_data="adm:list")],
    ])


def force_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Kanal qo‘shish", callback_data="force:add")],
        [InlineKeyboardButton(text="➖ Kanal olish", callback_data="force:remove")],
        [InlineKeyboardButton(text="📋 Kanallar ro‘yxati", callback_data="force:list")],
    ])


def seasons_keyboard(code: str, total_seasons: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎞 {s}-fasl", callback_data=f"season:{code}:{s}")]
        for s in range(1, total_seasons + 1)
    ])


def episodes_keyboard(code: str, season: int, total_ep: int):
    buttons, row = [], []
    for i in range(1, total_ep + 1):
        row.append(InlineKeyboardButton(text=f"▶️ {i}", callback_data=f"ep:{code}:{season}:{i}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def episode_count_kb(code: str, season: int):
    total_ep = db(
        "SELECT COUNT(*) FROM episodes WHERE anime_code=? AND season_number=?",
        (code, season), "one"
    )[0]
    return episodes_keyboard(code, season, total_ep)


# ================= ANIME VIEW =================
async def show_anime(message: Message, code: str, user_id: int):
    anime = db(
        "SELECT code,title,total_seasons,quality,genre,rating,views,downloads,poster_file_id "
        "FROM animes WHERE code=?", (code,), "one"
    )
    if not anime:
        await message.answer("❌ Bunday kodli anime topilmadi.")
        return
    acode, title, total_seasons, quality, genre, rating, views, downloads, poster = anime
    db("UPDATE animes SET views=views+1 WHERE code=?", (acode,))
    caption = (
        f"🎬 <b>{title}</b>\n━━━━━━━━━━━━━━━\n"
        f"🎞 Fasllar: {total_seasons}\n💿 Sifat: {quality}\n"
        f"⭐️ Reyting: {rating or '—'}\n🏷 Janr: {genre}\n"
        f"━━━━━━━━━━━━━━━\n👀 Ko‘rilgan: {views + 1}"
    )
    kb = seasons_keyboard(acode, total_seasons) if total_seasons > 1 else episode_count_kb(acode, 1)
    if poster:
        try:
            await message.answer_photo(poster, caption=caption, parse_mode="HTML", reply_markup=kb)
        except TelegramBadRequest:
            await message.answer(caption, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(caption, parse_mode="HTML", reply_markup=kb)


# ================= START / SEARCH =================
@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    ensure_user(message.from_user.id, message.from_user.username)

    if not await require_subscription(message):
        return

    if command.args:
        arg = command.args.strip()
        # Episode deep link: /start ep_CODE_SEASON_EPISODE
        if arg.startswith("ep_"):
            parts = arg.split("_")
            if len(parts) == 4:
                _, code, season, num = parts
                try:
                    season, num = int(season), int(num)
                    ep = db("SELECT file_id,caption FROM episodes WHERE anime_code=? AND season_number=? AND episode_number=?",
                             (code, season, num), "one")
                    if ep:
                        anime = db("SELECT title FROM animes WHERE code=?", (code,), "one")
                        ep_caption = ep[1] or (f"🎬 {anime[0]}\n🎞 {season}-fasl, {num}-qism" if anime else f"🎞 {num}-qism")
                        await bot.send_video(message.chat.id, ep[0], caption=ep_caption)
                        if is_admin(message.from_user.id):
                            await message.answer("🛠 Admin panel", reply_markup=admin_menu())
                        return
                except ValueError:
                    pass
        if db("SELECT 1 FROM animes WHERE code=?", (arg,), "one"):
            await show_anime(message, arg, message.from_user.id)
            if is_admin(message.from_user.id):
                await message.answer("🛠 Admin panel", reply_markup=admin_menu())
            return

    if is_admin(message.from_user.id):
        await message.answer("🎬 Xush kelibsiz!", reply_markup=admin_menu())
    else:
        await message.answer("🎬 Xush kelibsiz!", reply_markup=ReplyKeyboardRemove())


async def _do_search(message: Message, query: str):
    if not await require_subscription(message):
        return
    exact = db("SELECT code FROM animes WHERE code=?", (query,), "one")
    if exact:
        await show_anime(message, exact[0], message.from_user.id)
        return
    matches = db("SELECT code,title FROM animes WHERE title LIKE ?", (f"%{query}%",), "all")
    if not matches:
        await message.answer("❌ Hech narsa topilmadi.")
        return
    if len(matches) == 1:
        await show_anime(message, matches[0][0], message.from_user.id)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=f"open:{c}")] for c, t in matches[:20]
    ])
    await message.answer("Bir nechta natija topildi:", reply_markup=kb)


@router.callback_query(F.data.startswith("open:"))
async def cb_open(callback: CallbackQuery):
    if not await is_subscribed_to_all(callback.from_user.id) and not is_admin(callback.from_user.id):
        await callback.answer("Avval majburiy kanallarga obuna bo‘ling.", show_alert=True)
        return
    code = callback.data.split(":", 1)[1]
    await show_anime(callback.message, code, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("season:"))
async def cb_season(callback: CallbackQuery):
    if not await is_subscribed_to_all(callback.from_user.id) and not is_admin(callback.from_user.id):
        await callback.answer("Avval majburiy kanallarga obuna bo‘ling.", show_alert=True)
        return
    _, code, season = callback.data.split(":")
    await callback.message.answer(f"🎞 {season}-fasl qismlari:", reply_markup=episode_count_kb(code, int(season)))
    await callback.answer()


@router.callback_query(F.data.startswith("ep:"))
async def cb_episode(callback: CallbackQuery):
    if not await is_subscribed_to_all(callback.from_user.id) and not is_admin(callback.from_user.id):
        await callback.answer("Avval majburiy kanallarga obuna bo‘ling.", show_alert=True)
        return
    _, code, season, num = callback.data.split(":")
    season, num = int(season), int(num)
    ensure_user(callback.from_user.id, callback.from_user.username)
    ep = db(
        "SELECT file_id,caption FROM episodes WHERE anime_code=? AND season_number=? AND episode_number=?",
        (code, season, num), "one"
    )
    if not ep:
        await callback.answer("❌ Bu qism hali yuklanmagan.", show_alert=True)
        return
    file_id, ep_caption = ep
    anime = db("SELECT title FROM animes WHERE code=?", (code,), "one")
    caption = ep_caption or (f"🎬 {anime[0]}\n🎞 {season}-fasl, {num}-qism" if anime else f"🎞 {num}-qism")
    try:
        await bot.send_video(callback.message.chat.id, file_id, caption=caption)
        db("UPDATE animes SET downloads=downloads+1 WHERE code=?", (code,))
        db("INSERT OR REPLACE INTO history VALUES (?,?,?,?,?)",
           (callback.from_user.id, code, season, num, datetime.now().isoformat()))
        await callback.answer()
    except TelegramBadRequest as e:
        logging.error(e)
        await callback.answer("⚠️ Xatolik yuz berdi.", show_alert=True)


# ================= ADMIN: ADMINS =================
@router.message(F.text == "👑 Admin qo‘shish/olish")
async def btn_admin_manage(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer("👑 Admin boshqaruvi:", reply_markup=admin_manage_kb())


@router.callback_query(F.data == "adm:add")
async def adm_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminFSM.waiting_admin_add_id)
    await callback.message.answer("➕ Yangi adminning Telegram ID raqamini yuboring:")
    await callback.answer()


@router.message(StateFilter(AdminFSM.waiting_admin_add_id))
async def adm_add_process(message: Message, state: FSMContext):
    await state.clear()
    try:
        uid = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("❗️ Faqat raqamli ID yuboring.")
        return
    try:
        chat = await bot.get_chat(uid)
        username = f"@{chat.username}" if chat.username else chat.full_name
    except Exception:
        username = str(uid)
    db("INSERT OR REPLACE INTO admins(user_id,username,added_date) VALUES(?,?,?)",
       (uid, username, datetime.now().isoformat()))
    await message.answer(f"✅ {username} ({uid}) admin qilindi.", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:remove")
async def adm_remove(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminFSM.waiting_admin_remove_id)
    await callback.message.answer("➖ Olib tashlanadigan admin ID'sini yuboring:")
    await callback.answer()


@router.message(StateFilter(AdminFSM.waiting_admin_remove_id))
async def adm_remove_process(message: Message, state: FSMContext):
    await state.clear()
    try: uid = int(message.text.strip())
    except (ValueError, AttributeError):
        await message.answer("❗️ ID raqam bo‘lishi kerak."); return
    if uid == OWNER_ID:
        await message.answer("❌ Asosiy adminni olib bo‘lmaydi."); return
    db("DELETE FROM admins WHERE user_id=?", (uid,))
    await message.answer(f"✅ {uid} adminlikdan olindi.", reply_markup=admin_menu())


@router.callback_query(F.data == "adm:list")
async def adm_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    admins = db("SELECT user_id,username FROM admins ORDER BY added_date", (), "all")
    if not admins:
        await callback.message.answer("Adminlar yo‘q."); await callback.answer(); return
    text = "👑 <b>Adminlar ro‘yxati</b>\n\n"
    for i, (uid, uname) in enumerate(admins, 1):
        text += f"{i}. {uname or 'username yo‘q'} — <code>{uid}</code>\n"
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


# ================= ADMIN: USER STATISTICS =================
@router.message(F.text == "👥 Foydalanuvchilar")
async def btn_users(message: Message):
    if not is_admin(message.from_user.id): return
    now = datetime.now()
    cutoff = (now - timedelta(minutes=ONLINE_WINDOW_MIN)).isoformat()
    total = db("SELECT COUNT(*) FROM users", (), "one")[0]
    blocked = db("SELECT COUNT(*) FROM users WHERE blocked=1", (), "one")[0]
    online = db("SELECT COUNT(*) FROM users WHERE last_seen>=? AND blocked=0", (cutoff,), "one")[0]
    admins = db("SELECT COUNT(*) FROM admins", (), "one")[0]
    online_admins = db("SELECT COUNT(*) FROM users u JOIN admins a ON u.user_id=a.user_id WHERE u.last_seen>=? AND u.blocked=0", (cutoff,), "one")[0]
    joined_30 = db("SELECT COUNT(*) FROM users WHERE joined_date>=?", ((now-timedelta(days=30)).isoformat(),), "one")[0]
    await message.answer(
        "👥 <b>Foydalanuvchilar statistikasi</b>\n\n"
        f"👤 Jami kirgan: <b>{total}</b>\n"
        f"🟢 Online (5 daqiqa): <b>{online}</b>\n"
        f"📅 Oxirgi 30 kun kirgan: <b>{joined_30}</b>\n"
        f"🚫 Botni bloklagani aniqlangan: <b>{blocked}</b>\n"
        f"👑 Jami adminlar: <b>{admins}</b>\n"
        f"🟢 Online adminlar: <b>{online_admins}</b>\n\n"
        "ℹ️ Telegram bot foydalanuvchi chatni tark etganini har doim alohida hodisa sifatida bermaydi; bloklaganlari esa bot ularga xabar yuborganda aniqlanishi mumkin.",
        parse_mode="HTML"
    )


# ================= ADMIN: ANIME ADD =================
@router.message(F.text == "🎬 Anime qo‘shish")
async def btn_add_anime(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer(
        "🖼 Poster yuboring va captionni quyidagicha yozing:\n\n"
        "<code>Kod: 25\nNomi: Anime nomi\nFasllar: 1\nSifat: 720p\nReyting: 8.5\nJanr: Jangari</code>",
        parse_mode="HTML"
    )


@router.message(F.photo, F.caption.contains("Kod:"))
async def process_addanime(message: Message):
    if not is_admin(message.from_user.id): return
    fields = {}
    for line in message.caption.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip().lower()] = v.strip()
    try:
        code = fields["kod"]
        title = fields["nomi"]
        total_seasons = int(fields.get("fasllar", "1"))
        quality = fields.get("sifat", "-")
        rating = fields.get("reyting", "-")
        genre = fields.get("janr", "-")
    except (KeyError, ValueError):
        await message.answer("❌ Ma'lumotlar noto‘g‘ri yoki to‘liq emas."); return
    existing = db("SELECT title FROM animes WHERE code=?", (code,), "one")
    if existing and existing[0] != title:
        await message.answer(f"❌ {code} kodi allaqachon band."); return
    poster = message.photo[-1].file_id
    db("""INSERT OR REPLACE INTO animes
       (code,title,total_seasons,quality,genre,rating,views,downloads,poster_file_id,added_date)
       VALUES(?,?,?,?,?,?,COALESCE((SELECT views FROM animes WHERE code=?),0),
       COALESCE((SELECT downloads FROM animes WHERE code=?),0),?,?)""",
       (code,title,total_seasons,quality,genre,rating,code,code,poster,datetime.now().isoformat()))
    await message.answer(f"✅ Anime qo‘shildi. Kod: <code>{code}</code>", parse_mode="HTML")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💈 Anime Korish 💈", url=f"https://t.me/{BOT_USERNAME}?start={code}")
    ]])
    try:
        await bot.send_photo(int(ANNOUNCE_CHANNEL_ID), poster,
                             caption=f"🎬 {title}\n🎞 Fasllar: {total_seasons}\n💿 {quality}\n⭐️ {rating}\n🏷 {genre}",
                             reply_markup=kb)
    except Exception as e:
        logging.error("Anime channel post error: %s", e)


# ================= ADMIN: DELETE ANIME =================
@router.message(F.text == "🗑 Anime olish")
async def btn_delete_anime(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.set_state(AdminFSM.waiting_delete_anime)
    await message.answer("🗑 O‘chiriladigan anime kodini yuboring:")


@router.message(StateFilter(AdminFSM.waiting_delete_anime))
async def delete_anime_process(message: Message, state: FSMContext):
    await state.clear()
    code = message.text.strip()
    row = db("SELECT title FROM animes WHERE code=?", (code,), "one")
    if not row:
        await message.answer("❌ Bunday kod topilmadi."); return
    db("DELETE FROM animes WHERE code=?", (code,))
    db("DELETE FROM episodes WHERE anime_code=?", (code,))
    db("DELETE FROM history WHERE anime_code=?", (code,))
    await message.answer(f"✅ {row[0]} o‘chirildi.", reply_markup=admin_menu())


# ================= ADMIN: ADD EPISODE =================
@router.message(F.text == "➕ Qism qo‘shish")
async def btn_add_episode(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer(
        "➕ Videoni yuboring. Captionning birinchi qatori:\n"
        "<code>/addep KOD FASL QISM</code>\n\n"
        "Keyingi qatorlarga o‘zingiz xohlagan anime nomi/qism matnini yozishingiz mumkin.\n\n"
        "Masalan:\n<code>/addep 25 1 14\n🎬 Omadsizning qayta tug‘ilishi\n🎞 14-qism</code>", parse_mode="HTML"
    )


async def save_episode(message: Message, code: str, season: int, num: int, file_id: str, caption: str = ""):
    if not db("SELECT 1 FROM animes WHERE code=?", (code,), "one"):
        await message.answer("❌ Bunday anime kodi topilmadi."); return False
    db("INSERT OR REPLACE INTO episodes(anime_code,season_number,episode_number,file_id,caption) VALUES(?,?,?,?,?)",
       (code, season, num, file_id, caption.strip()))
    await message.answer(f"✅ {code}-anime, {season}-fasl, {num}-qism saqlandi.")
    return True


@router.message(F.video, F.caption, F.caption.startswith("/addep"))
async def process_addep_direct(message: Message):
    if not is_admin(message.from_user.id): return
    lines = message.caption.splitlines()
    parts = lines[0].split()
    if len(parts) < 4:
        await message.answer("❗️ Format: /addep KOD FASL QISM"); return
    try:
        code, season, num = parts[1], int(parts[2]), int(parts[3])
    except ValueError:
        await message.answer("❗️ Fasl va qism raqam bo‘lishi kerak."); return
    custom = "\n".join(lines[1:]).strip()
    await save_episode(message, code, season, num, message.video.file_id, custom)


@router.message(Command("addep"))
async def cmd_addep_reply(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id): return
    if not message.reply_to_message or not message.reply_to_message.video or not command.args:
        await message.answer("❗️ Videoga reply qilib: /addep KOD FASL QISM yozing."); return
    parts = command.args.split()
    if len(parts) < 3:
        await message.answer("❗️ Format: /addep KOD FASL QISM"); return
    try:
        code, season, num = parts[0], int(parts[1]), int(parts[2])
    except ValueError:
        await message.answer("❗️ Fasl va qism raqam bo‘lishi kerak."); return
    custom = " ".join(parts[3:]) if len(parts) > 3 else ""
    await save_episode(message, code, season, num, message.reply_to_message.video.file_id, custom)


# ================= ADMIN: DELETE EPISODE =================
@router.message(F.text == "🗑 Qism olish")
async def btn_delete_episode(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.set_state(AdminFSM.waiting_delete_episode)
    await message.answer("🗑 Format: <code>KOD FASL QISM</code>\nMasalan: <code>25 1 14</code>", parse_mode="HTML")


@router.message(StateFilter(AdminFSM.waiting_delete_episode))
async def delete_episode_process(message: Message, state: FSMContext):
    await state.clear()
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("❗️ Format: KOD FASL QISM"); return
    code, season, num = parts
    try: season, num = int(season), int(num)
    except ValueError:
        await message.answer("❗️ Fasl/qism raqam bo‘lishi kerak."); return
    db("DELETE FROM episodes WHERE anime_code=? AND season_number=? AND episode_number=?", (code,season,num))
    await message.answer(f"✅ {code} {season}-fasl {num}-qism o‘chirildi.", reply_markup=admin_menu())


# ================= ADMIN: CODE EDIT =================
@router.message(F.text == "🔄 Kodni olish/o‘zgartirish")
async def btn_edit_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.set_state(AdminFSM.waiting_edit_old_code)
    await message.answer("🔄 Eski anime kodini yuboring:")


@router.message(StateFilter(AdminFSM.waiting_edit_old_code))
async def edit_code_old(message: Message, state: FSMContext):
    old = message.text.strip()
    if not db("SELECT 1 FROM animes WHERE code=?", (old,), "one"):
        await message.answer("❌ Bunday kod topilmadi."); return
    await state.update_data(old_code=old)
    await state.set_state(AdminFSM.waiting_edit_new_code)
    await message.answer("Yangi kodni yuboring:")


@router.message(StateFilter(AdminFSM.waiting_edit_new_code))
async def edit_code_new(message: Message, state: FSMContext):
    data = await state.get_data(); old = data["old_code"]
    new = message.text.strip()
    await state.clear()
    if not new:
        await message.answer("❌ Yangi kod bo‘sh bo‘lishi mumkin emas."); return
    if db("SELECT 1 FROM animes WHERE code=?", (new,), "one"):
        await message.answer("❌ Yangi kod allaqachon ishlatilgan."); return
    db("UPDATE animes SET code=? WHERE code=?", (new, old))
    db("UPDATE episodes SET anime_code=? WHERE anime_code=?", (new, old))
    db("UPDATE history SET anime_code=? WHERE anime_code=?", (new, old))
    await message.answer(f"✅ Kod o‘zgartirildi: {old} → {new}", reply_markup=admin_menu())


# ================= ADMIN: CODE LIST =================
@router.message(F.text == "📋 Kodlar ro‘yxati")
async def btn_codes(message: Message):
    if not is_admin(message.from_user.id): return
    rows = db("SELECT code,title FROM animes ORDER BY added_date DESC", (), "all")
    if not rows:
        await message.answer("📋 Kodlar ro‘yxati bo‘sh."); return
    chunks = []
    text = "📋 <b>Kodlar ro‘yxati</b>\n\n"
    for code, title in rows:
        line = f"<code>{code}</code> — {title}\n"
        if len(text) + len(line) > 3500:
            chunks.append(text); text = ""
        text += line
    chunks.append(text)
    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML")


# ================= ADMIN: EPISODE POST =================
@router.message(F.text == "📢 Qism post qilish")
async def btn_post_episode(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.set_state(AdminFSM.waiting_post_video)
    await message.answer(
        "📢 Endi kanalga post qilinadigan qism videosini <b>forward</b> qiling yoki videoni yuboring.\n\n"
        "Keyin bot sizdan KOD FASL QISM so‘raydi.", parse_mode="HTML"
    )


@router.message(StateFilter(AdminFSM.waiting_post_video), F.video)
async def receive_post_video(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.update_data(file_id=message.video.file_id)
    await state.set_state(AdminFSM.waiting_post_meta)
    await message.answer("Video olindi. Endi <code>KOD FASL QISM</code> yuboring.\nMasalan: <code>25 1 14</code>", parse_mode="HTML")


@router.message(StateFilter(AdminFSM.waiting_post_meta))
async def process_post_meta(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("❗️ Format: KOD FASL QISM"); return
    code, season, num = parts
    try: season, num = int(season), int(num)
    except ValueError:
        await message.answer("❗️ Fasl va qism raqam bo‘lishi kerak."); return
    data = await state.get_data(); await state.clear()
    ep = db("SELECT file_id FROM episodes WHERE anime_code=? AND season_number=? AND episode_number=?",
            (code,season,num), "one")
    if not ep:
        await message.answer("❌ Bu qism avval botga qo‘shilmagan. Avval ➕ Qism qo‘shish orqali qo‘shing."); return
    # Prefer the stored episode file so the posted version is exactly the bot's episode.
    file_id = ep[0]
    anime = db("SELECT title FROM animes WHERE code=?", (code,), "one")
    title = anime[0] if anime else code
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🎰 {num} - qism ko‘rish 🎰", url=f"https://t.me/{BOT_USERNAME}?start=ep_{code}_{season}_{num}")
    ]])
    caption = f"🎬 {title}\n🎞 {season}-fasl, {num}-qism"
    try:
        await bot.send_video(int(ANNOUNCE_CHANNEL_ID), file_id, caption=caption, reply_markup=kb)
        await message.answer("✅ Qism kanalga post qilindi.", reply_markup=admin_menu())
    except Exception as e:
        logging.error("Episode post error: %s", e)
        await message.answer("❌ Kanalga post qilib bo‘lmadi. Bot kanal admini ekanini tekshiring.")


# ================= EPISODE DEEP LINK =================
@router.message(Command("start"), F.args.startswith("ep_"))
async def unused_episode_start(message: Message, command: CommandObject):
    # Kept only for documentation; the normal /start handler below parses it.
    pass


# ================= FORCE SUBSCRIPTION ADMIN =================
@router.message(F.text == "🔔 Majburiy obuna")
async def btn_force(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer("🔔 Majburiy obuna boshqaruvi:", reply_markup=force_menu_kb())


@router.callback_query(F.data == "force:add")
async def force_add(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminFSM.waiting_force_channel)
    await callback.message.answer(
        "➕ Kanal ID va linkini bitta qatorda yuboring:\n\n"
        "<code>-1001234567890 https://t.me/kanal</code>\n\n"
        "Bot shu kanalda admin bo‘lishi kerak.", parse_mode="HTML"
    )
    await callback.answer()


@router.message(StateFilter(AdminFSM.waiting_force_channel))
async def force_add_process(message: Message, state: FSMContext):
    await state.clear()
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("❗️ ID va link kerak."); return
    channel_id, link = parts[0], parts[1]
    try:
        chat = await bot.get_chat(int(channel_id))
        title = chat.title or channel_id
    except Exception:
        title = channel_id
    db("INSERT OR REPLACE INTO required_channels(channel_id,title,link,added_date) VALUES(?,?,?,?)",
       (channel_id,title,link,datetime.now().isoformat()))
    await message.answer(f"✅ Majburiy obuna kanali qo‘shildi: {title}", reply_markup=admin_menu())


@router.callback_query(F.data == "force:remove")
async def force_remove(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminFSM.waiting_force_remove)
    await callback.message.answer("➖ Olib tashlanadigan kanal ID'sini yuboring:")
    await callback.answer()


@router.message(StateFilter(AdminFSM.waiting_force_remove))
async def force_remove_process(message: Message, state: FSMContext):
    await state.clear()
    db("DELETE FROM required_channels WHERE channel_id=?", (message.text.strip(),))
    await message.answer("✅ Kanal majburiy obunadan olindi.", reply_markup=admin_menu())


@router.callback_query(F.data == "force:list")
async def force_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    rows = required_channels()
    if not rows:
        await callback.message.answer("🔔 Majburiy obuna kanallari hozircha yo‘q.")
    else:
        text = "🔔 <b>Majburiy obuna kanallari</b>\n\n"
        for cid, title, link in rows:
            text += f"• {title}\nID: <code>{cid}</code>\n{link}\n\n"
        await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


# ================= FALLBACK / ADMIN BUTTON GUARD =================
@router.message(F.text & ~F.text.startswith("/"))
async def fallback_text(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        return
    ensure_user(message.from_user.id, message.from_user.username)
    admin_buttons = {
        "👑 Admin qo‘shish/olish", "👥 Foydalanuvchilar", "🎬 Anime qo‘shish", "🗑 Anime olish",
        "➕ Qism qo‘shish", "🗑 Qism olish", "🔄 Kodni olish/o‘zgartirish", "📋 Kodlar ro‘yxati",
        "📢 Qism post qilish", "🔔 Majburiy obuna"
    }
    if message.text in admin_buttons:
        return

    # Deep-link sent as plain text is not expected, but normal codes are.
    await _do_search(message, message.text.strip())


# ================= KEEP ALIVE =================
async def handle_ping(request):
    return web.Response(text="An1Zen anime bot ishlayapti ✅")


async def start_webserver():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


# ================= START HANDLER PATCH =================
# Replace the normal start handler's command-argument section with support for episode deep links.
# This separate callback cannot coexist with the same Command("start") filter, so the main handler
# above is intentionally updated here by a second, more specific handler is not used.


async def main():
    db_init()
    await bot.set_my_commands([
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="addep", description="Admin: qism qo‘shish"),
    ])
    await start_webserver()
    logging.info("An1Zen bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
