#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Task Manager v1.0 – асинхронное выполнение долгих операций
с возможностью паузы, возобновления, отмены и сохранения прогресса.
Поддерживает восстановление задач после перезапуска сервера.
"""
import os
import sys
import json
import time
import sqlite3
import threading
import traceback
import importlib
from datetime import datetime
from typing import Dict, Any, Optional, Callable, List
from pathlib import Path
from mcp_shared import _log, BaseMCPServer, conversation_memory, dialog_ctx, send_progress, is_verbose

# ─── Конфигурация ──────────────────────────────────────────────────────────
TASK_DB_PATH = os.environ.get("MCP_TASK_DB", os.path.join(os.path.dirname(__file__), "mcp_tasks.db"))
TASK_CLEANUP_SECONDS = int(os.environ.get("MCP_TASK_CLEANUP_SEC", "86400"))  # удалять завершённые задачи через 24 часа
MAX_CONCURRENT_TASKS = int(os.environ.get("MCP_MAX_CONCURRENT_TASKS", "4"))

# ─── База данных задач ────────────────────────────────────────────────────
class TaskDB:
    def __init__(self, db_path: str = TASK_DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress_json TEXT,
                    result_chunks_json TEXT,
                    resume_data_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    dialog_id TEXT,
                    user_id TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON tasks(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_updated ON tasks(updated_at)")
            conn.commit()

    def create_task(self, task_id: str, tool_name: str, args: Dict,
                    dialog_id: str, user_id: str = "default") -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO tasks (task_id, tool_name, args_json, status, progress_json,
                                   result_chunks_json, resume_data_json, created_at, updated_at,
                                   dialog_id, user_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task_id, tool_name, json.dumps(args, default=str), "pending",
                "{}", "[]", "{}", time.time(), time.time(), dialog_id, user_id
            ))
            conn.commit()

    def update_task(self, task_id: str, **fields):
        set_clause = ", ".join(f"{k} = ?" for k in fields.keys())
        values = list(fields.values()) + [task_id]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"UPDATE tasks SET {set_clause}, updated_at = ? WHERE task_id = ?",
                         values + [time.time(), task_id])
            conn.commit()

    def get_task(self, task_id: str) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row:
                return dict(row)
        return None

    def delete_old_tasks(self, older_than_seconds: int = TASK_CLEANUP_SECONDS):
        cutoff = time.time() - older_than_seconds
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM tasks WHERE status IN ('completed', 'cancelled', 'failed') AND updated_at < ?", (cutoff,))
            conn.commit()

    def list_tasks(self, status: Optional[str] = None, limit: int = 50) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if status:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

# ─── Исполнитель задач (фоновые потоки) ───────────────────────────────────
class TaskExecutor:
    def __init__(self):
        self.db = TaskDB()
        self._running_tasks: Dict[str, threading.Thread] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._shutdown = False
        self._cleanup_thread = None
        self._start_cleanup()

    def _start_cleanup(self):
        def cleanup_loop():
            while not self._shutdown:
                time.sleep(3600)
                try:
                    self.db.delete_old_tasks()
                except Exception as e:
                    _log(f"Task cleanup error: {e}")
        self._cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True, name="task_cleanup")
        self._cleanup_thread.start()

    def submit(self, task_id: str, tool_name: str, args: Dict,
               func: Callable, dialog_id: str) -> bool:
        """Запускает задачу в фоновом потоке."""
        with self._lock:
            if len(self._running_tasks) >= MAX_CONCURRENT_TASKS:
                return False
            stop_event = threading.Event()
            self._stop_events[task_id] = stop_event
            thread = threading.Thread(
                target=self._run_task,
                args=(task_id, tool_name, args, func, dialog_id, stop_event),
                daemon=True,
                name=f"task_{task_id}"
            )
            self._running_tasks[task_id] = thread
            thread.start()
            return True

    def _run_task(self, task_id: str, tool_name: str, args: Dict,
                  func: Callable, dialog_id: str, stop_event: threading.Event):
        try:
            self.db.update_task(task_id, status="running", progress_json=json.dumps({"stage": "starting"}))
            progress = TaskProgress(task_id, self.db, stop_event)
            result = func(**args, _task_progress=progress, _task_id=task_id, _dialog_id=dialog_id)
            if result is not None:
                self.db.update_task(
                    task_id,
                    status="completed",
                    result_chunks_json=json.dumps([{"index": 0, "data": result}])
                )
            else:
                self.db.update_task(task_id, status="completed")
        except Exception as e:
            _log(f"Task {task_id} failed: {e}\n{traceback.format_exc()}")
            # Сохраняем последний прогресс (если есть) в resume_data перед фиксацией ошибки
            last_progress = {}
            try:
                task = self.db.get_task(task_id)
                if task and task["progress_json"]:
                    last_progress = json.loads(task["progress_json"])
            except Exception:
                pass
            self.db.update_task(
                task_id,
                status="failed",
                progress_json=json.dumps({"error": str(e), "traceback": traceback.format_exc()}),
                resume_data_json=json.dumps({"last_progress": last_progress, "error": str(e)})
            )
        finally:
            with self._lock:
                self._running_tasks.pop(task_id, None)
                self._stop_events.pop(task_id, None)

    def pause(self, task_id: str) -> bool:
        with self._lock:
            stop = self._stop_events.get(task_id)
            if stop:
                stop.set()
                self.db.update_task(task_id, status="paused")
                return True
        return False

    def _load_tool(self, tool_name: str):
        """Динамическая загрузка функции-исполнителя инструмента по его имени."""
        mapping = {
            "search_files": ("mcp_fs_search", "search_files"),
            "batch_move_files": ("mcp_fs_advanced", "batch_move_files"),
            "extract_text_from_file": ("mcp_fs_advanced", "extract_text_from_file"),
            "index_all_files_content": ("mcp_fs_advanced", "index_all_files_content"),
            # добавить другие инструменты по необходимости
        }
        if tool_name in mapping:
            mod_name, func_name = mapping[tool_name]
            try:
                mod = importlib.import_module(mod_name)
                return getattr(mod, func_name, None)
            except ImportError:
                return None
        return None

    def resume(self, task_id: str) -> bool:
        """Возобновляет задачу, загружая её параметры и функцию из БД."""
        task = self.db.get_task(task_id)
        if not task or task["status"] not in ("paused", "failed"):
            return False
        
        tool_name = task["tool_name"]
        args = json.loads(task["args_json"])
        resume_data = json.loads(task["resume_data_json"]) if task["resume_data_json"] else {}
        args["_resume_data"] = resume_data
        
        func = self._load_tool(tool_name)
        if not func:
            _log(f"Task resume failed: tool {tool_name} not found")
            return False
            
        return self.submit(task_id, tool_name, args, func, task["dialog_id"])

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            stop = self._stop_events.get(task_id)
            if stop:
                stop.set()
                self.db.update_task(task_id, status="cancelled")
                return True
        # Если задача не running, просто меняем статус
        task = self.db.get_task(task_id)
        if task and task["status"] not in ("completed", "cancelled", "failed"):
            self.db.update_task(task_id, status="cancelled")
            return True
        return False

    def get_status(self, task_id: str) -> Dict:
        task = self.db.get_task(task_id)
        if not task:
            return {"error": "Task not found"}
        return {
            "task_id": task["task_id"],
            "status": task["status"],
            "progress": json.loads(task["progress_json"]) if task["progress_json"] else {},
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
            "has_chunks": bool(json.loads(task["result_chunks_json"] or "[]")),
            "resume_available": bool(task["resume_data_json"] and task["resume_data_json"] != "{}")
        }

    def get_chunk(self, task_id: str, chunk_index: int) -> Optional[Dict]:
        task = self.db.get_task(task_id)
        if not task:
            return None
        chunks = json.loads(task["result_chunks_json"] or "[]")
        for ch in chunks:
            if ch.get("index") == chunk_index:
                return ch.get("data")
        return None

    def add_chunk(self, task_id: str, chunk_index: int, data: Any):
        task = self.db.get_task(task_id)
        if not task:
            return
        chunks = json.loads(task["result_chunks_json"] or "[]")
        # Проверяем, нет ли уже такого индекса
        for ch in chunks:
            if ch.get("index") == chunk_index:
                return
        chunks.append({"index": chunk_index, "data": data})
        self.db.update_task(task_id, result_chunks_json=json.dumps(chunks))

    def save_resume_data(self, task_id: str, resume_data: Dict):
        self.db.update_task(task_id, resume_data_json=json.dumps(resume_data))

    def update_progress(self, task_id: str, stage: str, current: int = None, total: int = None, message: str = None):
        progress = {"stage": stage}
        if current is not None: progress["current"] = current
        if total is not None: progress["total"] = total
        if message: progress["message"] = message
        self.db.update_task(task_id, progress_json=json.dumps(progress))

    def restore_running_tasks(self):
        """При старте сервера – задачи со статусом running переводим в paused."""
        tasks = self.db.list_tasks(status="running")
        for task in tasks:
            self.db.update_task(task["task_id"], status="paused")
            _log(f"Task {task['task_id']} restored to paused")

# ─── Объект прогресса, передаваемый в инструменты ─────────────────────────
class TaskProgress:
    def __init__(self, task_id: str, db: TaskDB, stop_event: threading.Event):
        self.task_id = task_id
        self.db = db
        self.stop_event = stop_event
        self._last_progress_time = 0
        self._progress_interval = 2.0  # не чаще раза в 2 секунды

    def should_stop(self) -> bool:
        return self.stop_event.is_set()

    def update_progress(self, stage: str, current: int = None, total: int = None, message: str = None):
        now = time.time()
        if now - self._last_progress_time < self._progress_interval and message is None:
            return
        self._last_progress_time = now
        prog_dict = {"stage": stage}
        if current is not None: prog_dict["current"] = current
        if total is not None: prog_dict["total"] = total
        if message: prog_dict["message"] = message
        self.db.update_task(self.task_id, progress_json=json.dumps(prog_dict))

        # Отправляем уведомление в чат (если verbose включён)
        task = self.db.get_task(self.task_id)
        if task and task.get("dialog_id"):
            dialog_id = task["dialog_id"]
            if is_verbose(dialog_id):
                msg = message or f"{stage}"
                if current is not None and total is not None:
                    percent = int(current / total * 100) if total > 0 else 0
                    msg = f"{stage}: {current}/{total} ({percent}%)"
                send_progress(dialog_id, f"[Task {self.task_id[:8]}] {msg}")

    def add_chunk(self, chunk_index: int, data: Any):
        executor.add_chunk(self.task_id, chunk_index, data)

    def save_resume_data(self, data: Dict):
        executor.save_resume_data(self.task_id, data)

    def log(self, message: str, level: str = "info"):
        """Отправляет сообщение в лог задачи (можно потом показать пользователю)"""
        self.update_progress("log", message=message)

# ─── Глобальный экземпляр исполнителя ─────────────────────────────────────
executor = TaskExecutor()

# ─── Восстановление задач при старте модуля ───────────────────────────────
executor.restore_running_tasks()

# ─── Инструменты MCP для управления задачами ──────────────────────────────
def submit_task(tool_name: str, args: Dict, dialog_id: str = None) -> Dict:
    """Создаёт новую асинхронную задачу. Возвращает task_id."""
    if dialog_id is None:
        dialog_id = dialog_ctx.get()
    import uuid
    task_id = f"{tool_name}_{uuid.uuid4().hex[:8]}_{int(time.time())}"
    executor.db.create_task(task_id, tool_name, args, dialog_id)
    return {"status": "submitted", "task_id": task_id}

def task_status(task_id: str) -> Dict:
    return executor.get_status(task_id)

def task_get_chunk(task_id: str, chunk_index: int) -> Dict:
    data = executor.get_chunk(task_id, chunk_index)
    if data is None:
        return {"error": "Chunk not found"}
    return {"chunk_index": chunk_index, "data": data}

def task_pause(task_id: str) -> Dict:
    ok = executor.pause(task_id)
    return {"status": "paused" if ok else "failed"}

def task_resume(task_id: str) -> Dict:
    ok = executor.resume(task_id)
    return {"status": "resumed" if ok else "failed", "task_id": task_id}

def task_cancel(task_id: str) -> Dict:
    ok = executor.cancel(task_id)
    return {"status": "cancelled" if ok else "failed"}

def task_list(status: str = None, limit: int = 50) -> Dict:
    tasks = executor.db.list_tasks(status, limit)
    return {"tasks": tasks, "count": len(tasks)}

# ─── Регистрация инструментов в MCP (вызывается из mcp_fs_server) ─────────
def register_tasks(server: BaseMCPServer):
    server.register_tool("task_submit", {
        "description": "Запустить длительную операцию в фоне. Возвращает task_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "args": {"type": "object"}
            },
            "required": ["tool_name", "args"]
        }
    }, lambda **kw: submit_task(kw["tool_name"], kw.get("args", {}), kw.get("dialog_id")))

    server.register_tool("task_status", {
        "description": "Получить статус и прогресс задачи",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"]
        }
    }, lambda **kw: task_status(kw["task_id"]))

    server.register_tool("task_get_chunk", {
        "description": "Получить очередной чанк результата задачи",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "chunk_index": {"type": "integer"}},
            "required": ["task_id", "chunk_index"]
        }
    }, lambda **kw: task_get_chunk(kw["task_id"], kw["chunk_index"]))

    server.register_tool("task_pause", {
        "description": "Приостановить выполнение задачи",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"]
        }
    }, lambda **kw: task_pause(kw["task_id"]))

    server.register_tool("task_resume", {
        "description": "Возобновить выполнение задачи",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"]
        }
    }, lambda **kw: task_resume(kw["task_id"]))

    server.register_tool("task_cancel", {
        "description": "Отменить выполнение задачи",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"]
        }
    }, lambda **kw: task_cancel(kw["task_id"]))

    server.register_tool("task_list", {
        "description": "Список задач с возможностью фильтрации по статусу",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "limit": {"type": "integer", "default": 50}
            }
        }
    }, lambda **kw: task_list(kw.get("status"), kw.get("limit", 50)))

# ─── Базовый класс для долгих задач (для использования в инструментах) ─────
class LongRunningTask:
    """Наследуйте ваш класс или оборачивайте функцию для поддержки паузы/возобновления."""
    def __init__(self, progress: TaskProgress):
        self.progress = progress

    def should_stop(self) -> bool:
        return self.progress.should_stop()

    def save_progress(self, stage: str, current: int = None, total: int = None, message: str = None):
        self.progress.update_progress(stage, current, total, message)

    def save_resume_data(self, data: Dict):
        self.progress.save_resume_data(data)

    def add_result_chunk(self, chunk_index: int, data: Any):
        self.progress.add_chunk(chunk_index, data)

    def log(self, message: str):
        self.progress.log(message)

# ─── Декоратор для превращения синхронной функции в асинхронную ───────────
def make_async(tool_func):
    """Декоратор: если в kwargs есть _task_progress – выполняется как часть задачи,
    иначе – синхронно."""
    def wrapper(*args, **kwargs):
        if "_task_progress" in kwargs:
            progress = kwargs.pop("_task_progress")
            task_id = kwargs.pop("_task_id", None)
            dialog_id = kwargs.pop("_dialog_id", None)
            # Вызываем оригинальную функцию, передавая ей progress
            return tool_func(*args, **kwargs, _task_progress=progress, _task_id=task_id, _dialog_id=dialog_id)
        else:
            return tool_func(*args, **kwargs)
    return wrapper

if __name__ == "__main__":
    _log("Task Manager module loaded")