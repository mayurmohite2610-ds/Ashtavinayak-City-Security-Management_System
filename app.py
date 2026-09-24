from flask import Flask, render_template, request, redirect, session, url_for
import sqlite3
from datetime import datetime
import os
import re
import threading
import asyncio
import concurrent.futures
import urllib.parse
import json
import webbrowser
from threading import Timer

from dotenv import load_dotenv

# Telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# ENVIRONMENT
# =========================================================

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FLASK_SECRET_KEY = os.getenv(
    "FLASK_SECRET_KEY",
    "ashtavinayak-city-secret-key-change-this"
)

# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

DATABASE = "ashtavinayak_city.db"
SOCIETY_NAME = "Ashtavinayak City"
RESIDENT_REGISTRY_FILE = "resident_registry.json"

# =========================================================
# SECURITY USERS
# =========================================================

SECURITY_USERS = {
    "arjun": "krishna123",
    "duryodhan": "krishna456",
    "sakuni": "krishna789",
}

BOSS_USERNAME = "krishna"
BOSS_PASSWORD = "jagannath"
BOSS_DISPLAY_NAME = "Vasudev Shri Krishna"

# =========================================================
# PHONE VALIDATION
# =========================================================

COUNTRY_CODES = {
    "+91": 10, "+1": 10, "+44": 10, "+61": 9, "+81": 10,
    "+49": 10, "+33": 9, "+39": 10, "+7": 10, "+86": 11,
    "+971": 9, "+966": 9, "+65": 8, "+60": 9, "+92": 10,
    "+880": 10, "+94": 9, "+977": 10, "+27": 9, "+55": 11,
    "+52": 10, "+34": 9, "+31": 9, "+41": 9, "+46": 9,
    "+47": 8, "+45": 8, "+32": 9, "+351": 9, "+90": 10,
    "+82": 10, "+66": 9, "+62": 10, "+63": 10, "+64": 9,
    "+20": 10, "+234": 10, "+254": 9, "+972": 9,
}

# =========================================================
# RESIDENTS
#
# EXACT LIST
# A-101 to A-105
# B-106 to B-110
# C-111
# =========================================================

DEFAULT_RESIDENTS = [
    ("Mayur", "A-101", "+918019362610"),
    ("Namrath", "A-102", "+917995315795"),
    ("Srinivasa", "A-103", "+919949556229"),
    ("Prudhwi", "A-104", "+917702942747"),
    ("Manidhar", "A-105", "+918332841900"),
    ("Prashant", "B-106", "+917207787872"),
    ("Uma", "B-107", "+919550012128"),
    ("Syam Kumar Sir", "B-108", "+919963055788"),
    ("Mansoor Bhai", "B-109", "+918247735322"),
    ("Siri", "B-110", "+918328666031"),
    ("Sreya", "C-111", "+919963001610"),
    ("Sri krishna Mohite","C-112","+919440887139"),
    ("Harshala Mohite","C-113","+919866440322"),
    ("Jnanendra","C-114","+919059162533")
]

# =========================================================
# DATABASE HELPERS
# =========================================================

