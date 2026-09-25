import asyncio
import hashlib
import logging
import os
import random
import re
import time
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ============================================================
# NOLEN BOT
# Chat-to-Earn + Referral + Conversion + Withdrawals + Admin
# ============================================================
# Local:
#   py -m pip install -r requirements.txt
#   Copy .env.example -> .env and add BOT_TOKEN
#   py nolen_bot.py
#
# Render:
#   Use a paid Web Service + persistent disk mounted at /var/data.
#   The bot automatically uses RENDER_EXTERNAL_URL for webhook mode.
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "6330924087"))
# Official Nolen group is intentionally locked here so a stale Render
# GROUP_LINK/GROUP_ID variable cannot point users to an unrelated chat.
GROUP_ID = --1003538144204
GROUP_LINK = "https://t.me/nolen_chat"
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "nolen-webhook-2026").strip()

# Render persistent disk: set DATABASE_PATH=/var/data/nolen.db
# Local default: nolen.db next to this file.
DB_PATH = Path("/tmp/nolen.db")
# Requested hidden earning behavior.
# Normal users are NOT shown these thresholds.
HIDDEN_MIN_MESSAGES = 5
HIDDEN_MAX_MESSAGES = 7
HIDDEN_MIN_REWARD = 2
HIDDEN_MAX_REWARD = 5
HIDDEN_MESSAGE_COOLDOWN = 2.0
HIDDEN_DUPLICATE_WINDOW = 30.0
HIDDEN_SESSION_GAP = 300.0
HIDDEN_MAX_SECONDS_PER_GAP = 60.0
CLAIM_MIN_REWARD = 15
CLAIM_MAX_REWARD = 33

USD_PACKAGES = (2000, 5000, 9000, 15000, 20000)
STAR_PACKAGES = (
    (3300, 100),
    (7750, 250),
    (15500, 500),
    (31000, 1000),
)

DEFAULT_SETTINGS = {
    "usd_per_point": "0.0006",
    "referral_percent": "10",
    "daily_earning_cap": "5000",
    "global_earning_multiplier": "1.0",
    "min_usd_withdraw": "1.0",
    "min_usd_withdraw_day": "0",
    "maintenance": "0",
    "stars_stock": "0",
    "low_stock_alert": "1000",
}

USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{4,32}$")
POLYGON_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("nolen")


# ============================================================
# Database
# ============================================================

def db():
    import sqlite3

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now_iso():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def day_key():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date().isoformat()


