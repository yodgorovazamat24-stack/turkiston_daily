import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import tempfile
from io import BytesIO
from calendar import monthrange
from contextlib import contextmanager
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
import urllib.error
import urllib.request
from urllib.parse import parse_qsl

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).with_name(".env"))

from database import get_connection, init_db
from excel_export import build_excel_report

try:
    from aiogram import Bot, Dispatcher, F
    from aiogram.exceptions import TelegramAPIError
    from aiogram.filters import Command, CommandStart
    from aiogram.types import BufferedInputFile, BotCommand, BotCommandScopeChat, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputProfilePhotoStatic, KeyboardButton, MenuButtonWebApp, ReplyKeyboardMarkup, WebAppInfo
except ImportError:
    Bot = Dispatcher = F = Command = CommandStart = None
    TelegramAPIError = Exception
    BufferedInputFile = BotCommand = BotCommandScopeChat = FSInputFile = InlineKeyboardButton = InlineKeyboardMarkup = InputProfilePhotoStatic = KeyboardButton = MenuButtonWebApp = ReplyKeyboardMarkup = WebAppInfo = None


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DEV_USER_ID = int(os.getenv("DEV_USER_ID", "12345678"))
ADMIN_USER_IDS = {
    5144043830,
    DEV_USER_ID,
    *([int(value.strip()) for value in os.getenv("ADMIN_USER_IDS", "").split(",") if value.strip().isdigit()])
}
TELEGRAM_ENABLED = bool(BOT_TOKEN and BOT_TOKEN != "YOUR_BOT_TOKEN_HERE")
LOCAL_TZ = timezone(timedelta(hours=5))
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))

app = FastAPI(title="Turkiston Daily")
static_dir = Path(__file__).with_name("static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/manifest.json")
async def get_manifest():
    return FileResponse(static_dir / "manifest.json", media_type="application/manifest+json")

@app.get("/sw.js")
async def get_sw():
    return FileResponse(static_dir / "sw.js", media_type="application/javascript")
dp = Dispatcher() if Dispatcher else None
bot = Bot(token=BOT_TOKEN) if TELEGRAM_ENABLED and Bot else None
PENDING_ACTIONS = {}
ADMIN_PENDING_ACTIONS = {}
init_db()


@contextmanager
def database():
    connection = get_connection()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_user_activity(user_id):
    seen_at = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    with database() as connection:
        connection.execute(
            "INSERT INTO users (user_id, first_seen_at, last_seen_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (user_id, seen_at, seen_at),
        )


def today_text():
    return datetime.now(LOCAL_TZ).date().isoformat()


def checked_date(value):
    try:
        return Date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Sana YYYY-MM-DD formatida bo'lishi kerak")


ACTIVE_SESSIONS = {}  # token -> user_id

async def generate_uzbek_tts(text: str, voice: str = "uz-UZ-MadinaNeural") -> bytes:
    if not text or not text.strip():
        return b""
    if voice not in {"uz-UZ-MadinaNeural", "uz-UZ-SardorNeural"}:
        voice = "uz-UZ-MadinaNeural"
    try:
        import edge_tts
        tmp_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_path = tmp_file.name
        tmp_file.close()

        communicate = edge_tts.Communicate(text.strip(), voice)
        await communicate.save(tmp_path)

        with open(tmp_path, "rb") as f:
            data = f.read()
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return data
    except Exception as e:
        logging.exception("Edge TTS xatosi: %s", e)
        return b""

def hash_password(pwd: str) -> str:
    return hashlib.sha256(pwd.strip().encode("utf-8")).hexdigest()

def normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 9:
        digits = "998" + digits
    return digits

def verify_telegram_user(init_data: str | None):
    if not init_data:
        return None
    try:
        values = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = values.pop("hash", None)
        if not received_hash or not BOT_TOKEN:
            return None
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_hash, received_hash):
            return None
        user = json.loads(values["user"])
        return int(user["id"])
    except Exception:
        return None

async def current_user(
    authorization: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None)
):
    # 1. Bearer token or Authorization header
    if authorization:
        token = authorization.replace("Bearer ", "").strip()
        if token in ACTIVE_SESSIONS:
            user_id = ACTIVE_SESSIONS[token]
            record_user_activity(user_id)
            return user_id
        # Check token directly if it's an existing numeric user_id
        if token.isdigit():
            user_id = int(token)
            with database() as conn:
                row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
                if row:
                    record_user_activity(user_id)
                    return user_id

    # 2. Telegram Mini App
    tg_user = verify_telegram_user(x_telegram_init_data)
    if tg_user:
        record_user_activity(tg_user)
        return tg_user

    # 3. Default fallback to primary active user in DB
    with database() as conn:
        row = conn.execute("SELECT user_id FROM users ORDER BY last_seen_at DESC LIMIT 1").fetchone()
        if row:
            uid = int(row["user_id"])
            record_user_activity(uid)
            return uid

    return DEV_USER_ID



class RegisterRequest(BaseModel):
    phone: str
    password: str = Field(min_length=4, max_length=64)
    full_name: str = Field(default="", max_length=120)
    username: str = Field(default="", max_length=60)
    telegram_user_id: int | None = None

class LoginRequest(BaseModel):
    login: str  # phone or username
    password: str

class ProfileUpdate(BaseModel):
    full_name: str | None = None
    username: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    password: str | None = None
    finance_reminder_enabled: bool | None = None
    evening_review_enabled: bool | None = None
    push_notifications_enabled: bool | None = None

class PushSubscriptionData(BaseModel):
    endpoint: str
    keys: dict | None = None

class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    date: Date | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    scheduled_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    remind_at: datetime | None = None
    recurrence: Literal["none", "daily", "weekly", "monthly"] = "none"
    reminder_interval: Literal["once", "hourly"] = "once"


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    is_completed: bool | None = None
    date: Date | None = None
    priority: Literal["low", "normal", "high"] | None = None
    scheduled_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    remind_at: datetime | None = None
    recurrence: Literal["none", "daily", "weekly", "monthly"] | None = None
    reminder_interval: Literal["once", "hourly"] | None = None
    missed_reason: str | None = Field(default=None, max_length=500)


class TransactionCreate(BaseModel):
    type: Literal["income", "expense"]
    category: str = Field(min_length=1, max_length=60)
    amount: float = Field(gt=0, le=1_000_000_000_000)
    date: Date | None = None
    note: str = Field(default="", max_length=240)


class TransactionUpdate(BaseModel):
    type: Literal["income", "expense"] | None = None
    category: str | None = Field(default=None, min_length=1, max_length=60)
    amount: float | None = Field(default=None, gt=0, le=1_000_000_000_000)
    date: Date | None = None
    note: str | None = Field(default=None, max_length=240)


class BudgetCreate(BaseModel):
    category: str = Field(min_length=1, max_length=60)
    monthly_limit: float = Field(gt=0, le=1_000_000_000_000)
    month: str | None = None


class AIChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1500)
    api_key: str | None = None


class BudgetUpdate(BaseModel):
    category: str | None = Field(default=None, min_length=1, max_length=60)
    monthly_limit: float | None = Field(default=None, gt=0, le=1_000_000_000_000)
    month: str | None = None


class GoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    target_amount: float = Field(gt=0, le=1_000_000_000_000)
    current_amount: float = Field(default=0, ge=0, le=1_000_000_000_000)
    deadline: Date | None = None


class GoalUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    target_amount: float | None = Field(default=None, gt=0, le=1_000_000_000_000)
    current_amount: float | None = Field(default=None, ge=0, le=1_000_000_000_000)
    deadline: Date | None = None


class ReportCreate(BaseModel):
    date: Date | None = None
    note: str = Field(default="", max_length=1000)
    score: int = Field(default=0, ge=0, le=5)


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html", {})



