import asyncio
import base64
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path



def _update_ytdlp() -> bool:
    """Ставит свежую версию yt-dlp (Instagram и YouTube часто меняют защиту)."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "--quiet", "--no-cache-dir", "yt-dlp[default]"],
            timeout=240,
            capture_output=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def _installed_ytdlp_version() -> str:
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import yt_dlp.version as v; print(v.__version__)"],
            capture_output=True, text=True, timeout=60,
        )
        return r.stdout.strip() or "?"
    except Exception:
        return "?"


# при каждом запуске бота подтягиваем свежий yt-dlp (отключить: AUTO_UPDATE_YTDLP=0)
if os.getenv("AUTO_UPDATE_YTDLP", "1") == "1":
    _update_ytdlp()

import yt_dlp  # noqa: E402
from aiogram import Bot, Dispatcher, F  # noqa: E402
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

# ====================== настройки ======================
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL = "@horizonmarkettj"  # канал для обязательной подписки
CHANNEL_URL = "https://t.me/horizonmarkettj"
CREATOR_URL = "https://t.me/IDIEVW"
# ID администратора(ов) через запятую. Свой ID можно узнать командой /id
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_ID", "").replace(" ", "").split(",") if x.isdigit()}
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "2"))  # сколько загрузок одновременно
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "3"))  # загрузок в минуту на человека
REF_NEEDED = 3  # сколько друзей нужно пригласить, чтобы не требовалась подписка
TZ_OFFSET = 5 * 3600  # Таджикистан, UTC+5 (для статистики «за сегодня»)
# Прокси для скачивания (нужен, если Instagram блокирует IP сервера).
# Формат: http://логин:пароль@хост:порт или socks5://логин:пароль@хост:порт
PROXY_URL = os.getenv("PROXY_URL")
PROXY_FOR = os.getenv("PROXY_FOR", "instagram").lower()  # instagram — только Instagram, all — всё
DB_PATH = os.getenv("DB_PATH") or ("/data/bot.db" if os.path.isdir("/data") else "bot.db")

MAX_SIZE = 50 * 1024 * 1024  # лимит Telegram Bot API на отправку файлов
MAX_PHOTO_SIZE = 10 * 1024 * 1024  # лимит Telegram на фото (больше -> как документ)
QUALITIES = ["360", "480", "720", "1080"]
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_EXT = {".mp3", ".mp4", ".mkv", ".webm", ".m4a"}

URL_RE = re.compile(
    r"https?://(?:[\w-]+\.)?(?:youtube\.com|youtu\.be|instagram\.com|tiktok\.com)/\S+",
    re.IGNORECASE,
)

LANG_PROMPT = "Забонро интихоб кунед 👇\nВыберите язык 👇"

TEXTS = {
    "ru": {
        "welcome": "Привет! 👋\nОтправь ссылку на видео из YouTube, Instagram или TikTok.",
        "subscribe": "📢 Чтобы пользоваться ботом, подпишись на каналы ниже и нажми «Проверить».\n\n"
        "Или пригласи друзей: /invite",
        "btn_sub": "📢 Подписаться",
        "btn_check": "✅ Проверить",
        "not_sub": "Ты ещё не подписался ❌",
        "ready": "✅ Готово!\nЧто скачать?",
        "video": "🎬 Видео",
        "mp3": "🎵 MP3",
        "photo": "🖼 Фото",
        "quality": "Выбери качество:",
        "expired": "Ссылка устарела, отправь её заново",
        "downloading": "⏳ Скачиваю...",
        "processing": "🔄 Обрабатываю...",
        "sending": "📤 Отправляю...",
        "queued": "⏳ Сейчас много загрузок. Ты в очереди (перед тобой: {n}). "
        "Начну, как только освободится место.",
        "compressing": "🗜 Файл больше 50 МБ — сжимаю видео, чтобы его можно было отправить. "
        "Это займёт немного времени...",
        "too_big": "❌ Файл больше 50 МБ — Telegram не позволяет боту его отправить. "
        "Попробуй меньшее качество или MP3.",
        "too_long": "❌ Видео слишком длинное: даже после сжатия оно не поместится в 50 МБ. Попробуй MP3.",
        "busy": "⏳ Дождись окончания предыдущей загрузки.",
        "rate": "🚦 Слишком часто. Подожди {s} сек.",
        "fail": "❌ Не удалось скачать: ",
        "no_photo": "❌ Фото по этой ссылке не найдено",
        "no_audio": "❌ В этом видео нет звука (или Instagram не отдаёт его боту), поэтому MP3 сделать нельзя.",
        "ask_no_audio": "⚠️ У этого видео нет аудио.\nВсё равно скачать без звука?",
        "yes": "✅ Да",
        "no": "❌ Нет",
        "cancelled": "Отменено. Можешь отправить другую ссылку.",
        "other": "Пришли ссылку на YouTube, Instagram или TikTok 🙂",
        "auto_on": "⚡ Авто-режим включён: буду скачивать так же, как в прошлый раз ({fmt}), "
        "без вопросов.\nВыключить: /auto",
        "auto_off": "Авто-режим выключен. Теперь буду спрашивать формат.",
        "auto_need": "Сначала скачай что-нибудь один раз, потом включи /auto.",
        "invite": "👥 Пригласи друзей!\nПриведи {need} друзей: когда они скачают хотя бы по одному "
        "файлу, подписка на канал тебе больше не понадобится.\n\n"
        "Твоя ссылка:\n{link}\n\nПриглашено: {n}/{need}",
        "invite_done": "🎉 Ты уже пригласил достаточно друзей — подписка не нужна!",
        "ref_new": "🎉 Твой друг начал пользоваться ботом! Приглашено: {n}/{need}",
        "ref_unlocked": "🎁 Ты пригласил {need} друзей — теперь подписка на канал не нужна!",
        "ad": "👨‍💻 Создатель бота: @IDIEVW\n\n"
        "Для создания веб-сайтов, чат-ботов или приложений iOS/Android "
        "для вашего бизнеса пишите в чат или звоните:\n"
        "📞 +992 978117811\n📞 +992 944442044",
        "btn_creator": "💬 Написать создателю",
    },
    "tg": {
        "welcome": "Салом! 👋\nПайванди видеоро аз YouTube, Instagram ё TikTok фиристед.",
        "subscribe": "📢 Барои истифодаи бот ба каналҳои зерин обуна шавед ва тугмаи «Санҷиш»-ро пахш кунед.\n\n"
        "Ё дӯстонро даъват кунед: /invite",
        "btn_sub": "📢 Обуна шудан",
        "btn_check": "✅ Санҷиш",
        "not_sub": "Шумо ҳанӯз обуна нашудаед ❌",
        "ready": "✅ Тайёр!\nЧӣ боргирӣ кунем?",
        "video": "🎬 Видео",
        "mp3": "🎵 MP3",
        "photo": "🖼 Акс",
        "quality": "Сифатро интихоб кунед:",
        "expired": "Пайванд кӯҳна шуд, онро дубора фиристед",
        "downloading": "⏳ Боргирӣ шуда истодааст...",
        "processing": "🔄 Коркард шуда истодааст...",
        "sending": "📤 Фиристода истодаам...",
        "queued": "⏳ Ҳоло боргириҳо бисёранд. Шумо дар навбатед (пеш аз шумо: {n}). "
        "Ҳамин ки ҷой холӣ шавад, оғоз мекунам.",
        "compressing": "🗜 Ҳаҷми файл аз 50 МБ зиёд аст — видеоро кӯчак карда истодаам, "
        "то фиристодан мумкин шавад. Каме вақт мегирад...",
        "too_big": "❌ Ҳаҷми файл аз 50 МБ зиёд аст — Telegram ба бот фиристодани онро иҷозат намедиҳад. "
        "Сифати пасттар ё MP3-ро санҷед.",
        "too_long": "❌ Видео хеле дароз аст: ҳатто пас аз кӯчак кардан ба 50 МБ намеғунҷад. MP3-ро санҷед.",
        "busy": "⏳ Интизор шавед, боргирии қаблӣ ба охир расад.",
        "rate": "🚦 Хеле зуд-зуд. {s} сония интизор шавед.",
        "fail": "❌ Боргирӣ нашуд: ",
        "no_photo": "❌ Аксе бо ин пайванд ёфт нашуд",
        "no_audio": "❌ Дар ин видео садо нест (ё Instagram онро ба бот намедиҳад), барои ҳамин MP3 сохтан мумкин нест.",
        "ask_no_audio": "⚠️ Ин видео аудио надорад.\nБо вуҷуди ин бе садо боргирӣ шавад?",
        "yes": "✅ Ҳа",
        "no": "❌ Не",
        "cancelled": "Бекор карда шуд. Метавонед пайванди дигар фиристед.",
        "other": "Пайванди YouTube, Instagram ё TikTok-ро фиристед 🙂",
        "auto_on": "⚡ Реҷаи худкор фаъол шуд: бе савол мисли дафъаи қаблӣ ({fmt}) боргирӣ мекунам.\n"
        "Хомӯш кардан: /auto",
        "auto_off": "Реҷаи худкор хомӯш шуд. Акнун формат мепурсам.",
        "auto_need": "Аввал як маротиба чизе боргирӣ кунед, баъд /auto-ро фаъол кунед.",
        "invite": "👥 Дӯстонро даъват кунед!\n{need} дӯстро биёред: вақте онҳо ақаллан як файл "
        "боргирӣ кунанд, ба шумо обуна ба канал лозим нест.\n\n"
        "Пайванди шумо:\n{link}\n\nДаъват шуд: {n}/{need}",
        "invite_done": "🎉 Шумо дӯстони кофӣ даъват кардед — обуна лозим нест!",
        "ref_new": "🎉 Дӯсти шумо ба истифодаи бот оғоз кард! Даъват шуд: {n}/{need}",
        "ref_unlocked": "🎁 Шумо {need} дӯстро даъват кардед — акнун обуна ба канал лозим нест!",
        "ad": "👨‍💻 Созандаи бот: @IDIEVW\n\n"
        "Барои сохтани вебсайт, чат-бот ё барномаҳои iOS/Android "
        "барои бизнеси худ ба чат нависед ё занг занед:\n"
        "📞 +992 978117811\n📞 +992 944442044",
        "btn_creator": "💬 Ба созанда навиштан",
    },
}

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

# ====================== база данных (SQLite) ======================
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    lang TEXT,
    joined INTEGER,
    referrer INTEGER,
    ref_credited INTEGER DEFAULT 0,
    ref_count INTEGER DEFAULT 0,
    last_mode TEXT,
    last_quality TEXT,
    auto INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS downloads(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    mode TEXT,
    ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dl_ts ON downloads(ts);
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER UNIQUE,
    title TEXT,
    url TEXT
);
"""
)
db.commit()