def get_db():
    conn = sqlite3.connect(
        DATABASE,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def current_time():
    return datetime.now().strftime("%d-%m-%Y %I:%M %p")


def security_display_name(username):
    return {
        "arjun": "Arjun",
        "duryodhan": "Duryodhan",
        "sakuni": "Sakuni",
    }.get(username, username.capitalize())


def validate_phone_number(country_code, mobile):
    if country_code not in COUNTRY_CODES:
        return False, "Invalid country code."

    mobile = re.sub(r"[\s\-\(\)]", "", mobile or "")

    if not mobile:
        return False, "Enter a phone number."

    if not mobile.isdigit():
        return False, "Phone number must contain digits only."

    required = COUNTRY_CODES[country_code]

    if len(mobile) != required:
        return False, f"For {country_code}, enter exactly {required} digits."

    return True, mobile


def ensure_column(conn, table, column, definition):
    columns = {
        row[1]
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }

    if column not in columns:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


# =========================================================
# DATABASE INITIALIZATION / MIGRATION
# =========================================================

def load_resident_registry():
    """Load persistent resident/Telegram mappings from JSON.

    This file intentionally lives outside SQLite so deleting/recreating the
    database does not force already-connected residents to reconnect.
    """
    try:
        with open(RESIDENT_REGISTRY_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_resident_registry(registry):
    temp_file = RESIDENT_REGISTRY_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(registry, file, indent=2, ensure_ascii=False)
    os.replace(temp_file, RESIDENT_REGISTRY_FILE)


def flat_key(flat):
    return re.sub(r"[^A-Z0-9]", "", (flat or "").upper())


def sync_resident_registry(conn):
    """Keep resident/Telegram mappings persistent while respecting removals."""
    registry = load_resident_registry()
    changed = False

    rows = conn.execute("SELECT * FROM residents ORDER BY id").fetchall()

    # Sync database residents into the registry. Removed residents stay in DB
    # but are marked inactive and their Telegram connection is cleared.
    for row in rows:
        key = flat_key(row["flat"])
        current = registry.get(key, {})
        active = bool(row["active"]) if "active" in row.keys() else True
        merged = {
            "name": row["name"],
            "flat": row["flat"],
            "phone": row["phone"],
            "telegram_chat_id": (current.get("telegram_chat_id") or row["telegram_chat_id"]) if active else None,
            "added_by_security": current.get("added_by_security") or (row["added_by_security"] if "added_by_security" in row.keys() else None),
            "added_time": current.get("added_time") or (row["added_time"] if "added_time" in row.keys() else None),
            "active": active,
            "removed_by_security": row["removed_by_security"] if "removed_by_security" in row.keys() else current.get("removed_by_security"),
            "removed_time": row["removed_time"] if "removed_time" in row.keys() else current.get("removed_time"),
        }
        if registry.get(key) != merged:
            registry[key] = merged
            changed = True

    # Restore only active residents from the registry if SQLite was recreated.
    for key, resident in list(registry.items()):
        if not resident.get("flat") or resident.get("active", True) is False:
            continue
        existing = conn.execute(
            "SELECT id FROM residents WHERE UPPER(flat)=UPPER(?) AND active=1 LIMIT 1",
            (resident["flat"],)
        ).fetchone()
        if existing is None:
            conn.execute("""
                INSERT INTO residents
                (name, flat, phone, telegram_chat_id, added_by_security, added_time, active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (
                resident.get("name", "").strip(),
                resident["flat"].strip().upper(),
                resident.get("phone", "").strip(),
                resident.get("telegram_chat_id"),
                resident.get("added_by_security"),
                resident.get("added_time"),
            ))
            changed = True

    # Restore chat IDs only for active residents.
    for key, resident in registry.items():
        if resident.get("active", True) is False:
            continue
        row = conn.execute(
            "SELECT * FROM residents WHERE UPPER(flat)=UPPER(?) AND active=1 ORDER BY id DESC LIMIT 1",
            (resident.get("flat", ""),)
        ).fetchone()
        if row is None:
            continue
        desired_chat_id = resident.get("telegram_chat_id") or row["telegram_chat_id"]
        if desired_chat_id and row["telegram_chat_id"] != desired_chat_id:
            conn.execute(
                "UPDATE residents SET telegram_chat_id=? WHERE id=?",
                (desired_chat_id, row["id"])
            )

    if changed:
        save_resident_registry(registry)


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS residents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            flat TEXT NOT NULL,
            phone TEXT NOT NULL,
            telegram_chat_id TEXT,
            added_by_security TEXT,
            added_time TEXT,
            UNIQUE(name, flat)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS visitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            mobile TEXT NOT NULL,
            purpose TEXT NOT NULL,
            flat TEXT NOT NULL,
            person_to_meet TEXT NOT NULL,
            resident_phone TEXT NOT NULL,
            duration TEXT,
            status TEXT NOT NULL DEFAULT 'Pending',
            entry_time TEXT,
            exit_time TEXT,
            entry_security TEXT,
            exit_security TEXT,
            approval_time TEXT,
            rejection_time TEXT,
            sms_sent INTEGER NOT NULL DEFAULT 0,
            sms_sid TEXT,
            created_time TEXT NOT NULL
        )
    """)

    visitor_columns = {
        "resident_phone": "TEXT",
        "duration": "TEXT",
        "status": "TEXT",
        "entry_time": "TEXT",
        "exit_time": "TEXT",
        "entry_security": "TEXT",
        "exit_security": "TEXT",
        "approval_time": "TEXT",
        "rejection_time": "TEXT",
        "sms_sent": "INTEGER",
        "sms_sid": "TEXT",
        "created_time": "TEXT",
    }
    for column, definition in visitor_columns.items():
        ensure_column(conn, "visitors", column, definition)

    ensure_column(conn, "residents", "telegram_chat_id", "TEXT")
    ensure_column(conn, "residents", "added_by_security", "TEXT")
    ensure_column(conn, "residents", "added_time", "TEXT")
    ensure_column(conn, "residents", "active", "INTEGER NOT NULL DEFAULT 1")
    ensure_column(conn, "residents", "removed_by_security", "TEXT")
    ensure_column(conn, "residents", "removed_time", "TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS resident_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resident_id INTEGER,
            name TEXT NOT NULL,
            flat TEXT NOT NULL,
            phone TEXT NOT NULL,
            telegram_chat_id TEXT,
            added_by_security TEXT,
            added_time TEXT,
            removed_by_security TEXT NOT NULL,
            removed_time TEXT NOT NULL
        )
    """)

    # Seed the fixed residents, but NEVER delete custom residents.
    for name, flat, phone in DEFAULT_RESIDENTS:
        existing = conn.execute(
            "SELECT id FROM residents WHERE UPPER(flat)=UPPER(?) LIMIT 1",
            (flat,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE residents SET name=?, phone=? WHERE id=?",
                (name, phone, existing["id"])
            )
        else:
            conn.execute(
                "INSERT INTO residents (name, flat, phone) VALUES (?, ?, ?)",
                (name, flat, phone)
            )

    # Import/restore the persistent registry before committing.
    sync_resident_registry(conn)

    conn.execute("""
        UPDATE visitors
        SET created_time=?
        WHERE created_time IS NULL OR created_time=''
    """, (current_time(),))

    conn.commit()
    conn.close()

# =========================================================
# TELEGRAM BOT
# =========================================================

telegram_application = None
telegram_thread = None
telegram_loop = None
telegram_bot_username = ""


async def telegram_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if update.effective_chat is None:
        return

    if update.message is None:
        return

    user_name = "Resident"

    if update.effective_user:
        user_name = (
            update.effective_user.full_name
            or update.effective_user.first_name
            or "Resident"
        )

    # Deep-link form: /start connect_A-101
    args = getattr(context, "args", []) or []
    if args and args[0].lower().startswith("connect_"):
        requested_flat = urllib.parse.unquote(args[0][8:]).strip().upper()
        await connect_telegram_to_flat(update, requested_flat)
        return

    await update.message.reply_text(
        f"🏠 {SOCIETY_NAME.upper()}\n\n"
        f"Hello {user_name}!\n\n"
        "To connect your Telegram account with your residence, "
        "send your flat number.\n\n"
        "Example:\n"
        "A-101"
    )


async def connect_telegram_to_flat(update, flat):
    if update.effective_chat is None or update.message is None:
        return

    key = flat_key(flat)
    conn = get_db()
    resident = conn.execute(
        "SELECT * FROM residents WHERE UPPER(flat)=UPPER(?) AND active=1 LIMIT 1",
        (flat,)
    ).fetchone()

    if resident is None:
        conn.close()
        await update.message.reply_text("❌ Residence not found. Please check the flat number.")
        return

    chat_id = str(update.effective_chat.id)
    conn.execute(
        "UPDATE residents SET telegram_chat_id=? WHERE id=?",
        (chat_id, resident["id"])
    )
    conn.commit()
    conn.close()

    registry = load_resident_registry()
    record = registry.get(key, {})
    record.update({
        "name": resident["name"],
        "flat": resident["flat"],
        "phone": resident["phone"],
        "telegram_chat_id": chat_id,
        "added_by_security": resident["added_by_security"],
        "added_time": resident["added_time"],
    })
    registry[key] = record
    save_resident_registry(registry)

    await update.message.reply_text(
        "✅ TELEGRAM CONNECTED\n\n"
        f"Resident: {resident['name']}\n"
        f"Flat: {resident['flat']}\n\n"
        f"Welcome to {SOCIETY_NAME}.\n"
        "You will now receive visitor approval requests here."
    )


async def telegram_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if update.effective_chat is None:
        return

    if update.message is None:
        return

    text = update.message.text.strip().upper()
    chat_id = str(update.effective_chat.id)

    # -----------------------------------------------------
    # CONNECT ANY EXISTING OR NEW RESIDENCE
    # -----------------------------------------------------
    conn = get_db()
    resident_exists = conn.execute(
        "SELECT 1 FROM residents WHERE UPPER(flat)=UPPER(?) AND active=1 LIMIT 1",
        (text,)
    ).fetchone()
    conn.close()

    if resident_exists:
        await connect_telegram_to_flat(update, text)
        return

    # -----------------------------------------------------
    # UNKNOWN TEXT
    # -----------------------------------------------------

    await update.message.reply_text(
        "Please send your flat number.\n\n"
        "Example:\n"
        "A-101"
    )


async def telegram_approval_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if query is None:
        return

    await query.answer()

    data = query.data or ""

    # Expected:
    # approve:123
    # reject:123

    try:
        action, visitor_id_text = data.split(":", 1)
        visitor_id = int(visitor_id_text)

    except Exception:

        await query.edit_message_text(
            "❌ Invalid approval request."
        )

        return

    chat_id = str(query.message.chat_id)

    conn = get_db()

    # -----------------------------------------------------
    # GET VISITOR
    # -----------------------------------------------------

    visitor = conn.execute("""
        SELECT *
        FROM visitors
        WHERE id = ?
    """, (
        visitor_id,
    )).fetchone()

    if visitor is None:

        conn.close()

        await query.edit_message_text(
            "❌ Visitor request not found."
        )

        return

    # -----------------------------------------------------
    # SECURITY:
    # ONLY THE RESIDENT CONNECTED TO THIS FLAT
    # CAN APPROVE / DISAPPROVE.
    # -----------------------------------------------------

    resident = conn.execute("""
        SELECT *
        FROM residents
        WHERE flat = ?
        AND active = 1
        AND telegram_chat_id = ?
    """, (
        visitor["flat"],
        chat_id,
    )).fetchone()

    if resident is None:

        conn.close()

        await query.edit_message_text(
            "❌ You are not authorized to make this decision."
        )

        return

    # -----------------------------------------------------
    # ALREADY PROCESSED
    # -----------------------------------------------------

    if visitor["status"] != "Pending":

        conn.close()

        await query.edit_message_text(
            "This visitor request has already been processed."
        )

        return

    now = current_time()

    # -----------------------------------------------------
    # APPROVE
    # -----------------------------------------------------

    if action == "approve":

        conn.execute("""
            UPDATE visitors
            SET
                status = 'Inside',
                approval_time = ?,
                entry_time = ?
            WHERE id = ?
            AND status = 'Pending'
        """, (
            now,
            now,
            visitor_id,
        ))

        conn.commit()
        conn.close()

        await query.edit_message_text(
            "✅ VISITOR APPROVED\n\n"
            f"Visitor: {visitor['name']}\n"
            f"Flat: {visitor['flat']}\n"
            f"Purpose: {visitor['purpose']}\n\n"
            f"Entry time: {now}\n\n"
            "Security can now allow entry."
        )

        return

    # -----------------------------------------------------
    # DISAPPROVE
    # -----------------------------------------------------

    if action in ("reject", "disapprove"):

        conn.execute("""
            UPDATE visitors
            SET
                status = 'Rejected',
                rejection_time = ?
            WHERE id = ?
            AND status = 'Pending'
        """, (
            now,
            visitor_id,
        ))

        conn.commit()
        conn.close()

        await query.edit_message_text(
            "❌ VISITOR DISAPPROVED\n\n"
            f"Visitor: {visitor['name']}\n"
            f"Flat: {visitor['flat']}\n\n"
            f"Decision time: {now}\n\n"
            "Entry is NOT allowed."
        )

        return

    conn.close()

    await query.edit_message_text(
        "❌ Unknown action."
    )


# =========================================================
# SEND TELEGRAM APPROVAL REQUEST
# =========================================================

async def send_telegram_approval_async(visitor):

    if telegram_application is None:
        print("Telegram application is not running.")
        return False, "Telegram application is not running."

    conn = get_db()

    resident = conn.execute("""
        SELECT *
        FROM residents
        WHERE flat = ?
        AND active = 1
    """, (
        visitor["flat"],
    )).fetchone()

    conn.close()

    if resident is None:
        return False, f"Resident not found for {visitor['flat']}."

    chat_id = resident["telegram_chat_id"]

    if not chat_id:
        return False, (
            f"{resident['name']} ({resident['flat']}) has not connected "
            "Telegram yet. Open the society bot, press START, and send "
            f"{resident['flat']}."
        )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ APPROVE",
                callback_data=f"approve:{visitor['id']}"
            ),
            InlineKeyboardButton(
                "❌ DISAPPROVE",
                callback_data=f"reject:{visitor['id']}"
            ),
        ]
    ])

    message = (
        f"🏠 {SOCIETY_NAME.upper()}\n\n"
        "🔔 VISITOR APPROVAL REQUEST\n\n"
        f"Visitor: {visitor['name']}\n"
        f"Flat: {visitor['flat']}\n"
        f"Purpose: {visitor['purpose']}\n"
        f"Mobile: {visitor['mobile']}\n"
        f"Duration: {visitor['duration'] or 'Not specified'}\n"
        f"Security: {security_display_name(visitor['entry_security'])}\n\n"
        "The visitor wants to meet you.\n\n"
        "Please choose:"
    )

    try:

        sent_message = await telegram_application.bot.send_message(
            chat_id=chat_id,
            text=message,
            reply_markup=keyboard
        )

        conn = get_db()

        # sms_sent/sms_sid are retained for compatibility with
        # the existing SQLite database and templates.
        conn.execute("""
            UPDATE visitors
            SET
                sms_sent = 1,
                sms_sid = ?
            WHERE id = ?
        """, (
            f"telegram:{sent_message.message_id}",
            visitor["id"],
        ))

        conn.commit()
        conn.close()

        return True, None

    except Exception as exc:

        print(
            "TELEGRAM SEND ERROR:",
            exc
        )

        return False, str(exc)


def send_telegram_approval(visitor):
    """Send using the Telegram bot's own event loop/thread."""
    if telegram_application is None or telegram_loop is None:
        return False, "Telegram bot is not running."

    try:
        future = asyncio.run_coroutine_threadsafe(
            send_telegram_approval_async(visitor),
            telegram_loop
        )
        return future.result(timeout=20)
    except Exception as exc:
        print("TELEGRAM SEND WRAPPER ERROR:", exc)
        return False, str(exc)


# =========================================================
# TELEGRAM POLLING THREAD
# =========================================================

def start_telegram_bot():
    global telegram_application, telegram_loop, telegram_bot_username

    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing from .env")
        return

    try:
        telegram_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(telegram_loop)

        telegram_application = (
            Application.builder()
            .token(TELEGRAM_BOT_TOKEN)
            .build()
        )

        telegram_application.add_handler(CommandHandler("start", telegram_start))
        telegram_application.add_handler(CallbackQueryHandler(telegram_approval_callback))
        telegram_application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, telegram_text)
        )

        async def load_bot_username():
            global telegram_bot_username
            me = await telegram_application.bot.get_me()
            telegram_bot_username = me.username or ""
            print("Telegram bot:", telegram_bot_username)

        telegram_loop.run_until_complete(telegram_application.initialize())
        telegram_loop.run_until_complete(load_bot_username())
        telegram_loop.run_until_complete(telegram_application.start())
        telegram_loop.run_until_complete(
            telegram_application.updater.start_polling(drop_pending_updates=True)
        )

        print("Telegram bot polling started.")
        telegram_loop.run_forever()

    except Exception as exc:
        print("TELEGRAM POLLING ERROR:", exc)
    finally:
        telegram_loop = None