@app.post("/api/auth/register")
async def register_account(data: RegisterRequest):
    phone = normalize_phone(data.phone)
    if not phone or len(phone) < 7:
        raise HTTPException(status_code=400, detail="Telefon raqamini to'g'ri kiriting")
    pwd_hash = hash_password(data.password)
    username = data.username.strip().lstrip("@")
    full_name = data.full_name.strip()

    with database() as connection:
        # Check existing phone
        existing = connection.execute("SELECT user_id FROM users WHERE phone = ?", (phone,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Bu telefon raqam allaqachon ro'yxatdan o'tgan")
        if username:
            exist_user = connection.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()
            if exist_user:
                raise HTTPException(status_code=400, detail="Bu username band, boshqa nom tanlang")

        # Telegramdagi mavjud hisobot va rejalarni ulash (migration)
        # Agar foydalanuvchi ma'lum telegram_user_id bergan bo'lsa yoki bazadagi eng faol foydalanuvchi ma'lumotlarini birlashtirish
        target_uid = data.telegram_user_id
        if not target_uid:
            # Eng ko'p reja/ma'lumotga ega bo'lgan foydalanuvchini olamiz (odatda 5144043830)
            active_row = connection.execute(
                "SELECT user_id, COUNT(*) as cnt FROM tasks GROUP BY user_id ORDER BY cnt DESC LIMIT 1"
            ).fetchone()
            if active_row:
                target_uid = int(active_row["user_id"])
            else:
                target_uid = int(phone[-8:]) if phone[-8:].isdigit() else DEV_USER_ID

        # Yangilaymiz yoki yangi yozuv yaratamiz
        now_str = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
        connection.execute(
            "INSERT INTO users (user_id, phone, full_name, username, password_hash, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET phone=excluded.phone, full_name=excluded.full_name, "
            "username=excluded.username, password_hash=excluded.password_hash, last_seen_at=excluded.last_seen_at",
            (target_uid, phone, full_name, username, pwd_hash, now_str, now_str)
        )

    import uuid
    token = f"tok_{uuid.uuid4().hex}"
    ACTIVE_SESSIONS[token] = target_uid
    return {
        "status": "ok",
        "token": token,
        "user_id": target_uid,
        "full_name": full_name,
        "username": username,
        "phone": phone
    }

@app.post("/api/auth/login")
async def login_account(data: LoginRequest):
    login_val = data.login.strip().lstrip("@")
    phone_val = normalize_phone(data.login)
    raw_phone = data.login.strip()
    pwd_hash = hash_password(data.password)

    with database() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE (phone = ? OR phone = ? OR username = ? OR CAST(user_id AS TEXT) = ?) AND password_hash = ?",
            (phone_val, raw_phone, login_val, login_val, pwd_hash)
        ).fetchone()

        if not user:
            raise HTTPException(status_code=401, detail="Telefon raqam yoki parol noto'g'ri")

        uid = int(user["user_id"])

    import uuid
    token = f"tok_{uuid.uuid4().hex}"
    ACTIVE_SESSIONS[token] = uid
    return {
        "status": "ok",
        "token": token,
        "user_id": uid,
        "full_name": user["full_name"],
        "username": user["username"],
        "phone": user["phone"],
        "avatar_url": user["avatar_url"]
    }

@app.get("/api/profile")
async def get_profile(user_id: int = Depends(current_user)):
    with database() as connection:
        user = connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        task_count = connection.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ?", (user_id,)).fetchone()[0]
        tx_count = connection.execute("SELECT COUNT(*) FROM transactions WHERE user_id = ?", (user_id,)).fetchone()[0]
        goal_count = connection.execute("SELECT COUNT(*) FROM goals WHERE user_id = ?", (user_id,)).fetchone()[0]

    u_dict = dict(user) if user else {}
    return {
        "user_id": user_id,
        "full_name": u_dict.get("full_name") or "Turkiston Daily Foydalanuvchisi",
        "username": u_dict.get("username") or "",
        "phone": u_dict.get("phone") or "",
        "bio": u_dict.get("bio") or "",
        "avatar_url": u_dict.get("avatar_url") or "/static/icon-192.png",
        "has_password": bool(u_dict.get("password_hash")),
        "stats": {
            "tasks": task_count,
            "transactions": tx_count,
            "goals": goal_count
        },
        "settings": {
            "push_notifications_enabled": bool(u_dict.get("push_notifications_enabled", 1)),
            "finance_reminder_enabled": bool(u_dict.get("finance_reminder_enabled", 1)),
            "evening_review_enabled": bool(u_dict.get("evening_review_enabled", 1))
        }
    }

@app.patch("/api/profile")
async def update_profile(data: ProfileUpdate, user_id: int = Depends(current_user)):
    with database() as connection:
        updates = []
        params = []
        if data.full_name is not None:
            updates.append("full_name = ?")
            params.append(data.full_name.strip())
        if data.username is not None:
            uname = data.username.strip().lstrip("@")
            if uname:
                existing = connection.execute("SELECT user_id FROM users WHERE username = ? AND user_id != ?", (uname, user_id)).fetchone()
                if existing:
                    raise HTTPException(status_code=400, detail="Bu username allaqachon band")
            updates.append("username = ?")
            params.append(uname)
        if data.avatar_url is not None:
            updates.append("avatar_url = ?")
            params.append(data.avatar_url.strip())
        if data.bio is not None:
            updates.append("bio = ?")
            params.append(data.bio.strip())
        if data.password:
            updates.append("password_hash = ?")
            params.append(hash_password(data.password))
        if data.push_notifications_enabled is not None:
            updates.append("push_notifications_enabled = ?")
            params.append(1 if data.push_notifications_enabled else 0)
        if data.finance_reminder_enabled is not None:
            updates.append("finance_reminder_enabled = ?")
            params.append(1 if data.finance_reminder_enabled else 0)
        if data.evening_review_enabled is not None:
            updates.append("evening_review_enabled = ?")
            params.append(1 if data.evening_review_enabled else 0)

        if updates:
            params.append(user_id)
            connection.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", params)

    return {"status": "ok", "message": "Profil muvaffaqiyatli yangilandi"}


@app.get("/api/admin/users")
async def admin_get_users(user_id: int = Depends(current_user)):
    with database() as conn:
        u = conn.execute("SELECT username FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not u or (u["username"] != "admin" and user_id not in ADMIN_USER_IDS and user_id != DEV_USER_ID):
            raise HTTPException(status_code=403, detail="Admin huquqi talab qilinadi")
        rows = conn.execute("SELECT user_id, full_name, username, phone, last_seen_at FROM users ORDER BY last_seen_at DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/admin/export-master", response_class=HTMLResponse)
async def admin_export_master(user_id: int = Depends(current_user)):
    with database() as conn:
        u = conn.execute("SELECT username FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not u or (u["username"] != "admin" and user_id not in ADMIN_USER_IDS and user_id != DEV_USER_ID):
            raise HTTPException(status_code=403, detail="Admin huquqi talab qilinadi")
        users = conn.execute("SELECT * FROM users").fetchall()
        tasks = conn.execute("SELECT * FROM tasks").fetchall()
        transactions = conn.execute("SELECT * FROM transactions").fetchall()

    html = """
    <!doctype html>
    <html lang="uz">
    <head><meta charset="utf-8"><title>Master Platform Report</title>
    <style>body{font-family:Arial;padding:30px;background:#f2f4ee;} table{width:100%;border-collapse:collapse;margin-bottom:30px;background:#fff;} th,td{border:1px solid #ddd;padding:8px;font-size:12px;} th{background:#245c43;color:#fff;}</style>
    </head>
    <body>
    <h1>👑 Master Platform Report (Admin)</h1>
    <h2>Foydalanuvchilar (Users)</h2>
    <table><tr><th>ID</th><th>Ism</th><th>Username</th><th>Telefon</th><th>Oxirgi faollik</th></tr>
    """
    for usr in users:
        html += f"<tr><td>{usr['user_id']}</td><td>{escapeHtml(usr['full_name'])}</td><td>@{escapeHtml(usr['username'])}</td><td>{escapeHtml(usr['phone'])}</td><td>{usr['last_seen_at']}</td></tr>"
    html += "</table><h2>Barcha Rejalar (Tasks)</h2><table><tr><th>User ID</th><th>Sarlavha</th><th>Sana</th><th>Bajarilgan</th></tr>"
    for t in tasks:
        html += f"<tr><td>{t['user_id']}</td><td>{escapeHtml(t['title'])}</td><td>{t['date']}</td><td>{'Ha' if t['is_completed'] else 'Yoq'}</td></tr>"
    html += "</table><h2>Barcha Amaliyotlar (Transactions)</h2><table><tr><th>User ID</th><th>Turi</th><th>Kategoriya</th><th>Summa</th><th>Sana</th></tr>"
    for tx in transactions:
        html += f"<tr><td>{tx['user_id']}</td><td>{tx['type']}</td><td>{escapeHtml(tx['category'])}</td><td>{tx['amount']}</td><td>{tx['date']}</td></tr>"
    html += "</table></body></html>"
    return html


@app.post("/api/push/subscribe")
async def subscribe_push(sub: PushSubscriptionData, user_id: int = Depends(current_user)):
    if not sub.endpoint:
        raise HTTPException(status_code=400, detail="Endpoint talab qilinadi")
    p256 = (sub.keys or {}).get("p256dh", "")
    auth_key = (sub.keys or {}).get("auth", "")
    with database() as conn:
        conn.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id",
            (user_id, sub.endpoint, p256, auth_key)
        )
    return {"status": "ok", "message": "Push obunasi saqlandi"}

@app.post("/api/profile/avatar")
async def upload_avatar(request: Request, user_id: int = Depends(current_user)):
    body = await request.body()
    if not body or len(body) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Rasm hajmi 5MB dan oshmasligi kerak")
    import uuid
    fname = f"avatar_{user_id}_{uuid.uuid4().hex[:8]}.png"
    out_dir = Path("static/uploads")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / fname
    out_path.write_bytes(body)
    avatar_url = f"/static/uploads/{fname}"
    with database() as conn:
        conn.execute("UPDATE users SET avatar_url = ? WHERE user_id = ?", (avatar_url, user_id))
    return {"status": "ok", "avatar_url": avatar_url}

@app.get("/api/tts")
async def get_tts(text: str, voice: str = "uz-UZ-MadinaNeural"):
    if not text or len(text) > 500:
        raise HTTPException(status_code=400, detail="Matn uzunligi 1 dan 500 belgigacha bo'lishi kerak")
    audio_bytes = await generate_uzbek_tts(text, voice)
    if not audio_bytes:
        raise HTTPException(status_code=500, detail="Ovoz yaratib bo'lmadi")
    return Response(content=audio_bytes, media_type="audio/mpeg")

@app.get("/api/me")
async def get_me(user_id: int = Depends(current_user)):
    return {"user_id": user_id, "telegram": TELEGRAM_ENABLED}


@app.get("/api/dashboard")
async def get_dashboard(selected_date: str | None = None, user_id: int = Depends(current_user)):
    chosen_date = checked_date(selected_date or today_text())
    month = chosen_date[:7]
    with database() as connection:
        task_rows = connection.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND date = ? ORDER BY is_completed, "
            "CASE WHEN scheduled_time IS NULL THEN 1 ELSE 0 END, scheduled_time, "
            "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id DESC",
            (user_id, chosen_date),
        ).fetchall()
        transaction_rows = connection.execute(
            "SELECT * FROM transactions WHERE user_id = ? AND date = ? ORDER BY id DESC",
            (user_id, chosen_date),
        ).fetchall()
        totals = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END), 0) income, "
            "COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) expense "
            "FROM transactions WHERE user_id = ? AND date = ?",
            (user_id, chosen_date),
        ).fetchone()
        monthly = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END), 0) income, "
            "COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) expense "
            "FROM transactions WHERE user_id = ? AND date LIKE ?",
            (user_id, f"{month}%"),
        ).fetchone()
        category_rows = connection.execute(
            "SELECT category, SUM(amount) amount FROM transactions "
            "WHERE user_id = ? AND date = ? AND type = 'expense' "
            "GROUP BY category ORDER BY amount DESC",
            (user_id, chosen_date),
        ).fetchall()
        budget_rows = connection.execute(
            "SELECT b.*, COALESCE(SUM(t.amount), 0) spent FROM budgets b "
            "LEFT JOIN transactions t ON t.user_id = b.user_id AND t.category = b.category "
            "AND t.type = 'expense' AND t.date LIKE ? "
            "WHERE b.user_id = ? AND b.month = ? GROUP BY b.id ORDER BY b.category",
            (f"{month}%", user_id, month),
        ).fetchall()
        goal_rows = connection.execute(
            "SELECT * FROM goals WHERE user_id = ? ORDER BY is_completed, id DESC", (user_id,)
        ).fetchall()
        report_row = connection.execute(
            "SELECT * FROM reports WHERE user_id = ? AND date = ?", (user_id, chosen_date)
        ).fetchone()

    tasks = [dict(row) for row in task_rows]
    transactions = [dict(row) for row in transaction_rows]
    income = float(totals["income"])
    expense = float(totals["expense"])
    monthly_income = float(monthly["income"])
    monthly_expense = float(monthly["expense"])
    return {
        "date": chosen_date,
        "tasks": tasks,
        "transactions": transactions,
        "summary": {
            "income": income,
            "expense": expense,
            "balance": income - expense,
            "tasks_total": len(tasks),
            "tasks_completed": sum(task["is_completed"] for task in tasks),
            "month_income": monthly_income,
            "month_expense": monthly_expense,
            "month_balance": monthly_income - monthly_expense,
        },
        "categories": [dict(row) for row in category_rows],
        "budgets": [dict(row) for row in budget_rows],
        "goals": [dict(row) for row in goal_rows],
        "report": dict(report_row) if report_row else None,
    }


@app.get("/api/suggestions")
async def get_suggestions(selected_date: str | None = None, user_id: int = Depends(current_user)):
    chosen_date = Date.fromisoformat(checked_date(selected_date or today_text()))
    month = chosen_date.strftime("%Y-%m")

    with database() as connection:
        tasks = [dict(r) for r in connection.execute("SELECT title, priority, is_completed, date FROM tasks WHERE user_id = ? AND date = ?", (user_id, chosen_date.isoformat())).fetchall()]
        transactions = [dict(r) for r in connection.execute("SELECT type, category, amount, date FROM transactions WHERE user_id = ? AND date = ?", (user_id, chosen_date.isoformat())).fetchall()]
        budgets = [dict(r) for r in connection.execute("SELECT category, monthly_limit FROM budgets WHERE user_id = ? AND month = ?", (user_id, month)).fetchall()]
        goals = [dict(r) for r in connection.execute("SELECT title, target_amount, current_amount FROM goals WHERE user_id = ? AND is_completed = 0", (user_id,)).fetchall()]
        totals = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END), 0) income, "
            "COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) expense "
            "FROM transactions WHERE user_id = ? AND date = ?",
            (user_id, chosen_date.isoformat()),
        ).fetchone()

    total_tasks = len(tasks)
    completed_tasks = sum(1 for t in tasks if t["is_completed"])
    income = float(totals["income"])
    expense = float(totals["expense"])
    balance = income - expense

    if total_tasks == 0 and expense == 0:
        return [{
            "title": "🌱 Yangi kun uchun AI maslahati",
            "message": "Bugun uchun hali reja va moliyaviy yozuvlar kiritilmadi. Kuningiz unumli va nazoratda o'tishi uchun kamida 3 ta muhim vazifa va xarajatlaringizni qo'shing!",
            "action_label": "Reja qo'shish",
            "kind": "general"
        }]
    elif total_tasks > 0 and completed_tasks < total_tasks:
        pending = total_tasks - completed_tasks
        return [{
            "title": "⚡ Unumdorlikni oshirish",
            "message": f"Bugungi {total_tasks} ta rejadan {completed_tasks} tasi bajarildi, yana {pending} ta vazifangiz qoldi. Pomodoro usolidan foydalanib, qolgan ishlarni tezroq yakunlang!",
            "action_label": "Rejalarga o'tish",
            "kind": "productivity"
        }]
    elif expense > income and expense > 0:
        return [{
            "title": "⚠️ Moliyaviy ogohlantirish",
            "message": f"Diqqat: Bugungi xarajatlaringiz ({expense:,.0f} so'm) kirimingizdan ({income:,.0f} so'm) oshib ketdi! Ertangi kun uchun xarajatlarni optimallashtirib, budjet limitlarini tekshiring.",
            "action_label": "Moliya tahlili",
            "kind": "finance"
        }]
    elif completed_tasks == total_tasks and total_tasks > 0:
        return [{
            "title": "🏆 Ajoyib natija!",
            "message": f"Qoyil! Bugungi barcha {total_tasks} ta rejangiz muvaffaqiyatli bajarildi. O'z ustingizda ishlashda davom eting, ertangi kun uchun marralarni belgilang.",
            "action_label": "Maqsad qo'shish",
            "kind": "general"
        }]
    else:
        return [{
            "title": "🎯 Balans va Maqsadlar",
            "message": f"Bugungi balans: {balance:,.0f} so'm. Rejalar va moliyaviy oqim barqaror. Maqsadlaringiz sari har kuni 1% oldinga harakat qiling!",
            "action_label": "Batafsil ko'rish",
            "kind": "general"
        }]
        suggestions.append({
            "kind": "productivity",
            "title": "🎯 Rejalarni yakunlash vaqti",
            "message": f"Bugungi {total_tasks} ta rejadan {completed_tasks} tasi bajarildi. Qolgan vazifalarni oxiriga yetkazib, kunni muvaffaqiyatli yakunlang!",
            "action_label": "Rejalarga o'tish"
        })
    elif float(totals["expense"]) > 0:
        suggestions.append({
            "kind": "finance",
            "title": "💡 Xarajatlarni optimallashtirish",
            "message": f"Bugun jami {float(totals['expense']):,.0f} so'm xarajat qilindi. Byudjet limitlarini kuzatib borish moliyaviy barqarorlikni ta'minlaydi.",
            "action_label": "Moliya tahlili"
        })
    else:
        suggestions.append({
            "kind": "general",
            "title": "✨ Kunni samarali boshlang",
            "message": "Bugungi rejalaringizni belgilang va moliyaviy amaliyotlaringizni kiriting. Maqsadlaringiz sari har kuni 1% oldinga!",
            "action_label": "Yangi reja qo'shish"
        })
    return suggestions


