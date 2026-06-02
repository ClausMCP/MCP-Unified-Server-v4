#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Extension Manager v1.0
Safe plugin discovery, dependency validation, and tool registration.
Falls back to legacy 'server' object if __mcp_plugin__ is absent.
"""
import os
import sys
import importlib
import importlib.util
from pathlib import Path
from typing import Dict, List, Optional
from mcp_shared import BaseMCPServer, _log, dialog_ctx

PLUGIN_DIRS = ["mcp_plugins", os.path.join(os.path.dirname(__file__), "mcp_plugins")]
LEGACY_MODULES = [
    "mcp_fs_operations", "mcp_fs_search", "mcp_fs_batch", "mcp_fs_trash",
    "mcp_fs_sync", "mcp_fs_watcher", "mcp_fs_archives", "mcp_fs_cloud",
    "mcp_fs_organizer", "mcp_fs_versioning", "mcp_fs_indexer",
    "mcp_fs_discovery", "mcp_fs_scripts", "mcp_fs_media",
    "mcp_orchestrator", "mcp_admin_server", "mcp_event_bus",
    "knowledge_base_server", "logic_verifier_server", "context_manager_server",
    "code_debugger_server", "mcp_memory_engine"
]

def _check_dependencies(deps: List[str]) -> List[str]:
    """Return list of missing packages."""
    missing = []
    for dep in deps:
        if importlib.util.find_spec(dep.split("==")[0].split(">=")[0].split("<")[0]) is None:
            missing.append(dep)
    return missing

def _load_module_from_path(module_path: Path) -> Optional[object]:
    spec = importlib.util.spec_from_file_location(module_path.stem, str(module_path))
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_path.stem] = mod
        spec.loader.exec_module(mod)
        return mod
    return None

def discover_and_load_plugins(unified: BaseMCPServer) -> Dict:
    loaded = []
    skipped = []
    failed = []
    tool_count = 0

    def _register_from_module(mod_name: str, mod_obj: object):
        nonlocal tool_count
        meta = getattr(mod_obj, "__mcp_plugin__", {})
        name = meta.get("name", mod_name)
        version = meta.get("version", "1.0.0")
        deps = meta.get("dependencies", [])
        
        missing = _check_dependencies(deps)
        if missing:
            _log(f"[SKIP] {name} v{version}: missing dependencies → {', '.join(missing)}")
            skipped.append(name)
            return

        # Try new register_tools() interface first
        reg_func = getattr(mod_obj, "register_tools", None)
        if reg_func:
            reg_func(unified)
            loaded.append(name)
            return

        # Fallback to legacy server object
        srv = getattr(mod_obj, "server", None)
        if srv and hasattr(srv, "tools"):
            for tool in srv.tools:
                t_name = tool["name"]
                if t_name in unified._handlers:
                    _log(f"[WARN] Tool '{t_name}' already registered. Skipping.")
                    continue
                handler = srv._handlers.get(t_name)
                if handler:
                    unified.register_tool(t_name, tool, handler)
                    tool_count += 1
            loaded.append(name)
        else:
            failed.append(name)
            _log(f"[FAIL] {name}: no 'register_tools' or 'server' object found")

    # 1. Load legacy/core modules
    for mod_name in LEGACY_MODULES:
        try:
            mod = importlib.import_module(mod_name)
            _register_from_module(mod_name, mod)
        except Exception as e:
            _log(f"[FAIL] Legacy {mod_name}: {e}")
            failed.append(mod_name)

    # 2. Auto-discover plugins from mcp_plugins/ directory
    for pdir in PLUGIN_DIRS:
        plugin_dir = Path(pdir)
        if not plugin_dir.is_dir():
            continue
        for p_file in plugin_dir.glob("*.py"):
            if p_file.name.startswith("_"): continue
            try:
                mod = _load_module_from_path(p_file)
                if mod:
                    _register_from_module(p_file.stem, mod)
            except Exception as e:
                _log(f"[FAIL] Plugin {p_file.stem}: {e}")
                failed.append(p_file.stem)

    return {
        "loaded": len(loaded),
        "skipped": len(skipped),
        "failed": len(failed),
        "total_tools": tool_count
    }