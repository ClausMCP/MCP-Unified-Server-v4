#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Dialog Manager v1.1 – с синхронизацией и проверкой актуальности
"""
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)

DB_PATH = os.environ.get("MCP_DIALOG_DB", os.path.join(os.path.dirname(__file__), "dialog_names.db"))
VERIFY_TTL_SEC = int(os.environ.get("MCP_DIALOG_VERIFY_TTL", "300"))  # 5 минут
ARCHIVE_KEEP_DAYS = int(os.environ.get("MCP_DIALOG_ARCHIVE_DAYS", "7"))


class DialogManagerDB:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dialogs (
                    dialog_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_used TEXT DEFAULT CURRENT_TIMESTAMP,
                    tags TEXT DEFAULT '',
                    deleted INTEGER DEFAULT 0,      -- 1 если диалог удалён из памяти
                    archived INTEGER DEFAULT 0,     -- 1 если есть сжатая история (архив)
                    last_verified TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON dialogs(name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_last_used ON dialogs(last_used)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_deleted ON dialogs(deleted)")
            conn.commit()

    def _is_dialog_alive(self, dialog_id: str) -> bool:
        """Проверяет, есть ли хотя бы одна запись в entries для этого dialog_id."""
        try:
            conn = conversation_memory._get_conn()
            row = conn.execute("SELECT 1 FROM entries WHERE dialog = ? LIMIT 1", (dialog_id,)).fetchone()
            conn.close()
            return row is not None
        except Exception:
            return False

    def _has_compressed_history(self, dialog_id: str) -> bool:
        """Проверяет наличие сжатой истории в таблице compressed_history."""
        try:
            conn = conversation_memory._get_conn()
            row = conn.execute("SELECT 1 FROM compressed_history WHERE dialog = ? LIMIT 1", (dialog_id,)).fetchone()
            conn.close()
            return row is not None
        except Exception:
            return False

    def _verify_and_update(self, dialog_id: str):
        """Проверяет статус диалога и обновляет поля deleted/archived."""
        alive = self._is_dialog_alive(dialog_id)
        archived = self._has_compressed_history(dialog_id) if not alive else False
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE dialogs
                SET deleted = ?, archived = ?, last_verified = ?
                WHERE dialog_id = ?
            """, (0 if alive else 1, 1 if archived else 0, datetime.now().isoformat(), dialog_id))
            conn.commit()

    def _maybe_verify(self, dialog_id: str):
        """Проверяет, нужно ли обновить статус диалога (ленивая проверка)."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT last_verified FROM dialogs WHERE dialog_id = ?", (dialog_id,)
            ).fetchone()
        if not row:
            return
        last_verified = datetime.fromisoformat(row[0])
        if (datetime.now() - last_verified).total_seconds() > VERIFY_TTL_SEC:
            self._verify_and_update(dialog_id)

    def set_name(self, dialog_id: str, name: str) -> bool:
        # При создании/обновлении имени сразу проверяем, жив ли диалог
        alive = self._is_dialog_alive(dialog_id)
        archived = self._has_compressed_history(dialog_id) if not alive else False
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO dialogs
                (dialog_id, name, last_used, deleted, archived, last_verified)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (dialog_id, name, datetime.now().isoformat(),
                  0 if alive else 1, 1 if archived else 0,
                  datetime.now().isoformat()))
            conn.commit()
        return True

    def get_name(self, dialog_id: str) -> Optional[str]:
        self._maybe_verify(dialog_id)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT name FROM dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone()
            return row[0] if row else None

    def search(self, keyword: str, limit: int = 10, include_deleted: bool = False) -> List[Dict]:
        """Поиск диалогов. По умолчанию исключает удалённые (deleted=1)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            pattern = f"%{keyword}%"
            query = """
                SELECT dialog_id, name, created_at, last_used, tags, deleted, archived
                FROM dialogs
                WHERE (name LIKE ? OR tags LIKE ?)
            """
            params = [pattern, pattern]
            if not include_deleted:
                query += " AND deleted = 0"
            query += " ORDER BY last_used DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            results = [dict(row) for row in rows]
            # Для каждого результата проверим актуальность (лениво)
            for r in results:
                self._maybe_verify(r["dialog_id"])
            return results

    def list_all(self, limit: int = 50, include_deleted: bool = False) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT dialog_id, name, created_at, last_used, tags, deleted, archived FROM dialogs"
            params = []
            if not include_deleted:
                query += " WHERE deleted = 0"
            query += " ORDER BY last_used DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            results = [dict(row) for row in rows]
            for r in results:
                self._maybe_verify(r["dialog_id"])
            return results

    def update_last_used(self, dialog_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE dialogs SET last_used = ? WHERE dialog_id = ?",
                         (datetime.now().isoformat(), dialog_id))
            conn.commit()

    def cleanup_deleted(self, older_than_days: int = ARCHIVE_KEEP_DAYS):
        """Удаляет из таблицы диалоги, помеченные deleted=1 и не использовавшиеся более N дней."""
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                DELETE FROM dialogs
                WHERE deleted = 1 AND last_used < ?
            """, (cutoff,))
            conn.commit()

    def mark_archived(self, dialog_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE dialogs SET archived = 1 WHERE dialog_id = ?", (dialog_id,))
            conn.commit()


db = DialogManagerDB()

# Фоновый поток для периодической очистки мёртвых диалогов (раз в сутки)
def _cleanup_loop():
    while True:
        time.sleep(86400)  # 24 часа
        try:
            db.cleanup_deleted()
            _log("[DialogManager] Cleaned up old deleted dialogs")
        except Exception as e:
            _log(f"[DialogManager] Cleanup error: {e}")

_thread = threading.Thread(target=_cleanup_loop, daemon=True, name="dialog_cleanup")
_thread.start()


# ─── Инструменты MCP ─────────────────────────────────────────────────────
def dialog_set_name(name: str, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    if not name or len(name.strip()) < 3:
        return {"status": "error", "message": "Имя должно содержать минимум 3 символа"}
    db.set_name(d_id, name)
    conversation_memory.add(
        op="dialog_set_name",
        paths={"name": name},
        status="named",
        dialog=d_id,
        context=f"Диалог получил имя: {name}"
    )
    return {"status": "ok", "dialog_id": d_id, "name": name}

def dialog_get_name(dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    name = db.get_name(d_id)
    return {"dialog_id": d_id, "name": name if name else "не назван"}

def dialog_search(keyword: str, limit: int = 10, include_deleted: bool = False) -> Dict:
    results = db.search(keyword, limit, include_deleted)
    return {"status": "ok", "keyword": keyword, "results": results, "count": len(results)}

def dialog_list(limit: int = 50, include_deleted: bool = False) -> Dict:
    dialogs = db.list_all(limit, include_deleted)
    return {"status": "ok", "dialogs": dialogs, "count": len(dialogs)}

def dialog_switch(name_or_id: str) -> Dict:
    # Сначала ищем по ID (точное совпадение)
    if db.get_name(name_or_id):
        new_id = name_or_id
    else:
        results = db.search(name_or_id, limit=1, include_deleted=False)
        if not results:
            return {"status": "error", "message": f"Диалог '{name_or_id}' не найден"}
        new_id = results[0]["dialog_id"]
    
    # Проверяем, есть ли активные записи в памяти
    thread = conversation_memory.get_dialog_thread(dialog=new_id)
    if not thread["entries"]:
        # Нет активных записей – пытаемся восстановить из архива последние 50 сообщений
        try:
            # Используем новый метод restore_dialog_from_archive (должен быть в mcp_shared)
            restored = conversation_memory.restore_dialog_from_archive(dialog=new_id, limit=50)
            if restored:
                _log(f"Dialog {new_id}: auto-restored {restored} messages from archive")
        except AttributeError:
            # Если метод отсутствует (старая версия), просто игнорируем
            _log(f"Dialog {new_id}: no active entries and restore_dialog_from_archive not available")
    
    dialog_ctx.set(new_id)
    db.update_last_used(new_id)
    name = db.get_name(new_id)
    return {"status": "switched", "dialog_id": new_id, "name": name}

def dialog_try_auto_name(query: str, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    existing = db.get_name(d_id)
    if existing:
        return {"status": "already_named", "name": existing}
    words = re.findall(r'\b\w{3,}\b', query)
    name = " ".join(words[:5]) if words else "Новый диалог"
    db.set_name(d_id, name)
    return {"status": "auto_named", "dialog_id": d_id, "name": name}

def dialog_refresh_status(dialog_id: str = None) -> Dict:
    """Принудительно проверить статус диалога (жив/архивирован)."""
    d_id = dialog_id or dialog_ctx.get()
    db._verify_and_update(d_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT deleted, archived FROM dialogs WHERE dialog_id = ?", (d_id,)).fetchone()
        if row:
            return {"status": "ok", "dialog_id": d_id, "deleted": bool(row["deleted"]), "archived": bool(row["archived"])}
        else:
            return {"status": "not_found", "dialog_id": d_id}

def dialog_cleanup(older_than_days: int = ARCHIVE_KEEP_DAYS) -> Dict:
    """Принудительно удалить мёртвые диалоги старше указанного числа дней."""
    db.cleanup_deleted(older_than_days)
    return {"status": "cleaned", "older_than_days": older_than_days}

def dialog_restore_from_archive(dialog_id: str) -> Dict:
    """Восстановить контекст диалога из сжатой истории (архива)."""
    try:
        conn = conversation_memory._get_conn()
        row = conn.execute(
            "SELECT summary FROM compressed_history WHERE dialog = ? ORDER BY ts DESC LIMIT 1",
            (dialog_id,)
        ).fetchone()
        conn.close()
        if row:
            db.mark_archived(dialog_id)
            return {"status": "success", "dialog_id": dialog_id, "summary": row[0]}
        else:
            return {"status": "error", "message": "Нет сжатой истории для этого диалога"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ─── Регистрация инструментов ───────────────────────────────────────────
def register_tools(server: BaseMCPServer):
    server.register_tool("dialog_set_name", {
        "description": "Присвоить понятное имя текущему диалогу",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "dialog_id": {"type": "string"}
            },
            "required": ["name"]
        }
    }, lambda **kw: dialog_set_name(kw["name"], kw.get("dialog_id")))

    server.register_tool("dialog_get_name", {
        "description": "Узнать имя текущего диалога",
        "inputSchema": {
            "type": "object",
            "properties": {"dialog_id": {"type": "string"}}
        }
    }, lambda **kw: dialog_get_name(kw.get("dialog_id")))

    server.register_tool("dialog_search", {
        "description": "Найти диалоги по ключевому слову",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "include_deleted": {"type": "boolean", "default": False}
            },
            "required": ["keyword"]
        }
    }, lambda **kw: dialog_search(kw["keyword"], kw.get("limit", 10), kw.get("include_deleted", False)))

    server.register_tool("dialog_list", {
        "description": "Показать список всех диалогов",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 50},
                "include_deleted": {"type": "boolean", "default": False}
            }
        }
    }, lambda **kw: dialog_list(kw.get("limit", 50), kw.get("include_deleted", False)))

    server.register_tool("dialog_switch", {
        "description": "Переключить контекст на диалог по имени или ID",
        "inputSchema": {
            "type": "object",
            "properties": {"name_or_id": {"type": "string"}},
            "required": ["name_or_id"]
        }
    }, lambda **kw: dialog_switch(kw["name_or_id"]))

    server.register_tool("dialog_try_auto_name", {
        "description": "Автоматически назвать диалог на основе запроса",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "dialog_id": {"type": "string"}
            },
            "required": ["query"]
        }
    }, lambda **kw: dialog_try_auto_name(kw["query"], kw.get("dialog_id")))

    server.register_tool("dialog_refresh_status", {
        "description": "Принудительно проверить, существует ли диалог в памяти",
        "inputSchema": {
            "type": "object",
            "properties": {"dialog_id": {"type": "string"}}
        }
    }, lambda **kw: dialog_refresh_status(kw.get("dialog_id")))

    server.register_tool("dialog_cleanup", {
        "description": "Удалить из базы мёртвые диалоги старше N дней",
        "inputSchema": {
            "type": "object",
            "properties": {"older_than_days": {"type": "integer", "default": ARCHIVE_KEEP_DAYS}}
        }
    }, lambda **kw: dialog_cleanup(kw.get("older_than_days", ARCHIVE_KEEP_DAYS)))

    server.register_tool("dialog_restore_from_archive", {
        "description": "Получить сжатую историю диалога из архива",
        "inputSchema": {
            "type": "object",
            "properties": {"dialog_id": {"type": "string"}},
            "required": ["dialog_id"]
        }
    }, lambda **kw: dialog_restore_from_archive(kw["dialog_id"]))


__mcp_plugin__ = {
    "name": "dialog-manager",
    "version": "1.1",
    "description": "Управление диалогами с проверкой актуальности и синхронизацией",
    "dependencies": [],
    "on_load": lambda: _log("[DialogManager] Loaded (v1.1). Auto-cleanup thread started.")
}

if __name__ == "__main__":
    server = BaseMCPServer("dialog-manager", "1.1")
    register_tools(server)
    server.run()