def start_telegram_thread():

    global telegram_thread

    if telegram_thread is not None and telegram_thread.is_alive():
        return

    telegram_thread = threading.Thread(
        target=start_telegram_bot,
        daemon=True,
        name="telegram-polling"
    )

    telegram_thread.start()


# =========================================================
# LOGIN
# =========================================================

@app.route("/", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        ).strip().lower()

        password = request.form.get(
            "password",
            ""
        )

        # SECURITY LOGIN
        if (
            username in SECURITY_USERS
            and password == SECURITY_USERS[username]
        ):

            session.clear()

            session["role"] = "security"
            session["username"] = username
            session["display_name"] = security_display_name(username)

            return redirect(
                url_for("dashboard")
            )

        # BOSS LOGIN
        if (
            username == BOSS_USERNAME
            and password == BOSS_PASSWORD
        ):

            session.clear()

            session["role"] = "boss"
            session["username"] = BOSS_USERNAME
            session["display_name"] = BOSS_DISPLAY_NAME

            return redirect(
                url_for("boss")
            )

        return render_template(
            "login.html",
            error="Invalid username or password"
        )

    return render_template("login.html")


# =========================================================
# SECURITY DASHBOARD
# =========================================================

@app.route("/dashboard")
def dashboard():

    if session.get("role") != "security":
        return redirect(url_for("login"))

    conn = get_db()

    total = conn.execute(
        "SELECT COUNT(*) FROM visitors"
    ).fetchone()[0]

    pending = conn.execute(
        "SELECT COUNT(*) FROM visitors WHERE status='Pending'"
    ).fetchone()[0]

    inside = conn.execute(
        "SELECT COUNT(*) FROM visitors WHERE status='Inside'"
    ).fetchone()[0]

    exited = conn.execute(
        "SELECT COUNT(*) FROM visitors WHERE status='Exited'"
    ).fetchone()[0]

    rejected = conn.execute(
        "SELECT COUNT(*) FROM visitors WHERE status='Rejected'"
    ).fetchone()[0]

    current_inside = conn.execute("""
        SELECT *
        FROM visitors
        WHERE status='Inside'
        ORDER BY id DESC
    """).fetchall()

    pending_visitors = conn.execute("""
        SELECT *
        FROM visitors
        WHERE status='Pending'
        ORDER BY id DESC
    """).fetchall()

    recent_visitors = conn.execute("""
        SELECT *
        FROM visitors
        ORDER BY id DESC
        LIMIT 10
    """).fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        total=total,
        pending=pending,
        inside=inside,
        exited=exited,
        rejected=rejected,
        current_inside=current_inside,
        pending_visitors=pending_visitors,
        recent_visitors=recent_visitors,
        society_name=SOCIETY_NAME,
        security_guard=session["username"],
        security_display_name=security_display_name(
            session["username"]
        ),
    )