@app.post("/api/tasks")
async def add_task(task: TaskCreate, user_id: int = Depends(current_user)):
    task_date = (task.date or Date.fromisoformat(today_text())).isoformat()
    title = task.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Reja nomini kiriting")
    remind_at = task.remind_at
    if remind_at and remind_at.tzinfo is None:
        remind_at = remind_at.replace(tzinfo=LOCAL_TZ)
    if remind_at is None and task.scheduled_time:
        scheduled_datetime = datetime.combine(
            Date.fromisoformat(task_date),
            datetime.strptime(task.scheduled_time, "%H:%M").time(),
        )
        remind_at = scheduled_datetime.replace(tzinfo=LOCAL_TZ)
    with database() as connection:
        cursor = connection.execute(
            "INSERT INTO tasks (user_id, title, date, priority, scheduled_time, remind_at, recurrence, "
            "reminder_interval, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                user_id,
                title,
                task_date,
                task.priority,
                task.scheduled_time,
                remind_at.strftime("%Y-%m-%dT%H:%M") if remind_at else None,
                task.recurrence,
                task.reminder_interval,
            ),
        )
        return {"id": cursor.lastrowid, "status": "ok"}


def next_recurrence(current_date, recurrence):
    if recurrence == "daily":
        return current_date + timedelta(days=1)
    if recurrence == "weekly":
        return current_date + timedelta(days=7)
    month_index = current_date.month % 12 + 1
    year = current_date.year + (current_date.month // 12)
    return Date(year, month_index, min(current_date.day, monthrange(year, month_index)[1]))


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: int, update: TaskUpdate, user_id: int = Depends(current_user)):
    values = update.model_dump(exclude_unset=True)
    invalid_nulls = [
        key for key, value in values.items()
        if value is None and key not in {"remind_at", "scheduled_time", "missed_reason"}
    ]
    if invalid_nulls:
        raise HTTPException(status_code=422, detail="Reja maydonlari bo'sh bo'lishi mumkin emas")
    if not values:
        raise HTTPException(status_code=422, detail="Yangilash uchun ma'lumot yuboring")
    if "title" in values:
        values["title"] = values["title"].strip()
        if not values["title"]:
            raise HTTPException(status_code=422, detail="Reja nomini kiriting")
    if "missed_reason" in values:
        values["missed_reason"] = values["missed_reason"].strip() if values["missed_reason"] else None
        if values["missed_reason"] == "":
            raise HTTPException(status_code=422, detail="Bajarilmagan reja sababini yozing")
    if "date" in values:
        values["date"] = values["date"].isoformat()
    if "remind_at" in values and values["remind_at"]:
        remind_at = values["remind_at"]
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=LOCAL_TZ)
        values["remind_at"] = remind_at.strftime("%Y-%m-%dT%H:%M")
    if "is_completed" in values:
        values["is_completed"] = int(values["is_completed"])

    with database() as connection:
        task = connection.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        ).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="Reja topilmadi")
        previous_date = task["date"]
        previous_scheduled_time = task["scheduled_time"]
        previous_reminder = task["remind_at"]
        next_date = values.get("date", previous_date)
        next_scheduled_time = values.get("scheduled_time", previous_scheduled_time)
        reminder_follows_schedule = (
            previous_scheduled_time
            and previous_reminder == f"{previous_date}T{previous_scheduled_time}"
        )
        reminder_unchanged = "remind_at" in values and values["remind_at"] == previous_reminder
        if reminder_unchanged and (
            "date" in values or "scheduled_time" in values
        ):
            if "scheduled_time" in values and reminder_follows_schedule:
                values["remind_at"] = (
                    f"{next_date}T{next_scheduled_time}" if next_scheduled_time else None
                )
            elif "date" in values and previous_reminder:
                values["remind_at"] = f"{next_date}T{previous_reminder[11:16]}"
        elif "remind_at" not in values:
            if "scheduled_time" in values and (not previous_reminder or reminder_follows_schedule):
                values["remind_at"] = (
                    f"{next_date}T{next_scheduled_time}" if next_scheduled_time else None
                )
            elif "date" in values:
                values["remind_at"] = (
                    f"{next_date}T{previous_reminder[11:16]}"
                    if previous_reminder
                    else f"{next_date}T{next_scheduled_time}"
                    if next_scheduled_time
                    else None
                )
        if (
            values.get("remind_at", task["remind_at"]) != task["remind_at"]
            or values.get("reminder_interval", task["reminder_interval"]) != task["reminder_interval"]
        ):
            values["reminder_sent_for"] = None
            values["last_reminder_sent_at"] = None
        if values.get("missed_reason"):
            planned_time = task["scheduled_time"]
            if planned_time:
                planned_at = datetime.fromisoformat(f"{values.get('date', task['date'])}T{planned_time}").replace(
                    tzinfo=LOCAL_TZ
                )
                values["late_minutes"] = max(
                    0, int((datetime.now(LOCAL_TZ) - planned_at).total_seconds() // 60)
                )
            else:
                values["late_minutes"] = 0
            values["missed_at"] = datetime.now(LOCAL_TZ).isoformat(timespec="minutes")
        assignments = ", ".join(f"{column} = ?" for column in values)
        connection.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ? AND user_id = ?",
            (*values.values(), task_id, user_id),
        )
        recurrence = values.get("recurrence", task["recurrence"])
        if values.get("is_completed") and recurrence != "none":
            task_date = values.get("date", task["date"])
            next_date = next_recurrence(Date.fromisoformat(task_date), recurrence)
            title = values.get("title", task["title"])
            priority = values.get("priority", task["priority"])
            scheduled_time = values.get("scheduled_time", task["scheduled_time"])
            reminder_time = values.get("remind_at", task["remind_at"])
            next_reminder = (
                f"{next_date.isoformat()}T{reminder_time[11:16]}"
                if reminder_time
                else f"{next_date.isoformat()}T{scheduled_time}"
                if scheduled_time
                else None
            )
            exists = connection.execute(
                "SELECT 1 FROM tasks WHERE user_id = ? AND title = ? AND date = ?",
                (user_id, title, next_date.isoformat()),
            ).fetchone()
            if not exists:
                connection.execute(
                    "INSERT INTO tasks (user_id, title, date, priority, scheduled_time, remind_at, recurrence, "
                    "reminder_interval, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (
                        user_id,
                        title,
                        next_date.isoformat(),
                        priority,
                        scheduled_time,
                        next_reminder,
                        recurrence,
                        values.get("reminder_interval", task["reminder_interval"]),
                    ),
                )
    return {"status": "ok"}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, user_id: int = Depends(current_user)):
    with database() as connection:
        cursor = connection.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Reja topilmadi")
    return {"status": "ok"}


@app.post("/api/transactions")
async def add_transaction(transaction: TransactionCreate, user_id: int = Depends(current_user)):
    transaction_date = (transaction.date or Date.fromisoformat(today_text())).isoformat()
    category = transaction.category.strip()
    if not category:
        raise HTTPException(status_code=422, detail="Kategoriyani kiriting")
    with database() as connection:
        cursor = connection.execute(
            "INSERT INTO transactions (user_id, type, category, amount, date, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                user_id,
                transaction.type,
                category,
                transaction.amount,
                transaction_date,
                transaction.note.strip(),
            ),
        )
        return {"id": cursor.lastrowid, "status": "ok"}


@app.delete("/api/transactions/{transaction_id}")
async def delete_transaction(transaction_id: int, user_id: int = Depends(current_user)):
    with database() as connection:
        cursor = connection.execute(
            "DELETE FROM transactions WHERE id = ? AND user_id = ?", (transaction_id, user_id)
        )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Amaliyot topilmadi")
    return {"status": "ok"}


@app.patch("/api/transactions/{transaction_id}")
async def update_transaction(
    transaction_id: int,
    update: TransactionUpdate,
    user_id: int = Depends(current_user),
):
    values = update.model_dump(exclude_unset=True)
    if any(value is None for value in values.values()):
        raise HTTPException(status_code=422, detail="Amaliyot maydonlari bo'sh bo'lishi mumkin emas")
    if not values:
        raise HTTPException(status_code=422, detail="Yangilash uchun ma'lumot yuboring")
    for key in ("category", "note"):
        if key in values and values[key] is not None:
            values[key] = values[key].strip()
    if "date" in values and values["date"] is not None:
        values["date"] = values["date"].isoformat()
    with database() as connection:
        assignments = ", ".join(f"{column} = ?" for column in values)
        cursor = connection.execute(
            f"UPDATE transactions SET {assignments} WHERE id = ? AND user_id = ?",
            (*values.values(), transaction_id, user_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Amaliyot topilmadi")
    return {"status": "ok"}


@app.post("/api/budgets")
async def save_budget(budget: BudgetCreate, user_id: int = Depends(current_user)):
    category = budget.category.strip()
    if not category:
        raise HTTPException(status_code=422, detail="Kategoriyani kiriting")
    month = budget.month or today_text()[:7]
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        raise HTTPException(status_code=422, detail="Oy YYYY-MM formatida bo'lishi kerak")
    with database() as connection:
        connection.execute(
            "INSERT INTO budgets (user_id, category, monthly_limit, month) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, category, month) DO UPDATE SET monthly_limit = excluded.monthly_limit",
            (user_id, category, budget.monthly_limit, month),
        )
    return {"status": "ok"}


@app.patch("/api/budgets/{budget_id}")
async def update_budget(budget_id: int, update: BudgetUpdate, user_id: int = Depends(current_user)):
    values = update.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=422, detail="Yangilash uchun ma'lumot yuboring")
    if any(value is None for value in values.values()):
        raise HTTPException(status_code=422, detail="Budjet maydonlari bo'sh bo'lishi mumkin emas")
    if "category" in values:
        values["category"] = values["category"].strip()
        if not values["category"]:
            raise HTTPException(status_code=422, detail="Kategoriyani kiriting")
    if "month" in values:
        try:
            datetime.strptime(values["month"], "%Y-%m")
        except ValueError:
            raise HTTPException(status_code=422, detail="Oy YYYY-MM formatida bo'lishi kerak")

    with database() as connection:
        budget = connection.execute(
            "SELECT * FROM budgets WHERE id = ? AND user_id = ?", (budget_id, user_id)
        ).fetchone()
        if not budget:
            raise HTTPException(status_code=404, detail="Budjet topilmadi")
        category = values.get("category", budget["category"])
        month = values.get("month", budget["month"])
        duplicate = connection.execute(
            "SELECT 1 FROM budgets WHERE user_id = ? AND category = ? AND month = ? AND id != ?",
            (user_id, category, month, budget_id),
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail="Bu kategoriya va oy uchun budjet allaqachon mavjud")
        assignments = ", ".join(f"{column} = ?" for column in values)
        connection.execute(
            f"UPDATE budgets SET {assignments} WHERE id = ? AND user_id = ?",
            (*values.values(), budget_id, user_id),
        )
    return {"status": "ok"}


@app.delete("/api/budgets/{budget_id}")
async def delete_budget(budget_id: int, user_id: int = Depends(current_user)):
    with database() as connection:
        cursor = connection.execute("DELETE FROM budgets WHERE id = ? AND user_id = ?", (budget_id, user_id))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Budjet topilmadi")
    return {"status": "ok"}


@app.post("/api/goals")
async def add_goal(goal: GoalCreate, user_id: int = Depends(current_user)):
    title = goal.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Maqsad nomini kiriting")
    with database() as connection:
        cursor = connection.execute(
            "INSERT INTO goals (user_id, title, target_amount, current_amount, deadline, is_completed, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                user_id,
                title,
                goal.target_amount,
                goal.current_amount,
                goal.deadline.isoformat() if goal.deadline else None,
                int(goal.current_amount >= goal.target_amount),
            ),
        )
        return {"id": cursor.lastrowid, "status": "ok"}


@app.patch("/api/goals/{goal_id}")
async def update_goal(goal_id: int, update: GoalUpdate, user_id: int = Depends(current_user)):
    values = update.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=422, detail="Yangilash uchun ma'lumot yuboring")
    if any(value is None for key, value in values.items() if key != "deadline"):
        raise HTTPException(status_code=422, detail="Maqsad maydonlari bo'sh bo'lishi mumkin emas")
    if "title" in values:
        values["title"] = values["title"].strip()
        if not values["title"]:
            raise HTTPException(status_code=422, detail="Maqsad nomini kiriting")
    if "deadline" in values and values["deadline"] is not None:
        values["deadline"] = values["deadline"].isoformat()

    with database() as connection:
        goal = connection.execute(
            "SELECT * FROM goals WHERE id = ? AND user_id = ?", (goal_id, user_id)
        ).fetchone()
        if not goal:
            raise HTTPException(status_code=404, detail="Maqsad topilmadi")
        new_amount = values.get("current_amount", goal["current_amount"])
        target_amount = values.get("target_amount", goal["target_amount"])
        assignments = ", ".join(f"{column} = ?" for column in values)
        connection.execute(
            f"UPDATE goals SET {assignments}, is_completed = ? WHERE id = ? AND user_id = ?",
            (*values.values(), int(new_amount >= target_amount), goal_id, user_id),
        )
    return {"status": "ok"}


@app.delete("/api/goals/{goal_id}")
async def delete_goal(goal_id: int, user_id: int = Depends(current_user)):
    with database() as connection:
        cursor = connection.execute("DELETE FROM goals WHERE id = ? AND user_id = ?", (goal_id, user_id))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Maqsad topilmadi")
    return {"status": "ok"}


