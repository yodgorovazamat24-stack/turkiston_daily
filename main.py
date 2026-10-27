import asyncio
import hashlib
import hmac
import json
import os
from io import BytesIO
from calendar import monthrange
from contextlib import contextmanager
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).with_name(".env"))

from database import get_connection, init_db
from excel_export import build_excel_report

try:
    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import Command, CommandStart
    from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
except ImportError:
    Bot = Dispatcher = F = Command = CommandStart = None
    BufferedInputFile = InlineKeyboardButton = InlineKeyboardMarkup = WebAppInfo = None


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()
DEV_USER_ID = int(os.getenv("DEV_USER_ID", "12345678"))
TELEGRAM_ENABLED = bool(BOT_TOKEN and BOT_TOKEN != "YOUR_BOT_TOKEN_HERE")
LOCAL_TZ = timezone(timedelta(hours=5))
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))

app = FastAPI(title="Turkiston Daily")
dp = Dispatcher() if Dispatcher else None
bot = Bot(token=BOT_TOKEN) if TELEGRAM_ENABLED and Bot else None
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


def today_text():
    return datetime.now(LOCAL_TZ).date().isoformat()


def checked_date(value):
    try:
        return Date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Sana YYYY-MM-DD formatida bo'lishi kerak")


def verify_telegram_user(init_data: str | None):
    if not TELEGRAM_ENABLED:
        return DEV_USER_ID
    if not init_data:
        raise HTTPException(status_code=401, detail="Telegram Mini App orqali kiring")

    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram ma'lumotlari tasdiqlanmadi")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Telegram ma'lumotlari tasdiqlanmadi")

    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Telegram foydalanuvchisi aniqlanmadi")

    if abs(datetime.now(timezone.utc).timestamp() - auth_date) > 86400:
        raise HTTPException(status_code=401, detail="Telegram sessiyasi eskirgan, ilovani qayta oching")
    return user_id


async def current_user(x_telegram_init_data: str | None = Header(default=None)):
    return verify_telegram_user(x_telegram_init_data)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    date: Date | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    remind_at: datetime | None = None
    recurrence: Literal["none", "daily", "weekly", "monthly"] = "none"


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    is_completed: bool | None = None
    priority: Literal["low", "normal", "high"] | None = None
    remind_at: datetime | None = None
    recurrence: Literal["none", "daily", "weekly", "monthly"] | None = None


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


class GoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    target_amount: float = Field(gt=0, le=1_000_000_000_000)
    current_amount: float = Field(default=0, ge=0)
    deadline: Date | None = None


class GoalUpdate(BaseModel):
    current_amount: float = Field(ge=0)


class ReportCreate(BaseModel):
    date: Date | None = None
    note: str = Field(default="", max_length=1000)
    score: int = Field(default=0, ge=0, le=5)


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html", {})


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