# =========================================================
# REGISTER VISITOR
# =========================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if session.get("role") != "security":
        return redirect(url_for("login"))

    conn = get_db()

    residents_rows = conn.execute("""
        SELECT *
        FROM residents
        WHERE active = 1
        ORDER BY id
    """).fetchall()

    conn.close()

    # Convert SQLite rows to dictionaries for safe JSON use in register.html.
    residents = [dict(row) for row in residents_rows]

    if request.method == "POST":

        # Preserve every field entered by the security guard.
        # This prevents the form from becoming empty after a validation error.
        form_data = request.form.to_dict()

        name = form_data.get("name", "").strip()
        country_code = form_data.get("country_code", "+91").strip()
        mobile = form_data.get("mobile", "").strip()
        purpose = form_data.get("purpose", "").strip()
        flat = form_data.get("flat", "").strip()
        person_to_meet = form_data.get("person_to_meet", "").strip()
        duration = form_data.get("duration", "").strip()

        def render_register_error(message):
            return render_template(
                "register.html",
                residents=residents,
                country_codes=COUNTRY_CODES.keys(),
                form_data=form_data,
                error=message
            )

        # -----------------------------------------------------
        # VALIDATION
        # -----------------------------------------------------

        if not name:
            return render_register_error("Enter visitor name.")

        valid, result = validate_phone_number(
            country_code,
            mobile
        )

        if not valid:
            # IMPORTANT:
            # form_data is sent back, so 9 digits will show the error
            # but all previously entered visitor information remains.
            return render_register_error(result)

        if not purpose:
            return render_register_error(
                "Enter purpose of visit."
            )

        if not flat:
            return render_register_error(
                "Select a flat."
            )

        if not person_to_meet:
            return render_register_error(
                "Select the person to meet."
            )

        if not duration:
            return render_register_error(
                "Select expected time."
            )

        # -----------------------------------------------------
        # FIND RESIDENT
        # -----------------------------------------------------

        conn = get_db()

        resident = conn.execute("""
            SELECT *
            FROM residents
            WHERE name = ?
            AND flat = ?
            AND active = 1
            LIMIT 1
        """, (
            person_to_meet,
            flat
        )).fetchone()

        if resident is None:
            resident = conn.execute("""
                SELECT *
                FROM residents
                WHERE name = ?
                AND active = 1
                LIMIT 1
            """, (
                person_to_meet,
            )).fetchone()

        if resident is None:
            conn.close()
            return render_register_error(
                "Resident not found."
            )

        # Always use the resident's database flat.
        resident_flat = resident["flat"]
        created_time = current_time()

        # -----------------------------------------------------
        # CREATE PENDING VISITOR
        #
        # The visitor becomes "Inside" ONLY after Telegram
        # approval. Registration itself creates "Pending".
        # -----------------------------------------------------

        conn.execute("""
            INSERT INTO visitors
            (
                name,
                mobile,
                purpose,
                flat,
                person_to_meet,
                resident_phone,
                duration,
                status,
                entry_security,
                created_time
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?)
        """, (
            name,
            country_code + result,
            purpose,
            resident_flat,
            resident["name"],
            resident["phone"],
            duration,
            session["username"],
            created_time,
        ))

        conn.commit()

        visitor_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

        visitor = conn.execute("""
            SELECT *
            FROM visitors
            WHERE id = ?
        """, (
            visitor_id,
        )).fetchone()

        conn.close()

        # -----------------------------------------------------
        # AUTOMATIC TELEGRAM SEND
        # -----------------------------------------------------

        success, telegram_error = send_telegram_approval(
            visitor
        )

        if success:
            return redirect(url_for("approval"))

        # Keep the visitor Pending if Telegram could not be sent.
        return render_template(
            "approval.html",
            visitors=[visitor],
            error=telegram_error,
            society_name=SOCIETY_NAME
        )

    return render_template(
        "register.html",
        residents=residents,
        country_codes=COUNTRY_CODES.keys(),
        form_data={}
    )