@app.post("/api/reports")
async def save_report(report: ReportCreate, user_id: int = Depends(current_user)):
    report_date = (report.date or Date.fromisoformat(today_text())).isoformat()
    with database() as connection:
        task_totals = connection.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(is_completed), 0) completed FROM tasks "
            "WHERE user_id = ? AND date = ?",
            (user_id, report_date),
        ).fetchone()
        totals = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END), 0) income, "
            "COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) expense "
            "FROM transactions WHERE user_id = ? AND date = ?",
            (user_id, report_date),
        ).fetchone()
        report_values = (
            report.note.strip(),
            report.score,
            task_totals["total"],
            task_totals["completed"],
            totals["income"],
            totals["expense"],
            user_id,
            report_date,
        )
        cursor = connection.execute(
            "UPDATE reports SET note = ?, score = ?, tasks_total = ?, tasks_completed = ?, income = ?, expense = ? "
            "WHERE user_id = ? AND date = ?",
            report_values,
        )
        if cursor.rowcount == 0:
            connection.execute(
                "INSERT INTO reports (user_id, date, note, score, tasks_total, tasks_completed, income, expense) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    report_date,
                    report.note.strip(),
                    report.score,
                    task_totals["total"],
                    task_totals["completed"],
                    totals["income"],
                    totals["expense"],
                ),
            )
    return {"status": "ok"}


@app.get("/api/history")
async def get_history(month: str | None = None, user_id: int = Depends(current_user)):
    selected_month = month or today_text()[:7]
    try:
        datetime.strptime(selected_month, "%Y-%m")
    except ValueError:
        raise HTTPException(status_code=422, detail="Oy YYYY-MM formatida bo'lishi kerak")
    with database() as connection:
        rows = connection.execute(
            "SELECT date, SUM(type = 'income') income_count, SUM(type = 'expense') expense_count, "
            "SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END) income, "
            "SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END) expense "
            "FROM transactions WHERE user_id = ? AND date LIKE ? GROUP BY date ORDER BY date DESC",
            (user_id, f"{selected_month}%"),
        ).fetchall()
        task_rows = connection.execute(
            "SELECT date, COUNT(*) tasks_total, SUM(is_completed) tasks_completed FROM tasks "
            "WHERE user_id = ? AND date LIKE ? GROUP BY date",
            (user_id, f"{selected_month}%"),
        ).fetchall()
        report_rows = connection.execute(
            "SELECT date, note, score FROM reports WHERE user_id = ? AND date LIKE ?",
            (user_id, f"{selected_month}%"),
        ).fetchall()
    days = {row["date"]: dict(row) for row in rows}
    for row in task_rows:
        days.setdefault(row["date"], {"date": row["date"], "income": 0, "expense": 0})
        days[row["date"]].update({"tasks_total": row["tasks_total"], "tasks_completed": row["tasks_completed"]})
    for row in report_rows:
        days.setdefault(row["date"], {"date": row["date"], "income": 0, "expense": 0})
        days[row["date"]].update({"note": row["note"], "score": row["score"]})
    return sorted(days.values(), key=lambda item: item["date"], reverse=True)


@app.get("/api/export")
async def export_data(user_id: int = Depends(current_user)):
    return {"exported_at": datetime.now(LOCAL_TZ).isoformat(), "data": collect_export_data(user_id)}


def collect_export_data(user_id):
    with database() as connection:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} WHERE user_id = ?", (user_id,))]
            for table in ("tasks", "transactions", "reports", "budgets", "goals")
        }


def excel_filename(period_slug):
    return f"turkiston-daily-{period_slug}.xlsx"


def create_excel_report(user_id, period, start_date=None, end_date=None):
    period_start, period_end, period_label, period_slug = resolve_report_period(
        period, start_date, end_date
    )
    report_data = collect_export_data(user_id)
    if period_start and period_end:
        report_data = filter_export_data(report_data, period_start, period_end)
    group_by_day = bool(period_start and period_end and (period_end - period_start).days < 31)
    try:
        workbook = build_excel_report(report_data, period_label, group_by_day)
    except Exception as error:
        logging.exception("Foydalanuvchi %s uchun Excel hisoboti yaratilmadi", user_id)
        raise HTTPException(
            status_code=500,
            detail="Excel hisobotini yaratishda xato yuz berdi. Keyinroq qayta urinib ko'ring.",
        ) from error
    return workbook, excel_filename(period_slug), period_label


@app.get("/api/export.xlsx")
async def export_excel(
    period: Literal["day", "week", "month", "year", "all", "custom"] = "all",
    start_date: Date | None = None,
    end_date: Date | None = None,
    user_id: int = Depends(current_user),
):
    workbook, filename, _ = create_excel_report(user_id, period, start_date, end_date)
    return Response(
        content=workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/export.xlsx/send")
async def send_excel_report_to_telegram(
    period: Literal["day", "week", "month", "year", "all", "custom"] = "all",
    start_date: Date | None = None,
    end_date: Date | None = None,
    user_id: int = Depends(current_user),
):
    if not bot:
        raise HTTPException(status_code=503, detail="Telegram bot hozircha Excel hisobotini yubora olmaydi")
    workbook, filename, period_label = create_excel_report(
        user_id, period, start_date, end_date
    )
    document = BufferedInputFile(workbook, filename=filename)
    try:
        await bot.send_document(
            chat_id=user_id,
            document=document,
            caption=f"📊 {period_label}\nTo'liq Excel hisobotingiz ilova qilindi.",
        )
    except TelegramAPIError as error:
        logging.exception("Foydalanuvchi %s ga Excel hisoboti yuborilmadi", user_id)
        raise HTTPException(
            status_code=502,
            detail="Excel fayli Telegramga yuborilmadi. Bot chatini /start orqali ochib, qayta urinib ko'ring.",
        ) from error
    return {"status": "sent", "filename": filename, "period": period_label}


def resolve_report_period(period, start_date=None, end_date=None):
    today = Date.fromisoformat(today_text())
    if period == "day":
        start = end = today
        label = "Kunlik hisobot"
    elif period == "week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        label = "Haftalik hisobot"
    elif period == "month":
        start = today.replace(day=1)
        end = today.replace(day=monthrange(today.year, today.month)[1])
        label = "Oylik hisobot"
    elif period == "year":
        start = today.replace(month=1, day=1)
        end = today.replace(month=12, day=31)
        label = "Yillik hisobot"
    elif period == "custom":
        if not start_date or not end_date:
            raise HTTPException(status_code=422, detail="Maxsus hisobot uchun boshlanish va tugash sanasini tanlang")
        if start_date > end_date:
            raise HTTPException(status_code=422, detail="Boshlanish sanasi tugash sanasidan keyin bo'lishi mumkin emas")
        start, end = start_date, end_date
        label = "Hisobot davri"
    else:
        return None, None, "Barcha davr bo'yicha hisobot", "barcha-davr"
    label = f"{label} ({start:%d.%m.%Y} - {end:%d.%m.%Y})"
    slug = f"{period}-{start.isoformat()}-{end.isoformat()}"
    return start, end, label, slug


def build_period_summary(user_id, period):
    start, end, label, _ = resolve_report_period(period)
    start_text, end_text = start.isoformat(), end.isoformat()
    with database() as connection:
        tasks = connection.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(is_completed), 0) completed, "
            "COALESCE(SUM(CASE WHEN missed_reason IS NOT NULL THEN 1 ELSE 0 END), 0) missed, "
            "COALESCE(AVG(CASE WHEN late_minutes > 0 THEN late_minutes END), 0) average_late "
            "FROM tasks WHERE user_id = ? AND date BETWEEN ? AND ?",
            (user_id, start_text, end_text),
        ).fetchone()
        money_totals = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END), 0) income, "
            "COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) expense "
            "FROM transactions WHERE user_id = ? AND date BETWEEN ? AND ?",
            (user_id, start_text, end_text),
        ).fetchone()
    return {
        "period": period,
        "label": label,
        "start_date": start_text,
        "end_date": end_text,
        "tasks_total": tasks["total"],
        "tasks_completed": tasks["completed"],
        "tasks_missed": tasks["missed"],
        "average_late_minutes": round(tasks["average_late"]),
        "income": money_totals["income"],
        "expense": money_totals["expense"],
        "balance": money_totals["income"] - money_totals["expense"],
    }


@app.get("/api/reports/summary")
async def report_summary(
    period: Literal["day", "week", "month"] = "day",
    user_id: int = Depends(current_user),
):
    return build_period_summary(user_id, period)


@app.post("/api/reports/send")
async def send_report_to_telegram(
    period: Literal["day", "week", "month"] = "day",
    user_id: int = Depends(current_user),
):
    if not bot:
        raise HTTPException(status_code=503, detail="Telegram bot hozircha hisobot yubora olmaydi")
    summary = build_period_summary(user_id, period)
    try:
        await bot.send_message(user_id, format_report_message(summary))
    except TelegramAPIError as error:
        logging.exception("Telegram orqali hisobotni yuborib bo'lmadi")
        raise HTTPException(
            status_code=502,
            detail="Hisobot yuborilmadi. Bot chatini /start orqali ochib, qayta urinib ko'ring.",
        ) from error
    return {"status": "sent"}


def _fetch_gemini_rest(api_key: str, user_msg: str, system_prompt: str, model_name: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": user_msg}
                ]
            }
        ],
        "systemInstruction": {
            "parts": [
                {"text": system_prompt}
            ]
        },
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        res_bytes = resp.read()
        res_data = json.loads(res_bytes.decode("utf-8"))
        candidates = res_data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                return parts[0].get("text", "")
    return ""


def _fetch_pollinations_free_ai(user_msg: str, system_prompt: str) -> str:
    combined = f"System: {system_prompt}\n\nUser: {user_msg}\n\nReturn strictly valid JSON: {{\"reply\": \"...\", \"action\": null}}"
    encoded = urllib.parse.quote(combined)
    url = f"https://text.pollinations.ai/{encoded}?model=openai&jsonMode=true"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


async def query_gemini_api(api_key: str, user_msg: str, system_prompt: str) -> dict:
    raw_text = ""
    models_to_try = ["gemini-2.0-flash", "gemini-1.5-flash"]
    last_error_msg = ""

    # 1. API key berilgan bo'lsa, Gemini SDK va REST API orqali urunish
    if api_key:
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            for model in models_to_try:
                try:
                    response = client.models.generate_content(
                        model=model,
                        contents=user_msg,
                        config={"system_instruction": system_prompt, "response_mime_type": "application/json"}
                    )
                    if response and response.text:
                        raw_text = response.text
                        break
                except Exception as ex:
                    last_error_msg = str(ex)
                    logging.warning("SDK Gemini %s xatosi: %s", model, ex)
        except ImportError:
            pass
        except Exception as ex:
            last_error_msg = str(ex)
            logging.warning("SDK Gemini import/mijoz xatosi: %s", ex)

        if not raw_text:
            for model in models_to_try:
                try:
                    raw_text = await asyncio.to_thread(_fetch_gemini_rest, api_key, user_msg, system_prompt, model)
                    if raw_text:
                        break
                except urllib.error.HTTPError as he:
                    err_body = he.read().decode("utf-8", errors="ignore")
                    logging.warning("REST Gemini %s HTTP xatosi %s: %s", model, he.code, err_body)
                    last_error_msg = f"HTTP {he.code}: {err_body}"
                except Exception as ex:
                    last_error_msg = str(ex)
                    logging.warning("REST Gemini %s ulanish xatosi: %s", model, ex)

    # 2. Key bo'lmasa yoki Gemini xato bersa -> Bepul Ochiq AI ulanishi (Pollinations AI)
    if not raw_text:
        try:
            raw_text = await asyncio.to_thread(_fetch_pollinations_free_ai, user_msg, system_prompt)
        except Exception as ex:
            logging.warning("Bepul AI ulanish xatosi: %s", ex)
            if not api_key:
                last_error_msg = "Bepul AI serveriga ulanishda muammo"

    if not raw_text:
        raise Exception(f"AI ulanishida xato: {last_error_msg or 'Javob olinmadi'}")

    # 3. JSON natijani tozalash va o'qish
    cleaned_text = raw_text.strip()
    if cleaned_text.startswith("```"):
        lines = cleaned_text.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned_text = "\n".join(lines).strip()

    try:
        return json.loads(cleaned_text)
    except Exception:
        return {"reply": cleaned_text, "action": None}