USER_FIELDS = {"lang", "ref_credited", "ref_count", "last_mode", "last_quality", "auto"}


def get_user(uid: int):
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def ensure_user(uid: int, referrer: int | None = None) -> bool:
    """Создаёт пользователя при первой встрече. Возвращает True, если он новый."""
    if get_user(uid):
        return False
    if referrer and (referrer == uid or not get_user(referrer)):
        referrer = None
    db.execute(
        "INSERT INTO users(id, joined, referrer) VALUES(?,?,?)",
        (uid, int(time.time()), referrer),
    )
    db.commit()
    return True


def set_field(uid: int, field: str, value):
    assert field in USER_FIELDS
    db.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, uid))
    db.commit()


def add_download(uid: int, mode: str):
    db.execute("INSERT INTO downloads(user_id, mode, ts) VALUES(?,?,?)", (uid, mode, int(time.time())))
    db.commit()


def list_channels():
    return db.execute("SELECT * FROM channels ORDER BY id").fetchall()


def add_channel(chat_id: int, title: str, url: str) -> bool:
    cur = db.execute(
        "INSERT OR IGNORE INTO channels(chat_id, title, url) VALUES(?,?,?)",
        (chat_id, title, url),
    )
    db.commit()
    return cur.rowcount > 0


def delete_channel(channel_id: int):
    db.execute("DELETE FROM channels WHERE id=?", (channel_id,))
    db.commit()