# =========================================================
# ADD RESIDENT / ADD RESIDENCE
# =========================================================

@app.route("/add-resident", methods=["GET", "POST"])
@app.route("/add_residence", methods=["GET", "POST"])
def add_resident():
    if session.get("role") != "security":
        return redirect(url_for("login"))

    form_data = request.form.to_dict() if request.method == "POST" else {}
    error = None
    success = None
    telegram_link = None

    if request.method == "POST":
        name = form_data.get("name", "").strip()
        block = form_data.get("block", "").strip().upper()
        flat_number = form_data.get("flat_number", "").strip()
        country_code = form_data.get("country_code", "+91").strip()
        phone = form_data.get("phone", "").strip()

        if not name:
            error = "Enter resident name."
        elif block not in {"A", "B", "C"}:
            error = "Select Block A, B, or C."
        elif not re.fullmatch(r"\d{1,4}", flat_number):
            error = "Enter a valid flat number, for example 116."
        else:
            flat = f"{block}-{flat_number}"
            valid_phone, phone_result = validate_phone_number(country_code, phone)

            if not valid_phone:
                error = phone_result
            else:
                phone = phone_result
                full_phone = country_code + phone
                conn = get_db()
                existing = conn.execute(
                    "SELECT * FROM residents WHERE UPPER(flat)=UPPER(?) AND active=1 LIMIT 1",
                    (flat,)
                ).fetchone()

                if existing:
                    conn.close()
                    error = f"Flat {flat} is already registered to {existing['name']}."
                else:
                    # If the same resident returns to the same flat, reuse the
                    # inactive row. Otherwise create a new resident record.
                    inactive_same = conn.execute(
                        "SELECT * FROM residents WHERE UPPER(flat)=UPPER(?) AND UPPER(name)=UPPER(?) AND active=0 ORDER BY id DESC LIMIT 1",
                        (flat, name)
                    ).fetchone()
                    added_time = current_time()

                    if inactive_same:
                        conn.execute("""
                            UPDATE residents
                            SET phone=?, telegram_chat_id=NULL, added_by_security=?, added_time=?, active=1,
                                removed_by_security=NULL, removed_time=NULL
                            WHERE id=?
                        """, (
                            full_phone, session["username"], added_time, inactive_same["id"]
                        ))
                    else:
                        conn.execute("""
                            INSERT INTO residents
                            (name, flat, phone, telegram_chat_id, added_by_security, added_time, active)
                            VALUES (?, ?, ?, NULL, ?, ?, 1)
                        """, (
                            name, flat, full_phone, session["username"], added_time
                        ))
                    conn.commit()
                    conn.close()

                    registry = load_resident_registry()
                    registry[flat_key(flat)] = {
                        "name": name,
                        "flat": flat,
                        "phone": full_phone,
                        "telegram_chat_id": None,
                        "added_by_security": session["username"],
                        "added_time": added_time,
                        "active": True,
                        "removed_by_security": None,
                        "removed_time": None,
                    }
                    save_resident_registry(registry)

                    if telegram_bot_username:
                        payload = urllib.parse.quote("connect_" + flat, safe="")
                        telegram_link = f"https://t.me/{telegram_bot_username}?start={payload}"
                        success = (
                            f"Residence added successfully for {name} ({flat}). "
                            "Share the Telegram Connect Link below with the resident."
                        )
                    else:
                        success = (
                            f"Residence added successfully for {name} ({flat}). "
                            "Open the society Telegram bot and send this flat number to connect."
                        )

                    form_data = {}

    return render_template(
        "add_resident.html",
        society_name=SOCIETY_NAME,
        form_data=form_data,
        error=error,
        success=success,
        telegram_link=telegram_link,
        security_display_name=security_display_name(session["username"]),
        blocks=["A", "B", "C"],
        country_codes=COUNTRY_CODES.keys(),
    )