@app.post("/api/ai/chat")
async def ai_assistant_chat(req: AIChatRequest, user_id: int = Depends(current_user)):
    user_msg = req.message.strip()
    if not user_msg:
        raise HTTPException(status_code=422, detail="Xabar matnini kiriting")

    active_key = (req.api_key or "").strip() or GEMINI_API_KEY

    # Foydalanuvchining bugungi holatini kontekst uchun olish
    today = today_text()
    with database() as connection:
        user_tasks = connection.execute(
            "SELECT title, is_completed, scheduled_time FROM tasks WHERE user_id = ? AND date = ? ORDER BY id DESC LIMIT 5",
            (user_id, today)
        ).fetchall()
        user_trans = connection.execute(
            "SELECT type, category, amount FROM transactions WHERE user_id = ? AND date = ? ORDER BY id DESC LIMIT 5",
            (user_id, today)
        ).fetchall()

    tasks_info = ", ".join([f"{t['title']} ({'bajarildi' if t['is_completed'] else 'bajarilmagan'})" for t in user_tasks]) or "reja yo'q"
    trans_info = ", ".join([f"{t['type']}: {t['category']} {t['amount']:,.0f} so'm" for t in user_trans]) or "kirim-chiqim yo'q"

    system_prompt = (
        f"Siz 'Turkiston Daily' shaxsiy rivojlanish va moliyaviy boshqaruv ilovasining sun'iy intellekt yordamchisisiz. "
        f"Bugungi sana: {today}. "
        f"Foydalanuvchining bugungi rejalari: [{tasks_info}]. "
        f"Bugungi moliyasi: [{trans_info}]. "
        "Vazifangiz:\n"
        "1. Foydalanuvchi savollariga istalgan tilda (o'zbek, rus, ingliz) muloyim va aniq javob berish.\n"
        "2. Reja qo'shish ('create_task'), tahrirlash ('update_task' - keyword yoki title orqali topib, title, priority, date, scheduled_time o'zgartirish), o'chirish ('delete_task');\n"
        "   Moliyaviy yozuv qo'shish ('create_transaction'), tahrirlash ('update_transaction' - keyword orqali topib amount, category, note o'zgartirish), o'chirish ('delete_transaction');\n"
        "   Budjet qo'yish ('create_budget') va Maqsad qo'yish ('create_goal') buyruqlarini shakllantiring.\n"
        "Javobingizni FAQAT quyidagi JSON formatida qaytaring, boshqa hech qanday izoh qo'shmang:\n"
        "{\n"
        '  "reply": "Chiroyli javobingiz",\n'
        '  "action": null | {\n'
        '     "type": "create_task" | "update_task" | "delete_task" | "create_transaction" | "update_transaction" | "delete_transaction" | "create_budget" | "create_goal",\n'
        '     "data": { ... }\n'
        '  }\n'
        "}\n"
        "create_task: {\"title\": \"...\", \"date\": \"YYYY-MM-DD\", \"scheduled_time\": \"HH:MM\", \"priority\": \"normal\" | \"high\"}\n"
        "update_task: {\"keyword\": \"...\", \"title\": \"...\", \"priority\": \"normal\" | \"high\", \"date\": \"YYYY-MM-DD\", \"scheduled_time\": \"HH:MM\"}\n"
        "delete_task: {\"keyword\": \"...\"}\n"
        "create_transaction: {\"type\": \"income\" | \"expense\", \"category\": \"...\", \"amount\": 45000, \"note\": \"...\"}\n"
        "update_transaction: {\"keyword\": \"...\", \"amount\": 50000, \"category\": \"...\", \"note\": \"...\"}\n"
        "delete_transaction: {\"keyword\": \"...\"}\n"
        "create_budget: {\"category\": \"...\", \"monthly_limit\": 500000}\n"
        "create_goal: {\"title\": \"...\", \"target_amount\": 8000000, \"current_amount\": 0}"
    )

    try:
        parsed = await query_gemini_api(active_key, user_msg, system_prompt)
    except Exception as e:
        logging.warning("Gemini AI chaqiruvida xato: %s", e)
        return {
            "status": "error",
            "reply": f"Gemini AI bilan bog'lanishda xatolik yuz berdi: {str(e)}",
            "action": None
        }

    # Agar AI action bergan bo'lsa, to'g'ridan-to'g'ri bazaga bajarish
    action = parsed.get("action")
    action_result = None
    if action and isinstance(action, dict):
        act_type = action.get("type")
        act_data = action.get("data") or {}
        with database() as connection:
            if act_type == "create_task" and act_data.get("title"):
                t_date = act_data.get("date") or today
                t_time = act_data.get("scheduled_time")
                remind = f"{t_date}T{t_time}" if t_time else None
                priority = act_data.get("priority", "normal")
                connection.execute(
                    "INSERT INTO tasks (user_id, title, date, priority, scheduled_time, remind_at, recurrence, reminder_interval, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'none', 'once', CURRENT_TIMESTAMP)",
                    (user_id, act_data["title"], t_date, priority, t_time, remind)
                )
                action_result = "Reja avtomatik saqlandi ✅"
            elif act_type == "update_task" and act_data.get("keyword"):
                kw = f"%{act_data['keyword']}%"
                task_row = connection.execute("SELECT id FROM tasks WHERE user_id = ? AND title LIKE ? LIMIT 1", (user_id, kw)).fetchone()
                if task_row:
                    tid = task_row["id"]
                    fields = []
                    params = []
                    if act_data.get("title"):
                        fields.append("title = ?")
                        params.append(act_data["title"])
                    if act_data.get("priority"):
                        fields.append("priority = ?")
                        params.append(act_data["priority"])
                    if act_data.get("date"):
                        fields.append("date = ?")
                        params.append(act_data["date"])
                    if act_data.get("scheduled_time"):
                        fields.append("scheduled_time = ?")
                        params.append(act_data["scheduled_time"])
                        fields.append("remind_at = ?")
                        params.append(f"{act_data.get('date', today)}T{act_data['scheduled_time']}")
                    if fields:
                        params.extend([tid, user_id])
                        connection.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ? AND user_id = ?", params)
                        action_result = "Reja muvaffaqiyatli tahrirlandi ✍️"
                else:
                    action_result = "Tahrirlash uchun reja topilmadi"
            elif act_type == "delete_task" and act_data.get("keyword"):
                kw = f"%{act_data['keyword']}%"
                connection.execute("DELETE FROM tasks WHERE user_id = ? AND title LIKE ?", (user_id, kw))
                action_result = "Reja o'chirildi 🗑"
            elif act_type == "create_transaction" and act_data.get("amount"):
                t_type = "income" if act_data.get("type") == "income" else "expense"
                t_cat = act_data.get("category") or "Boshqa"
                t_amt = float(act_data.get("amount") or 0)
                t_note = act_data.get("note") or ""
                connection.execute(
                    "INSERT INTO transactions (user_id, type, category, amount, date, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (user_id, t_type, t_cat, t_amt, today, t_note)
                )
                action_result = f"{'Kirim' if t_type == 'income' else 'Chiqim'} avtomatik saqlandi ✅"
            elif act_type == "update_transaction" and act_data.get("keyword"):
                kw = f"%{act_data['keyword']}%"
                tx_row = connection.execute("SELECT id FROM transactions WHERE user_id = ? AND (category LIKE ? OR note LIKE ?) LIMIT 1", (user_id, kw, kw)).fetchone()
                if tx_row:
                    txid = tx_row["id"]
                    fields = []
                    params = []
                    if act_data.get("amount"):
                        fields.append("amount = ?")
                        params.append(float(act_data["amount"]))
                    if act_data.get("category"):
                        fields.append("category = ?")
                        params.append(act_data["category"])
                    if act_data.get("note"):
                        fields.append("note = ?")
                        params.append(act_data["note"])
                    if fields:
                        params.extend([txid, user_id])
                        connection.execute(f"UPDATE transactions SET {', '.join(fields)} WHERE id = ? AND user_id = ?", params)
                        action_result = "Moliya yozuvi tahrirlandi ✍️"
                else:
                    action_result = "Tahrirlash uchun moliya yozuvi topilmadi"
            elif act_type == "delete_transaction" and act_data.get("keyword"):
                kw = f"%{act_data['keyword']}%"
                connection.execute("DELETE FROM transactions WHERE user_id = ? AND (category LIKE ? OR note LIKE ?)", (user_id, kw, kw))
                action_result = "Moliya yozuvi o'chirildi 🗑"
            elif act_type == "create_budget" and act_data.get("category") and act_data.get("monthly_limit"):
                b_cat = act_data["category"].strip()
                b_lim = float(act_data["monthly_limit"])
                b_month = today[:7]
                connection.execute(
                    "INSERT INTO budgets (user_id, category, monthly_limit, month) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(user_id, category, month) DO UPDATE SET monthly_limit = excluded.monthly_limit",
                    (user_id, b_cat, b_lim, b_month)
                )
                action_result = f"Budjet limiti saqlandi: {b_cat} - {b_lim:,.0f} so'm ✅"
            elif act_type == "create_goal" and act_data.get("title") and act_data.get("target_amount"):
                g_title = act_data["title"].strip()
                g_target = float(act_data["target_amount"])
                g_current = float(act_data.get("current_amount", 0))
                connection.execute(
                    "INSERT INTO goals (user_id, title, target_amount, current_amount, is_completed, created_at) "
                    "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (user_id, g_title, g_target, g_current, int(g_current >= g_target))
                )
                action_result = f"Maqsad yaratildi: {g_title} ({g_target:,.0f} so'm) ✅"

    return {
        "status": "ok",
        "reply": parsed.get("reply", "Bajarildi!"),
        "action": action,
        "action_result": action_result
    }


def format_report_message(summary):
    return (
        f"📊 {summary['label']}\n\n"
        f"📋 Rejalar: {summary['tasks_completed']} / {summary['tasks_total']} bajarildi\n"
        f"⏳ Bajarilmagan: {summary['tasks_missed']}\n"
        f"💰 Kirim: {summary['income']:,.0f} so'm\n"
        f"💸 Chiqim: {summary['expense']:,.0f} so'm\n"
        f"⚖️ Balans: {summary['balance']:,.0f} so'm"
        + (
            f"\n⌛ O'rtacha kechikish: {summary['average_late_minutes']} daqiqa"
            if summary["average_late_minutes"]
            else ""
        )
    )


def filter_export_data(data, start, end):
    start_text, end_text = start.isoformat(), end.isoformat()
    filtered = dict(data)
    for table in ("tasks", "transactions", "reports"):
        filtered[table] = [
            row for row in data[table]
            if start_text <= str(row.get("date", ""))[:10] <= end_text
        ]
    filtered_budgets = []
    for row in data["budgets"]:
        try:
            year, month = (int(part) for part in str(row.get("month", "")).split("-", 1))
            budget_start = Date(year, month, 1)
            budget_end = Date(year, month, monthrange(year, month)[1])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Budjet oyini YYYY-MM formatida saqlangan bo'lishi kerak")
        if budget_start <= end and budget_end >= start:
            filtered_budgets.append(row)
    filtered["budgets"] = filtered_budgets
    return filtered


async def start_cmd(message):
    record_user_activity(message.from_user.id)
    await message.answer(
        "Quyidagi paneldan kerakli bo'limni tanlang yoki /buyruqlar orqali buyruqlarni ko'ring.",
        reply_markup=build_reply_keyboard(message.from_user.id),
    )
    if not WEBAPP_URL:
        await message.answer(
            "Mini App manzili sozlanmagan. start_bot.bat orqali botni ishga tushiring va /start ni qayta yuboring."
        )
        return

    web_app = WebAppInfo(url=WEBAPP_URL)
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Turkiston Daily'ni ochish", web_app=web_app)
    ]])
    await message.answer(
        "Kunlik reja, eslatma, kirim-chiqim va hisobotlaringiz — barchasi Mini App ichida.",
        reply_markup=markup,
    )
    await bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonWebApp(text="Turkiston Daily", web_app=web_app),
    )


async def add_plan_cmd(message):
    record_user_activity(message.from_user.id)
    arguments = (message.text or "").split(maxsplit=1)
    if len(arguments) < 2 or not arguments[1].strip():
        await message.answer("Reja qo'shish: /reja [HH:MM] reja matni\nMasalan: /reja 18:30 Kitob o'qish")
        return

    content = arguments[1].strip()
    reminder_text, separator, title = content.partition(" ")
    remind_at = None
    scheduled_time = None
    try:
        scheduled_time = datetime.strptime(reminder_text, "%H:%M").time()
    except ValueError:
        title = content
    else:
        if not separator or not title.strip():
            await message.answer("Reja matnini ham yozing. Masalan: /reja 18:30 Kitob o'qish")
            return
        remind_at = datetime.combine(Date.fromisoformat(today_text()), scheduled_time).isoformat(timespec="minutes")
        title = title.strip()

    with database() as connection:
        connection.execute(
            "INSERT INTO tasks (user_id, title, date, scheduled_time, remind_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (message.from_user.id, title, today_text(), scheduled_time.strftime("%H:%M") if scheduled_time else None, remind_at),
        )

    if remind_at:
        await message.answer(f"Reja qo'shildi: {title}\nBajarish vaqti: {scheduled_time:%H:%M}. Shu payt eslataman.")
    else:
        await message.answer(f"Bugungi rejaga qo'shildi: {title}\nEslatma uchun /reja HH:MM reja matni deb yozing.")


async def add_transaction_cmd(message, transaction_type):
    record_user_activity(message.from_user.id)
    arguments = (message.text or "").split(maxsplit=3)
    if len(arguments) < 3:
        command = "kirim" if transaction_type == "income" else "chiqim"
        await message.answer(
            f"Foydalanish: /{command} summa kategoriya [izoh]\n"
            f"Masalan: /{command} 25000 {'Maosh' if transaction_type == 'income' else 'Oziq-ovqat'}"
        )
        return

    amount_text, category = arguments[1], arguments[2].strip()
    try:
        amount = float(amount_text.replace(",", "."))
    except ValueError:
        amount = 0
    if not math.isfinite(amount) or amount <= 0 or amount > 1_000_000_000_000 or not category:
        await message.answer("Summani musbat son, kategoriyani esa bo'sh bo'lmagan matn qilib kiriting.")
        return

    note = arguments[3].strip() if len(arguments) > 3 else ""
    with database() as connection:
        connection.execute(
            "INSERT INTO transactions (user_id, type, category, amount, date, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (message.from_user.id, transaction_type, category, amount, today_text(), note),
        )

    label = "Kirim" if transaction_type == "income" else "Chiqim"
    await message.answer(f"{label} saqlandi: {amount:,.0f} so'm — {category}")


async def income_cmd(message):
    await add_transaction_cmd(message, "income")