def parse_channel_input(text: str):
    """Из текста админа достаёт канал (@username / t.me/ссылка / -100ID) и ссылку-приглашение."""
    chat, url = None, None
    for p in text.split():
        if re.fullmatch(r"-100\d+", p):
            chat = int(p)
        elif re.fullmatch(r"@[A-Za-z0-9_]{4,}", p):
            if chat is None:
                chat = p
            if url is None:
                url = f"https://t.me/{p[1:]}"
        elif re.match(r"(?:https?://)?t\.me/(\+|joinchat/)\S+", p):
            url = p if p.startswith("http") else "https://" + p
        else:
            m = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?", p)
            if m:
                if chat is None:
                    chat = "@" + m.group(1)
                url = f"https://t.me/{m.group(1)}"
    return chat, url


user_lang: dict[int, str] = {}  # кэш языков


def get_lang(uid: int) -> str | None:
    if uid in user_lang:
        return user_lang[uid]
    row = get_user(uid)
    if row and row["lang"]:
        user_lang[uid] = row["lang"]
        return row["lang"]
    return None


def t(uid: int, key: str) -> str:
    return TEXTS[get_lang(uid) or "ru"][key]


def fmt_name(uid: int, mode: str, quality: str | None) -> str:
    if mode == "video":
        return f"{t(uid, 'video')} {quality or ''}p"
    return t(uid, mode)


def day_start() -> int:
    now = int(time.time())
    return (now + TZ_OFFSET) // 86400 * 86400 - TZ_OFFSET


def stats_text() -> str:
    ds = day_start()
    wk = ds - 6 * 86400

    def q(sql, *args):
        return db.execute(sql, args).fetchone()[0]

    by_mode = db.execute(
        "SELECT mode, COUNT(*) c FROM downloads WHERE ts>=? GROUP BY mode", (ds,)
    ).fetchall()
    modes = ", ".join(f"{r['mode']}: {r['c']}" for r in by_mode) or "—"
    return (
        "📊 Статистика\n\n"
        f"👥 Всего пользователей: {q('SELECT COUNT(*) FROM users')}\n"
        f"🆕 Новых сегодня: {q('SELECT COUNT(*) FROM users WHERE joined>=?', ds)}\n"
        f"🔥 Активных сегодня: {q('SELECT COUNT(DISTINCT user_id) FROM downloads WHERE ts>=?', ds)}\n\n"
        f"⬇️ Загрузок сегодня: {q('SELECT COUNT(*) FROM downloads WHERE ts>=?', ds)}\n"
        f"   ({modes})\n"
        f"⬇️ За 7 дней: {q('SELECT COUNT(*) FROM downloads WHERE ts>=?', wk)}\n"
        f"⬇️ Всего: {q('SELECT COUNT(*) FROM downloads')}\n\n"
        f"🔗 Пришли по приглашениям: {q('SELECT COUNT(*) FROM users WHERE referrer IS NOT NULL')}\n"
        f"🎁 Открыли доступ без подписки: {q('SELECT COUNT(*) FROM users WHERE ref_count>=?', REF_NEEDED)}"
    )


# ====================== состояние и лимиты ======================
pending: dict[str, str] = {}  # id -> ссылка (callback_data ограничена 64 байтами)
held: dict[str, dict] = {}  # видео без звука, ждут ответа «да/нет»
bc_pending: dict[str, dict] = {}  # рассылка, ждёт подтверждения
bg_tasks: set[asyncio.Task] = set()
download_sem = asyncio.Semaphore(MAX_PARALLEL)
queue = {"waiting": 0}
active_users: set[int] = set()
recent: dict[int, deque] = defaultdict(deque)
BOT_USERNAME = ""


def limit_problem(uid: int) -> str | None:
    """Возвращает текст проблемы, если человеку пока нельзя качать."""
    if uid in ADMIN_IDS:
        return None
    if uid in active_users:
        return t(uid, "busy")
    now = time.time()
    dq = recent[uid]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= RATE_LIMIT:
        return t(uid, "rate").format(s=int(60 - (now - dq[0])) + 1)
    return None