def make_id(prefix):
    return f"{prefix}-{uuid4().hex[:10].upper()}"


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen TEXT,
                last_reset_day TEXT,

                joined_group INTEGER NOT NULL DEFAULT 0,
                banned INTEGER NOT NULL DEFAULT 0,
                earning_frozen INTEGER NOT NULL DEFAULT 0,

                nolen REAL NOT NULL DEFAULT 0,
                stars REAL NOT NULL DEFAULT 0,
                usd REAL NOT NULL DEFAULT 0,

                message_count INTEGER NOT NULL DEFAULT 0,
                daily_messages INTEGER NOT NULL DEFAULT 0,
                daily_earned REAL NOT NULL DEFAULT 0,
                chat_seconds REAL NOT NULL DEFAULT 0,

                last_message_ts REAL,
                last_message_hash TEXT,
                earning_progress INTEGER NOT NULL DEFAULT 0,
                next_trigger INTEGER NOT NULL DEFAULT 5,

                referred_by INTEGER,
                referral_activated INTEGER NOT NULL DEFAULT 0,
                referrals INTEGER NOT NULL DEFAULT 0,
                referral_earned REAL NOT NULL DEFAULT 0,
                earning_multiplier REAL NOT NULL DEFAULT 1.0,

                claim_streak INTEGER NOT NULL DEFAULT 0,
                last_claim_date TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_users_username
                ON users(username);
            CREATE INDEX IF NOT EXISTS idx_users_referred_by
                ON users(referred_by);

            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount REAL NOT NULL,
                direction TEXT NOT NULL,
                tx_type TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_transactions_user
                ON transactions(user_id);
            CREATE INDEX IF NOT EXISTS idx_transactions_created
                ON transactions(created_at);

            CREATE TABLE IF NOT EXISTS withdrawals (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount REAL NOT NULL,
                wallet TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                processed_at TEXT,
                processed_by INTEGER,
                note TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_withdrawals_status
                ON withdrawals(status);
            CREATE INDEX IF NOT EXISTS idx_withdrawals_user
                ON withdrawals(user_id);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id TEXT PRIMARY KEY,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_user_id INTEGER,
                details TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS support_messages (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                message_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                replied INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS group_warnings (
                id TEXT PRIMARY KEY,
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                admin_id INTEGER NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_group_warnings_user
                ON group_warnings(group_id, user_id, created_at);
            """
        )

        # Backward-compatible migrations for existing nolen.db files.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "referral_activated" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN referral_activated INTEGER NOT NULL DEFAULT 0")
        if "claim_streak" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN claim_streak INTEGER NOT NULL DEFAULT 0")
        if "last_claim_date" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_claim_date TEXT")
        conn.execute(
            "UPDATE users SET referral_activated = 1 WHERE referral_activated = 0 AND referred_by IS NOT NULL AND joined_group = 1"
        )

        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )


def get_setting(key, default=None):
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )


def get_user(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (user_id,),
        ).fetchone()


def get_user_by_username(username):
    username = username.strip().lstrip("@").lower()
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = ? LIMIT 1",
            (username,),
        ).fetchone()


def ensure_user(tg_user, referrer_id=None):
    user_id = tg_user.id
    username = (tg_user.username or "").strip()
    full_name = " ".join(
        part for part in [tg_user.first_name, tg_user.last_name] if part
    ).strip()
    timestamp = now_iso()
    today = day_key()

    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (user_id,),
        ).fetchone()

        if existing:
            # A referral can be attached only while the account has no referrer yet.
            if not existing["referred_by"] and referrer_id and referrer_id != user_id:
                ref = conn.execute(
                    "SELECT telegram_id FROM users WHERE telegram_id = ? AND banned = 0",
                    (referrer_id,),
                ).fetchone()
                if ref:
                    conn.execute(
                        """
                        UPDATE users
                        SET username = ?, full_name = ?, updated_at = ?, last_seen = ?, referred_by = ?
                        WHERE telegram_id = ?
                        """,
                        (username, full_name, timestamp, timestamp, referrer_id, user_id),
                    )
                    return False

            conn.execute(
                """
                UPDATE users
                SET username = ?, full_name = ?, updated_at = ?, last_seen = ?
                WHERE telegram_id = ?
                """,
                (username, full_name, timestamp, timestamp, user_id),
            )
            return False

        valid_referrer = None
        if referrer_id and referrer_id != user_id:
            ref = conn.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = ? AND banned = 0",
                (referrer_id,),
            ).fetchone()
            if ref:
                valid_referrer = referrer_id

        conn.execute(
            """
            INSERT INTO users(
                telegram_id, username, full_name, created_at, updated_at,
                last_seen, last_reset_day, next_trigger, referred_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                full_name,
                timestamp,
                timestamp,
                timestamp,
                today,
                random.randint(HIDDEN_MIN_MESSAGES, HIDDEN_MAX_MESSAGES),
                valid_referrer,
            ),
        )

    return True


def set_joined(user_id, joined):
    with db() as conn:
        conn.execute(
            "UPDATE users SET joined_group = ?, updated_at = ? WHERE telegram_id = ?",
            (1 if joined else 0, now_iso(), user_id),
        )


def activate_referral(user_id):
    """Activate a referral exactly once, after the referred user joins the official group."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT referred_by, referral_activated FROM users WHERE telegram_id = ?",
            (user_id,),
        ).fetchone()
        if not row or not row["referred_by"] or row["referral_activated"]:
            conn.commit()
            return False

        referrer_id = row["referred_by"]
        ref = conn.execute(
            "SELECT telegram_id, banned FROM users WHERE telegram_id = ?",
            (referrer_id,),
        ).fetchone()
        if not ref or ref["banned"]:
            conn.execute(
                "UPDATE users SET referral_activated = 1, updated_at = ? WHERE telegram_id = ?",
                (now_iso(), user_id),
            )
            conn.commit()
            return False

        conn.execute(
            "UPDATE users SET referral_activated = 1, updated_at = ? WHERE telegram_id = ?",
            (now_iso(), user_id),
        )
        conn.execute(
            "UPDATE users SET referrals = referrals + 1, updated_at = ? WHERE telegram_id = ?",
            (now_iso(), referrer_id),
        )
        conn.commit()
        return True


async def is_group_admin(bot, user_id):
    """True for official group admins/owner, plus the configured bot owner."""
    if int(user_id) == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(GROUP_ID, user_id)
        return member.status in {"administrator", "creator"}
    except (BadRequest, Forbidden, TelegramError):
        return False


def set_banned(user_id, banned):
    with db() as conn:
        conn.execute(
            "UPDATE users SET banned = ?, updated_at = ? WHERE telegram_id = ?",
            (1 if banned else 0, now_iso(), user_id),
        )


def set_earning_frozen(user_id, frozen):
    with db() as conn:
        conn.execute(
            "UPDATE users SET earning_frozen = ?, updated_at = ? WHERE telegram_id = ?",
            (1 if frozen else 0, now_iso(), user_id),
        )


def audit(admin_id, action, target_user_id=None, details=""):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs(id, admin_id, action, target_user_id, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (make_id("AU"), admin_id, action, target_user_id, details, now_iso()),
        )


def add_tx(user_id, currency, amount, direction, tx_type, note=""):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO transactions(
                id, user_id, currency, amount, direction, tx_type, note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("TX"),
                user_id,
                currency,
                float(amount),
                direction,
                tx_type,
                note,
                now_iso(),
            ),
        )


# ============================================================
# Formatting / UI
# ============================================================

def fmt_num(value):
    return f"{float(value):,.2f}".rstrip("0").rstrip(".")


def fmt_usd(value):
    return f"${float(value):,.2f}"


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    pieces = []
    if days:
        pieces.append(f"{days}d")
    if hours:
        pieces.append(f"{hours}h")
    if minutes:
        pieces.append(f"{minutes}m")
    if secs or not pieces:
        pieces.append(f"{secs}s")
    return " ".join(pieces)


def user_label(row):
    return f"@{row['username']}" if row["username"] else str(row["telegram_id"])


def home_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👤 Profile", callback_data="profile"),
                InlineKeyboardButton("💰 Balance", callback_data="balance"),
            ],
            [
                InlineKeyboardButton("💱 Convert", callback_data="convert"),
                InlineKeyboardButton("💸 Withdraw", callback_data="withdraw"),
            ],
            [
                InlineKeyboardButton("👥 Refer", callback_data="refer"),
                InlineKeyboardButton("📊 Activity", callback_data="activity"),
            ],
            [
                InlineKeyboardButton("📜 History", callback_data="history"),
                InlineKeyboardButton("🆘 Support", callback_data="support"),
            ],
            [InlineKeyboardButton("📖 Help", callback_data="help")],
        ]
    )


def home_back():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Home", callback_data="home")]]
    )


def cancel_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
    )


def admin_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Dashboard", callback_data="adm_dashboard"),
                InlineKeyboardButton("🔎 Search User", callback_data="adm_search"),
            ],
            [
                InlineKeyboardButton("💸 Requests", callback_data="adm_requests"),
                InlineKeyboardButton("⭐ Star Stock", callback_data="adm_stock"),
            ],
            [
                InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast"),
                InlineKeyboardButton("🎫 Support", callback_data="adm_support"),
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="adm_settings"),
                InlineKeyboardButton("📋 Audit Logs", callback_data="adm_audit"),
            ],
            [InlineKeyboardButton("🛡 Controls", callback_data="adm_controls")],
            [InlineKeyboardButton("❌ Close", callback_data="adm_close")],
        ]
    )


def join_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 Join Nolen Chat", url=GROUP_LINK)],
            [InlineKeyboardButton("✅ Check Membership", callback_data="check_join")],
        ]
    )


async def edit_or_reply(query, text, keyboard=None, parse_mode=None):
    try:
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode=parse_mode)
    except TelegramError:
        await query.message.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode)


# ============================================================
# Telegram access checks
# ============================================================

async def is_group_member(bot, user_id):
    try:
        member = await bot.get_chat_member(GROUP_ID, user_id)
        if member.status in {"member", "administrator", "creator"}:
            return True
        if member.status == "restricted":
            return bool(getattr(member, "is_member", False))
        return False
    except (BadRequest, Forbidden, TelegramError):
        return False


def is_admin(user_id):
    return int(user_id) == ADMIN_ID


async def show_join_gate(bot, user_id, message=None, edit=False):
    text = (
        "👋 <b>Welcome to Nolen!</b>\n\n"
        "Join our official chat group to unlock your Nolen profile.\n\n"
        "After joining, tap <b>✅ Check Membership</b>."
    )
    if edit and message:
        try:
            await message.edit_text(text, reply_markup=join_keyboard(), parse_mode="HTML")
            return
        except TelegramError:
            pass
    await bot.send_message(user_id, text, reply_markup=join_keyboard(), parse_mode="HTML")


async def user_access_allowed(update, context, callback=False):
    user = update.effective_user
    if not user:
        return False
    if is_admin(user.id):
        return True

    ensure_user(user)

    if get_setting("maintenance", "0") == "1":
        if callback:
            await update.callback_query.answer("Nolen is under maintenance.", show_alert=True)
        else:
            await update.effective_message.reply_text("🛠 Nolen is currently under maintenance.")
        return False

    row = get_user(user.id)
    if row and row["banned"]:
        if callback:
            await update.callback_query.answer("Your account is restricted.", show_alert=True)
        else:
            await update.effective_message.reply_text("🚫 Your Nolen account is restricted.")
        return False

    member = await is_group_member(context.bot, user.id)
    set_joined(user.id, member)
    if not member:
        if callback:
            await update.callback_query.answer("Join the official group first.", show_alert=True)
            await show_join_gate(context.bot, user.id, update.callback_query.message, edit=True)
        else:
            await show_join_gate(context.bot, user.id, update.effective_message, edit=False)
        return False
    return True


# ============================================================
# User pages
# ============================================================

async def show_home(query, context):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    row = get_user(query.from_user.id)
    name = row["full_name"] or query.from_user.first_name or "User"
    text = (
        f"🌟 <b>Welcome to Nolen, {name}</b>\n\n"
        "💬 Chat in the official group and earn Nolen Points.\n"
        "💱 Convert your points into supported balances.\n"
        "👥 Invite friends and earn referral rewards.\n\n"
        "Choose an option below."
    )
    await edit_or_reply(query, text, home_keyboard(), "HTML")


async def show_profile(query, context):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    row = get_user(query.from_user.id)
    text = (
        "👤 <b>Your Profile</b>\n\n"
        f"Name: {row['full_name'] or '-'}\n"
        f"Username: {user_label(row)}\n"
        f"User ID: <code>{row['telegram_id']}</code>\n\n"
        f"💬 Total messages: <b>{row['message_count']:,}</b>\n"
        f"⏱ Active chatting time: <b>{fmt_duration(row['chat_seconds'])}</b>\n"
        f"👥 Referrals: <b>{row['referrals']:,}</b>\n"
        f"💸 Referral earnings: <b>{fmt_num(row['referral_earned'])}</b> Nolen"
    )
    await edit_or_reply(
        query,
        text,
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📊 Activity", callback_data="activity")],
                [InlineKeyboardButton("🏠 Home", callback_data="home")],
            ]
        ),
        "HTML",
    )


async def show_balance(query, context):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    row = get_user(query.from_user.id)
    text = (
        "💰 <b>Your Balance</b>\n\n"
        f"🪙 Nolen Points: <b>{fmt_num(row['nolen'])}</b>\n"
        f"⭐ Telegram Stars: <b>{fmt_num(row['stars'])}</b>\n"
        f"💵 USD: <b>{fmt_usd(row['usd'])}</b>"
    )
    await edit_or_reply(query, text, home_back(), "HTML")


async def show_activity(query, context):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    row = get_user(query.from_user.id)
    cap = float(get_setting("daily_earning_cap", "5000"))
    text = (
        "📊 <b>Your Activity</b>\n\n"
        f"Today messages: <b>{row['daily_messages']:,}</b>\n"
        f"Today earned: <b>{fmt_num(row['daily_earned'])}</b> Nolen\n"
        f"Daily earning cap: <b>{fmt_num(cap)}</b> Nolen\n\n"
        f"Lifetime messages: <b>{row['message_count']:,}</b>\n"
        f"Active chatting time: <b>{fmt_duration(row['chat_seconds'])}</b>"
    )
    await edit_or_reply(query, text, home_back(), "HTML")


async def show_history(query, context):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    with db() as conn:
        txs = conn.execute(
            """
            SELECT id, currency, amount, direction, tx_type, note, created_at
            FROM transactions WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 15
            """,
            (query.from_user.id,),
        ).fetchall()
        wds = conn.execute(
            """
            SELECT id, currency, amount, status, created_at
            FROM withdrawals WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 10
            """,
            (query.from_user.id,),
        ).fetchall()

    lines = ["📜 <b>Recent History</b>", ""]
    if txs:
        for tx in txs:
            sign = "+" if tx["direction"] == "credit" else "-"
            if tx["currency"] == "USD":
                amount = fmt_usd(tx["amount"])
            elif tx["currency"] == "Stars":
                amount = f"{fmt_num(tx['amount'])} ⭐"
            else:
                amount = f"{fmt_num(tx['amount'])} Nolen"
            lines.append(f"{sign}{amount} · {tx['tx_type']} · <code>{tx['id']}</code>")
            if tx["note"]:
                lines.append(f"  {tx['note']}")
    else:
        lines.append("No transactions yet.")

    lines += ["", "💸 <b>Withdrawals</b>"]
    if wds:
        for w in wds:
            amount = fmt_usd(w["amount"]) if w["currency"] == "USD" else f"{fmt_num(w['amount'])} ⭐"
            lines.append(f"{w['id']} · {amount} · <b>{w['status']}</b>")
    else:
        lines.append("No withdrawals yet.")

    await edit_or_reply(query, "\n".join(lines), home_back(), "HTML")


async def show_refer(query, context):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    row = get_user(query.from_user.id)
    bot_username = context.bot_data.get("bot_username", "")
    link = f"https://t.me/{bot_username}?start=ref_{query.from_user.id}"
    pct = float(get_setting("referral_percent", "10"))
    text = (
        "👥 <b>Referral Center</b>\n\n"
        f"Referral rate: <b>{pct:g}%</b> of eligible referred-user chat earnings.\n"
        f"Referrals: <b>{row['referrals']:,}</b>\n"
        f"Referral earnings: <b>{fmt_num(row['referral_earned'])}</b> Nolen\n\n"
        f"🔗 Your referral link:\n<code>{link}</code>"
    )
    await edit_or_reply(query, text, home_back(), "HTML")


async def show_help(query, context):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    text = (
        "📖 <b>Nolen Help</b>\n\n"
        "• Join the official group to unlock your profile.\n"
        "• Chat normally to earn Nolen Points.\n"
        "• Convert Nolen Points into supported currencies.\n"
        "• Request USD or Stars withdrawals.\n"
        "• Refer friends for referral earnings.\n\n"
        "⚠️ Spam, automation, repeated messages, referral abuse, or other manipulation may cause restrictions."
    )
    await edit_or_reply(query, text, home_back(), "HTML")


# ============================================================
# Conversion
# ============================================================

async def show_convert(query, context):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return

    rate = float(get_setting("usd_per_point", "0.0006"))
    rows = []
    for points in USD_PACKAGES:
        rows.append([
            InlineKeyboardButton(
                f"🪙 {points:,} → {fmt_usd(points * rate)}",
                callback_data=f"conv_u_{points}",
            )
        ])

    rows.append([InlineKeyboardButton("⭐ Nolen → Stars", callback_data="noop")])
    for points, stars in STAR_PACKAGES:
        rows.append([
            InlineKeyboardButton(
                f"🪙 {points:,} → ⭐ {stars:,}",
                callback_data=f"conv_s_{points}_{stars}",
            )
        ])
    rows.append([InlineKeyboardButton("🏠 Home", callback_data="home")])

    text = "💱 <b>Convert Nolen Points</b>\n\n💵 <b>Nolen → USD</b>\nChoose a package:\n\n⭐ <b>Nolen → Stars</b>\nChoose a package:"
    await edit_or_reply(query, text, InlineKeyboardMarkup(rows), "HTML")


async def convert_usd(query, context, points):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    rate = float(get_setting("usd_per_point", "0.0006"))
    usd_amount = points * rate
    txid = make_id("TX")

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT nolen, banned FROM users WHERE telegram_id = ?", (query.from_user.id,)).fetchone()
        if not row or row["banned"]:
            conn.rollback()
            await query.answer("Account unavailable.", show_alert=True)
            return
        if row["nolen"] + 1e-9 < points:
            conn.rollback()
            await query.answer("Insufficient Nolen Points.", show_alert=True)
            return

        ts = now_iso()
        conn.execute(
            "UPDATE users SET nolen = nolen - ?, usd = usd + ?, updated_at = ? WHERE telegram_id = ?",
            (points, usd_amount, ts, query.from_user.id),
        )
        conn.execute(
            """
            INSERT INTO transactions(id, user_id, currency, amount, direction, tx_type, note, created_at)
            VALUES (?, ?, 'Nolen', ?, 'debit', 'CONVERT_USD', ?, ?)
            """,
            (txid, query.from_user.id, points, f"Converted to {fmt_usd(usd_amount)}", ts),
        )
        conn.execute(
            """
            INSERT INTO transactions(id, user_id, currency, amount, direction, tx_type, note, created_at)
            VALUES (?, ?, 'USD', ?, 'credit', 'CONVERT_USD', ?, ?)
            """,
            (make_id("TX"), query.from_user.id, usd_amount, f"From {fmt_num(points)} Nolen", ts),
        )
        conn.commit()

    await query.answer("Conversion successful.")
    await edit_or_reply(
        query,
        f"✅ <b>Conversion Successful</b>\n\n🪙 Spent: {fmt_num(points)} Nolen\n💵 Added: {fmt_usd(usd_amount)}\n\nTransaction: <code>{txid}</code>",
        home_back(),
        "HTML",
    )


async def convert_stars(query, context, points, stars):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    txid = make_id("TX")

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT nolen, banned FROM users WHERE telegram_id = ?",
            (query.from_user.id,),
        ).fetchone()
        stock_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'stars_stock'"
        ).fetchone()
        stock = float(stock_row["value"]) if stock_row else 0.0

        if not row or row["banned"]:
            conn.rollback()
            await query.answer("Account unavailable.", show_alert=True)
            return
        if row["nolen"] + 1e-9 < points:
            conn.rollback()
            await query.answer("Insufficient Nolen Points.", show_alert=True)
            return
        if stock + 1e-9 < stars:
            conn.rollback()
            await query.answer("Not enough Stars stock available.", show_alert=True)
            return

        ts = now_iso()
        conn.execute(
            "UPDATE users SET nolen = nolen - ?, stars = stars + ?, updated_at = ? WHERE telegram_id = ?",
            (points, stars, ts, query.from_user.id),
        )
        # Atomically consume the payout stock with the conversion.
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'stars_stock'",
            (str(stock - stars),),
        )
        conn.execute(
            """
            INSERT INTO transactions(id, user_id, currency, amount, direction, tx_type, note, created_at)
            VALUES (?, ?, 'Nolen', ?, 'debit', 'CONVERT_STARS', ?, ?)
            """,
            (txid, query.from_user.id, points, f"Converted to {stars} Stars", ts),
        )
        conn.execute(
            """
            INSERT INTO transactions(id, user_id, currency, amount, direction, tx_type, note, created_at)
            VALUES (?, ?, 'Stars', ?, 'credit', 'CONVERT_STARS', ?, ?)
            """,
            (make_id("TX"), query.from_user.id, stars, f"From {fmt_num(points)} Nolen", ts),
        )
        conn.commit()

    await query.answer("Conversion successful.")
    await edit_or_reply(
        query,
        (
            f"✅ <b>Conversion Successful</b>\n\n"
            f"🪙 Spent: {fmt_num(points)} Nolen\n"
            f"⭐ Added: {stars:,} Stars\n"
            f"📦 Stock left: {fmt_num(stock - stars)} Stars\n\n"
            f"Transaction: <code>{txid}</code>"
        ),
        home_back(),
        "HTML",
    )


# ============================================================
# Withdrawals
# ============================================================

async def show_withdraw(query, context):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    row = get_user(query.from_user.id)
    text = (
        "💸 <b>Withdraw</b>\n\n"
        f"⭐ Internal Stars balance: <b>{fmt_num(row['stars'])}</b>\n"
        f"💵 USD balance: <b>{fmt_usd(row['usd'])}</b>\n\n"
        "Choose withdrawal type:"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ Withdraw Stars", callback_data="wd_stars"),
            InlineKeyboardButton("💵 Withdraw USD", callback_data="wd_usd"),
        ],
        [InlineKeyboardButton("🏠 Home", callback_data="home")],
    ])
    await edit_or_reply(query, text, kb, "HTML")


async def show_star_withdraw(query, context):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    row = get_user(query.from_user.id)
    stock = float(get_setting("stars_stock", "0"))
    buttons = []
    for amount in (100, 250, 500, 1000):
        if row["stars"] >= amount and stock >= amount:
            buttons.append([InlineKeyboardButton(f"⭐ {amount:,} Stars", callback_data=f"wd_s_{amount}")])
        elif row["stars"] >= amount:
            buttons.append([InlineKeyboardButton(f"⭐ {amount:,} — Out of Stock", callback_data="noop")])
    if not buttons:
        buttons = [[InlineKeyboardButton("No available payout package", callback_data="noop")]]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="withdraw")])
    text = (
        "⭐ <b>Stars Withdrawal</b>\n\n"
        f"Your internal Stars balance: <b>{fmt_num(row['stars'])}</b>\n"
        f"Payout stock: <b>{fmt_num(stock)}</b> Stars\n\n"
        "Select an amount. Requests reserve stock until approved or rejected."
    )
    await edit_or_reply(query, text, InlineKeyboardMarkup(buttons), "HTML")


async def create_star_withdraw(query, context, amount):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT stars, banned, username FROM users WHERE telegram_id = ?", (query.from_user.id,)).fetchone()
        stock = float((conn.execute("SELECT value FROM settings WHERE key = 'stars_stock'").fetchone() or {"value": "0"})["value"])

        if not user or user["banned"]:
            conn.rollback()
            await query.answer("Account unavailable.", show_alert=True)
            return
        if user["stars"] + 1e-9 < amount:
            conn.rollback()
            await query.answer("Insufficient Stars balance.", show_alert=True)
            return
        if stock + 1e-9 < amount:
            conn.rollback()
            await query.answer("Not enough payout stock.", show_alert=True)
            return

        wid = make_id("WD")
        ts = now_iso()
        conn.execute("UPDATE users SET stars = stars - ?, updated_at = ? WHERE telegram_id = ?", (amount, ts, query.from_user.id))
        conn.execute("UPDATE settings SET value = ? WHERE key = 'stars_stock'", (str(stock - amount),))
        conn.execute(
            """
            INSERT INTO withdrawals(id, user_id, currency, amount, wallet, status, created_at)
            VALUES (?, ?, 'Stars', ?, NULL, 'pending', ?)
            """,
            (wid, query.from_user.id, amount, ts),
        )
        conn.execute(
            """
            INSERT INTO transactions(id, user_id, currency, amount, direction, tx_type, note, created_at)
            VALUES (?, ?, 'Stars', ?, 'debit', 'WITHDRAW_HOLD', ?, ?)
            """,
            (make_id("TX"), query.from_user.id, amount, f"Reserved for withdrawal {wid}", ts),
        )
        conn.commit()

    username = f"@{query.from_user.username}" if query.from_user.username else "-"
    await context.bot.send_message(
        ADMIN_ID,
        (
            "⭐ <b>New Stars Withdrawal</b>\n\n"
            f"Request: <code>{wid}</code>\n"
            f"User: {username}\n"
            f"User ID: <code>{query.from_user.id}</code>\n"
            f"Amount: <b>{amount:,} Stars</b>\n\n"
            "Complete the real supported Telegram payout, then press Confirm Paid."
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm Paid", callback_data=f"req_ok_{wid}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"req_no_{wid}"),
            ]
        ]),
    )

    await query.answer("Stars withdrawal submitted.")
    await edit_or_reply(
        query,
        f"✅ <b>Stars Withdrawal Submitted</b>\n\nAmount: <b>{amount:,} Stars</b>\nRequest ID: <code>{wid}</code>\n\nStatus: pending.",
        home_back(),
        "HTML",
    )


async def start_usd_withdraw(query, context):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    row = get_user(query.from_user.id)
    minimum = float(get_setting("min_usd_withdraw", "1.0"))
    if row["usd"] + 1e-9 < minimum:
        await query.answer(f"Minimum USD withdrawal is {fmt_usd(minimum)}.", show_alert=True)
        return
    context.user_data.clear()
    context.user_data["flow"] = "usd_amount"
    await edit_or_reply(
        query,
        f"💵 <b>USD Withdrawal</b>\n\nAvailable: <b>{fmt_usd(row['usd'])}</b>\nMinimum: <b>{fmt_usd(minimum)}</b>\n\nSend the USD amount you want to withdraw.",
        cancel_keyboard(),
        "HTML",
    )


async def process_usd_amount(update, context):
    message = update.effective_message
    try:
        amount = float((message.text or "").strip())
    except ValueError:
        await message.reply_text("Send a valid USD amount, for example 3 or 5.50.", reply_markup=cancel_keyboard())
        return

    minimum = float(get_setting("min_usd_withdraw", "1.0"))
    if amount < minimum:
        await message.reply_text(f"Minimum withdrawal is {fmt_usd(minimum)}.", reply_markup=cancel_keyboard())
        return

    row = get_user(message.from_user.id)
    if not row or row["usd"] + 1e-9 < amount:
        await message.reply_text("Insufficient USD balance.", reply_markup=cancel_keyboard())
        return

    context.user_data["usd_amount"] = round(amount, 8)
    context.user_data["flow"] = "usd_wallet"
    await message.reply_text(
        "💳 Send your <b>USDT (Polygon)</b> wallet address.\n\nExample: <code>0x...</code>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


async def process_usd_wallet(update, context):
    message = update.effective_message
    wallet = (message.text or "").strip()
    amount = float(context.user_data.get("usd_amount", 0))
    if not POLYGON_ADDRESS_RE.fullmatch(wallet):
        await message.reply_text("Invalid Polygon address. Send a valid 0x... wallet address.", reply_markup=cancel_keyboard())
        return

    # Optional: prevent more than one pending USD withdrawal at once.
    with db() as conn:
        pending = conn.execute(
            "SELECT id FROM withdrawals WHERE user_id = ? AND currency = 'USD' AND status = 'pending' LIMIT 1",
            (message.from_user.id,),
        ).fetchone()
        if pending:
            context.user_data.clear()
            await message.reply_text(
                f"You already have a pending USD withdrawal: {pending['id']}",
                reply_markup=home_keyboard(),
            )
            return

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT usd, banned FROM users WHERE telegram_id = ?", (message.from_user.id,)).fetchone()
        if not row or row["banned"]:
            conn.rollback()
            context.user_data.clear()
            await message.reply_text("Account unavailable.")
            return
        if row["usd"] + 1e-9 < amount:
            conn.rollback()
            context.user_data.clear()
            await message.reply_text("Your USD balance changed. Open Withdraw again.")
            return

        wid = make_id("WD")
        ts = now_iso()
        conn.execute("UPDATE users SET usd = usd - ?, updated_at = ? WHERE telegram_id = ?", (amount, ts, message.from_user.id))
        conn.execute(
            """
            INSERT INTO withdrawals(id, user_id, currency, amount, wallet, status, created_at)
            VALUES (?, ?, 'USD', ?, ?, 'pending', ?)
            """,
            (wid, message.from_user.id, amount, wallet, ts),
        )
        conn.execute(
            """
            INSERT INTO transactions(id, user_id, currency, amount, direction, tx_type, note, created_at)
            VALUES (?, ?, 'USD', ?, 'debit', 'WITHDRAW_HOLD', ?, ?)
            """,
            (make_id("TX"), message.from_user.id, amount, f"Reserved for withdrawal {wid}", ts),
        )
        conn.commit()

    context.user_data.clear()
    username = f"@{message.from_user.username}" if message.from_user.username else "-"
    await context.bot.send_message(
        ADMIN_ID,
        (
            "💵 <b>New USD Withdrawal</b>\n\n"
            f"Request: <code>{wid}</code>\n"
            f"User: {username}\n"
            f"User ID: <code>{message.from_user.id}</code>\n"
            f"Amount: <b>{fmt_usd(amount)}</b>\n"
            "Network: <b>Polygon</b>\n"
            f"Wallet: <code>{wallet}</code>\n\n"
            "Send the actual USDT payment, then press Confirm Paid."
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm Paid", callback_data=f"req_ok_{wid}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"req_no_{wid}"),
            ]
        ]),
    )
    await message.reply_text(
        f"✅ <b>USD Withdrawal Submitted</b>\n\nAmount: <b>{fmt_usd(amount)}</b>\nRequest ID: <code>{wid}</code>\n\nPlease allow up to 24 hours for processing.",
        parse_mode="HTML",
        reply_markup=home_keyboard(),
    )


# ============================================================
# Support
# ============================================================

async def start_support(query, context):
    if not await user_access_allowed(Update(update_id=0, callback_query=query), context, True):
        return
    context.user_data.clear()
    context.user_data["flow"] = "support"
    await edit_or_reply(query, "🆘 <b>Support</b>\n\nSend your support message.", cancel_keyboard(), "HTML")


async def handle_support(update, context):
    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        await message.reply_text("Please send a text support message.")
        return

    sid = make_id("SUP")
    with db() as conn:
        conn.execute(
            "INSERT INTO support_messages(id, user_id, message_text, created_at) VALUES (?, ?, ?, ?)",
            (sid, message.from_user.id, text[:4000], now_iso()),
        )

    username = f"@{message.from_user.username}" if message.from_user.username else "-"
    await context.bot.send_message(
        ADMIN_ID,
        (
            "🎫 <b>New Support Message</b>\n\n"
            f"Ticket: <code>{sid}</code>\n"
            f"User: {username}\n"
            f"User ID: <code>{message.from_user.id}</code>\n\n"
            f"{text[:4000]}"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Reply", callback_data=f"sup_reply_{message.from_user.id}")]
        ]),
    )

    context.user_data.clear()
    await message.reply_text("✅ Your message was sent to support.", reply_markup=home_keyboard())


# ============================================================
# Admin dashboard
# ============================================================

def admin_stats():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) AS c FROM users WHERE joined_group = 1 AND banned = 0").fetchone()["c"]
        banned = conn.execute("SELECT COUNT(*) AS c FROM users WHERE banned = 1").fetchone()["c"]
        frozen = conn.execute("SELECT COUNT(*) AS c FROM users WHERE earning_frozen = 1").fetchone()["c"]
        messages = conn.execute("SELECT COALESCE(SUM(message_count), 0) AS s FROM users").fetchone()["s"]
        nolen = conn.execute("SELECT COALESCE(SUM(nolen), 0) AS s FROM users").fetchone()["s"]
        stars = conn.execute("SELECT COALESCE(SUM(stars), 0) AS s FROM users").fetchone()["s"]
        usd = conn.execute("SELECT COALESCE(SUM(usd), 0) AS s FROM users").fetchone()["s"]
        pending = conn.execute("SELECT COUNT(*) AS c FROM withdrawals WHERE status = 'pending'").fetchone()["c"]
    return {
        "total": total,
        "active": active,
        "banned": banned,
        "frozen": frozen,
        "messages": messages,
        "nolen": nolen,
        "stars": stars,
        "usd": usd,
        "pending": pending,
        "stock": float(get_setting("stars_stock", "0")),
    }


async def show_admin(query, context):
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return
    s = admin_stats()
    text = (
        "🛡 <b>Nolen Admin Panel</b>\n\n"
        f"👥 Users: <b>{s['total']:,}</b>\n"
        f"🟢 Active group members: <b>{s['active']:,}</b>\n"
        f"🚫 Banned: <b>{s['banned']:,}</b>\n"
        f"⏸ Frozen earning: <b>{s['frozen']:,}</b>\n"
        f"💬 Messages tracked: <b>{s['messages']:,}</b>\n\n"
        f"🪙 User Nolen balance: <b>{fmt_num(s['nolen'])}</b>\n"
        f"⭐ User Stars balance: <b>{fmt_num(s['stars'])}</b>\n"
        f"💵 User USD balance: <b>{fmt_usd(s['usd'])}</b>\n"
        f"📦 Stars payout stock: <b>{fmt_num(s['stock'])}</b>\n"
        f"💸 Pending withdrawals: <b>{s['pending']:,}</b>"
    )
    await edit_or_reply(query, text, admin_keyboard(), "HTML")


async def show_admin_requests(query, context):
    if not is_admin(query.from_user.id):
        return
    with db() as conn:
        rows = conn.execute(
            """
            SELECT w.id, w.currency, w.amount, w.status, u.username, u.telegram_id
            FROM withdrawals w JOIN users u ON u.telegram_id = w.user_id
            WHERE w.status = 'pending' ORDER BY w.created_at ASC LIMIT 30
            """
        ).fetchall()

    lines = ["💸 <b>Pending Withdrawal Requests</b>", ""]
    buttons = []
    if not rows:
        lines.append("No pending requests.")
    else:
        for r in rows:
            name = f"@{r['username']}" if r["username"] else str(r["telegram_id"])
            amount = fmt_usd(r["amount"]) if r["currency"] == "USD" else f"{fmt_num(r['amount'])} ⭐"
            lines.append(f"<code>{r['id']}</code> · {name} · {amount}")
            buttons.append([InlineKeyboardButton(f"🔎 {r['id']}", callback_data=f"req_view_{r['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin")])
    await edit_or_reply(query, "\n".join(lines), InlineKeyboardMarkup(buttons), "HTML")


async def show_request(query, context, request_id):
    if not is_admin(query.from_user.id):
        return
    with db() as conn:
        r = conn.execute(
            """
            SELECT w.*, u.username, u.full_name
            FROM withdrawals w JOIN users u ON u.telegram_id = w.user_id
            WHERE w.id = ?
            """,
            (request_id,),
        ).fetchone()
    if not r:
        await query.answer("Request not found.", show_alert=True)
        return

    username = f"@{r['username']}" if r["username"] else "-"
    amount = fmt_usd(r["amount"]) if r["currency"] == "USD" else f"{fmt_num(r['amount'])} Stars"
    text = (
        "📄 <b>Withdrawal Request</b>\n\n"
        f"Request: <code>{r['id']}</code>\n"
        f"User: {username}\n"
        f"User ID: <code>{r['user_id']}</code>\n"
        f"Name: {r['full_name'] or '-'}\n"
        f"Currency: <b>{r['currency']}</b>\n"
        f"Amount: <b>{amount}</b>\n"
        f"Wallet: <code>{r['wallet'] or '-'}</code>\n"
        f"Status: <b>{r['status']}</b>\n"
        f"Created: {r['created_at']}"
    )
    buttons = []
    if r["status"] == "pending":
        buttons.append([
            InlineKeyboardButton("✅ Confirm Paid", callback_data=f"req_ok_{r['id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"req_no_{r['id']}"),
        ])
    buttons.append([InlineKeyboardButton("👤 Open User", callback_data=f"adm_user_{r['user_id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Requests", callback_data="adm_requests")])
    await edit_or_reply(query, text, InlineKeyboardMarkup(buttons), "HTML")


async def process_request(query, context, request_id, approve):
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        r = conn.execute("SELECT * FROM withdrawals WHERE id = ?", (request_id,)).fetchone()
        if not r or r["status"] != "pending":
            conn.rollback()
            await query.answer("Request is no longer pending.", show_alert=True)
            return

        ts = now_iso()
        status = "paid" if approve else "rejected"
        conn.execute(
            "UPDATE withdrawals SET status = ?, processed_at = ?, processed_by = ? WHERE id = ?",
            (status, ts, query.from_user.id, request_id),
        )

        if not approve:
            if r["currency"] == "USD":
                conn.execute(
                    "UPDATE users SET usd = usd + ?, updated_at = ? WHERE telegram_id = ?",
                    (r["amount"], ts, r["user_id"]),
                )
            else:
                conn.execute(
                    "UPDATE users SET stars = stars + ?, updated_at = ? WHERE telegram_id = ?",
                    (r["amount"], ts, r["user_id"]),
                )
                stock = float(get_setting("stars_stock", "0"))
                conn.execute("UPDATE settings SET value = ? WHERE key = 'stars_stock'", (str(stock + r["amount"]),))

            conn.execute(
                """
                INSERT INTO transactions(id, user_id, currency, amount, direction, tx_type, note, created_at)
                VALUES (?, ?, ?, ?, 'credit', 'WITHDRAW_REFUND', ?, ?)
                """,
                (make_id("TX"), r["user_id"], r["currency"], r["amount"], f"Refund for {request_id}", ts),
            )
        conn.commit()

    audit(query.from_user.id, "withdrawal_paid" if approve else "withdrawal_rejected", r["user_id"], request_id)

    try:
        if approve:
            msg = f"✅ <b>Withdrawal paid</b>\n\nRequest: <code>{request_id}</code>\nAmount: {r['amount']} {r['currency']}"
        else:
            msg = f"❌ <b>Withdrawal rejected</b>\n\nRequest: <code>{request_id}</code>\nYour reserved balance has been returned."
        await context.bot.send_message(r["user_id"], msg, parse_mode="HTML")
    except TelegramError:
        pass

    await query.answer("Updated.")
    await show_request(query, context, request_id)


async def show_admin_stock(query, context):
    if not is_admin(query.from_user.id):
        return
    stock = float(get_setting("stars_stock", "0"))
    low = float(get_setting("low_stock_alert", "1000"))
    text = f"⭐ <b>Stars Stock</b>\n\nCurrent: <b>{fmt_num(stock)}</b> Stars\nLow-stock alert: <b>{fmt_num(low)}</b>\n\nUse Restock or Stock Out."
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Restock", callback_data="stock_add"),
            InlineKeyboardButton("➖ Stock Out", callback_data="stock_remove"),
        ],
        [InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin")],
    ])
    await edit_or_reply(query, text, kb, "HTML")


async def show_admin_settings(query, context):
    if not is_admin(query.from_user.id):
        return
    text = (
        "⚙️ <b>Bot Settings</b>\n\n"
        f"USD rate: <code>{get_setting('usd_per_point')}</code>\n"
        f"Referral %: <code>{get_setting('referral_percent')}</code>\n"
        f"Daily cap: <code>{get_setting('daily_earning_cap')}</code>\n"
        f"Global multiplier: <code>{get_setting('global_earning_multiplier')}</code>\n"
        f"Min USD withdrawal: <code>{get_setting('min_usd_withdraw')}</code>\n"
        f"Maintenance: <code>{get_setting('maintenance')}</code>\n"
        f"Low stock alert: <code>{get_setting('low_stock_alert')}</code>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("USD Rate", callback_data="set_usd_rate"),
            InlineKeyboardButton("Referral %", callback_data="set_ref_pct"),
        ],
        [
            InlineKeyboardButton("Daily Cap", callback_data="set_daily_cap"),
            InlineKeyboardButton("Global Mult.", callback_data="set_global_mult"),
        ],
        [
            InlineKeyboardButton("Min USD WD", callback_data="set_min_usd"),
            InlineKeyboardButton("Low Stock", callback_data="set_low_stock"),
        ],
        [InlineKeyboardButton("🛠 Toggle Maintenance", callback_data="toggle_maintenance")],
        [InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin")],
    ])
    await edit_or_reply(query, text, kb, "HTML")


async def show_admin_audit(query, context):
    if not is_admin(query.from_user.id):
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT action, target_user_id, details, created_at FROM audit_logs ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    lines = ["📋 <b>Recent Audit Logs</b>", ""]
    if not rows:
        lines.append("No logs yet.")
    else:
        for r in rows:
            lines.append(f"• {r['created_at']} · {r['action']} · user={r['target_user_id'] or '-'}")
            if r["details"]:
                lines.append(f"  {r['details'][:250]}")
    await edit_or_reply(
        query,
        "\n".join(lines),
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin")]]),
        "HTML",
    )


async def show_admin_controls(query, context):
    if not is_admin(query.from_user.id):
        return
    text = (
        "🛡 <b>User Controls</b>\n\n"
        "Open Search User to manage a specific account.\n\n"
        "Available: add/remove Nolen, earning multiplier, freeze/unfreeze earning, ban/unban."
    )
    await edit_or_reply(
        query,
        text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Search User", callback_data="adm_search")],
            [InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin")],
        ]),
        "HTML",
    )


async def show_admin_search(query, context):
    if not is_admin(query.from_user.id):
        return
    context.user_data.clear()
    context.user_data["admin_flow"] = "search"
    await edit_or_reply(query, "🔎 Send a username such as <code>@john</code> or a Telegram User ID.", cancel_keyboard(), "HTML")


async def show_admin_user(query, context, user_id):
    if not is_admin(query.from_user.id):
        return
    row = get_user(int(user_id))
    if not row:
        await query.answer("User not found.", show_alert=True)
        return

    statuses = []
    statuses.append("BANNED" if row["banned"] else "ACTIVE")
    if row["earning_frozen"]:
        statuses.append("EARNING FROZEN")
    statuses.append("GROUP MEMBER" if row["joined_group"] else "GROUP LEFT")
    ban_text = "✅ Unban" if row["banned"] else "🚫 Ban"
    freeze_text = "▶️ Unfreeze" if row["earning_frozen"] else "⏸ Freeze"

    text = (
        "👤 <b>User Control</b>\n\n"
        f"Name: {row['full_name'] or '-'}\n"
        f"Username: {user_label(row)}\n"
        f"ID: <code>{row['telegram_id']}</code>\n"
        f"Status: <b>{' | '.join(statuses)}</b>\n\n"
        f"🪙 Nolen: <b>{fmt_num(row['nolen'])}</b>\n"
        f"⭐ Stars: <b>{fmt_num(row['stars'])}</b>\n"
        f"💵 USD: <b>{fmt_usd(row['usd'])}</b>\n"
        f"💬 Messages: <b>{row['message_count']:,}</b>\n"
        f"⏱ Chat time: <b>{fmt_duration(row['chat_seconds'])}</b>\n"
        f"👥 Referrals: <b>{row['referrals']:,}</b>\n"
        f"🎚 Multiplier: <b>{row['earning_multiplier']:.2f}x</b>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Nolen", callback_data=f"u_add_{user_id}"),
            InlineKeyboardButton("➖ Remove Nolen", callback_data=f"u_rm_{user_id}"),
        ],
        [
            InlineKeyboardButton("🎚 Multiplier", callback_data=f"u_mult_{user_id}"),
            InlineKeyboardButton(freeze_text, callback_data=f"u_freeze_{user_id}"),
        ],
        [InlineKeyboardButton(ban_text, callback_data=f"u_ban_{user_id}")],
        [InlineKeyboardButton("⬅️ Search", callback_data="adm_search"), InlineKeyboardButton("🛡 Admin", callback_data="admin")],
    ])
    await edit_or_reply(query, text, kb, "HTML")


async def show_admin_broadcast(query, context):
    if not is_admin(query.from_user.id):
        return
    context.user_data.clear()
    context.user_data["admin_flow"] = "broadcast_target"
    await edit_or_reply(query, "📢 Send <code>all</code> to broadcast everyone, or send a username like <code>@user</code>.", cancel_keyboard(), "HTML")


# ============================================================
# Admin input flows
# ============================================================

async def handle_admin_text(update, context):
    message = update.effective_message
    text = (message.text or "").strip()
    flow = context.user_data.get("admin_flow")
    if not flow:
        await message.reply_text("Use /admin.", reply_markup=admin_keyboard())
        return

    if flow == "search":
        context.user_data.clear()
        row = get_user(int(text)) if text.isdigit() else get_user_by_username(text) if USERNAME_RE.fullmatch(text) else None
        if not row:
            await message.reply_text("User not found.", reply_markup=admin_keyboard())
            return
        await message.reply_text(
            f"✅ Found {user_label(row)} (ID {row['telegram_id']})",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 Open User", callback_data=f"adm_user_{row['telegram_id']}")], [InlineKeyboardButton("🛡 Admin", callback_data="admin")]]),
        )
        return

    if flow in {"stock_add", "stock_remove", "set_usd_rate", "set_ref_pct", "set_daily_cap", "set_global_mult", "set_min_usd", "set_low_stock"}:
        context.user_data.clear()
        await handle_admin_setting(message, context, flow, text)
        return

    if flow in {"user_add", "user_remove", "user_mult"}:
        target = int(context.user_data.get("target_user_id", 0))
        context.user_data.clear()
        await handle_admin_user_numeric(message, context, flow, target, text)
        return

    if flow == "broadcast_target":
        if text.lower() == "all":
            target = "all"
        else:
            row = get_user_by_username(text)
            if not row:
                await message.reply_text("User not found. Send @username or all.", reply_markup=cancel_keyboard())
                return
            target = str(row["telegram_id"])
        context.user_data["broadcast_target"] = target
        context.user_data["admin_flow"] = "broadcast_message"
        await message.reply_text("📢 Now send the message to broadcast.", reply_markup=cancel_keyboard())
        return

    if flow == "broadcast_message":
        target = context.user_data.get("broadcast_target")
        context.user_data.clear()
        await do_broadcast(message, context, text, target)
        return

    if flow.startswith("support_reply_"):
        target_user = int(flow.split("_")[-1])
        context.user_data.clear()
        try:
            await context.bot.send_message(target_user, f"🆘 <b>Support reply</b>\n\n{text}", parse_mode="HTML")
        except TelegramError:
            pass
        with db() as conn:
            conn.execute("UPDATE support_messages SET replied = 1 WHERE user_id = ? AND replied = 0", (target_user,))
        audit(message.from_user.id, "support_reply", target_user, text[:500])
        await message.reply_text("✅ Reply sent.", reply_markup=admin_keyboard())
        return


async def handle_admin_setting(message, context, flow, text):
    try:
        value = float(text)
        if flow == "stock_add" or flow == "stock_remove":
            if value <= 0:
                raise ValueError
            current = float(get_setting("stars_stock", "0"))
            new_stock = current + value if flow == "stock_add" else max(0.0, current - value)
            set_setting("stars_stock", new_stock)
            audit(message.from_user.id, flow, None, f"{current} -> {new_stock}")
            await message.reply_text(f"✅ Stars stock is now {fmt_num(new_stock)}.", reply_markup=admin_keyboard())
            if flow == "stock_add":
                await broadcast_text(context, f"⭐ Nolen Stars payout stock has been restocked. Current stock: {fmt_num(new_stock)} Stars.")
            return

        if flow == "set_usd_rate":
            if value <= 0: raise ValueError
            set_setting("usd_per_point", value)
        elif flow == "set_ref_pct":
            if not 0 <= value <= 100: raise ValueError
            set_setting("referral_percent", value)
        elif flow == "set_daily_cap":
            if value <= 0: raise ValueError
            set_setting("daily_earning_cap", value)
        elif flow == "set_global_mult":
            if value < 0: raise ValueError
            set_setting("global_earning_multiplier", value)
        elif flow == "set_min_usd":
            if value <= 0: raise ValueError
            set_setting("min_usd_withdraw", value)
        elif flow == "set_low_stock":
            if value < 0: raise ValueError
            set_setting("low_stock_alert", value)
        else:
            raise ValueError
    except ValueError:
        await message.reply_text("Invalid value. Try again.", reply_markup=admin_keyboard())
        return

    audit(message.from_user.id, flow, None, text)
    await message.reply_text("✅ Setting updated.", reply_markup=admin_keyboard())


async def handle_admin_user_numeric(message, context, flow, target_user_id, text):
    row = get_user(target_user_id)
    if not row:
        await message.reply_text("User not found.", reply_markup=admin_keyboard())
        return
    try:
        value = float(text)
    except ValueError:
        await message.reply_text("Send a valid number.", reply_markup=admin_keyboard())
        return

    if flow in {"user_add", "user_remove"}:
        if value <= 0:
            await message.reply_text("Value must be positive.", reply_markup=admin_keyboard())
            return
        delta = value if flow == "user_add" else -value
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if delta >= 0:
                conn.execute("UPDATE users SET nolen = nolen + ?, updated_at = ? WHERE telegram_id = ?", (delta, now_iso(), target_user_id))
                direction = "credit"
            else:
                conn.execute("UPDATE users SET nolen = MAX(0, nolen - ?), updated_at = ? WHERE telegram_id = ?", (abs(delta), now_iso(), target_user_id))
                direction = "debit"
            conn.execute(
                """
                INSERT INTO transactions(id, user_id, currency, amount, direction, tx_type, note, created_at)
                VALUES (?, ?, 'Nolen', ?, ?, 'ADMIN_ADJUST', ?, ?)
                """,
                (make_id("TX"), target_user_id, value, direction, f"Admin {message.from_user.id}", now_iso()),
            )
            conn.commit()
        audit(message.from_user.id, "admin_balance_adjust", target_user_id, f"{flow}: {value}")
        await message.reply_text("✅ User Nolen balance updated.", reply_markup=admin_keyboard())
        try:
            await context.bot.send_message(target_user_id, "ℹ️ Your Nolen balance was adjusted by an administrator.")
        except TelegramError:
            pass
        return

    if flow == "user_mult":
        if value < 0:
            await message.reply_text("Multiplier cannot be negative.", reply_markup=admin_keyboard())
            return
        with db() as conn:
            conn.execute(
                "UPDATE users SET earning_multiplier = ?, updated_at = ? WHERE telegram_id = ?",
                (value, now_iso(), target_user_id),
            )
        audit(message.from_user.id, "set_user_multiplier", target_user_id, str(value))
        await message.reply_text(f"✅ Multiplier set to {value:g}x.", reply_markup=admin_keyboard())


async def do_broadcast(message, context, text, target):
    if not text:
        await message.reply_text("Message is empty.", reply_markup=admin_keyboard())
        return
    if target == "all":
        await broadcast_text(context, text)
        audit(message.from_user.id, "broadcast_all", None, text[:500])
        await message.reply_text("✅ Broadcast started/completed.", reply_markup=admin_keyboard())
    else:
        try:
            await context.bot.send_message(int(target), text)
            audit(message.from_user.id, "broadcast_user", int(target), text[:500])
            await message.reply_text("✅ Message sent.", reply_markup=admin_keyboard())
        except TelegramError as exc:
            await message.reply_text(f"Could not send: {exc}", reply_markup=admin_keyboard())


async def broadcast_text(context, text):
    with db() as conn:
        rows = conn.execute("SELECT telegram_id FROM users WHERE banned = 0").fetchall()
    sent = 0
    failed = 0
    for row in rows:
        try:
            await context.bot.send_message(row["telegram_id"], text)
            sent += 1
        except TelegramError:
            failed += 1
        await asyncio.sleep(0.05)
    logger.info("Broadcast: sent=%s failed=%s", sent, failed)


# ============================================================
# Chat-to-earn engine
# ============================================================

def hash_text(text):
    normalized = " ".join((text or "").lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def process_group_message(user, text):
    if not user or user.is_bot or not text.strip():
        return {"reward": 0.0, "referral": 0.0}

    uid = user.id
    ts = time.time()
    text_hash = hash_text(text)

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (uid,)).fetchone()
        if not row:
            conn.rollback()
            return {"reward": 0.0, "referral": 0.0}
        if row["banned"] or row["earning_frozen"] or uid == ADMIN_ID:
            conn.rollback()
            return {"reward": 0.0, "referral": 0.0}

        last_ts = float(row["last_message_ts"] or 0)
        last_hash = row["last_message_hash"] or ""
        if last_ts and ts - last_ts < HIDDEN_MESSAGE_COOLDOWN:
            conn.rollback()
            return {"reward": 0.0, "referral": 0.0}
        if last_hash == text_hash and last_ts and ts - last_ts <= HIDDEN_DUPLICATE_WINDOW:
            conn.rollback()
            return {"reward": 0.0, "referral": 0.0}

        today = day_key()
        daily_messages = row["daily_messages"]
        daily_earned = row["daily_earned"]
        progress = row["earning_progress"]
        if row["last_reset_day"] != today:
            daily_messages = 0
            daily_earned = 0.0
            progress = 0

        chat_seconds = float(row["chat_seconds"])
        if last_ts and 0 < ts - last_ts <= HIDDEN_SESSION_GAP:
            chat_seconds += min(ts - last_ts, HIDDEN_MAX_SECONDS_PER_GAP)

        daily_messages += 1
        message_count = row["message_count"] + 1
        progress += 1
        trigger = int(row["next_trigger"] or random.randint(HIDDEN_MIN_MESSAGES, HIDDEN_MAX_MESSAGES))

        reward = 0.0
        referral_reward = 0.0
        cap = float(get_setting("daily_earning_cap", "5000"))
        global_mult = max(0.0, float(get_setting("global_earning_multiplier", "1.0")))
        personal_mult = max(0.0, float(row["earning_multiplier"] or 1.0))

        if progress >= trigger and daily_earned < cap:
            reward = random.randint(HIDDEN_MIN_REWARD, HIDDEN_MAX_REWARD) * global_mult * personal_mult
            reward = round(min(reward, cap - daily_earned), 2)
            progress = 0
            trigger = random.randint(HIDDEN_MIN_MESSAGES, HIDDEN_MAX_MESSAGES)

            if reward > 0:
                conn.execute("UPDATE users SET nolen = nolen + ? WHERE telegram_id = ?", (reward, uid))
                conn.execute(
                    """
                    INSERT INTO transactions(id, user_id, currency, amount, direction, tx_type, note, created_at)
                    VALUES (?, ?, 'Nolen', ?, 'credit', 'CHAT_EARNING', 'Chat activity reward', ?)
                    """,
                    (make_id("TX"), uid, reward, now_iso()),
                )

                referrer = row["referred_by"] if row["referral_activated"] else None
                if referrer:
                    ref = conn.execute(
                        "SELECT telegram_id, banned, earning_frozen FROM users WHERE telegram_id = ?",
                        (referrer,),
                    ).fetchone()
                    if ref and not ref["banned"] and not ref["earning_frozen"]:
                        pct = float(get_setting("referral_percent", "10"))
                        referral_reward = round(reward * pct / 100.0, 2)
                        if referral_reward > 0:
                            conn.execute(
                                "UPDATE users SET nolen = nolen + ?, referral_earned = referral_earned + ?, updated_at = ? WHERE telegram_id = ?",
                                (referral_reward, referral_reward, now_iso(), referrer),
                            )
                            conn.execute(
                                """
                                INSERT INTO transactions(id, user_id, currency, amount, direction, tx_type, note, created_at)
                                VALUES (?, ?, 'Nolen', ?, 'credit', 'REFERRAL_EARNING', ?, ?)
                                """,
                                (make_id("TX"), referrer, referral_reward, f"Referral from {uid}", now_iso()),
                            )

                daily_earned += reward

        conn.execute(
            """
            UPDATE users SET
                username = ?, full_name = ?, message_count = ?, daily_messages = ?,
                daily_earned = ?, chat_seconds = ?, last_message_ts = ?,
                last_message_hash = ?, earning_progress = ?, next_trigger = ?,
                last_seen = ?, last_reset_day = ?, updated_at = ?, joined_group = 1
            WHERE telegram_id = ?
            """,
            (
                (user.username or "").strip(),
                " ".join(p for p in [user.first_name, user.last_name] if p).strip(),
                message_count,
                daily_messages,
                daily_earned,
                chat_seconds,
                ts,
                text_hash,
                progress,
                trigger,
                now_iso(),
                today,
                now_iso(),
                uid,
            ),
        )
        conn.commit()

    return {"reward": reward, "referral": referral_reward}


async def group_message_handler(update, context):
    message = update.effective_message
    if not message or message.chat.id != GROUP_ID or not message.text:
        return
    user = message.from_user
    if not user or user.is_bot:
        return

    ensure_user(user)
    if not await is_group_member(context.bot, user.id):
        set_joined(user.id, False)
        return

    result = process_group_message(user, message.text)
    if result["reward"] > 0:
        try:
            await context.bot.send_message(
                user.id,
                f"🎉 You earned <b>{fmt_num(result['reward'])}</b> Nolen Points from chat activity.",
                parse_mode="HTML",
            )
        except TelegramError:
            pass
    if result["referral"] > 0:
        row = get_user(user.id)
        if row and row["referred_by"]:
            try:
                await context.bot.send_message(
                    row["referred_by"],
                    f"👥 Referral earning received: <b>{fmt_num(result['referral'])}</b> Nolen",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass


async def chat_member_handler(update, context):
    cm = update.chat_member
    if not cm or cm.chat.id != GROUP_ID:
        return
    user = cm.new_chat_member.user
    if user.is_bot:
        return
    ensure_user(user)
    status = cm.new_chat_member.status
    is_member = status in {"member", "administrator", "creator"} or (
        status == "restricted" and bool(getattr(cm.new_chat_member, "is_member", False))
    )
    set_joined(user.id, is_member)

    if is_member:
        activate_referral(user.id)
    if is_member and cm.old_chat_member.status in {"left", "kicked"}:
        try:
            await context.bot.send_message(
                user.id,
                "✅ <b>Membership confirmed.</b>\n\nYour Nolen profile is unlocked.",
                parse_mode="HTML",
                reply_markup=home_keyboard(),
            )
        except TelegramError:
            pass
    elif not is_member:
        try:
            await context.bot.send_message(
                user.id,
                "🔒 Your Nolen profile is locked because you left the official group. Join again to restore access.",
                reply_markup=join_keyboard(),
            )
        except TelegramError:
            pass


# ============================================================
# Callbacks
# ============================================================

async def callback_handler(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "noop":
        return
    if data == "cancel":
        context.user_data.clear()
        if is_admin(query.from_user.id):
            await show_admin(query, context)
        else:
            await show_home(query, context)
        return
    if data == "check_join":
        ensure_user(query.from_user)
        if await is_group_member(context.bot, query.from_user.id):
            set_joined(query.from_user.id, True)
            activate_referral(query.from_user.id)
            await edit_or_reply(query, "✅ <b>Membership confirmed!</b>\n\nYour profile is unlocked.", home_keyboard(), "HTML")
        else:
            await query.answer("Membership not confirmed yet.", show_alert=True)
            await show_join_gate(context.bot, query.from_user.id, query.message, edit=True)
        return

    user_callbacks = {
        "home": show_home,
        "profile": show_profile,
        "balance": show_balance,
        "activity": show_activity,
        "history": show_history,
        "refer": show_refer,
        "help": show_help,
        "convert": show_convert,
        "withdraw": show_withdraw,
        "support": start_support,
    }
    if data in user_callbacks:
        await user_callbacks[data](query, context)
        return

    if data.startswith("conv_u_"):
        await convert_usd(query, context, int(data.split("_")[-1]))
        return
    if data.startswith("conv_s_"):
        parts = data.split("_")
        await convert_stars(query, context, int(parts[2]), int(parts[3]))
        return
    if data == "wd_stars":
        await show_star_withdraw(query, context)
        return
    if data == "wd_usd":
        await start_usd_withdraw(query, context)
        return
    if data.startswith("wd_s_"):
        await create_star_withdraw(query, context, int(data.split("_")[-1]))
        return

    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    if data in {"admin", "adm_dashboard"}:
        await show_admin(query, context)
        return
    if data == "adm_requests":
        await show_admin_requests(query, context)
        return
    if data.startswith("req_view_"):
        await show_request(query, context, data[len("req_view_"):])
        return
    if data.startswith("req_ok_"):
        await process_request(query, context, data[len("req_ok_"):], True)
        return
    if data.startswith("req_no_"):
        await process_request(query, context, data[len("req_no_"):], False)
        return
    if data == "adm_stock":
        await show_admin_stock(query, context)
        return
    if data == "stock_add":
        context.user_data.clear()
        context.user_data["admin_flow"] = "stock_add"
        await edit_or_reply(query, "➕ Send the number of Stars to add to stock.", cancel_keyboard())
        return
    if data == "stock_remove":
        context.user_data.clear()
        context.user_data["admin_flow"] = "stock_remove"
        await edit_or_reply(query, "➖ Send the number of Stars to remove from stock.", cancel_keyboard())
        return
    if data == "adm_settings":
        await show_admin_settings(query, context)
        return
    if data in {"set_usd_rate", "set_ref_pct", "set_daily_cap", "set_global_mult", "set_min_usd", "set_low_stock"}:
        context.user_data.clear()
        context.user_data["admin_flow"] = data
        await edit_or_reply(query, f"Send the new value for <code>{data}</code>.", cancel_keyboard(), "HTML")
        return
    if data == "toggle_maintenance":
        current = get_setting("maintenance", "0")
        new = "0" if current == "1" else "1"
        set_setting("maintenance", new)
        audit(query.from_user.id, "toggle_maintenance", None, new)
        await show_admin_settings(query, context)
        return
    if data == "adm_audit":
        await show_admin_audit(query, context)
        return
    if data == "adm_controls":
        await show_admin_controls(query, context)
        return
    if data == "adm_search":
        await show_admin_search(query, context)
        return
    if data == "adm_broadcast":
        await show_admin_broadcast(query, context)
        return
    if data == "adm_support":
        await edit_or_reply(query, "🎫 Support messages arrive here with a Reply button.\n\nOpen a ticket notification in this chat to reply.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin")]]), "HTML")
        return
    if data.startswith("sup_reply_"):
        target = data[len("sup_reply_"):]
        context.user_data.clear()
        context.user_data["admin_flow"] = f"support_reply_{target}"
        await edit_or_reply(query, f"↩️ Send your reply for user <code>{target}</code>.", cancel_keyboard(), "HTML")
        return
    if data.startswith("adm_user_"):
        await show_admin_user(query, context, int(data[len("adm_user_"):]))
        return
    if data.startswith("u_add_"):
        target = int(data[len("u_add_"):])
        context.user_data.clear()
        context.user_data["admin_flow"] = "user_add"
        context.user_data["target_user_id"] = target
        await edit_or_reply(query, f"➕ How many Nolen Points should be added to <code>{target}</code>?", cancel_keyboard(), "HTML")
        return
    if data.startswith("u_rm_"):
        target = int(data[len("u_rm_"):])
        context.user_data.clear()
        context.user_data["admin_flow"] = "user_remove"
        context.user_data["target_user_id"] = target
        await edit_or_reply(query, f"➖ How many Nolen Points should be removed from <code>{target}</code>?", cancel_keyboard(), "HTML")
        return
    if data.startswith("u_mult_"):
        target = int(data[len("u_mult_"):])
        context.user_data.clear()
        context.user_data["admin_flow"] = "user_mult"
        context.user_data["target_user_id"] = target
        await edit_or_reply(query, "🎚 Send earning multiplier. Example: <code>2</code> = 2x.", cancel_keyboard(), "HTML")
        return
    if data.startswith("u_freeze_"):
        target = int(data[len("u_freeze_"):])
        row = get_user(target)
        if row:
            new_state = 0 if row["earning_frozen"] else 1
            set_earning_frozen(target, new_state)
            audit(query.from_user.id, "toggle_earning_freeze", target, str(new_state))
        await show_admin_user(query, context, target)
        return
    if data.startswith("u_ban_"):
        target = int(data[len("u_ban_"):])
        row = get_user(target)
        if row:
            new_state = 0 if row["banned"] else 1
            set_banned(target, new_state)
            audit(query.from_user.id, "toggle_ban", target, str(new_state))
            try:
                await context.bot.send_message(target, "🚫 Your Nolen account was restricted by an administrator." if new_state else "✅ Your Nolen account restriction was removed.")
            except TelegramError:
                pass
        await show_admin_user(query, context, target)
        return
    if data == "adm_close":
        try:
            await query.message.delete()
        except TelegramError:
            pass
        return


# ============================================================
# Commands / private messages
# ============================================================

async def start_command(update, context):
    # /start is private-chat only. In the group it does absolutely nothing.
    if not update.effective_chat or update.effective_chat.type != "private":
        return

    user = update.effective_user
    referrer_id = None
    if context.args:
        arg = context.args[0].strip()
        if arg.startswith("ref_") and arg[4:].isdigit():
            candidate = int(arg[4:])
            if candidate != user.id:
                referrer_id = candidate

    ensure_user(user, referrer_id=referrer_id)

    if is_admin(user.id):
        await update.message.reply_text(
            "🛡 <b>Nolen Admin</b>\n\nUse /admin.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛡️ Admin Panel", callback_data="admin")]]),
        )
        return

    if not await is_group_member(context.bot, user.id):
        set_joined(user.id, False)
        await show_join_gate(context.bot, user.id, update.message, edit=False)
        return

    set_joined(user.id, True)
    activate_referral(user.id)
    await update.message.reply_text(
        "✅ <b>Nolen unlocked.</b>\n\nYour profile is ready.",
        parse_mode="HTML",
        reply_markup=home_keyboard(),
    )


async def admin_command(update, context):
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    ensure_user(update.effective_user)
    await update.message.reply_text(
        "🛡️ <b>Nolen Admin Panel</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


async def id_command(update, context):
    if not update.effective_chat or update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        f"Your Telegram User ID: <code>{update.effective_user.id}</code>",
        parse_mode="HTML",
    )


# ============================================================
# Group commands: profile / daily claim / moderation / support
# ============================================================

def group_target_from_update(update, context, require_target=True):
    """Return (target_user, extra_args). Replies are preferred; otherwise username/ID."""
    message = update.effective_message
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        return target, list(context.args)

    args = list(context.args)
    if not args:
        return (None, args) if require_target else (message.from_user, args)

    token = args.pop(0).strip()
    target = None
    if token.startswith("@"):
        row = get_user_by_username(token)
        if row:
            target = type("TargetUser", (), {
                "id": int(row["telegram_id"]),
                "is_bot": False,
                "username": row["username"],
                "first_name": row["full_name"] or row["username"] or "User",
                "last_name": None,
            })()
    elif token.isdigit():
        try:
            target = awaitable_noop_target(int(token))
        except Exception:
            target = None

    return target, args


def awaitable_noop_target(user_id):
    return type("TargetUser", (), {
        "id": int(user_id),
        "is_bot": False,
        "username": None,
        "first_name": "User",
        "last_name": None,
    })()


async def get_target_user(update, context):
    message = update.effective_message
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user, list(context.args)

    args = list(context.args)
    if not args:
        return None, args
    token = args.pop(0).strip()
    if token.startswith("@"):
        row = get_user_by_username(token)
        if not row:
            return None, args
        ensure_user(type("TargetUser", (), {
            "id": int(row["telegram_id"]),
            "is_bot": False,
            "username": row["username"],
            "first_name": row["full_name"] or "User",
            "last_name": None,
        })())
        return type("TargetUser", (), {
            "id": int(row["telegram_id"]),
            "is_bot": False,
            "username": row["username"],
            "first_name": row["full_name"] or "User",
            "last_name": None,
        })(), args
    if token.isdigit():
        row = get_user(int(token))
        if not row:
            return None, args
        return type("TargetUser", (), {
            "id": int(row["telegram_id"]),
            "is_bot": False,
            "username": row["username"],
            "first_name": row["full_name"] or "User",
            "last_name": None,
        })(), args
    return None, args


async def group_profile_command(update, context):
    message = update.effective_message
    if not message or message.chat.id != GROUP_ID:
        return
    user = update.effective_user
    ensure_user(user)
    member = await is_group_member(context.bot, user.id)
    if not member:
        set_joined(user.id, False)
        return
    set_joined(user.id, True)
    activate_referral(user.id)

    row = get_user(user.id)
    username = f"@{user.username}" if user.username else "—"
    caption = (
        "👤 <b>Nolen Profile</b>\n"
        f"<b>{(row['full_name'] or user.first_name or 'User')[:60]}</b>\n"
        f"{username}\n\n"
        f"💬 Messages: <b>{row['message_count']:,}</b>\n"
        f"⏱ Active: <b>{fmt_duration(row['chat_seconds'])}</b>\n"
        f"🪙 Nolen: <b>{fmt_num(row['nolen'])}</b>\n"
        f"🔥 Claim streak: <b>{row['claim_streak']}d</b>"
    )

    try:
        photos = await context.bot.get_user_profile_photos(user.id, limit=1)
        if photos.photos:
            file_id = photos.photos[0][-1].file_id
            await message.reply_photo(photo=file_id, caption=caption, parse_mode="HTML")
            return
    except TelegramError:
        pass
    await message.reply_text(caption, parse_mode="HTML")


async def group_claim_command(update, context):
    message = update.effective_message
    if not message or message.chat.id != GROUP_ID:
        return
    user = update.effective_user
    if user.is_bot:
        return
    ensure_user(user)
    if not await is_group_member(context.bot, user.id):
        set_joined(user.id, False)
        return
    set_joined(user.id, True)
    activate_referral(user.id)

    today = day_key()
    reward = 0.0
    referral_reward = 0.0
    streak = 0

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT nolen, banned, claim_streak, last_claim_date, referred_by, referral_activated, daily_earned FROM users WHERE telegram_id = ?",
            (user.id,),
        ).fetchone()
        if not row or row["banned"]:
            conn.rollback()
            return
        if row["last_claim_date"] == today:
            conn.rollback()
            await message.reply_text("🎁 <b>Daily Claim already collected.</b>\nCome back tomorrow.", parse_mode="HTML")
            return

        streak = int(row["claim_streak"] or 0)
        last_claim = row["last_claim_date"]
        try:
            from datetime import date, timedelta
            prev = date.fromisoformat(last_claim) if last_claim else None
            today_date = date.fromisoformat(today)
            if prev and today_date == prev + timedelta(days=1):
                streak += 1
            else:
                streak = 1
        except Exception:
            streak = 1

        cap = float(get_setting("daily_earning_cap", "5000"))
        remaining = max(0.0, cap - float(row["daily_earned"] or 0))
        reward = round(min(random.randint(CLAIM_MIN_REWARD, CLAIM_MAX_REWARD), remaining), 2)
        if reward <= 0:
            conn.rollback()
            await message.reply_text("⚠️ Daily earning limit reached. Try again tomorrow.", parse_mode="HTML")
            return

        ts = now_iso()
        conn.execute(
            "UPDATE users SET nolen = nolen + ?, claim_streak = ?, last_claim_date = ?, daily_earned = daily_earned + ?, updated_at = ? WHERE telegram_id = ?",
            (reward, streak, today, reward, ts, user.id),
        )
        conn.execute(
            """
            INSERT INTO transactions(id, user_id, currency, amount, direction, tx_type, note, created_at)
            VALUES (?, ?, 'Nolen', ?, 'credit', 'DAILY_CLAIM', ?, ?)
            """,
            (make_id("TX"), user.id, reward, f"Daily claim streak {streak}d", ts),
        )

        if row["referred_by"] and row["referral_activated"]:
            ref = conn.execute(
                "SELECT telegram_id, banned, earning_frozen FROM users WHERE telegram_id = ?",
                (row["referred_by"],),
            ).fetchone()
            if ref and not ref["banned"] and not ref["earning_frozen"]:
                pct = float(get_setting("referral_percent", "10"))
                referral_reward = round(reward * pct / 100.0, 2)
                if referral_reward > 0:
                    conn.execute(
                        "UPDATE users SET nolen = nolen + ?, referral_earned = referral_earned + ?, updated_at = ? WHERE telegram_id = ?",
                        (referral_reward, referral_reward, ts, row["referred_by"]),
                    )
                    conn.execute(
                        """
                        INSERT INTO transactions(id, user_id, currency, amount, direction, tx_type, note, created_at)
                        VALUES (?, ?, 'Nolen', ?, 'credit', 'REFERRAL_EARNING', ?, ?)
                        """,
                        (make_id("TX"), row["referred_by"], referral_reward, f"Claim referral from {user.id}", ts),
                    )
        conn.commit()

    text = f"🎁 <b>Daily Claim</b>\n\n+<b>{fmt_num(reward)}</b> Nolen Points\n🔥 Streak: <b>{streak} day{'s' if streak != 1 else ''}</b>"
    if referral_reward > 0:
        text += f"\n👥 Referral bonus generated: <b>{fmt_num(referral_reward)}</b> Nolen"
    await message.reply_text(text, parse_mode="HTML")


async def group_support_command(update, context):
    message = update.effective_message
    if not message or message.chat.id != GROUP_ID:
        return
    body = " ".join(context.args).strip()
    if message.reply_to_message and message.reply_to_message.text:
        body = body or message.reply_to_message.text.strip()
    if not body:
        await message.reply_text("🆘 Usage: <code>/support your message</code>", parse_mode="HTML")
        return
    user = update.effective_user
    ensure_user(user)
    username = f"@{user.username}" if user.username else "-"
    sid = make_id("SUP")
    with db() as conn:
        conn.execute(
            "INSERT INTO support_messages(id, user_id, message_text, created_at) VALUES (?, ?, ?, ?)",
            (sid, user.id, body[:4000], now_iso()),
        )
    try:
        await context.bot.send_message(
            ADMIN_ID,
            (
                "🎫 <b>Group Support Request</b>\n\n"
                f"Ticket: <code>{sid}</code>\n"
                f"User: {username}\n"
                f"User ID: <code>{user.id}</code>\n\n{body[:4000]}"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Reply", callback_data=f"sup_reply_{user.id}")]]),
        )
        await message.reply_text("✅ Support request sent to Nolen staff.")
    except TelegramError:
        await message.reply_text("⚠️ Support is temporarily unavailable.")


async def moderate_target(update, context, action):
    message = update.effective_message
    if not message or message.chat.id != GROUP_ID:
        return
    if not await is_group_admin(context.bot, update.effective_user.id):
        await message.reply_text("🛡️ This command is for group admins only.")
        return
    target, args = await get_target_user(update, context)
    if not target:
        await message.reply_text("Reply to a user or use <code>@username</code>/<code>UserID</code>.", parse_mode="HTML")
        return
    if target.id == update.effective_user.id:
        await message.reply_text("⚠️ Invalid target.")
        return
    if target.id == ADMIN_ID:
        await message.reply_text("⚠️ The main bot admin cannot be moderated here.")
        return

    target_row = get_user(target.id)
    target_name = f"@{target.username}" if getattr(target, "username", None) else (getattr(target, "first_name", None) or str(target.id))

    try:
        if action == "warn":
            reason = " ".join(args).strip() or "No reason provided"
            with db() as conn:
                conn.execute(
                    "INSERT INTO group_warnings(id, group_id, user_id, admin_id, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (make_id("WR"), GROUP_ID, target.id, update.effective_user.id, reason[:1000], now_iso()),
                )
                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM group_warnings WHERE group_id = ? AND user_id = ?",
                    (GROUP_ID, target.id),
                ).fetchone()["c"]
            await message.reply_text(f"⚠️ <b>{target_name}</b> warned. Total warnings: <b>{count}</b>.", parse_mode="HTML")
            audit(update.effective_user.id, "group_warn", target.id, reason[:500])
            return

        if action == "ban":
            reason = " ".join(args).strip() or "No reason provided"
            await context.bot.ban_chat_member(GROUP_ID, target.id)
            set_banned(target.id, True)
            audit(update.effective_user.id, "group_ban", target.id, reason[:500])
            await message.reply_text(f"🚫 <b>{target_name}</b> has been banned from Nolen Chat.", parse_mode="HTML")
            return

        if action == "unban":
            await context.bot.unban_chat_member(GROUP_ID, target.id, only_if_banned=True)
            set_banned(target.id, False)
            audit(update.effective_user.id, "group_unban", target.id, "")
            await message.reply_text(f"✅ <b>{target_name}</b> has been unbanned.", parse_mode="HTML")
            return

        if action == "mute":
            minutes = 10
            if args and args[0].isdigit():
                minutes = max(1, min(int(args[0]), 10080))
                args = args[1:]
            reason = " ".join(args).strip() or "No reason provided"
            from datetime import datetime, timedelta, timezone
            until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            await context.bot.restrict_chat_member(
                GROUP_ID,
                target.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            audit(update.effective_user.id, "group_mute", target.id, f"{minutes}m | {reason[:450]}")
            await message.reply_text(f"🔇 <b>{target_name}</b> muted for <b>{minutes}m</b>.", parse_mode="HTML")
            return

        if action == "unmute":
            try:
                permissions = ChatPermissions.all_permissions()
            except AttributeError:
                permissions = ChatPermissions(
                    can_send_messages=True,
                    can_send_audios=True,
                    can_send_documents=True,
                    can_send_photos=True,
                    can_send_videos=True,
                    can_send_video_notes=True,
                    can_send_voice_notes=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_invite_users=True,
                )
            await context.bot.restrict_chat_member(GROUP_ID, target.id, permissions=permissions)
            audit(update.effective_user.id, "group_unmute", target.id, "")
            await message.reply_text(f"🔊 <b>{target_name}</b> can chat again.", parse_mode="HTML")
            return
    except TelegramError as exc:
        await message.reply_text(f"⚠️ Telegram could not complete this action. Check that the bot is an admin with the required group permissions.\n\n<code>{str(exc)[:500]}</code>", parse_mode="HTML")


async def warn_command(update, context):
    await moderate_target(update, context, "warn")


async def ban_group_command(update, context):
    await moderate_target(update, context, "ban")


async def unban_group_command(update, context):
    await moderate_target(update, context, "unban")


async def mute_group_command(update, context):
    await moderate_target(update, context, "mute")


async def unmute_group_command(update, context):
    await moderate_target(update, context, "unmute")



async def private_message_handler(update, context):
    user = update.effective_user
    ensure_user(user)

    # Admin flows
    if is_admin(user.id) and context.user_data.get("admin_flow"):
        await handle_admin_text(update, context)
        return

    if not await user_access_allowed(update, context, False):
        return

    flow = context.user_data.get("flow")
    if flow == "usd_amount":
        await process_usd_amount(update, context)
        return
    if flow == "usd_wallet":
        await process_usd_wallet(update, context)
        return
    if flow == "support":
        await handle_support(update, context)
        return

    await update.message.reply_text("Use the Nolen menu below.", reply_markup=home_keyboard())


async def post_init(application: Application):
    me = await application.bot.get_me()
    application.bot_data["bot_username"] = me.username or ""
    logger.info("Running as @%s", me.username)
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Open Nolen"),
            BotCommand("id", "Show your Telegram ID"),
            BotCommand("admin", "Admin panel"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await application.bot.set_my_commands(
        [
            BotCommand("profile", "Show your mini profile"),
            BotCommand("claim", "Daily Nolen bonus"),
            BotCommand("support", "Contact Nolen support"),
            BotCommand("warn", "Admin: warn a member"),
            BotCommand("ban", "Admin: ban a member"),
            BotCommand("mute", "Admin: mute a member"),
            BotCommand("unban", "Admin: unban a member"),
            BotCommand("unmute", "Admin: unmute a member"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )


async def error_handler(update, context):
    logger.exception("Unhandled exception", exc_info=context.error)
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"⚠️ Nolen error:\n<code>{str(context.error)[:3500]}</code>",
            parse_mode="HTML",
        )
    except TelegramError:
        pass


# ============================================================
# Main
# ============================================================

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Add it to .env or Render Environment Variables.")

    init_db()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("id", id_command))

    # Group-only commands. The handlers themselves ignore private chats.
    application.add_handler(CommandHandler("profile", group_profile_command))
    application.add_handler(CommandHandler("claim", group_claim_command))
    application.add_handler(CommandHandler("support", group_support_command))
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CommandHandler("ban", ban_group_command))
    application.add_handler(CommandHandler("mute", mute_group_command))
    application.add_handler(CommandHandler("unban", unban_group_command))
    application.add_handler(CommandHandler("unmute", unmute_group_command))

    # Private conversations and user input.
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            private_message_handler,
        )
    )

    # Official Nolen group messages.
    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
            group_message_handler,
        )
    )

    # Join/leave events.
    application.add_handler(ChatMemberHandler(chat_member_handler, ChatMemberHandler.CHAT_MEMBER))

    # All inline callbacks.
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_error_handler(error_handler)

    # Render Web Service -> webhook mode.
    # Local machine -> long polling.
    if os.getenv("RENDER", "").lower() == "true" or PUBLIC_URL:
        external_url = PUBLIC_URL or os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        if not external_url:
            raise SystemExit("PUBLIC_URL/RENDER_EXTERNAL_URL is required for Render webhook mode.")
        webhook_url = f"{external_url}/{WEBHOOK_SECRET}"
        logger.info("Starting webhook: %s", webhook_url)
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_SECRET,
            webhook_url=webhook_url,
            drop_pending_updates=False,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Starting local polling mode...")
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )


if __name__ == "__main__":
    main()