async def expense_cmd(message):
    await add_transaction_cmd(message, "expense")


async def today_cmd(message):
    record_user_activity(message.from_user.id)
    chosen_date = today_text()
    user_id = message.from_user.id
    with database() as connection:
        tasks = connection.execute(
            "SELECT title, is_completed, scheduled_time, remind_at FROM tasks WHERE user_id = ? AND date = ? "
            "ORDER BY is_completed, CASE WHEN scheduled_time IS NULL THEN 1 ELSE 0 END, scheduled_time, id",
            (user_id, chosen_date),
        ).fetchall()
        totals = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END), 0) income, "
            "COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) expense "
            "FROM transactions WHERE user_id = ? AND date = ?",
            (user_id, chosen_date),
        ).fetchone()

    lines = ["Bugungi rejalaringiz:"]
    if tasks:
        for task in tasks:
            status = "✅ Bajarildi" if task["is_completed"] else "▫️ Bajarilmagan"
            scheduled = f" — soat {task['scheduled_time']}" if task["scheduled_time"] else ""
            reminder_time = task["remind_at"][11:16] if task["remind_at"] else None
            reminder = (
                f" (eslatma {reminder_time})"
                if reminder_time and reminder_time != task["scheduled_time"]
                else ""
            )
            lines.append(f"{status}: {task['title']}{scheduled}{reminder}")
    else:
        lines.append("Hozircha reja yo'q. /reja matn deb yuboring.")
    lines.extend([
        "",
        f"Kirim: {totals['income']:,.0f} so'm",
        f"Chiqim: {totals['expense']:,.0f} so'm",
        f"Balans: {totals['income'] - totals['expense']:,.0f} so'm",
    ])
    await message.answer("\n".join(lines))


async def send_excel(message, user_id, period="month"):
    record_user_activity(user_id)
    start, end, label, slug = resolve_report_period(period)
    report_data = collect_export_data(user_id)
    if start and end:
        report_data = filter_export_data(report_data, start, end)
    group_by_day = bool(start and end and (end - start).days < 31)
    content = build_excel_report(report_data, label, group_by_day)
    document = BufferedInputFile(content, filename=excel_filename(slug))
    await message.answer_document(document=document, caption=f"{label}.")


async def excel_cmd(message):
    arguments = (message.text or "").split()
    period = arguments[1].lower() if len(arguments) > 1 else "month"
    if period not in {"week", "month", "year", "all"}:
        await message.answer("Davrni tanlang: /excel week, /excel month, /excel year yoki /excel all")
        return
    await send_excel(message, message.from_user.id, period)


async def report_cmd(message):
    record_user_activity(message.from_user.id)
    arguments = (message.text or "").split()
    period = arguments[1].lower() if len(arguments) > 1 else "day"
    if period not in {"day", "week", "month"}:
        await message.answer("Davrni tanlang: /hisobot day, /hisobot week yoki /hisobot month")
        return
    await message.answer(format_report_message(build_period_summary(message.from_user.id, period)))


async def help_cmd(message):
    admin_commands = (
        "\n🛠 Admin buyruqlari: /admin, /stat, /users, /user ID, /health, "
        "/broadcast matn, /cancel"
        if message.from_user.id in ADMIN_USER_IDS
        else ""
    )
    await message.answer(
        "📋 Asosiy buyruqlar\n"
        "/start — Mini App va pastki panel\n"
        "/reja [HH:MM] matn — reja qo'shish\n"
        "/bugun — bugungi rejalar va balans\n"
        "/kirim summa kategoriya — kirim yozish\n"
        "/chiqim summa kategoriya — chiqim yozish\n"
        "/hisobot day|week|month — davriy hisobot\n"
        "/excel week|month|year|all — Excel hisobot\n"
        "Buyruqlarni yozmasdan pastki panel tugmalaridan ham foydalanishingiz mumkin."
        f"{admin_commands}"
    )


async def excel_callback(callback):
    await callback.answer("Excel hisoboti tayyorlanmoqda")
    period = (callback.data or "excel:month").partition(":")[2] or "month"
    await send_excel(callback.message, callback.from_user.id, period)


async def report_callback(callback):
    record_user_activity(callback.from_user.id)
    period = (callback.data or "report:day").partition(":")[2]
    if period not in {"day", "week", "month"}:
        await callback.answer("Hisobot davri noto'g'ri", show_alert=True)
        return
    await callback.answer("Hisobot tayyorlanmoqda")
    await callback.message.answer(
        format_report_message(build_period_summary(callback.from_user.id, period))
    )


async def admin_cmd(message):
    record_user_activity(message.from_user.id)
    if message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("Bu bo'lim faqat administratorlar uchun.")
        return
    await message.answer(
        "🛠 ADMIN BOSHQARUV PANELI\n\n"
        "Quyidagi tugmalardan kerakli amalni tanlang. Har bir bo'lim bot ichida ochiladi:\n"
        "📊 Statistika — foydalanish ko'rsatkichlari\n"
        "👥 Foydalanuvchilar — ro'yxat yoki ID bo'yicha ma'lumot\n"
        "📢 E'lon — xabarni ko'rib, tasdiqlagandan keyin yuborish\n"
        "🩺 Tizim holati — bot, baza va Mini App tekshiruvi\n"
        "ℹ️ Qo'llanma — amallar bo'yicha qisqa izohlar",
        reply_markup=admin_main_keyboard(),
    )


def admin_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Statistika", callback_data="admin:stats"),
            InlineKeyboardButton(text="👥 Foydalanuvchilar ro'yxati", callback_data="admin:users:0"),
        ],
        [
            InlineKeyboardButton(text="🔎 ID bo'yicha qidirish", callback_data="admin:lookup"),
            InlineKeyboardButton(text="📢 E'lon yuborish", callback_data="admin:broadcast"),
        ],
        [
            InlineKeyboardButton(text="🩺 Tizim holati", callback_data="admin:health"),
            InlineKeyboardButton(text="ℹ️ Qo'llanma", callback_data="admin:guide"),
        ],
    ])


def build_admin_stats():
    now = datetime.now(LOCAL_TZ)
    today = now.date().isoformat()
    week_start = (now.date() - timedelta(days=6)).isoformat()
    month_start = now.date().replace(day=1).isoformat()
    with database() as connection:
        users = connection.execute(
            "SELECT COUNT(*) total, "
            "COALESCE(SUM(CASE WHEN substr(last_seen_at, 1, 10) = ? THEN 1 ELSE 0 END), 0) today, "
            "COALESCE(SUM(CASE WHEN substr(last_seen_at, 1, 10) >= ? THEN 1 ELSE 0 END), 0) week, "
            "COALESCE(SUM(CASE WHEN substr(last_seen_at, 1, 10) >= ? THEN 1 ELSE 0 END), 0) month "
            "FROM users",
            (today, week_start, month_start),
        ).fetchone()
        tasks = connection.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(is_completed), 0) completed, "
            "COALESCE(SUM(CASE WHEN missed_reason IS NOT NULL THEN 1 ELSE 0 END), 0) missed "
            "FROM tasks"
        ).fetchone()
        transactions = connection.execute(
            "SELECT COUNT(*) total, "
            "COALESCE(SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END), 0) income, "
            "COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) expense "
            "FROM transactions"
        ).fetchone()
        budgets = connection.execute("SELECT COUNT(*) FROM budgets").fetchone()[0]
        goals = connection.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
        reports = connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    return (
        "📊 Bot statistikasi\n\n"
        f"👥 Jami foydalanuvchi: {users['total']}\n"
        f"🟢 Bugun faol: {users['today']}\n"
        f"📈 7 kunda faol: {users['week']}\n"
        f"🗓 Shu oy faol: {users['month']}\n\n"
        f"📋 Rejalar: {tasks['total']} (✅ {tasks['completed']}, ⏳ {tasks['missed']})\n"
        f"💳 Moliya yozuvlari: {transactions['total']}\n"
        f"↗ Jami kirim: {transactions['income']:,.0f} so'm\n"
        f"↘ Jami chiqim: {transactions['expense']:,.0f} so'm\n"
        f"🎯 Budjetlar: {budgets} · Jamg'arma maqsadlari: {goals}\n"
        f"📝 Kun yakuni hisobotlari: {reports}"
    )


def admin_panel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Admin panelga qaytish", callback_data="admin:home"),
    ]])


def admin_users_keyboard(page, total, page_size):
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"admin:users:{page - 1}"))
    if (page + 1) * page_size < total:
        buttons.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"admin:users:{page + 1}"))
    rows = [buttons] if buttons else []
    rows.append([InlineKeyboardButton(text="🔎 ID bo'yicha qidirish", callback_data="admin:lookup")])
    rows.append([InlineKeyboardButton(text="⬅️ Admin panelga qaytish", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_stats(message):
    if message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("Bu bo'lim faqat administratorlar uchun.")
        return
    await message.answer(build_admin_stats(), reply_markup=admin_panel_keyboard())


async def admin_users_page(target, page=0, edit=False):
    page = max(0, page)
    page_size = 8
    with database() as connection:
        total = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = connection.execute(
            "SELECT user_id, first_seen_at, last_seen_at FROM users "
            "ORDER BY last_seen_at DESC, user_id DESC LIMIT ? OFFSET ?",
            (page_size, page * page_size),
        ).fetchall()
    if not rows and page:
        page = max(0, (total - 1) // page_size)
        with database() as connection:
            rows = connection.execute(
                "SELECT user_id, first_seen_at, last_seen_at FROM users "
                "ORDER BY last_seen_at DESC, user_id DESC LIMIT ? OFFSET ?",
                (page_size, page * page_size),
            ).fetchall()
    lines = [f"👥 Foydalanuvchilar: {total}", "So'nggi faollik bo'yicha:"]
    lines.extend(
        f"\n• {row['user_id']} — oxirgi faollik: {(row['last_seen_at'] or 'noma’lum')[:16]}"
        for row in rows
    )
    markup = admin_users_keyboard(page, total, page_size)
    if edit:
        await target.edit_text("\n".join(lines), reply_markup=markup)
    else:
        await target.answer("\n".join(lines), reply_markup=markup)


async def admin_users_cmd(message):
    if message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("Bu bo'lim faqat administratorlar uchun.")
        return
    await admin_users_page(message)


async def admin_user_cmd(message):
    if message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("Bu bo'lim faqat administratorlar uchun.")
        return
    arguments = (message.text or "").split(maxsplit=1)
    if len(arguments) < 2 or not arguments[1].strip().isdigit():
        await message.answer("Foydalanish: /user Telegram_ID")
        return
    user_id = int(arguments[1].strip())
    await admin_show_user(message, user_id)


async def admin_show_user(target, user_id, edit=False):
    with database() as connection:
        user = connection.execute(
            "SELECT full_name, username, phone, first_seen_at, last_seen_at FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            for table in ("tasks", "transactions", "budgets", "goals", "reports")
        }
    if not user:
        msg = "Bu ID bilan foydalanuvchi topilmadi."
        if edit:
            await target.edit_text(msg, reply_markup=admin_panel_keyboard())
        else:
            await target.answer(msg, reply_markup=admin_panel_keyboard())
        return

    text = (
        f"👤 Foydalanuvchi: {escapeHtml(user['full_name'] or 'Noma’lum')} (@{escapeHtml(user['username'] or 'n/a')})\n"
        f"🆔 ID: <code>{user_id}</code> | Tel: {escapeHtml(user['phone'] or 'n/a')}\n"
        f"🗓 Birinchi faollik: {user['first_seen_at'] or 'noma’lum'}\n"
        f"🕒 Oxirgi faollik: {user['last_seen_at'] or 'noma’lum'}\n\n"
        f"📋 Rejalar: {counts['tasks']}\n"
        f"💳 Moliya yozuvlari: {counts['transactions']}\n"
        f"🎯 Budjetlar: {counts['budgets']} · Maqsadlar: {counts['goals']}\n"
        f"📝 Hisobotlar: {counts['reports']}"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Rejalarini ko'rish", callback_data=f"admin:user:tasks:{user_id}"),
            InlineKeyboardButton(text="💳 Moliyasini ko'rish", callback_data=f"admin:user:tx:{user_id}"),
        ],
        [
            InlineKeyboardButton(text="👥 Foydalanuvchilar ro'yxati", callback_data="admin:users:0"),
            InlineKeyboardButton(text="⬅️ Admin panelga qaytish", callback_data="admin:home"),
        ]
    ])
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


async def admin_health(message):
    if message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("Bu bo'lim faqat administratorlar uchun.")
        return
    try:
        with database() as connection:
            connection.execute("SELECT 1").fetchone()
        database_status = "✅ ishlayapti"
    except Exception:
        logging.exception("Admin panelida baza holatini tekshirib bo'lmadi")
        database_status = "❌ xato (logni tekshiring)"
    bot_status = "✅ ishlayapti" if bot else "❌ token sozlanmagan"
    await message.answer(
        "🩺 Tizim holati\n\n"
        f"🤖 Telegram bot: {bot_status}\n"
        f"🗄 Ma'lumotlar bazasi: {database_status}\n"
        f"🌐 Mini App manzili: {'✅ sozlangan' if WEBAPP_URL else '❌ sozlanmagan'}",
        reply_markup=admin_panel_keyboard(),
    )


def admin_broadcast_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yuborish", callback_data="admin:broadcast:confirm"),
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin:broadcast:cancel"),
    ]])


