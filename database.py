import os
import sqlite3
from pathlib import Path


DB_NAME = os.getenv("DATABASE_PATH", str(Path(__file__).with_name("turkiston_daily.db")))


def get_connection():
    connection = sqlite3.connect(DB_NAME, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _add_missing_columns(connection, table, columns):
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    with get_connection() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                is_completed INTEGER NOT NULL DEFAULT 0,
                date TEXT NOT NULL DEFAULT CURRENT_DATE,
                priority TEXT NOT NULL DEFAULT 'normal',
                scheduled_time TEXT,
                remind_at TEXT,
                recurrence TEXT NOT NULL DEFAULT 'none',
                reminder_sent_for TEXT,
                reminder_interval TEXT NOT NULL DEFAULT 'once',
                last_reminder_sent_at TEXT,
                missed_reason TEXT,
                missed_at TEXT,
                late_minutes INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen_at TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK (type IN ('income', 'expense')),
                category TEXT NOT NULL,
                amount REAL NOT NULL CHECK (amount > 0),
                date TEXT NOT NULL DEFAULT CURRENT_DATE,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                score INTEGER NOT NULL DEFAULT 0,
                date TEXT NOT NULL DEFAULT CURRENT_DATE,
                tasks_total INTEGER NOT NULL DEFAULT 0,
                tasks_completed INTEGER NOT NULL DEFAULT 0,
                income REAL NOT NULL DEFAULT 0,
                expense REAL NOT NULL DEFAULT 0,
                UNIQUE(user_id, date)
            );

            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                monthly_limit REAL NOT NULL CHECK (monthly_limit > 0),
                month TEXT NOT NULL,
                UNIQUE(user_id, category, month)
            );

            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                target_amount REAL NOT NULL CHECK (target_amount > 0),
                current_amount REAL NOT NULL DEFAULT 0,
                deadline TEXT,
                is_completed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL DEFAULT '',
                auth TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_user_date ON tasks(user_id, date);
            CREATE INDEX IF NOT EXISTS idx_transactions_user_date ON transactions(user_id, date);
            CREATE INDEX IF NOT EXISTS idx_budgets_user_month ON budgets(user_id, month);
            CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id);
            """
        )

        connection.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (5144043830)")

        _add_missing_columns(
            connection,
            "users",
            {
                "phone": "TEXT DEFAULT ''",
                "full_name": "TEXT NOT NULL DEFAULT ''",
                "username": "TEXT DEFAULT ''",
                "bio": "TEXT DEFAULT ''",
                "avatar_url": "TEXT NOT NULL DEFAULT ''",
                "password_hash": "TEXT NOT NULL DEFAULT ''",
                "telegram_user_id": "INTEGER",
                "last_finance_reminder_date": "TEXT DEFAULT ''",
                "last_evening_review_date": "TEXT DEFAULT ''",
                "push_notifications_enabled": "INTEGER DEFAULT 1",
                "finance_reminder_enabled": "INTEGER DEFAULT 1",
                "evening_review_enabled": "INTEGER DEFAULT 1",
            },
        )
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone) WHERE phone != ''")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username) WHERE username != ''")
