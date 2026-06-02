#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Indexer v3.1 (Context-Isolated & Thread-Safe)
Background content indexing with SQLite FTS5, incremental updates,
and fast full-text search across file contents.
Uses contextvars for secure dialog isolation.
"""
import os
import sys
import json
import time
import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
from mcp_shared import (
    _log, normalize_path, _ensure_allowed,
    BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Database ───────────────────────────────────────────────────────────────
class ContentIndex:
    def __init__(self, db_path: str = "mcp_index.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA cache_size=-65536")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS files (
                        id INTEGER PRIMARY KEY,
                        path TEXT UNIQUE NOT NULL,
                        content TEXT,
                        size INTEGER,
                        mtime REAL,
                        indexed_at REAL,
                        hash TEXT
                    )
                """)
                try:
                    conn.execute("""
                        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
                            path, content,
                            content='files', content_rowid='id'
                        )
                    """)
                    conn.execute("""
                        CREATE TRIGGER IF NOT EXISTS fts_insert AFTER INSERT ON files BEGIN
                            INSERT INTO fts(rowid, path, content) VALUES (new.id, new.path, new.content);
                        END
                    """)
                    conn.execute("""
                        CREATE TRIGGER IF NOT EXISTS fts_delete AFTER DELETE ON files BEGIN
                            DELETE FROM fts WHERE rowid = old.id;
                        END
                    """)
                    conn.execute("""
                        CREATE TRIGGER IF NOT EXISTS fts_update AFTER UPDATE ON files BEGIN
                            UPDATE fts SET path = new.path, content = new.content WHERE rowid = old.id;
                        END
                    """)
                except sqlite3.OperationalError as e:
                    _log(f"FTS5 initialization warning: {e}")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS indexed_roots (
                        root TEXT PRIMARY KEY,
                        last_scan REAL
                    )
                """)
                conn.commit()

    def index_file(self, path: str, content: str, size: int, mtime: float) -> bool:
        h = hashlib.md5(content.encode()).hexdigest()[:16]
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute("SELECT id, hash FROM files WHERE path = ?", (path,))
                row = cur.fetchone()
                if row:
                    if row[1] == h:
                        return False
                    conn.execute("DELETE FROM files WHERE id = ?", (row[0],))
                conn.execute("""
                    INSERT INTO files (path, content, size, mtime, indexed_at, hash)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (path, content, size, mtime, time.time(), h))
                conn.commit()
                return True

    def search(self, query: str, limit: int = 50) -> List[Dict]:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                try:
                    cur = conn.execute("""
                        SELECT f.path, f.size, f.mtime, rank
                        FROM fts
                        JOIN files f ON fts.rowid = f.id
                        WHERE fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                    """, (query, limit))
                    return [
                        {"path": r[0], "size": r[1], "mtime": r[2], "rank": r[3]}
                        for r in cur.fetchall()
                    ]
                except sqlite3.OperationalError:
                    return []

    def get_stats(self) -> Dict:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                roots = conn.execute("SELECT COUNT(*) FROM indexed_roots").fetchone()[0]
                size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
                return {"files_indexed": files, "roots": roots, "db_size_bytes": size}

_idx = ContentIndex()

# ─── Indexing ───────────────────────────────────────────────────────────────
def build_index(path: str, extensions: List[str] = None, max_size_kb: int = 1024,
                dry_run: bool = False) -> Dict:
    p = Path(normalize_path(path))
    _ensure_allowed(p, "build_index")
    if not p.is_dir():
        raise ValueError(f"Path is not a directory: {path}")

    exts = set(e.lower() for e in (extensions or [".txt", ".py", ".md", ".json", ".log", ".csv"]))
    max_bytes = max_size_kb * 1024
    scanned = 0
    indexed = 0
    skipped = 0
    errors = 0
    start_time = time.time()

    for root, dirs, files in os.walk(str(p)):
        for f in files:
            scanned += 1
            fp = Path(root) / f
            if fp.suffix.lower() not in exts:
                skipped += 1
                continue
            try:
                st = fp.stat()
                if st.st_size > max_bytes or st.st_size == 0:
                    skipped += 1
                    continue
                with open(fp, 'r', encoding='utf-8', errors='replace') as fh:
                    content = fh.read(max_bytes)
                if not dry_run:
                    added = _idx.index_file(str(fp), content, st.st_size, st.st_mtime)
                    if added:
                        indexed += 1
            except Exception:
                errors += 1

        if time.time() - start_time > 300:
            _log("Index build timeout reached (5m)")
            break

    if not dry_run:
        with sqlite3.connect(_idx.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO indexed_roots (root, last_scan)
                VALUES (?, ?)
            """, (str(p), time.time()))
            conn.commit()
            
        conversation_memory.add(
            op="build_index", paths={"path": str(p)},
            status="ok", dialog=dialog_ctx.get(),
            context=f"Indexed {indexed}/{scanned} files from {str(p)}"
        )

    return {
        "status": "dry_run" if dry_run else "completed",
        "path": str(p),
        "scanned": scanned,
        "indexed": indexed,
        "skipped": skipped,
        "errors": errors,
        "extensions": list(exts),
        "elapsed_sec": round(time.time() - start_time, 2)
    }

def search_indexed(query: str, limit: int = 50) -> Dict:
    results = _idx.search(query, limit)
    return {
        "query": query,
        "results": results,
        "count": len(results),
        "db_stats": _idx.get_stats()
    }

def index_stats() -> Dict:
    return _idx.get_stats()

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-indexer", "3.1")
server.register_tool("build_index", {
    "description": "Index file contents for full-text search (incremental)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "extensions": {"type": "array", "items": {"type": "string"}},
            "max_size_kb": {"type": "integer", "default": 1024},
            "dry_run": {"type": "boolean", "default": False}
        },
        "required": ["path"]
    }
}, lambda **kw: build_index(
    kw["path"], kw.get("extensions"), kw.get("max_size_kb", 1024), kw.get("dry_run", False)
))

server.register_tool("search_indexed", {
    "description": "Full-text search in indexed content (FTS5)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 50}
        },
        "required": ["query"]
    }
}, lambda **kw: search_indexed(kw["query"], kw.get("limit", 50)))

server.register_tool("index_stats", {
    "description": "Indexer database statistics",
    "inputSchema": {"type": "object", "properties": {}}
}, index_stats)

if __name__ == "__main__":
    server.run()