# =========================================================
# REMOVE RESIDENT
# =========================================================

@app.route("/remove-resident", methods=["GET", "POST"])
@app.route("/remove_resident", methods=["GET", "POST"])
def remove_resident():
    if session.get("role") != "security":
        return redirect(url_for("login"))

    form_data = request.form.to_dict() if request.method == "POST" else request.args.to_dict()
    error = None
    success = None
    resident = None

    block = form_data.get("block", "").strip().upper()
    flat_number = form_data.get("flat_number", "").strip()
    flat = f"{block}-{flat_number}" if block and flat_number.isdigit() else ""

    conn = get_db()

    if request.method == "POST" and form_data.get("action") == "search":
        if block not in {"A", "B", "C"} or not re.fullmatch(r"\d{1,4}", flat_number):
            error = "Select Block A, B, or C and enter a valid flat number."
        else:
            resident = conn.execute("""
                SELECT * FROM residents
                WHERE UPPER(flat)=UPPER(?) AND active=1
                LIMIT 1
            """, (flat,)).fetchone()
            if resident is None:
                error = f"No active resident found for Flat {flat}."

    elif request.method == "POST" and form_data.get("action") == "remove":
        resident_id = form_data.get("resident_id", "").strip()
        resident = conn.execute("""
            SELECT * FROM residents
            WHERE id=? AND active=1
            LIMIT 1
        """, (resident_id,)).fetchone()

        if resident is None:
            error = "Resident not found or already removed."
        else:
            removed_time = current_time()
            removed_by = session["username"]
            # Preserve a complete audit snapshot before clearing the live
            # Telegram connection and marking the resident inactive.
            conn.execute("""
                INSERT INTO resident_history
                (resident_id, name, flat, phone, telegram_chat_id, added_by_security, added_time, removed_by_security, removed_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                resident["id"], resident["name"], resident["flat"], resident["phone"],
                resident["telegram_chat_id"], resident["added_by_security"], resident["added_time"],
                removed_by, removed_time
            ))

            conn.execute("""
                UPDATE residents
                SET active=0, telegram_chat_id=NULL, removed_by_security=?, removed_time=?
                WHERE id=?
            """, (removed_by, removed_time, resident["id"]))
            conn.commit()

            removed_name = resident["name"]
            removed_flat = resident["flat"]
            conn.close()

            registry = load_resident_registry()
            key = flat_key(removed_flat)
            old = registry.get(key, {})
            old.update({
                "name": removed_name,
                "flat": removed_flat,
                "phone": resident["phone"],
                "telegram_chat_id": None,
                "added_by_security": resident["added_by_security"],
                "added_time": resident["added_time"],
                "active": False,
                "removed_by_security": removed_by,
                "removed_time": removed_time,
            })
            registry[key] = old
            save_resident_registry(registry)

            return redirect(url_for("remove_resident", removed=removed_flat))

    if request.args.get("removed"):
        success = f"Resident of Flat {request.args.get('removed')} removed successfully. Telegram connection has been disconnected. Old visitor/history records are preserved."

    conn.close()

    return render_template(
        "remove_resident.html",
        society_name=SOCIETY_NAME,
        form_data=form_data,
        error=error,
        success=success,
        resident=resident,
        blocks=["A", "B", "C"],
        security_display_name=security_display_name(session["username"]),
    )


# =========================================================
# APPROVAL PAGE
# =========================================================

@app.route("/approval")
def approval():

    if session.get("role") != "security":
        return redirect(url_for("login"))

    conn = get_db()

    visitors = conn.execute("""
        SELECT *
        FROM visitors
        WHERE status='Pending'
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return render_template(
        "approval.html",
        visitors=visitors,
        society_name=SOCIETY_NAME
    )


# =========================================================
# APPROVAL STATUS POLLING
#
# approval.html calls this endpoint automatically in the
# background. This lets the security page notice a Telegram
# APPROVE/DISAPPROVE without manual browser refresh.
# =========================================================

@app.route("/approval/status")
def approval_status():

    if session.get("role") != "security":
        return {"error": "Unauthorized"}, 401

    conn = get_db()

    rows = conn.execute("""
        SELECT id, status
        FROM visitors
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return {
        "visitors": [
            {
                "id": row["id"],
                "status": row["status"]
            }
            for row in rows
        ]
    }


# =========================================================
# SEND TELEGRAM AGAIN
# =========================================================

@app.route(
    "/send_telegram/<int:visitor_id>",
    methods=["POST"]
)
def send_telegram(visitor_id):

    if session.get("role") != "security":
        return redirect(url_for("login"))

    conn = get_db()

    visitor = conn.execute("""
        SELECT *
        FROM visitors
        WHERE id=?
    """, (
        visitor_id,
    )).fetchone()

    conn.close()

    if visitor is None:
        return redirect(url_for("approval"))

    if visitor["status"] != "Pending":
        return redirect(url_for("approval"))

    success, error = send_telegram_approval(
        visitor
    )

    if success:
        return redirect(url_for("approval"))

    conn = get_db()

    visitors = conn.execute("""
        SELECT *
        FROM visitors
        WHERE status='Pending'
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return render_template(
        "approval.html",
        visitors=visitors,
        error=error,
        society_name=SOCIETY_NAME
    )


# =========================================================
# COMPATIBILITY ROUTE
#
# Existing approval.html may still call /send_whatsapp.
# Keep this route so the old button does NOT break.
# It now sends TELEGRAM instead of WhatsApp.
# =========================================================

@app.route(
    "/send_whatsapp/<int:visitor_id>",
    methods=["POST"]
)
def send_whatsapp(visitor_id):
    return send_telegram(visitor_id)


# =========================================================
# SECURITY APPROVE / REJECT
#
# DISABLED.
#
# Security cannot approve or disapprove.
# Only resident Telegram buttons can do that.
# =========================================================

@app.route("/approve/<int:id>")
def security_approve(id):
    return redirect(url_for("approval"))


@app.route("/reject/<int:id>")
def security_reject(id):
    return redirect(url_for("approval"))


# =========================================================
# OLD WEB RESIDENT ROUTES
#
# These are intentionally disabled.
# Resident decisions are made only from Telegram.
# =========================================================

@app.route("/resident/approve/<int:visitor_id>")
def resident_approve(visitor_id):
    return render_template(
        "resident_response.html",
        approved=False,
        message=(
            "Resident approval is now handled through Telegram. "
            "Please use the APPROVE button in the Telegram message."
        ),
    )


@app.route("/resident/reject/<int:visitor_id>")
def resident_reject(visitor_id):
    return render_template(
        "resident_response.html",
        approved=False,
        message=(
            "Resident disapproval is now handled through Telegram. "
            "Please use the DISAPPROVE button in the Telegram message."
        ),
    )


# =========================================================
# EXIT
# =========================================================

@app.route("/exit")
def exit_page():

    if session.get("role") != "security":
        return redirect(url_for("login"))

    conn = get_db()

    visitors = conn.execute("""
        SELECT *
        FROM visitors
        WHERE status='Inside'
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return render_template(
        "exit.html",
        visitors=visitors,
        current_security=session["username"]
    )


@app.route(
    "/mark_exit/<int:id>",
    methods=["POST"]
)
def mark_exit(id):

    if session.get("role") != "security":
        return redirect(url_for("login"))

    now = current_time()

    current_security = session["username"]

    conn = get_db()

    visitor = conn.execute("""
        SELECT *
        FROM visitors
        WHERE id=?
        AND status='Inside'
    """, (
        id,
    )).fetchone()

    if visitor is None:

        conn.close()

        return redirect(
            url_for("exit_page")
        )

    conn.execute("""
        UPDATE visitors
        SET
            status='Exited',
            exit_time=?,
            exit_security=?
        WHERE id=?
        AND status='Inside'
    """, (
        now,
        current_security,
        id
    ))

    conn.commit()
    conn.close()

    return redirect(
        url_for("exit_page")
    )


# =========================================================
# HISTORY
# =========================================================

@app.route("/history")
def history():

    if session.get("role") != "security":
        return redirect(url_for("login"))

    conn = get_db()

    visitors = conn.execute("""
        SELECT *
        FROM visitors
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return render_template(
        "history.html",
        visitors=visitors,
        society_name=SOCIETY_NAME
    )


# =========================================================
# BOSS DASHBOARD
# =========================================================

@app.route("/boss")
def boss():

    if session.get("role") != "boss":
        return redirect(url_for("login"))

    conn = get_db()

    visitors = conn.execute("""
        SELECT *
        FROM visitors
        ORDER BY id DESC
    """).fetchall()

    total = conn.execute("""
        SELECT COUNT(*)
        FROM visitors
    """).fetchone()[0]

    inside = conn.execute("""
        SELECT COUNT(*)
        FROM visitors
        WHERE status='Inside'
    """).fetchone()[0]

    exited = conn.execute("""
        SELECT COUNT(*)
        FROM visitors
        WHERE status='Exited'
    """).fetchone()[0]

    rejected = conn.execute("""
        SELECT COUNT(*)
        FROM visitors
        WHERE status='Rejected'
    """).fetchone()[0]

    pending = conn.execute("""
        SELECT COUNT(*)
        FROM visitors
        WHERE status='Pending'
    """).fetchone()[0]

    arjun = conn.execute("""
        SELECT COUNT(*)
        FROM visitors
        WHERE entry_security='arjun'
    """).fetchone()[0]

    duryodhan = conn.execute("""
        SELECT COUNT(*)
        FROM visitors
        WHERE entry_security='duryodhan'
    """).fetchone()[0]

    sakuni = conn.execute("""
        SELECT COUNT(*)
        FROM visitors
        WHERE entry_security='sakuni'
    """).fetchone()[0]

    arjun_exits = conn.execute("""
        SELECT COUNT(*)
        FROM visitors
        WHERE exit_security='arjun'
    """).fetchone()[0]

    duryodhan_exits = conn.execute("""
        SELECT COUNT(*)
        FROM visitors
        WHERE exit_security='duryodhan'
    """).fetchone()[0]

    sakuni_exits = conn.execute("""
        SELECT COUNT(*)
        FROM visitors
        WHERE exit_security='sakuni'
    """).fetchone()[0]

    current_inside = conn.execute("""
        SELECT *
        FROM visitors
        WHERE status='Inside'
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    return render_template(
        "boss.html",
        visitors=visitors,
        total=total,
        inside=inside,
        exited=exited,
        rejected=rejected,
        pending=pending,
        current_inside=current_inside,
        arjun=arjun,
        duryodhan=duryodhan,
        sakuni=sakuni,
        arjun_exits=arjun_exits,
        duryodhan_exits=duryodhan_exits,
        sakuni_exits=sakuni_exits,
        boss_name=BOSS_DISPLAY_NAME,
    )


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("login")
    )


# =========================================================
# START APPLICATION
# =========================================================

if __name__ == "__main__":

    # Initialize / migrate SQLite database first.
    init_db()

    # Start Telegram exactly once.
    start_telegram_thread()

    # -----------------------------------------------------
    # IMPORTANT 409 FIX:
    #
    # Flask's debug reloader used to start app.py twice.
    # That created two Telegram getUpdates processes and
    # caused:
    #
    # 409 Conflict: terminated by other getUpdates request
    #
    # use_reloader=False prevents the second process.
    # -----------------------------------------------------

    # -----------------------------------------------------
    # AUTO OPEN BROWSER
    # -----------------------------------------------------
    # Prefer Microsoft Edge. If Edge is not installed, use
    # Google Chrome. If neither is found, use the default
    # Windows browser.
    # -----------------------------------------------------

    def open_browser():
        url = "http://127.0.0.1:5000"

        edge_paths = [
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        ]

        chrome_paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]

        for browser_path in edge_paths:
            if os.path.exists(browser_path):
                webbrowser.register(
                    "ashtavinayak_edge",
                    None,
                    webbrowser.BackgroundBrowser(browser_path)
                )
                webbrowser.get("ashtavinayak_edge").open(url)
                return

        for browser_path in chrome_paths:
            if os.path.exists(browser_path):
                webbrowser.register(
                    "ashtavinayak_chrome",
                    None,
                    webbrowser.BackgroundBrowser(browser_path)
                )
                webbrowser.get("ashtavinayak_chrome").open(url)
                return

        webbrowser.open(url)

    Timer(1.5, open_browser).start()

    app.run(
        debug=True,
        use_reloader=False,
        host="0.0.0.0",
        port=5000
    )