def proxy_for(url: str) -> str | None:
    if not PROXY_URL:
        return None
    if PROXY_FOR == "all" or "instagram.com" in url.lower():
        return PROXY_URL
    return None


def setup_cookies() -> str | None:
    """Cookies нужны для Instagram и YouTube на серверах. Берём из COOKIES_B64 или COOKIES_FILE."""
    b64 = os.getenv("COOKIES_B64")
    if b64:
        path = "/tmp/cookies.txt"
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return path
    return os.getenv("COOKIES_FILE")


COOKIES = setup_cookies()


# ====================== клавиатуры ======================
def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇹🇯 Тоҷикӣ", callback_data="lang:tg"),
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
            ]
        ]
    )


def format_keyboard(uid: int, key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(uid, "video"), callback_data=f"choose:{key}"),
                InlineKeyboardButton(text=t(uid, "mp3"), callback_data=f"mp3:0:{key}"),
            ],
            [InlineKeyboardButton(text=t(uid, "photo"), callback_data=f"photo:0:{key}")],
        ]
    )


def subscribe_keyboard(uid: int, key: str, channels) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📢 {c['title'][:40]}", url=c["url"])] for c in channels
    ]
    rows.append([InlineKeyboardButton(text=t(uid, "btn_check"), callback_data=f"check:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def yes_no_keyboard(uid: int, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t(uid, "yes"), callback_data=f"noaudio:yes:{token}"),
                InlineKeyboardButton(text=t(uid, "no"), callback_data=f"noaudio:no:{token}"),
            ]
        ]
    )


def creator_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t(uid, "btn_creator"), url=CREATOR_URL)]]
    )


# ====================== доступ: подписка и приглашения ======================
async def is_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        # бота убрали из админов канала или канал удалён — не блокируем людей из-за этого
        logging.exception("Не удалось проверить подписку на %s (бот админ канала?)", chat_id)
        return True
    if member.status in ("member", "administrator", "creator"):
        return True
    return member.status == "restricted" and getattr(member, "is_member", False)


async def missing_channels(bot: Bot, uid: int) -> list:
    """Каналы, на которые человек ещё не подписан. Пустой список — доступ открыт."""
    if uid in ADMIN_IDS:
        return []
    row = get_user(uid)
    if row and row["ref_count"] >= REF_NEEDED:
        return []  # пригласил достаточно друзей
    missing = []
    for c in list_channels():
        if not await is_member(bot, c["chat_id"], uid):
            missing.append(c)
    return missing


async def credit_referral(bot: Bot, uid: int):
    """Засчитываем приглашение, когда друг скачал свой первый файл."""
    row = get_user(uid)
    if not row or not row["referrer"] or row["ref_credited"]:
        return
    set_field(uid, "ref_credited", 1)
    ref = row["referrer"]
    ref_row = get_user(ref)
    if not ref_row:
        return
    n = (ref_row["ref_count"] or 0) + 1
    set_field(ref, "ref_count", n)
    key = "ref_unlocked" if n == REF_NEEDED else "ref_new"
    try:
        await bot.send_message(ref, t(ref, key).format(n=min(n, REF_NEEDED), need=REF_NEEDED))
    except Exception:
        pass


# ====================== прогресс ======================
def bar(pct: int) -> str:
    filled = pct // 10
    return "▓" * filled + "░" * (10 - filled)


class ProgressFile(FSInputFile):
    """Файл, который считает, сколько байт уже ушло в Telegram."""

    def __init__(self, path, state: dict, **kwargs):
        super().__init__(path, **kwargs)
        self.state = state

    async def read(self, bot):
        async for chunk in super().read(bot):
            self.state["sent"] += len(chunk)
            yield chunk


async def progress_loop(status: Message, uid: int, state: dict):
    """Раз в 3 секунды обновляет сообщение с процентами."""
    last = ""
    while True:
        await asyncio.sleep(3)
        stage = state["stage"]
        if stage == "queued":
            text = t(uid, "queued").format(n=state["ahead"])
        elif stage == "download":
            pct = state["pct"]
            text = f"{t(uid, 'downloading')}\n{bar(pct)} {pct}%"
        elif stage == "processing":
            text = t(uid, "processing")
        elif stage == "compressing":
            text = t(uid, "compressing")
        else:
            total = state["total"] or 1
            pct = min(99, int(state["sent"] * 100 / total))
            text = f"{t(uid, 'sending')}\n{bar(pct)} {pct}%"
        if text == last:
            continue
        try:
            await status.edit_text(text)
            last = text
        except Exception:
            pass  # слишком частые правки или то же самое сообщение — не страшно