@app.post("/api/tasks")
async def add_task(task: TaskCreate, user_id: int = Depends(current_user)):
    task_date = (task.date or Date.fromisoformat(today_text())).isoformat()
    title = task.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Reja nomini kiriting")
    remind_at = task.remind_at
    if remind_at and remind_at.tzinfo is None:
        remind_at = remind_at.replace(tzinfo=LOCAL_TZ)
    with database() as connection:
        cursor = connection.execute(
            "INSERT INTO tasks (user_id, title, date, priority, remind_at, recurrence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (
                user_id,
                title,
                task_date,
                task.priority,
                remind_at.strftime("%Y-%m-%dT%H:%M") if remind_at else None,
                task.recurrence,
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
    invalid_nulls = [key for key, value in values.items() if value is None and key != "remind_at"]
    if invalid_nulls:
        raise HTTPException(status_code=422, detail="Reja maydonlari bo'sh bo'lishi mumkin emas")
    if not values:
        raise HTTPException(status_code=422, detail="Yangilash uchun ma'lumot yuboring")
    if "title" in values:
        values["title"] = values["title"].strip()
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
        assignments = ", ".join(f"{column} = ?" for column in values)
        connection.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ? AND user_id = ?",
            (*values.values(), task_id, user_id),
        )
        if values.get("is_completed") and task["recurrence"] != "none":
            next_date = next_recurrence(Date.fromisoformat(task["date"]), task["recurrence"])
            exists = connection.execute(
                "SELECT 1 FROM tasks WHERE user_id = ? AND title = ? AND date = ?",
                (user_id, task["title"], next_date.isoformat()),
            ).fetchone()
            if not exists:
                connection.execute(
                    "INSERT INTO tasks (user_id, title, date, priority, recurrence, created_at) "
                    "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (user_id, task["title"], next_date.isoformat(), task["priority"], task["recurrence"]),
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
    with database() as connection:
        goal = connection.execute(
            "SELECT target_amount FROM goals WHERE id = ? AND user_id = ?", (goal_id, user_id)
        ).fetchone()
        if not goal:
            raise HTTPException(status_code=404, detail="Maqsad topilmadi")
        cursor = connection.execute(
            "UPDATE goals SET current_amount = ?, is_completed = ? "
            "WHERE id = ? AND user_id = ?",
            (update.current_amount, int(update.current_amount >= goal["target_amount"]), goal_id, user_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Maqsad topilmadi")
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


@app.get("/api/export.xlsx")
async def export_excel(
    period: Literal["week", "month", "year", "all", "custom"] = "all",
    start_date: Date | None = None,
    end_date: Date | None = None,
    user_id: int = Depends(current_user),
):
    period_start, period_end, period_label, period_slug = resolve_report_period(
        period, start_date, end_date
    )
    report_data = collect_export_data(user_id)
    if period_start and period_end:
        report_data = filter_export_data(report_data, period_start, period_end)
    group_by_day = bool(period_start and period_end and (period_end - period_start).days < 31)
    workbook = build_excel_report(report_data, period_label, group_by_day)
    return Response(
        content=workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{excel_filename(period_slug)}"'},
    )


def resolve_report_period(period, start_date=None, end_date=None):
    today = Date.fromisoformat(today_text())
    if period == "week":
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
        if start <= budget_start and budget_end <= end:
            filtered_budgets.append(row)
    filtered["budgets"] = filtered_budgets
    return filtered


async def start_cmd(message):
    keyboard = []
    if WEBAPP_URL:
        keyboard.append([InlineKeyboardButton(text="Turkiston Daily'ni ochish", web_app=WebAppInfo(url=WEBAPP_URL))])
    else:
        await message.answer(
            "Mini-ilova hali ulanmagan. Telegram ichida ochish uchun .env faylidagi "
            "WEBAPP_URL qiymatiga ommaviy HTTPS manzilini kiriting."
        )
    keyboard.extend([
        [InlineKeyboardButton(text="Haftalik Excel hisoboti", callback_data="excel:week")],
        [InlineKeyboardButton(text="Oylik Excel hisoboti", callback_data="excel:month")],
        [InlineKeyboardButton(text="Yillik Excel hisoboti", callback_data="excel:year")],
        [InlineKeyboardButton(text="Barcha davr Excel hisoboti", callback_data="excel:all")],
    ])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    await message.answer(
        "Hisobot davrini tanlang yoki /excel week, /excel month, /excel year yoxud /excel all yuboring.",
        reply_markup=markup,
    )


async def send_excel(message, user_id, period="month"):
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


async def excel_callback(callback):
    await callback.answer("Excel hisoboti tayyorlanmoqda")
    period = (callback.data or "excel:month").partition(":")[2] or "month"
    await send_excel(callback.message, callback.from_user.id, period)


if dp:
    dp.message(CommandStart())(start_cmd)
    dp.message(Command("excel"))(excel_cmd)
    dp.callback_query(F.data.startswith("excel:"))(excel_callback)


async def send_reminders():
    while True:
        current_time = datetime.now(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")
        with database() as connection:
            due_tasks = connection.execute(
                "SELECT id, user_id, title, remind_at FROM tasks WHERE is_completed = 0 "
                "AND remind_at IS NOT NULL AND remind_at <= ? AND (reminder_sent_for IS NULL OR reminder_sent_for != remind_at)",
                (current_time,),
            ).fetchall()
            for task in due_tasks:
                connection.execute("UPDATE tasks SET reminder_sent_for = remind_at WHERE id = ?", (task["id"],))
        for task in due_tasks:
            try:
                await bot.send_message(task["user_id"], f"Eslatma: {task['title']}")
            except Exception:
                continue
        await asyncio.sleep(30)


async def start_services():
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_level="info")
    server = uvicorn.Server(config)
    if not bot:
        await server.serve()
        return
    reminder_task = asyncio.create_task(send_reminders())
    try:
        await asyncio.gather(server.serve(), dp.start_polling(bot))
    finally:
        reminder_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(start_services())