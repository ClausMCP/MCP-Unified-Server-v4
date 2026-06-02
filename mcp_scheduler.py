#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Scheduler v1.1 – планировщик задач (cron/интервалы)
Исправления: 
- Устранён SyntaxError в register_tool (scheduler_enable)
- Добавлен graceful shutdown через atexit и сигналы
"""
import os
import sys
import json
import sqlite3
import threading
import time
import re
import atexit
import signal
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

# Попытка импортировать schedule для cron
try:
    import schedule
    HAS_SCHEDULE = True
except ImportError:
    HAS_SCHEDULE = False

from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Конфигурация ─────────────────────────────────────────────────────────
DB_PATH = os.environ.get("MCP_SCHEDULER_DB", os.path.join(os.path.dirname(__file__), "mcp_scheduler.db"))
CHECK_INTERVAL_SEC = 1   # интервал проверки pending задач (сек)
DEFAULT_INTERVAL_SEC = 3600

# ─── База данных заданий ─────────────────────────────────────────────────
class SchedulerDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    tool_name TEXT NOT NULL,
                    tool_args TEXT,
                    schedule_type TEXT CHECK(schedule_type IN ('interval', 'cron')) NOT NULL,
                    interval_seconds INTEGER,
                    cron_expression TEXT,
                    enabled INTEGER DEFAULT 1,
                    last_run TEXT,
                    next_run TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_enabled ON jobs(enabled)")
            conn.commit()

    def add_job(self, name: str, tool_name: str, tool_args: Dict,
                schedule_type: str, interval_seconds: int = None, cron_expr: str = None) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("""
                INSERT INTO jobs (name, tool_name, tool_args, schedule_type, interval_seconds, cron_expression)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (name, tool_name, json.dumps(tool_args, default=str), schedule_type, interval_seconds, cron_expr))
            conn.commit()
            return cur.lastrowid

    def get_enabled_jobs(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM jobs WHERE enabled = 1").fetchall()
            return [dict(row) for row in rows]

    def get_job_by_id(self, job_id: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def update_last_run(self, job_id: int, next_run_dt: datetime = None):
        with sqlite3.connect(self.db_path) as conn:
            now_iso = datetime.now().isoformat()
            next_iso = next_run_dt.isoformat() if next_run_dt else None
            conn.execute("""
                UPDATE jobs SET last_run = ?, next_run = ? WHERE id = ?
            """, (now_iso, next_iso, job_id))
            conn.commit()

    def delete_job(self, job_id: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()

    def list_jobs(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            return [dict(row) for row in rows]

    def enable_job(self, job_id: int, enabled: bool):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE jobs SET enabled = ? WHERE id = ?", (1 if enabled else 0, job_id))
            conn.commit()

# ─── Исполнитель задач ───────────────────────────────────────────────────
class JobExecutor:
    def __init__(self):
        self.db = SchedulerDB()
        self._tools_cache = {}

    def _load_tool(self, tool_name: str):
        if tool_name in self._tools_cache:
            return self._tools_cache[tool_name]
        tool_map = {
            "empty_trash": ("mcp_fs_trash", "empty_trash"),
            "sync_directories": ("mcp_fs_sync", "sync_directories"),
            "remind": ("mcp_calendar", "remind"),
            "batch_delete": ("mcp_fs_batch", "batch_delete"),
            "move_to_trash": ("mcp_fs_trash", "move_to_trash"),
            "archive_files": ("mcp_fs_archives", "archive_files"),
            "extract_archive": ("mcp_fs_archives", "extract_archive"),
            "sync_to_cloud": ("mcp_fs_cloud", "sync_to_cloud"),
            "sync_from_cloud": ("mcp_fs_cloud", "sync_from_cloud"),
            "backup_database": ("knowledge_base_server", "backup_database"),
        }
        if tool_name in tool_map:
            module_name, func_name = tool_map[tool_name]
            try:
                mod = __import__(module_name, fromlist=[func_name])
                func = getattr(mod, func_name)
                self._tools_cache[tool_name] = func
                return func
            except Exception as e:
                _log(f"[Scheduler] Failed to load tool {tool_name}: {e}")
                return None
        else:
            _log(f"[Scheduler] Tool {tool_name} not mapped.")
            return None

    def run_job(self, job: Dict) -> Dict:
        name = job["name"]
        tool_name = job["tool_name"]
        args = json.loads(job["tool_args"]) if job["tool_args"] else {}
        _log(f"[Scheduler] Executing job '{name}': {tool_name}({args})")
        try:
            func = self._load_tool(tool_name)
            if not func:
                raise ValueError(f"Tool '{tool_name}' not found or could not be loaded")
            result = func(**args)
            if not isinstance(result, dict):
                result = {"result": str(result)}
            _log(f"[Scheduler] Job '{name}' completed: {str(result)[:200]}")
            conversation_memory.add(
                op="scheduled_job",
                paths={"job": name, "tool": tool_name},
                status="success",
                dialog="scheduler",
                context=f"Scheduled job '{name}' executed, result: {result.get('status', 'ok')}"
            )
            return result
        except Exception as e:
            _log(f"[Scheduler] Job '{name}' failed: {e}")
            conversation_memory.add(
                op="scheduled_job",
                paths={"job": name, "tool": tool_name},
                status="error",
                dialog="scheduler",
                context=f"Job failed: {e}"
            )
            return {"error": str(e), "job": name}

# ─── Планировщик (фоновый поток) ────────────────────────────────────────
class SchedulerThread:
    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None
        self.executor = JobExecutor()
        self.db = SchedulerDB()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mcp_scheduler_loop")
        self._thread.start()
        _log("[Scheduler] Background thread started")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        _log("[Scheduler] Background thread stopped")

    def _run(self):
        self._reload_all_jobs()
        while not self._stop_event.is_set():
            try:
                if HAS_SCHEDULE:
                    schedule.run_pending()
                else:
                    self._check_pending_jobs()
                time.sleep(CHECK_INTERVAL_SEC)
            except Exception as e:
                _log(f"[Scheduler] Loop error: {e}")

    def _reload_all_jobs(self):
        if not HAS_SCHEDULE:
            return
        schedule.clear()
        jobs = self.db.get_enabled_jobs()
        for job in jobs:
            if job["schedule_type"] == "cron" and job["cron_expression"]:
                self._schedule_cron_job(job)
            elif job["schedule_type"] == "interval" and job["interval_seconds"]:
                self._schedule_interval_job(job)
            else:
                _log(f"[Scheduler] Job {job['name']} has invalid schedule config")

    def _schedule_cron_job(self, job: Dict):
        try:
            def job_wrapper():
                self.executor.run_job(job)
                self.db.update_last_run(job["id"], None)
            schedule.every(1).minutes.do(job_wrapper)
            _log(f"[Scheduler] Scheduled cron job '{job['name']}' (approx every minute)")
        except Exception as e:
            _log(f"[Scheduler] Failed to schedule cron job {job['name']}: {e}")

    def _schedule_interval_job(self, job: Dict):
        interval = job["interval_seconds"]
        if not isinstance(interval, int) or interval <= 0:
            return
        def job_wrapper():
            self.executor.run_job(job)
            self.db.update_last_run(job["id"], None)
        schedule.every(interval).seconds.do(job_wrapper)
        _log(f"[Scheduler] Scheduled interval job '{job['name']}' every {interval}s")

    def _check_pending_jobs(self):
        jobs = self.db.get_enabled_jobs()
        now = datetime.now()
        for job in jobs:
            next_run_str = job.get("next_run")
            if next_run_str:
                try:
                    next_run = datetime.fromisoformat(next_run_str)
                except Exception:
                    next_run = None
            else:
                next_run = None

            if next_run is None or now >= next_run:
                self.executor.run_job(job)
                if job["schedule_type"] == "interval" and job["interval_seconds"]:
                    next_run_dt = now + timedelta(seconds=job["interval_seconds"])
                elif job["schedule_type"] == "cron" and job["cron_expression"]:
                    next_run_dt = now + timedelta(minutes=1)
                else:
                    next_run_dt = None
                self.db.update_last_run(job["id"], next_run_dt)

# ─── Глобальный экземпляр планировщика ──────────────────────────────────
_scheduler = SchedulerThread()

def scheduler_start() -> Dict:
    _scheduler.start()
    return {"status": "started"}

def scheduler_stop() -> Dict:
    _scheduler.stop()
    return {"status": "stopped"}

def scheduler_add_interval(name: str, tool_name: str, interval_seconds: int,
                           args: Dict = None) -> Dict:
    args = args or {}
    db = SchedulerDB()
    job_id = db.add_job(name, tool_name, args, "interval", interval_seconds=interval_seconds)
    _scheduler._reload_all_jobs()
    return {"status": "created", "job_id": job_id, "name": name}

def scheduler_add_cron(name: str, tool_name: str, cron_expression: str,
                       args: Dict = None) -> Dict:
    args = args or {}
    db = SchedulerDB()
    job_id = db.add_job(name, tool_name, args, "cron", cron_expr=cron_expression)
    _scheduler._reload_all_jobs()
    return {"status": "created", "job_id": job_id, "name": name}

def scheduler_list() -> Dict:
    db = SchedulerDB()
    jobs = db.list_jobs()
    return {"jobs": jobs, "count": len(jobs)}

def scheduler_delete(job_id: int) -> Dict:
    db = SchedulerDB()
    db.delete_job(job_id)
    _scheduler._reload_all_jobs()
    return {"status": "deleted", "job_id": job_id}

def scheduler_enable(job_id: int, enabled: bool) -> Dict:
    db = SchedulerDB()
    db.enable_job(job_id, enabled)
    _scheduler._reload_all_jobs()
    return {"status": "updated", "job_id": job_id, "enabled": enabled}

# ─── Graceful Shutdown ───────────────────────────────────────────────────
def _shutdown_scheduler():
    try:
        scheduler_stop()
    except Exception as e:
        _log(f"[Scheduler] Shutdown error: {e}")

atexit.register(_shutdown_scheduler)
try:
    signal.signal(signal.SIGINT, lambda s, f: _shutdown_scheduler())
    signal.signal(signal.SIGTERM, lambda s, f: _shutdown_scheduler())
except Exception:
    pass  # В некоторых окружениях (например, Docker) signal может быть недоступен

# ─── Регистрация инструментов MCP ───────────────────────────────────────
def register_tools(server: BaseMCPServer):
    server.register_tool("scheduler_start", {
        "description": "Запустить фоновый планировщик задач",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: scheduler_start())

    server.register_tool("scheduler_stop", {
        "description": "Остановить планировщик задач",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: scheduler_stop())

    server.register_tool("scheduler_add_interval", {
        "description": "Добавить задание с интервалом в секундах",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "tool_name": {"type": "string"},
                "interval_seconds": {"type": "integer"},
                "args": {"type": "object"}
            },
            "required": ["name", "tool_name", "interval_seconds"]
        }
    }, lambda **kw: scheduler_add_interval(
        kw["name"], kw["tool_name"], kw["interval_seconds"], kw.get("args", {})
    ))

    server.register_tool("scheduler_add_cron", {
        "description": "Добавить задание с cron-выражением",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "tool_name": {"type": "string"},
                "cron_expression": {"type": "string"},
                "args": {"type": "object"}
            },
            "required": ["name", "tool_name", "cron_expression"]
        }
    }, lambda **kw: scheduler_add_cron(
        kw["name"], kw["tool_name"], kw["cron_expression"], kw.get("args", {})
    ))

    server.register_tool("scheduler_list", {
        "description": "Список всех заданий",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: scheduler_list())

    server.register_tool("scheduler_delete", {
        "description": "Удалить задание по ID",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "integer"}},
            "required": ["job_id"]
        }
    }, lambda **kw: scheduler_delete(kw["job_id"]))

    # 🔒 ИСПРАВЛЕНИЕ: восстановлена пропущенная `}` в inputSchema
    server.register_tool("scheduler_enable", {
        "description": "Включить или выключить задание",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "integer"},
                "enabled": {"type": "boolean", "default": True}
            },
            "required": ["job_id"]
        }
    }, lambda **kw: scheduler_enable(kw["job_id"], kw.get("enabled", True)))

__mcp_plugin__ = {
    "name": "scheduler",
    "version": "1.1",
    "description": "Планировщик задач (интервалы и cron)",
    "dependencies": ["schedule"],
    "on_load": lambda: _log("[Scheduler] Plugin loaded."),
    "on_unload": lambda: scheduler_stop()
}

if __name__ == "__main__":
    server = BaseMCPServer("scheduler", "1.1")
    register_tools(server)
    server.run()