async def prepare_admin_broadcast(message, text):
    if not text.strip():
        ADMIN_PENDING_ACTIONS[message.from_user.id] = "broadcast"
        await message.answer("Foydalanuvchilarga yuboriladigan e'lon matnini yozing. Bekor qilish: /cancel")
        return
    text = text.strip()
    if len(text) > 3500:
        await message.answer("E'lon 3500 belgidan oshmasligi kerak.")
        return
    ADMIN_PENDING_ACTIONS[message.from_user.id] = {"broadcast": text}
    await message.answer(
        f"📢 E'lonni barcha bot foydalanuvchilariga yuborish?\n\n{text}\n\n"
        "Yuborishdan oldin matnni tekshiring:",
        reply_markup=admin_broadcast_keyboard(),
    )


async def admin_broadcast_cmd(message):
    if message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("Bu bo'lim faqat administratorlar uchun.")
        return
    text = (message.text or "").partition(" ")[2]
    await prepare_admin_broadcast(message, text)


async def admin_callback(callback):
    if callback.from_user.id not in ADMIN_USER_IDS:
        await callback.answer("Bu bo'lim faqat administratorlar uchun.", show_alert=True)
        return
    data = callback.data or ""
    if data == "admin:home":
        await callback.answer()
        await callback.message.edit_text(
            "🛠 ADMIN BOSHQARUV PANELI\n\n"
            "📊 Statistika — foydalanish ko'rsatkichlari\n"
            "👥 Foydalanuvchilar — ro'yxat yoki ID bo'yicha ma'lumot\n"
            "📢 E'lon — xabarni ko'rib, tasdiqlagandan keyin yuborish\n"
            "🩺 Tizim holati — bot, baza va Mini App tekshiruvi\n"
            "ℹ️ Qo'llanma — amallar bo'yicha qisqa izohlar",
            reply_markup=admin_main_keyboard(),
        )
    elif data == "admin:stats":
        await callback.answer()
        await callback.message.edit_text(build_admin_stats(), reply_markup=admin_panel_keyboard())
    elif data == "admin:users:0" or data.startswith("admin:users:"):
        try:
            page = int(data.rsplit(":", 1)[1])
        except ValueError:
            await callback.answer("Sahifa aniqlanmadi.", show_alert=True)
            return
        await callback.answer()
        await admin_users_page(callback.message, page, edit=True)
    elif data == "admin:lookup":
        ADMIN_PENDING_ACTIONS[callback.from_user.id] = "user_lookup"
        await callback.answer()
        await callback.message.answer(
            "🔎 Foydalanuvchi Telegram ID raqamini yuboring.\n"
            "Masalan: 5144043830\n\nBekor qilish uchun /cancel yozing."
        )
    elif data == "admin:guide":
        await callback.answer()
        await callback.message.edit_text(
            "ℹ️ ADMIN PANEL QO'LLANMASI\n\n"
            "📊 Statistika — jami va yaqinda faol foydalanuvchilar, rejalar, "
            "kirim-chiqim, budjetlar, maqsadlar va hisobotlar.\n\n"
            "👥 Foydalanuvchilar ro'yxati — oxirgi faollik bo'yicha sahifalangan ro'yxat.\n\n"
            "🔎 ID bo'yicha qidirish — Telegram ID kiriting; birinchi/oxirgi faollik "
            "va yozuvlar soni ko'rsatiladi. Shaxsiy yozuvlar matni ochilmaydi.\n\n"
            "📢 E'lon yuborish — matnni yuboring, oldindan ko'rib chiqing va "
            "faqat ✅ Yuborish tugmasini bosganingizda tarqatiladi. ❌ Bekor qilish "
            "yoki /cancel bilan to'xtatish mumkin.\n\n"
            "🩺 Tizim holati — bot, ma'lumotlar bazasi va Mini App manzili "
            "sozlanganini tekshiradi.",
            reply_markup=admin_panel_keyboard(),
        )
    elif data == "admin:health":
        try:
            with database() as connection:
                connection.execute("SELECT 1").fetchone()
            status = "✅ Baza ishlayapti"
        except Exception:
            logging.exception("Admin panelida baza holatini tekshirib bo'lmadi")
            status = "❌ Baza xatosi (logni tekshiring)"
        await callback.answer()
        await callback.message.edit_text(
            "🩺 Tizim holati\n\n"
            f"🤖 Telegram bot: {'✅ ishlayapti' if bot else '❌ token sozlanmagan'}\n"
            f"🗄 Ma'lumotlar bazasi: {status}\n"
            f"🌐 Mini App manzili: {'✅ sozlangan' if WEBAPP_URL else '❌ sozlanmagan'}",
            reply_markup=admin_panel_keyboard(),
        )
    elif data == "admin:broadcast":
        ADMIN_PENDING_ACTIONS[callback.from_user.id] = "broadcast"
        await callback.answer()
        await callback.message.answer(
            "📢 Foydalanuvchilarga yuboriladigan e'lon matnini yozing.\n"
            "Avval matnni ko'rib chiqasiz; tasdiqlamaguningizcha hech kimga yuborilmaydi.\n"
            "Bekor qilish uchun /cancel yozing."
        )
    elif data == "admin:broadcast:cancel":
        ADMIN_PENDING_ACTIONS.pop(callback.from_user.id, None)
        await callback.answer("E'lon bekor qilindi.")
        await callback.message.edit_reply_markup(reply_markup=None)
    elif data == "admin:broadcast:confirm":
        pending = ADMIN_PENDING_ACTIONS.get(callback.from_user.id)
        text = pending.get("broadcast") if isinstance(pending, dict) else None
        if not text:
            await callback.answer("E'lon matni topilmadi. Qaytadan boshlang.", show_alert=True)
            return
        ADMIN_PENDING_ACTIONS.pop(callback.from_user.id, None)
        await callback.answer("Yuborish boshlandi.")
        await callback.message.edit_reply_markup(reply_markup=None)
        with database() as connection:
            recipients = [
                row["user_id"] for row in connection.execute(
                    "SELECT user_id FROM users ORDER BY user_id"
                ).fetchall()
            ]
        sent = 0
        failed = 0
        for recipient_id in recipients:
            try:
                await bot.send_message(recipient_id, f"📢 Admin e'loni\n\n{text}")
                sent += 1
            except TelegramAPIError:
                failed += 1
                logging.exception("Admin e'lonini foydalanuvchiga yuborib bo'lmadi: %s", recipient_id)
            await asyncio.sleep(0.05)
        await callback.message.answer(
            f"📢 E'lon yuborish yakunlandi.\n✅ Yetkazildi: {sent}\n⚠️ Yuborilmadi: {failed}"
        )
    elif data.startswith("admin:user:tasks:"):
        uid = int(data.split(":")[3])
        with database() as connection:
            tasks = connection.execute("SELECT title, date, is_completed FROM tasks WHERE user_id = ? ORDER BY date DESC LIMIT 10", (uid,)).fetchall()
        t_lines = [f"📋 Foydalanuvchi ({uid}) rejalari (oxirgi 10 ta):"]
        if tasks:
            for t in tasks:
                t_lines.append(f"• {'✅' if t['is_completed'] else '⏳'} {t['date']}: {escapeHtml(t['title'])}")
        else:
            t_lines.append("Rejalar topilmadi.")
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"admin:user:back:{uid}")]])
        await callback.answer()
        await callback.message.edit_text("\n".join(t_lines), reply_markup=markup)
    elif data.startswith("admin:user:tx:"):
        uid = int(data.split(":")[3])
        with database() as connection:
            txs = connection.execute("SELECT type, category, amount, date FROM transactions WHERE user_id = ? ORDER BY date DESC LIMIT 10", (uid,)).fetchall()
        tx_lines = [f"💳 Foydalanuvchi ({uid}) moliya amaliyotlari (oxirgi 10 ta):"]
        if txs:
            for tx in txs:
                tx_lines.append(f"• {'↗' if tx['type']=='income' else '↘'} {tx['date']}: {escapeHtml(tx['category'])} — {tx['amount']:,.0f} so'm")
        else:
            tx_lines.append("Moliya yozuvlari topilmadi.")
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"admin:user:back:{uid}")]])
        await callback.answer()
        await callback.message.edit_text("\n".join(tx_lines), reply_markup=markup)
    elif data.startswith("admin:user:back:"):
        uid = int(data.split(":")[3])
        await callback.answer()
        await admin_show_user(callback.message, uid, edit=True)
    else:
        await callback.answer("Amal topilmadi.", show_alert=True)


async def admin_cancel_cmd(message):
    if message.from_user.id not in ADMIN_USER_IDS:
        await message.answer("Bu bo'lim faqat administratorlar uchun.")
        return
    if ADMIN_PENDING_ACTIONS.pop(message.from_user.id, None) is None:
        await message.answer("Bekor qilinadigan admin amali yo'q.")
        return
    await message.answer("Admin amali bekor qilindi.")


async def menu_action(message):
    user_id = message.from_user.id
    record_user_activity(user_id)
    text = (message.text or "").strip()
    if text == "📱 Mini App":
        if not WEBAPP_URL:
            await message.answer("Mini App manzili sozlanmagan.")
            return
        await message.answer(
            "Mini App'ni oching:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Turkiston Daily'ni ochish", web_app=WebAppInfo(url=WEBAPP_URL))
            ]]),
        )
    elif text == "📋 Reja qo'shish":
        PENDING_ACTIONS[user_id] = "plan"
        await message.answer("Reja matnini yuboring. Vaqt bilan: 18:30 Kitob o'qish")
    elif text in {"💰 Kirim", "💸 Chiqim"}:
        PENDING_ACTIONS[user_id] = "income" if text == "💰 Kirim" else "expense"
        await message.answer("Summa va kategoriya yuboring. Masalan: 25000 Oziq-ovqat tushlik")
    elif text == "📅 Bugun":
        await today_cmd(message)
    elif text == "📊 Hisobot":
        await message.answer(
            "Qaysi davr hisoboti kerak?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Kunlik", callback_data="report:day"),
                InlineKeyboardButton(text="Haftalik", callback_data="report:week"),
                InlineKeyboardButton(text="Oylik", callback_data="report:month"),
            ]]),
        )
    elif text == "🛠 Admin panel":
        await admin_cmd(message)


async def capture_text_input(message):
    user_id = message.from_user.id
    admin_pending = ADMIN_PENDING_ACTIONS.get(user_id)
    if admin_pending == "user_lookup":
        if user_id not in ADMIN_USER_IDS:
            ADMIN_PENDING_ACTIONS.pop(user_id, None)
            await message.answer("Bu bo'lim faqat administratorlar uchun.")
            return
        text = (message.text or "").strip()
        if not text.isdigit():
            await message.answer("Telegram ID faqat raqamlardan iborat bo'lishi kerak. Qaytadan kiriting yoki /cancel yozing.")
            return
        ADMIN_PENDING_ACTIONS.pop(user_id, None)
        await admin_show_user(message, int(text))
        return
    if admin_pending == "broadcast":
        if user_id not in ADMIN_USER_IDS:
            ADMIN_PENDING_ACTIONS.pop(user_id, None)
            await message.answer("Bu bo'lim faqat administratorlar uchun.")
            return
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            await message.answer("E'lon matnini yozing yoki /cancel bilan bekor qiling.")
            return
        ADMIN_PENDING_ACTIONS.pop(user_id, None)
        await prepare_admin_broadcast(message, text)
        return
    pending = PENDING_ACTIONS.get(user_id)
    action = pending.get("action") if isinstance(pending, dict) else pending
    if not action:
        await menu_action(message)
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        await message.answer("Iltimos, kerakli ma'lumotni matn ko'rinishida yuboring.")
        return
    if action == "missed_reason":
        task_id = pending["task_id"]
        PENDING_ACTIONS.pop(user_id, None)
        await update_task(task_id, TaskUpdate(missed_reason=text), user_id)
        await message.answer("Sabab va kechikish vaqti saqlandi.")
        return
    PENDING_ACTIONS.pop(user_id, None)
    if action == "plan":
        parts = text.split(maxsplit=1)
        scheduled_time = None
        title = text
        if len(parts) == 2:
            try:
                scheduled_time = datetime.strptime(parts[0], "%H:%M").strftime("%H:%M")
                title = parts[1].strip()
            except ValueError:
                pass
        if not title:
            await message.answer("Reja nomini kiriting.")
            return
        remind_at = f"{today_text()}T{scheduled_time}" if scheduled_time else None
        with database() as connection:
            connection.execute(
                "INSERT INTO tasks (user_id, title, date, scheduled_time, remind_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (user_id, title, today_text(), scheduled_time, remind_at),
            )
        await message.answer(f"Reja saqlandi: {title}")
        return
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Summa va kategoriya kiriting. Masalan: 25000 Oziq-ovqat")
        return
    try:
        amount = float(parts[0].replace(",", "."))
    except ValueError:
        amount = 0
    if not math.isfinite(amount) or amount <= 0 or amount > 1_000_000_000_000:
        await message.answer("Summani musbat son ko'rinishida yuboring.")
        return
    with database() as connection:
        connection.execute(
            "INSERT INTO transactions (user_id, type, category, amount, date, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (user_id, action, parts[1].strip(), amount, today_text(), parts[2] if len(parts) > 2 else ""),
        )
    await message.answer(f"{'Kirim' if action == 'income' else 'Chiqim'} saqlandi.")


