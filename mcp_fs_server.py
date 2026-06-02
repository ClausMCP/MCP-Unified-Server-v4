#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Unified Filesystem Server v3.4 (Plugin-Ready + Task Manager + Help + Export + Shell + Office Editor + Memory Graph + Auto-Discovery)
Добавлена поддержка: асинхронные задачи, !command, справка, экспорт диалога, безопасный шелл,
экспорт чатов LM Studio, полноценное редактирование Excel/Word/PPT, граф памяти, авто-обнаружение модулей.
"""
import sys
import os
import atexit
import importlib
import importlib.util
import threading
import time
import json
from pathlib import Path
from datetime import datetime
from mcp_shared import BaseMCPServer, _log, dialog_ctx
from mcp_verbose import set_verbose as set_dialog_verbose, is_verbose as get_dialog_verbose

# --- Новые модули ---
from mcp_task_manager import register_tasks
from mcp_help import register_help_tool
from mcp_export_dialog import register_export_tool
from mcp_shell import register_shell_tool
from mcp_export_lmstudio import register_export_lmstudio_tool

# Активация координатора (опционально, но улучшает интеграцию)
try:
    import cognitive_coordinator
    _log("Cognitive Coordinator loaded")
except ImportError:
    _log("Cognitive Coordinator not available")

# --- Функция автоматического обнаружения модулей ---
def discover_modules_in_root():
    """Автоматически находит все модули mcp_*.py в корневой папке."""
    root = Path(__file__).parent
    modules = []
    excluded = {
        "mcp_fs_server.py",
        "mcp_orchestrator.py", 
        "mcp_shared.py",
        "mcp_setup.py",
        "fix_lmstudio_config.py",
        "mcp_verbose.py",
        "mcp_rate_limiter.py",
        "mcp_help.py",
        "mcp_export_dialog.py",
        "mcp_shell.py",
        "mcp_export_lmstudio.py"
    }
    
    _log(f"[Auto-Discovery] Scanning {root} for mcp_*.py")
    for py_file in root.glob("mcp_*.py"):
        if py_file.name in excluded:
            _log(f"[Auto-Discovery] Excluded: {py_file.name}")
            continue
        modules.append(py_file.stem)
        _log(f"[Auto-Discovery] Found: {py_file.stem}")
    
    _log(f"[Auto-Discovery] Total found: {len(modules)} modules")
    return modules

# --- Список основных модулей (автоматически обнаруживается) ---
SERVER_MODULES = discover_modules_in_root()
PLUGIN_DIR = Path(__file__).parent / "mcp_plugins"
_loaded_modules = []

MODULE_DEPS = {
    "mcp_fs_search": ["watchdog"],
    "mcp_fs_watcher": ["watchdog"],
    "mcp_fs_media": ["PIL", "mutagen"],
    "mcp_fs_indexer": [],
    "mcp_office_reader": ["docx", "openpyxl", "pptx"],
    "mcp_office_editor": ["openpyxl", "python-docx", "python-pptx"],
    "mcp_web_reader": ["requests", "bs4", "feedparser"],
    "mcp_db_client": ["duckdb", "pyodbc"],
    "mcp_calendar": ["icalendar"],
    "mcp_email_client": ["keyring"],
    "code_debugger_server": [],
    "knowledge_base_server": [],
    "mcp_mempalace": [],
    "mcp_smart_search": [],
    "dialog_manager": [],
    "mcp_rag_engine": ["chromadb", "sentence_transformers", "pypdf", "docx", "ebooklib", "bs4"],
    "mcp_memory_engine": [],
    "mcp_memory_tools": [],
    "mcp_memory_graph": [],
    "mcp_dialog_indexer": [],
}

def _check_dependencies(deps: list) -> list:
    missing = []
    for dep in deps:
        pkg = dep.split("==")[0].split(">=")[0].split("<")[0].strip()
        if importlib.util.find_spec(pkg) is None:
            missing.append(pkg)
    return missing

def _copy_tools(from_server: BaseMCPServer, to_server: BaseMCPServer) -> int:
    registered = 0
    if not hasattr(from_server, "tools") or not from_server.tools:
        return 0
    for tool in from_server.tools:
        name = tool["name"]
        if name in to_server._handlers:
            continue
        handler = from_server._handlers.get(name)
        if handler:
            to_server.register_tool(name, tool, handler)
            registered += 1
    return registered

def _load_module(mod_name: str, unified: BaseMCPServer) -> bool:
    _log(f"[Load] Trying to load module: {mod_name}")
    optional_deps = MODULE_DEPS.get(mod_name, [])
    if optional_deps:
        missing_opt = _check_dependencies(optional_deps)
        if missing_opt:
            _log(f"[INFO] {mod_name}: optional deps missing → {', '.join(missing_opt)}")
    try:
        mod = importlib.import_module(mod_name)
        _log(f"[Load] Imported {mod_name}")
    except ImportError as e:
        _log(f"[SKIP] {mod_name}: missing dependency → {e}")
        return False
    except Exception as e:
        _log(f"[FAIL] {mod_name}: import error → {e}")
        return False
    return _register_module(mod_name, mod, unified)

def _register_module(mod_name: str, mod: object, unified: BaseMCPServer) -> bool:
    meta = getattr(mod, "__mcp_plugin__", {})
    deps = meta.get("dependencies", [])
    missing = _check_dependencies(deps)
    if missing:
        _log(f"[SKIP] {mod_name}: missing deps → {', '.join(missing)}")
        return False
    on_load = meta.get("on_load")
    if callable(on_load):
        try:
            on_load()
        except Exception as e:
            _log(f"[WARN] {mod_name}: on_load failed → {e}")
    reg_func = getattr(mod, "register_tools", None)
    if callable(reg_func):
        reg_func(unified)
        _loaded_modules.append((meta, mod))
        _log(f"[Load] Successfully registered tools for: {mod_name}")
        return True
    elif hasattr(mod, "server") and mod.server is not None:
        cnt = _copy_tools(mod.server, unified)
        _log(f"[OK] {mod_name} legacy ({cnt} tools)")
        _loaded_modules.append((meta, mod))
        return True
    else:
        _log(f"[WARN] {mod_name}: no register_tools or server")
        return False

def _discover_plugins(unified: BaseMCPServer) -> dict:
    if not PLUGIN_DIR.is_dir():
        PLUGIN_DIR.mkdir(exist_ok=True)
        return {"loaded": 0, "skipped": 0}
    loaded, skipped = 0, 0
    for p_file in sorted(PLUGIN_DIR.glob("*.py")):
        if p_file.name.startswith("_"):
            continue
        mod_name = p_file.stem
        spec = importlib.util.spec_from_file_location(mod_name, str(p_file))
        if not spec or not spec.loader:
            skipped += 1
            continue
        try:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            if _register_module(mod_name, mod, unified):
                loaded += 1
            else:
                skipped += 1
        except Exception as e:
            _log(f"[FAIL] Plugin {mod_name}: {e}")
            skipped += 1
    return {"loaded": loaded, "skipped": skipped}

def _graceful_shutdown():
    _log("Shutting down MCP Server. Running unload hooks...")
    try:
        from mcp_shared import conversation_memory
        if hasattr(conversation_memory, '_stop_auto_threads'):
            conversation_memory._stop_auto_threads()
            _log("ConversationMemory auto-threads stop signal sent.")
    except Exception as e:
        _log(f"[WARN] Failed to stop ConversationMemory threads: {e}")
    for meta, mod in reversed(_loaded_modules):
        on_unload = meta.get("on_unload")
        if callable(on_unload):
            try:
                on_unload()
            except Exception as e:
                _log(f"[WARN] Unload hook failed: {e}")
    _log("All cleanup completed. Exiting.")

def print_tools_summary(unified: BaseMCPServer):
    _log("=" * 60)
    _log(f"TOOLS REGISTRY: {len(unified._handlers)} tools available")
    _log("=" * 60)
    from collections import defaultdict
    by_module = defaultdict(list)
    for name in sorted(unified._handlers.keys()):
        handler = unified._handlers[name]
        module = getattr(handler, '__module__', 'unknown')
        by_module[module].append(name)
    for module in sorted(by_module.keys()):
        tools = by_module[module]
        _log(f"  [{module}] ({len(tools)} tools): {', '.join(tools)}")

os.environ.setdefault("MCP_SEARCH_TIMEOUT", "3600")
os.environ.setdefault("MCP_ANALYSIS_TIMEOUT", "3600")
os.environ.setdefault("MCP_GRAPH_AUTO_EXTRACT", "true")

def main():
    os.environ.setdefault("MCP_AUTO_INDEX_DIALOGS", "1")
    _log("Initializing MCP Unified Filesystem Server v3.4 (with Auto-Discovery)...")
    unified = BaseMCPServer("filesystem-unified", "3.4")

    # Register verbose control tools
    unified.register_tool("set_verbose", {
        "description": "Включить/выключить подробные уведомления о прогрессе (прогресс-бар в чате)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "enable": {"type": "boolean", "default": True},
                "dialog_id": {"type": "string"}
            }
        }
    }, lambda **kw: set_dialog_verbose(kw.get("dialog_id") or dialog_ctx.get(), kw.get("enable", True)) or {"status": "ok"})
    
    unified.register_tool("get_verbose", {
        "description": "Проверить, включены ли подробные уведомления для диалога",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dialog_id": {"type": "string"}
            }
        }
    }, lambda **kw: {"verbose": get_dialog_verbose(kw.get("dialog_id") or dialog_ctx.get())})

    register_help_tool(unified)
    register_export_tool(unified)
    register_shell_tool(unified)
    register_tasks(unified)
    register_export_lmstudio_tool(unified)

    from mcp_rate_limiter import rate_limiter, DB_PATH as RATE_DB_PATH
    import sqlite3
    def rate_limiter_stats():
        conn = sqlite3.connect(str(RATE_DB_PATH))
        cur = conn.execute("SELECT service, failures, last_failure FROM rate_history")
        rows = cur.fetchall()
        conn.close()
        return {"services": [{"service": r[0], "failures": r[1], "last_failure": r[2]} for r in rows]}

    unified.register_tool("rate_limiter_stats", {
        "description": "Статистика rate limiter и circuit breaker",
        "inputSchema": {"type": "object", "properties": {}}
    }, rate_limiter_stats)

    unified.register_tool("rate_limiter_reset", {
        "description": "Сбросить rate limiter для сервиса",
        "inputSchema": {"type": "object", "properties": {"service": {"type": "string"}}}
    }, lambda **kw: rate_limiter.reset(kw.get("service")) or {"status": "reset"})

    core_loaded, core_skipped = 0, 0
    for mod in SERVER_MODULES:
        if _load_module(mod, unified):
            core_loaded += 1
        else:
            core_skipped += 1

    plugin_stats = _discover_plugins(unified)
    total_tools = len(unified._handlers)
    _log(f"Server ready: {core_loaded} core + {plugin_stats['loaded']} plugins loaded. {total_tools} tools available.")
    if core_skipped or plugin_stats['skipped']:
        _log(f"Skipped: {core_skipped} core, {plugin_stats['skipped']} plugins")

    print_tools_summary(unified)
    atexit.register(_graceful_shutdown)

    # ========== Автоматическая индексация memPalace ==========
    def auto_index_mempalace():
        """Фоновый запуск индексации диалогов и кода (один раз при старте)."""
        time.sleep(3)  # даём серверу полностью стартовать
        _log("[MemPalace] Starting background auto-indexing...")
        try:
            from mcp_mempalace import mempalace_mine
        except ImportError:
            _log("[MemPalace] mempalace module not available, skipping auto-index")
            return
        
        server_dir = Path(__file__).parent.resolve()
        
        # --- 1. Индексация диалогов LM Studio (один раз) ---
        lmstudio_chats = Path.home() / ".lmstudio" / "conversations"
        if lmstudio_chats.exists() and lmstudio_chats.is_dir():
            index_flag = server_dir / ".mempalace_chats_indexed"
            if not index_flag.exists():
                _log(f"[MemPalace] Indexing LM Studio chats from {lmstudio_chats}")
                res = mempalace_mine(str(lmstudio_chats), mode="convos")
                if res.get("status") == "success":
                    # Создаём флаг, чтобы больше не индексировать при следующих запусках
                    index_flag.touch()
                    _log("[MemPalace] LM Studio chats indexed (flag created)")
                else:
                    _log(f"[MemPalace] Failed to index chats: {res.get('error')}")
            else:
                _log("[MemPalace] LM Studio chats already indexed, skipping")
        else:
            _log(f"[MemPalace] LM Studio chats folder not found: {lmstudio_chats}")
        
        # --- 2. Индексация кода (проект mempalace_project) с проверкой по дате ---
        code_project = server_dir / "mempalace_project"
        if not code_project.exists():
            code_project.mkdir(exist_ok=True)
            _log(f"[MemPalace] Created project folder: {code_project}")
        
        # Проверяем, когда в последний раз индексировался код
        code_index_flag = code_project / ".mempalace_code_indexed"
        need_index = False
        if not code_index_flag.exists():
            need_index = True
        else:
            try:
                with open(code_index_flag, 'r') as f:
                    data = json.load(f)
                last_index = datetime.fromisoformat(data.get("last_index", "2000-01-01"))
                if (datetime.now() - last_index).days >= 1:  # раз в сутки
                    need_index = True
                else:
                    _log(f"[MemPalace] Code already indexed today ({last_index.date()})")
            except Exception:
                need_index = True
        
        if need_index:
            _log(f"[MemPalace] Indexing code project: {code_project}")
            res = mempalace_mine(str(code_project), mode="files")
            if res.get("status") == "success":
                with open(code_index_flag, 'w') as f:
                    json.dump({"last_index": datetime.now().isoformat()}, f)
                _log("[MemPalace] Code project indexed")
            else:
                _log(f"[MemPalace] Failed to index code: {res.get('error')}")
        else:
            _log("[MemPalace] Code project indexing skipped (fresh)")

    # Запускаем фоновый поток
    threading.Thread(target=auto_index_mempalace, daemon=True, name="mempalace_auto_index").start()
    _log("Listening on STDIO (JSON-RPC 2.0)...")
    unified.run()

if __name__ == "__main__":
    main()