# ====================== ffmpeg: проверка звука и сжатие ======================
async def has_audio(path: Path) -> bool:
    """Есть ли в файле звуковая дорожка (через ffprobe)."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return True  # не смогли проверить — не мешаем отправке
    return bool(out.strip())


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def compress(path: Path, out_dir: str) -> Path:
    """Сжимает видео так, чтобы оно уложилось в лимит Telegram (50 МБ)."""
    dur = probe_duration(path)
    if dur <= 0:
        raise RuntimeError("TOO_LONG")
    audio_bits = 96_000
    video_bits = int(MAX_SIZE * 0.9 * 8 / dur) - audio_bits
    if video_bits < 120_000:  # слишком длинное — картинка превратится в кашу
        raise RuntimeError("TOO_LONG")
    out = Path(out_dir) / "compressed.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", str(path),
        "-c:v", "libx264", "-b:v", str(video_bits),
        "-maxrate", str(video_bits), "-bufsize", str(video_bits * 2),
        "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
    ]
    if video_bits < 700_000:
        cmd += ["-vf", "scale=-2:480"]
    cmd.append(str(out))
    subprocess.run(cmd, check=True, capture_output=True, timeout=1800)
    if out.stat().st_size > MAX_SIZE:
        raise RuntimeError("TOO_LONG")
    return out


# ====================== загрузка yt-dlp ======================
def download(url: str, mode: str, quality: str, out_dir: str, state: dict) -> list[Path]:
    def hook(d):
        if d["status"] == "downloading":
            state["stage"] = "download"
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                state["pct"] = min(99, int(d.get("downloaded_bytes", 0) * 100 / total))
        elif d["status"] == "finished":
            state["pct"] = 100
            state["stage"] = "processing"

    opts = {
        "outtmpl": f"{out_dir}/%(title).60s_%(autonumber)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    proxy = proxy_for(url)
    if proxy:
        opts["proxy"] = proxy
    if COOKIES:
        # копия, т.к. yt-dlp может перезаписывать файл, а /app может быть read-only
        tmp_cookies = os.path.join(out_dir, "cookies.txt")
        shutil.copy(COOKIES, tmp_cookies)
        opts["cookiefile"] = tmp_cookies

    if mode == "mp3":
        opts.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )
    elif mode == "photo":
        # Фото: берём картинки (обложки/слайды поста) без скачивания видео
        opts.update(
            {
                "skip_download": True,
                "writethumbnail": True,
                "noplaylist": False,
                "playlistend": 10,
                "postprocessors": [
                    {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}
                ],
            }
        )
    else:
        h = quality
        opts.update(
            {
                # ba[ext=m4a] даёт AAC-звук, а [acodec!=none] не даёт выбрать видео без звука
                "format": (
                    f"bv*[height<={h}]+ba[ext=m4a]/bv*[height<={h}]+ba/"
                    f"b[height<={h}][acodec!=none]/b[acodec!=none]/b"
                ),
                "merge_output_format": "mp4",
                # при склейке перекодируем звук в AAC, его проигрывают все телефоны
                "postprocessor_args": {"merger": ["-c:v", "copy", "-c:a", "aac"]},
            }
        )

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    allowed = IMAGE_EXT if mode == "photo" else MEDIA_EXT
    files = sorted(
        (p for p in Path(out_dir).iterdir() if p.is_file() and p.suffix.lower() in allowed),
        key=lambda p: p.name,
    )
    if not files:
        raise RuntimeError("NO_FILES")
    if mode == "photo":
        return files[:10]
    return [max(files, key=lambda p: p.stat().st_size)]


# ====================== команды: язык, старт, id ======================
@dp.message(CommandStart())
async def start(message: Message, command: CommandObject):
    uid = message.from_user.id
    referrer = None
    if command.args and command.args.startswith("ref_") and command.args[4:].isdigit():
        referrer = int(command.args[4:])
    ensure_user(uid, referrer)
    await message.answer(LANG_PROMPT, reply_markup=lang_keyboard())


@dp.message(Command("lang"))
async def lang_cmd(message: Message):
    ensure_user(message.from_user.id)
    await message.answer(LANG_PROMPT, reply_markup=lang_keyboard())


@dp.message(Command("id"))
async def id_cmd(message: Message):
    await message.answer(f"ID: {message.from_user.id}")


@dp.callback_query(F.data.startswith("lang:"))
async def set_lang(call: CallbackQuery):
    lang = call.data.split(":", 1)[1]
    if lang not in TEXTS:
        await call.answer()
        return
    uid = call.from_user.id
    ensure_user(uid)
    set_field(uid, "lang", lang)
    user_lang[uid] = lang
    await call.answer()
    await call.message.edit_text(TEXTS[lang]["welcome"])


# ====================== авто-режим и приглашения ======================
@dp.message(Command("auto"))
async def auto_cmd(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    row = get_user(uid)
    if row["auto"]:
        set_field(uid, "auto", 0)
        await message.answer(t(uid, "auto_off"))
        return
    if not row["last_mode"]:
        await message.answer(t(uid, "auto_need"))
        return
    set_field(uid, "auto", 1)
    await message.answer(
        t(uid, "auto_on").format(fmt=fmt_name(uid, row["last_mode"], row["last_quality"]))
    )


@dp.message(Command("invite"))
async def invite_cmd(message: Message):
    uid = message.from_user.id
    ensure_user(uid)
    row = get_user(uid)
    n = row["ref_count"] or 0
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    text = t(uid, "invite").format(link=link, n=min(n, REF_NEEDED), need=REF_NEEDED)
    if n >= REF_NEEDED:
        text += "\n\n" + t(uid, "invite_done")
    await message.answer(text)


# ====================== админ: /stats и /broadcast ======================
@dp.message(Command("stats"))
async def stats_cmd(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(stats_text())


@dp.message(Command("update"))
async def update_cmd(message: Message):
    """Админ: обновить yt-dlp и перезапустить бота (без пересборки на Railway)."""
    if message.from_user.id not in ADMIN_IDS:
        return
    old = _installed_ytdlp_version()
    msg = await message.answer(f"🔄 Обновляю yt-dlp (сейчас {old})...")
    ok = await asyncio.to_thread(_update_ytdlp)
    new = await asyncio.to_thread(_installed_ytdlp_version)
    if not ok:
        await msg.edit_text(f"❌ Не удалось обновить (версия {new}). Смотрите логи Railway.")
        return
    await msg.edit_text(f"✅ yt-dlp: {old} → {new}\nПерезапускаю бота...")
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)  # перезапуск процесса на месте


@dp.message(Command("broadcast"))
async def broadcast_cmd(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    if message.reply_to_message:
        payload = {
            "kind": "copy",
            "chat": message.chat.id,
            "msg": message.reply_to_message.message_id,
        }
    elif command.args:
        payload = {"kind": "text", "text": command.args}
    else:
        await message.answer(
            "Как пользоваться:\n"
            "• /broadcast текст — разослать текст\n"
            "• ответьте командой /broadcast на любое сообщение (фото, видео, пост) — "
            "и оно уйдёт всем"
        )
        return
    token = uuid.uuid4().hex[:8]
    bc_pending[token] = payload
    n = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить", callback_data=f"bc:yes:{token}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"bc:no:{token}"),
            ]
        ]
    )
    await message.answer(f"Отправить это сообщение {n} пользователям?", reply_markup=kb)


async def send_one(bot: Bot, uid: int, payload: dict):
    if payload["kind"] == "copy":
        await bot.copy_message(uid, payload["chat"], payload["msg"])
    else:
        await bot.send_message(uid, payload["text"])


async def run_broadcast(bot: Bot, payload: dict, report: Message):
    ids = [r[0] for r in db.execute("SELECT id FROM users").fetchall()]
    ok = blocked = failed = 0
    for uid in ids:
        try:
            await send_one(bot, uid, payload)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await send_one(bot, uid, payload)
                ok += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            blocked += 1  # пользователь заблокировал бота
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # ~20 сообщений в секунду, в пределах лимитов Telegram
    await report.answer(
        f"✅ Рассылка завершена\nДоставлено: {ok}\nЗаблокировали бота: {blocked}\nОшибок: {failed}"
    )


@dp.callback_query(F.data.startswith("bc:"))
async def broadcast_confirm(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer()
        return
    _, answer, token = call.data.split(":", 2)
    payload = bc_pending.pop(token, None)
    await call.answer()
    if not payload or answer != "yes":
        await call.message.edit_text("Отменено.")
        return
    await call.message.edit_text("📣 Рассылка началась...")
    task = asyncio.create_task(run_broadcast(bot, payload, call.message))
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)


# ====================== админ: каналы для подписки ======================
awaiting_channel: set[int] = set()


def channels_view():
    chans = list_channels()
    if chans:
        text = "📢 Каналы для обязательной подписки:\n\n" + "\n".join(
            f"{i}. {c['title']}" for i, c in enumerate(chans, 1)
        )
    else:
        text = "Каналов нет — подписка сейчас не требуется."
    rows = [
        [InlineKeyboardButton(text=f"🗑 Удалить: {c['title'][:30]}", callback_data=f"chdel:{c['id']}")]
        for c in chans
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="chadd")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("channels"))
async def channels_cmd(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    text, kb = channels_view()
    await message.answer(text, reply_markup=kb)


@dp.message(Command("cancel"))
async def cancel_cmd(message: Message):
    if message.from_user.id in awaiting_channel:
        awaiting_channel.discard(message.from_user.id)
        await message.answer("Отменено.")


@dp.callback_query(F.data == "chadd")
async def channel_add_start(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer()
        return
    awaiting_channel.add(call.from_user.id)
    await call.answer()
    await call.message.answer(
        "Отправь канал одним из способов:\n"
        "• @username или ссылку https://t.me/username\n"
        "• приватный канал: ID и ссылка-приглашение через пробел, например:\n"
        "-1001234567890 https://t.me/+abcdef\n\n"
        "⚠️ Бот должен быть администратором этого канала.\n"
        "Отмена: /cancel"
    )


@dp.callback_query(F.data.startswith("chdel:"))
async def channel_delete(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer()
        return
    delete_channel(int(call.data.split(":", 1)[1]))
    await call.answer("Удалено")
    text, kb = channels_view()
    await call.message.edit_text(text, reply_markup=kb)


@dp.message(F.text, lambda m: m.from_user.id in awaiting_channel and not m.text.startswith("/"))
async def channel_input(message: Message, bot: Bot):
    chat, url = parse_channel_input(message.text)
    if chat is None:
        await message.answer("Не понял. Отправь @username или ссылку на канал (или /cancel).")
        return
    try:
        info = await bot.get_chat(chat)
    except Exception:
        await message.answer("❌ Не нашёл такой канал. Проверь ссылку и попробуй снова (или /cancel).")
        return
    try:
        me = await bot.get_chat_member(info.id, bot.id)
        is_admin = me.status in ("administrator", "creator")
    except Exception:
        is_admin = False
    if not is_admin:
        await message.answer(
            "❌ Бот не администратор этого канала, поэтому не сможет проверять подписку.\n"
            "Добавьте бота в администраторы и отправьте канал ещё раз (или /cancel)."
        )
        return
    if not url:
        if info.username:
            url = f"https://t.me/{info.username}"
        else:
            await message.answer(
                "Это приватный канал: добавьте ссылку-приглашение через пробел, например:\n"
                f"{info.id} https://t.me/+abcdef"
            )
            return
    awaiting_channel.discard(message.from_user.id)
    if add_channel(info.id, info.title or str(info.id), url):
        await message.answer(f"✅ Канал добавлен: {info.title}")
    else:
        await message.answer("Этот канал уже есть в списке.")
    text, kb = channels_view()
    await message.answer(text, reply_markup=kb)


# ====================== ссылка, подписка, формат ======================
@dp.message(F.text.regexp(URL_RE))
async def got_link(message: Message, bot: Bot):
    uid = message.from_user.id
    ensure_user(uid)
    if get_lang(uid) is None:
        await message.answer(LANG_PROMPT, reply_markup=lang_keyboard())
        return

    url = URL_RE.search(message.text).group(0)

    missing = await missing_channels(bot, uid)
    if missing:
        key = uuid.uuid4().hex[:10]
        pending[key] = url
        await message.answer(
            t(uid, "subscribe"), reply_markup=subscribe_keyboard(uid, key, missing)
        )
        return

    row = get_user(uid)
    if row["auto"] and row["last_mode"]:
        # авто-режим: качаем сразу в прошлом формате
        problem = limit_problem(uid)
        if problem:
            await message.answer(problem)
            return
        status = await message.answer(f"{t(uid, 'downloading')}\n{bar(0)} 0%")
        await process_download(
            bot, message, status, uid, url, row["last_mode"], row["last_quality"] or "720"
        )
        return

    key = uuid.uuid4().hex[:10]
    pending[key] = url
    await message.answer(t(uid, "ready"), reply_markup=format_keyboard(uid, key))


@dp.callback_query(F.data.startswith("check:"))
async def check_sub(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    key = call.data.split(":", 1)[1]
    if key not in pending:
        await call.answer(t(uid, "expired"), show_alert=True)
        return
    missing = await missing_channels(bot, uid)
    if missing:
        await call.answer(t(uid, "not_sub"), show_alert=True)
        try:  # оставляем кнопки только для каналов, на которые ещё не подписался
            await call.message.edit_reply_markup(reply_markup=subscribe_keyboard(uid, key, missing))
        except Exception:
            pass
        return
    await call.answer()
    await call.message.edit_text(t(uid, "ready"), reply_markup=format_keyboard(uid, key))


@dp.callback_query(F.data.startswith("choose:"))
async def choose_quality(call: CallbackQuery):
    uid = call.from_user.id
    key = call.data.split(":", 1)[1]
    if key not in pending:
        await call.answer(t(uid, "expired"), show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"{q}p", callback_data=f"video:{q}:{key}")
                for q in QUALITIES
            ]
        ]
    )
    await call.message.edit_text(t(uid, "quality"), reply_markup=kb)
    await call.answer()


# ====================== загрузка и отправка ======================
async def send_photos(message: Message, paths: list[Path]):
    if len(paths) == 1:
        p = paths[0]
        if p.stat().st_size <= MAX_PHOTO_SIZE:
            await message.answer_photo(FSInputFile(p))
        else:
            await message.answer_document(FSInputFile(p))
        return
    media = []
    for p in paths:
        if p.stat().st_size <= MAX_PHOTO_SIZE:
            media.append(InputMediaPhoto(media=FSInputFile(p)))
        else:
            await message.answer_document(FSInputFile(p))
    if len(media) == 1:
        await message.answer_photo(media[0].media)
    elif media:
        await message.answer_media_group(media)


async def process_download(
    bot: Bot, base: Message, status: Message, uid: int, url: str, mode: str, quality: str
):
    """Вся загрузка: очередь -> скачивание -> (сжатие) -> проверка звука -> отправка."""
    state = {"stage": "download", "pct": 0, "sent": 0, "total": 0, "ahead": 0}
    active_users.add(uid)
    recent[uid].append(time.time())
    set_field(uid, "last_mode", mode)
    set_field(uid, "last_quality", quality)
    task = asyncio.create_task(progress_loop(status, uid, state))
    tmp = tempfile.mkdtemp()
    keep_tmp = False
    try:
        # очередь: одновременно работают только MAX_PARALLEL загрузок
        queued = download_sem.locked()
        if queued:
            state["stage"] = "queued"
            state["ahead"] = MAX_PARALLEL + queue["waiting"]
            queue["waiting"] += 1
        try:
            await download_sem.acquire()
        finally:
            if queued:
                queue["waiting"] -= 1
        try:
            state["stage"] = "download"
            paths = await asyncio.to_thread(download, url, mode, quality, tmp, state)
            if mode == "video" and paths[0].stat().st_size > MAX_SIZE:
                state["stage"] = "compressing"
                paths = [await asyncio.to_thread(compress, paths[0], tmp)]
        finally:
            download_sem.release()

        if mode == "mp3" and paths[0].stat().st_size > MAX_SIZE:
            task.cancel()
            await status.edit_text(t(uid, "too_big"))
            return

        if mode == "video" and not await has_audio(paths[0]):
            task.cancel()
            token = uuid.uuid4().hex[:10]
            held[token] = {"path": paths[0], "tmp": tmp, "time": time.time()}
            keep_tmp = True
            await status.edit_text(
                t(uid, "ask_no_audio"), reply_markup=yes_no_keyboard(uid, token)
            )
            return

        state["stage"] = "sending"
        if mode == "photo":
            await status.edit_text(t(uid, "sending"))
            await send_photos(base, paths)
        else:
            state["total"] = paths[0].stat().st_size
            file = ProgressFile(paths[0], state)
            if mode == "mp3":
                await base.answer_audio(file)
            else:
                await base.answer_video(file, supports_streaming=True)

        task.cancel()
        await status.delete()
        add_download(uid, mode)
        await credit_referral(bot, uid)
        # реклама создателя после отправки файла
        await base.answer(t(uid, "ad"), reply_markup=creator_keyboard(uid))
    except Exception as e:
        task.cancel()
        logging.exception("Ошибка загрузки")
        msg = str(e)
        if "NO_FILES" in msg and mode == "photo":
            await status.edit_text(t(uid, "no_photo"))
        elif "TOO_LONG" in msg:
            await status.edit_text(t(uid, "too_long"))
        elif mode == "mp3" and "audio codec" in msg:
            await status.edit_text(t(uid, "no_audio"))
        else:
            await status.edit_text(f"{t(uid, 'fail')}{msg[:200]}")
    finally:
        task.cancel()
        active_users.discard(uid)
        if not keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)


@dp.callback_query(F.data.regexp(r"^(video|mp3|photo):\d+:"))
async def on_download(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    mode, quality, key = call.data.split(":", 2)

    # повторная проверка, чтобы нельзя было обойти подписку старыми кнопками
    missing = await missing_channels(bot, uid)
    if missing:
        await call.answer()
        await call.message.edit_text(
            t(uid, "subscribe"), reply_markup=subscribe_keyboard(uid, key, missing)
        )
        return

    problem = limit_problem(uid)
    if problem:
        await call.answer(problem, show_alert=True)
        return

    url = pending.pop(key, None)
    if not url:
        await call.answer(t(uid, "expired"), show_alert=True)
        return

    await call.answer()
    status = await call.message.edit_text(f"{t(uid, 'downloading')}\n{bar(0)} 0%")
    await process_download(bot, call.message, status, uid, url, mode, quality)


@dp.callback_query(F.data.startswith("noaudio:"))
async def on_no_audio(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    _, answer, token = call.data.split(":", 2)
    item = held.pop(token, None)
    if not item:
        await call.answer(t(uid, "expired"), show_alert=True)
        return
    await call.answer()

    if answer != "yes":
        shutil.rmtree(item["tmp"], ignore_errors=True)
        await call.message.edit_text(t(uid, "cancelled"))
        return

    path: Path = item["path"]
    state = {"stage": "sending", "pct": 0, "sent": 0, "total": path.stat().st_size}
    status = await call.message.edit_text(f"{t(uid, 'sending')}\n{bar(0)} 0%")
    task = asyncio.create_task(progress_loop(status, uid, state))
    try:
        await call.message.answer_video(ProgressFile(path, state), supports_streaming=True)
        task.cancel()
        await status.delete()
        add_download(uid, "video")
        await credit_referral(bot, uid)
        await call.message.answer(t(uid, "ad"), reply_markup=creator_keyboard(uid))
    except Exception as e:
        task.cancel()
        logging.exception("Ошибка отправки")
        await status.edit_text(f"{t(uid, 'fail')}{str(e)[:200]}")
    finally:
        task.cancel()
        shutil.rmtree(item["tmp"], ignore_errors=True)


@dp.message()
async def other(message: Message):
    await message.answer(t(message.from_user.id, "other"))


# ====================== запуск ======================
async def cleanup_loop():
    """Удаляет файлы, на вопрос «да/нет» по которым не ответили 15 минут."""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for k, v in list(held.items()):
            if now - v["time"] > 900:
                held.pop(k, None)
                shutil.rmtree(v["tmp"], ignore_errors=True)


async def setup_commands(bot: Bot):
    ru = [
        BotCommand(command="start", description="🚀 Начать"),
        BotCommand(command="lang", description="🌐 Сменить язык / Иваз кардани забон"),
        BotCommand(command="auto", description="⚡ Авто-режим формата"),
        BotCommand(command="invite", description="👥 Пригласить друзей"),
    ]
    tg = [
        BotCommand(command="start", description="🚀 Оғоз"),
        BotCommand(command="lang", description="🌐 Иваз кардани забон / Сменить язык"),
        BotCommand(command="auto", description="⚡ Реҷаи худкори формат"),
        BotCommand(command="invite", description="👥 Даъвати дӯстон"),
    ]
    await bot.set_my_commands(ru)
    await bot.set_my_commands(tg, language_code="tg")
    admin_cmds = ru + [
        BotCommand(command="stats", description="📊 Статистика"),
        BotCommand(command="channels", description="📢 Каналы подписки"),
        BotCommand(command="broadcast", description="📣 Рассылка"),
        BotCommand(command="update", description="🔄 Обновить yt-dlp"),
        BotCommand(command="id", description="🆔 Мой ID"),
    ]
    for aid in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=aid))
        except Exception:
            logging.warning("Не удалось задать меню команд для админа %s", aid)


async def main():
    global BOT_USERNAME
    if not TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    bot = Bot(TOKEN)
    BOT_USERNAME = (await bot.get_me()).username
    await setup_commands(bot)
    if not list_channels():  # первый запуск: добавляем канал по умолчанию
        try:
            info = await bot.get_chat(CHANNEL)
            add_channel(info.id, info.title or CHANNEL, CHANNEL_URL)
        except Exception:
            logging.warning("Не удалось добавить канал по умолчанию — добавьте через /channels")
    cleanup = asyncio.create_task(cleanup_loop())
    bg_tasks.add(cleanup)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