async def task_result_callback(callback):
    record_user_activity(callback.from_user.id)
    try:
        action, _, task_id_text = (callback.data or "").partition(":")
        task_id = int(task_id_text)
    except ValueError:
        await callback.answer("Reja aniqlanmadi", show_alert=True)
        return
    with database() as connection:
        task = connection.execute(
            "SELECT id, user_id, title FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, callback.from_user.id),
        ).fetchone()
    if not task:
        await callback.answer("Reja topilmadi", show_alert=True)
        return
    if action == "done":
        await update_task(task_id, TaskUpdate(is_completed=True), callback.from_user.id)
        await callback.answer("Reja bajarildi")
        await callback.message.answer(f"✅ «{task['title']}» bajarildi.")
    elif action == "missed":
        PENDING_ACTIONS[callback.from_user.id] = {"action": "missed_reason", "task_id": task_id}
        await callback.answer()
        await callback.message.answer("Nima sababdan reja bajarilmadi? Sababni yozing.")
    else:
        await callback.answer("Amal aniqlanmadi", show_alert=True)


async def configure_bot_profile():
    if not bot:
        return

    profile_updates = (
        ("nom", bot.set_my_name(name="Turkiston Daily | Reja va Moliya")),
        (
            "qisqa tavsif",
            bot.set_my_short_description(
                short_description=(
                    "Kunlik reja va eslatmalar, kirim-chiqim, budjet, jamg'arma va hisobotlar. "
                    "Admin: @yodgorov_life"
                )
            ),
        ),
        (
            "tavsif",
            bot.set_my_description(
                description=(
                    "📋 Rejalaringizni tuzing, vaqtini belgilang va eslatma oling.\n"
                    "💰 Kirim-chiqim, oylik budjet va jamg'armalarni yuriting.\n"
                    "📊 Kunlik, haftalik va oylik hisobotlarni ko'ring, Excel hisobotini yuklab oling.\n"
                    "🚀 Boshlash uchun /start yoki pastdagi Mini App tugmasini bosing.\n"
                    "✉️ Admin: @yodgorov_life"
                )
            ),
        ),
        (
            "buyruqlar",
            bot.set_my_commands(commands=[
                BotCommand(command="start", description="Kunlik reja va moliya ilovasini ochish"),
                BotCommand(command="bugun", description="Bugungi reja va balansni ko'rish"),
                BotCommand(command="reja", description="Reja qo'shish va eslatma belgilash"),
                BotCommand(command="kirim", description="Kirim yozuvini qo'shish"),
                BotCommand(command="chiqim", description="Chiqim yozuvini qo'shish"),
                BotCommand(command="hisobot", description="Kunlik, haftalik yoki oylik hisobot"),
                BotCommand(command="excel", description="Excel hisobotini olish"),
                BotCommand(command="buyruqlar", description="Buyruqlar ro'yxatini ko'rish"),
            ]),
        ),
    )
    for label, update in profile_updates:
        try:
            await update
        except Exception:
            logging.exception("Telegram bot profilining %s qismini yangilab bo'lmadi", label)

    for admin_id in ADMIN_USER_IDS:
        try:
            await bot.set_my_commands(
                commands=[
                    BotCommand(command="admin", description="Admin statistik paneli"),
                    BotCommand(command="stat", description="Batafsil bot statistikasi"),
                    BotCommand(command="users", description="So'nggi foydalanuvchilar"),
                    BotCommand(command="user", description="Foydalanuvchi faolligi: /user ID"),
                    BotCommand(command="health", description="Bot, baza va Mini App holati"),
                    BotCommand(command="broadcast", description="Foydalanuvchilarga e'lon yuborish"),
                    BotCommand(command="cancel", description="Joriy admin amalini bekor qilish"),
                    BotCommand(command="start", description="Asosiy menyuni ochish"),
                    BotCommand(command="bugun", description="Bugungi reja va balans"),
                    BotCommand(command="reja", description="Reja qo'shish"),
                    BotCommand(command="kirim", description="Kirim yozuvi"),
                    BotCommand(command="chiqim", description="Chiqim yozuvi"),
                    BotCommand(command="hisobot", description="Kunlik, haftalik yoki oylik hisobot"),
                    BotCommand(command="excel", description="Excel hisobotini olish"),
                    BotCommand(command="buyruqlar", description="Buyruqlar ro'yxati"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            logging.exception("Admin uchun Telegram buyruqlarini sozlab bo'lmadi")

    avatar_path = Path(__file__).with_name("assets") / "bot-avatar.png"
    if avatar_path.is_file():
        try:
            await bot.set_my_profile_photo(
                photo=InputProfilePhotoStatic(photo=FSInputFile(avatar_path))
            )
        except Exception:
            logging.exception("Telegram bot profil rasmini yangilab bo'lmadi")

    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Turkiston Daily",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            )
        except Exception:
            logging.exception("Telegram botning asosiy Mini App menyusini yangilab bo'lmadi")


def build_reminder_message(task):
    reminder_time = task["remind_at"][11:16]
    if task["priority"] == "high":
        prompt = "Siz bu rejani muhim deb belgilagansiz. Avvaliga atigi 5 daqiqalik qadamni bajaring — boshlashning o‘zi oldinga siljish."
    else:
        prompt = "Hozir 5 daqiqalik birinchi qadamni tanlang. Davom ettirish keyin osonlashadi; hozir qulay bo‘lmasa, reja vaqtini o‘zgartirishingiz mumkin."
    previous_late = task["previous_late_minutes"]
    previous_note = (
        f"\n\n⏱ O'tgan safar bu rejadan {previous_late} daqiqa kech qolgansiz. "
        "Bu safar ertaroq boshlashga harakat qiling."
        if previous_late
        else ""
    )
    interval_note = (
        "\n\nEslatma har soatda takrorlanadi — rejani bajarganingizda belgilang."
        if task["reminder_interval"] == "hourly"
        else ""
    )
    return f"🔔 Rejangiz vaqti — {reminder_time}\n\n📌 {task['title']}\n\n{prompt}{previous_note}{interval_note}"


def reminder_markup(task_id):
    rows = [[
        InlineKeyboardButton(text="✅ Bajarildi", callback_data=f"done:{task_id}"),
        InlineKeyboardButton(text="⏳ Bajarilmadi", callback_data=f"missed:{task_id}"),
    ]]
    if WEBAPP_URL:
        rows.append([InlineKeyboardButton(
            text="Rejani Mini App’da ochish",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_reply_keyboard(user_id):
    rows = [
        [KeyboardButton(text="📱 Mini App"), KeyboardButton(text="📊 Hisobot")],
        [KeyboardButton(text="📋 Reja qo'shish"), KeyboardButton(text="📅 Bugun")],
        [KeyboardButton(text="💰 Kirim"), KeyboardButton(text="💸 Chiqim")],
    ]
    if user_id in ADMIN_USER_IDS or user_id == 5144043830 or user_id == DEV_USER_ID:
        rows.append([KeyboardButton(text="🛠 Admin panel")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Bo'limni tanlang")


if dp:
    dp.message(CommandStart())(start_cmd)
    dp.message(Command("reja"))(add_plan_cmd)
    dp.message(Command("bugun"))(today_cmd)
    dp.message(Command("kirim", "daromad"))(income_cmd)
    dp.message(Command("chiqim"))(expense_cmd)
    dp.message(Command("hisobot"))(report_cmd)
    dp.message(Command("excel"))(excel_cmd)
    dp.message(Command("buyruqlar"))(help_cmd)
    dp.message(Command("admin"))(admin_cmd)
    dp.message(Command("stat"))(admin_stats)
    dp.message(Command("users"))(admin_users_cmd)
    dp.message(Command("user"))(admin_user_cmd)
    dp.message(Command("health"))(admin_health)
    dp.message(Command("broadcast"))(admin_broadcast_cmd)
    dp.message(Command("cancel"))(admin_cancel_cmd)
    dp.callback_query(F.data.startswith("admin:"))(admin_callback)
    dp.callback_query(F.data.startswith("excel:"))(excel_callback)
    dp.callback_query(F.data.startswith("report:"))(report_callback)
    dp.callback_query(F.data.startswith("done:") | F.data.startswith("missed:"))(task_result_callback)
    dp.message(F.text)(capture_text_input)


async def send_reminders():
    while True:
        now = datetime.now(LOCAL_TZ)
        current_time = now.strftime("%Y-%m-%dT%H:%M")
        hourly_cutoff = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        with database() as connection:
            due_tasks = connection.execute(
                "SELECT t.*, (SELECT p.late_minutes FROM tasks p WHERE p.user_id = t.user_id "
                "AND p.title = t.title AND p.id != t.id AND p.missed_reason IS NOT NULL "
                "AND p.late_minutes > 0 ORDER BY p.date DESC, p.id DESC LIMIT 1) previous_late_minutes "
                "FROM tasks t WHERE t.is_completed = 0 AND t.remind_at IS NOT NULL AND t.remind_at <= ? "
                "AND ((t.reminder_interval = 'once' AND "
                "(t.reminder_sent_for IS NULL OR t.reminder_sent_for != t.remind_at)) "
                "OR (t.reminder_interval = 'hourly' AND "
                "(t.last_reminder_sent_at IS NULL OR t.last_reminder_sent_at <= ?)))",
                (current_time, hourly_cutoff),
            ).fetchall()
        for task in due_tasks:
            msg_text = build_reminder_message(task)
            markup = reminder_markup(task["id"])
            sent_ok = False

            # O'zbek tilidagi ravon ovozli eslatma yaratish va yuborish
            spoken_prompt = f"Vaqt keldi! Rejangiz: {task['title']}. Hoziroq birinchi qadamni boshlang!"
            try:
                voice_bytes = await generate_uzbek_tts(spoken_prompt, "uz-UZ-MadinaNeural")
                if voice_bytes:
                    voice_file = BufferedInputFile(voice_bytes, filename=f"reminder_{task['id']}.mp3")
                    await bot.send_voice(
                        task["user_id"],
                        voice=voice_file,
                        caption=msg_text,
                        reply_markup=markup,
                    )
                    sent_ok = True
            except Exception:
                logging.exception("Ovozli eslatma yuborishda xatolik yuz berdi, matnli eslatmaga o'tiladi")

            if not sent_ok:
                try:
                    await bot.send_message(
                        task["user_id"],
                        msg_text,
                        reply_markup=markup,
                    )
                    sent_ok = True
                except Exception:
                    logging.exception("Eslatmani Telegram foydalanuvchisiga yuborib bo'lmadi")
                    continue
            with database() as connection:
                connection.execute(
                    "UPDATE tasks SET reminder_sent_for = remind_at, last_reminder_sent_at = ? "
                    "WHERE id = ? AND remind_at = ?",
                    (current_time, task["id"], task["remind_at"]),
                )

        # Smart Avtomatik Eslatmalar (Moliya va Kun sarhisobi)
        current_hour = now.hour
        today_str = now.date().isoformat()

        # 1. Kunlik moliya eslatmasi (soat 20:00 atrofida)
        if current_hour == 20:
            with database() as connection:
                users_to_remind = connection.execute(
                    "SELECT user_id FROM users WHERE finance_reminder_enabled = 1 "
                    "AND (last_finance_reminder_date IS NULL OR last_finance_reminder_date != ?)",
                    (today_str,)
                ).fetchall()
                for u_row in users_to_remind:
                    u_id = u_row["user_id"]
                    tx_cnt = connection.execute(
                        "SELECT COUNT(*) FROM transactions WHERE user_id = ? AND date = ?",
                        (u_id, today_str)
                    ).fetchone()[0]

                    if tx_cnt == 0:
                        fin_msg = "💰 Moliyaviy eslatma\n\nBugun hali birorta xarajat yoki kirim kiritmadingiz. Kunlik hisob-kitobingizni yangilab qo'ying!"
                        if bot:
                            try:
                                await bot.send_message(u_id, fin_msg)
                            except Exception:
                                pass
                    connection.execute(
                        "UPDATE users SET last_finance_reminder_date = ? WHERE user_id = ?",
                        (today_str, u_id)
                    )

        # 2. Kun sarhisobi va ertangi kun rejasi (soat 21:00 atrofida)
        if current_hour == 21:
            with database() as connection:
                users_to_review = connection.execute(
                    "SELECT user_id FROM users WHERE evening_review_enabled = 1 "
                    "AND (last_evening_review_date IS NULL OR last_evening_review_date != ?)",
                    (today_str,)
                ).fetchall()
                for u_row in users_to_review:
                    u_id = u_row["user_id"]
                    rev_msg = "📋 Kun sarhisobi va ertangi reja\n\nBugungi kuningizni sarhisob qilish hamda ertangi kun rejalarini belgilash vaqti bo'ldi! ✨"
                    if bot:
                        try:
                            await bot.send_message(u_id, rev_msg)
                        except Exception:
                            pass
                    connection.execute(
                        "UPDATE users SET last_evening_review_date = ? WHERE user_id = ?",
                        (today_str, u_id)
                    )

        await asyncio.sleep(30)


async def start_services():
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_level="info")
    server = uvicorn.Server(config)
    if not bot:
        await server.serve()
        return
    await configure_bot_profile()
    reminder_task = asyncio.create_task(send_reminders())
    try:
        await asyncio.gather(server.serve(), dp.start_polling(bot))
    finally:
        reminder_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(start_services())