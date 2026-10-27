import os
import sqlite3
from pathlib import Path


DB_NAME = os.getenv("DATABASE_PATH", str(Path(__file__).with_name("turkiston_daily.db")))


def get_connection():
    connection = sqlite3.connect(DB_NAME)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _add_missing_columns(connection, table, columns):
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db():
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                is_completed INTEGER NOT NULL DEFAULT 0,
                date TEXT NOT NULL DEFAULT CURRENT_DATE,
                priority TEXT NOT NULL DEFAULT 'normal',
                remind_at TEXT,
                recurrence TEXT NOT NULL DEFAULT 'none',
                reminder_sent_for TEXT,
                created_at TEXT NOT NULL DEFAULT ''
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

            CREATE INDEX IF NOT EXISTS idx_tasks_user_date ON tasks(user_id, date);
            CREATE INDEX IF NOT EXISTS idx_transactions_user_date ON transactions(user_id, date);
            CREATE INDEX IF NOT EXISTS idx_budgets_user_month ON budgets(user_id, month);
            """
        )

        _add_missing_columns(
            connection,
            "tasks",
            {
                "priority": "TEXT NOT NULL DEFAULT 'normal'",
                "remind_at": "TEXT",
                "recurrence": "TEXT NOT NULL DEFAULT 'none'",
                "reminder_sent_for": "TEXT",
                "created_at": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _add_missing_columns(
            connection,
            "transactions",
            {
                "note": "TEXT NOT NULL DEFAULT ''",
                "created_at": "TEXT NOT NULL DEFAULT ''",
            },
        )
        _add_missing_columns(
            connection,
            "reports",
            {
                "tasks_total": "INTEGER NOT NULL DEFAULT 0",
                "tasks_completed": "INTEGER NOT NULL DEFAULT 0",
                "income": "REAL NOT NULL DEFAULT 0",
                "expense": "REAL NOT NULL DEFAULT 0",
            },
        )
        connection.execute(
            "UPDATE tasks SET created_at = CURRENT_TIMESTAMP WHERE created_at = ''"
        )
        connection.execute(
            "UPDATE transactions SET created_at = CURRENT_TIMESTAMP WHERE created_at = ''"
        )
        connection.execute(
            "UPDATE goals SET created_at = CURRENT_TIMESTAMP WHERE created_at = ''"
        )