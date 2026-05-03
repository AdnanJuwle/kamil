import sqlite3
import os
from datetime import datetime
from core.config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "tasks.db")

def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'open',
                project TEXT,
                created_at TEXT,
                completed_at TEXT
            )
        """)

def create_task(title: str, description: str = "", project: str = "") -> dict:
    with _get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (title, description, project, status, created_at) VALUES (?,?,?,?,?)",
            (title, description, project, "open", datetime.now().isoformat())
        )
        return {"success": True, "id": cursor.lastrowid, "title": title}

def list_tasks(project: str = None, status: str = None) -> dict:
    query = "SELECT * FROM tasks WHERE 1=1"
    params = []
    if project:
        query += " AND project=?"
        params.append(project)
    if status:
        query += " AND status=?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return {"success": True, "tasks": [dict(r) for r in rows]}

def complete_task(task_id: int) -> dict:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
            (datetime.now().isoformat(), task_id)
        )
        return {"success": True, "id": task_id, "status": "done"}

def delete_task(task_id: int) -> dict:
    with _get_conn() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        return {"success": True, "id": task_id}

init_db()