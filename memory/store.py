import sqlite3
import json
import os
from datetime import datetime
from core.config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "sessions.db")

def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT
            )
        """)

def save_setting(key: str, value: str):
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO sessions (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (key, value, datetime.now().isoformat()))

def load_setting(key: str, default=None) -> str:
    with _get_conn() as conn:
        row = conn.execute("SELECT value FROM sessions WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def save_history(history: list[dict]):
    with _get_conn() as conn:
        conn.execute("DELETE FROM messages")
        for msg in history:
            conn.execute(
                "INSERT INTO messages (role, content, created_at) VALUES (?,?,?)",
                (msg["role"], msg["content"], datetime.now().isoformat())
            )

def load_history() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT role, content FROM messages ORDER BY id ASC").fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

def clear_history():
    with _get_conn() as conn:
        conn.execute("DELETE FROM messages")

init_db()
