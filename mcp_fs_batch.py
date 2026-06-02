#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Filesystem Batch v3.5 (Context-Isolated & Validated)
Secure batch operations with hallucination prevention, chunked execution,
and dialog context isolation. Integrates with batch_validate_helper.
"""
import os
import sys
import json
import time
import re
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from mcp_shared import (
    _log, normalize_path, _ensure_allowed, is_placeholder_path,
    BaseMCPServer, conversation_memory, dialog_ctx,
    list_directory_sync, query_llm, validate_paths_decorator
)

try:
    from batch_validate_helper import validate_operations
    HAS_VALIDATOR = True
except ImportError:
    HAS_VALIDATOR = False

# ─── Core Batch Operations ───────────────────────────────────────────────────
def batch_move(operations: List[Dict[str, str]], dry_run: bool = False,
               strict_validation: bool = True, chunk_size: int = 50,
               dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    
    # Validation Phase
    if HAS_VALIDATOR:
        validation = validate_operations(operations, strict=strict_validation)
        if not validation["valid"]:
            conversation_memory.add(
                op="batch_move", paths={"count": len(operations)},
                status="validation_failed", dialog=d_id,
                context=f"Batch validation failed: {validation.get('placeholders_found', 0)} placeholders"
            )
            return validation
        operations = validation["valid_ops"]
    else:
        # Fallback: basic check using is_placeholder_path
        for i, op in enumerate(operations):
            is_ph, reason = is_placeholder_path(op.get("source", ""))
            if is_ph:
                return {"status": "validation_failed", "index": i, "reason": reason}

    if not operations:
        return {"status": "empty", "message": "No valid operations after validation"}

    # Execution Phase (Chunked)
    total = len(operations)
    success = []
    failed = []
    start = time.time()

    for chunk_start in range(0, total, chunk_size):
        chunk = operations[chunk_start:chunk_start + chunk_size]
        for op in chunk:
            src = Path(normalize_path(op["source"]))
            dst = Path(normalize_path(op["destination"]))
            try:
                _ensure_allowed(src, "batch_move")
                _ensure_allowed(dst.parent, "batch_move")
                
                if dry_run:
                    success.append({"source": str(src), "destination": str(dst), "status": "dry_run"})
                    continue

                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    import shutil
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    shutil.rmtree(src)
                else:
                    import shutil
                    shutil.move(str(src), str(dst))
                success.append({"source": str(src), "destination": str(dst), "status": "moved"})
            except Exception as e:
                failed.append({"source": str(src), "destination": str(dst), "error": str(e)})

    elapsed = time.time() - start
    status = "completed" if not failed else "partial"
    
    conversation_memory.add(
        op="batch_move",
        paths={"source_count": total, "success_count": len(success)},
        status=status, dialog=d_id,
        context=f"Moved {len(success)}/{total} files in {elapsed:.1f}s"
    )

    return {
        "status": status,
        "total": total,
        "success": len(success),
        "failed": len(failed),
        "details": failed if failed else None,
        "elapsed_sec": round(elapsed, 2),
        "dry_run": dry_run
    }

def batch_copy(operations: List[Dict[str, str]], dry_run: bool = False,
               chunk_size: int = 50, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    if HAS_VALIDATOR:
        validation = validate_operations(operations, strict=False)
        operations = validation["valid_ops"]

    total = len(operations)
    success, failed = [], []
    start = time.time()

    for op in operations:
        src = Path(normalize_path(op["source"]))
        dst = Path(normalize_path(op["destination"]))
        try:
            _ensure_allowed(src, "batch_copy")
            _ensure_allowed(dst.parent, "batch_copy")
            if dry_run:
                success.append({"source": str(src), "destination": str(dst), "status": "dry_run"})
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            success.append({"source": str(src), "destination": str(dst), "status": "copied"})
        except Exception as e:
            failed.append({"source": str(src), "destination": str(dst), "error": str(e)})

    conversation_memory.add(
        op="batch_copy", paths={"count": total}, status="completed" if not failed else "partial",
        dialog=d_id, context=f"Copied {len(success)}/{total} files"
    )
    return {
        "status": "completed" if not failed else "partial",
        "success": len(success), "failed": len(failed),
        "details": failed if failed else None, "elapsed_sec": round(time.time() - start, 2)
    }

def batch_delete(paths: List[str], use_trash: bool = True,
                 dry_run: bool = False, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    success, failed = [], []
    start = time.time()

    for p_str in paths:
        p = Path(normalize_path(p_str))
        try:
            _ensure_allowed(p, "batch_delete")
            if dry_run:
                success.append({"path": str(p), "status": "dry_run"})
                continue
            
            if use_trash:
                try:
                    from mcp_fs_trash import move_to_trash
                    res = move_to_trash(str(p), dialog_id=d_id)
                    success.append({"path": str(p), "status": "trashed", "trash_id": res.get("trash_id")})
                except ImportError:
                    use_trash = False
            
            if not use_trash:
                if p.is_dir():
                    import shutil
                    shutil.rmtree(p)
                else:
                    p.unlink()
                success.append({"path": str(p), "status": "deleted"})
        except Exception as e:
            failed.append({"path": str(p), "error": str(e)})

    conversation_memory.add(
        op="batch_delete", paths={"count": len(paths)},
        status="completed" if not failed else "partial", dialog=d_id,
        context=f"Deleted {len(success)}/{len(paths)} items (trash={use_trash})"
    )
    return {
        "status": "completed" if not failed else "partial",
        "success": len(success), "failed": len(failed),
        "details": failed if failed else None, "elapsed_sec": round(time.time() - start, 2)
    }

# ─── Smart Operations (LLM-Assisted) ────────────────────────────────────────
def smart_move(natural_query: str, target_dir: str, source_dir: str = None,
               dry_run: bool = False, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    src = Path(normalize_path(source_dir or "."))
    tgt = Path(normalize_path(target_dir))
    _ensure_allowed(src, "smart_move")
    _ensure_allowed(tgt, "smart_move")

    if not tgt.exists():
        tgt.mkdir(parents=True, exist_ok=True)

    # 1. Scan directory
    dir_listing = list_directory_sync(str(src))
    if "error" in dir_listing:
        return {"status": "error", "message": dir_listing["error"]}

    files = [e["name"] for e in dir_listing.get("entries", []) if e["is_file"]]
    if not files:
        return {"status": "error", "message": "No files in source directory"}

    # 2. Ask LLM to generate move plan
    prompt = f"""
    You are a file organization assistant. 
    User query: "{natural_query}"
    Available files: {json.dumps(files[:50])}
    Target directory: "{tgt}"
    
    Return ONLY a valid JSON array of objects with "source" and "destination" keys.
    Example: [{{"source": "file1.jpg", "destination": "/full/path/to/target/file1.jpg"}}]
    Do not invent files. Do not use markdown formatting.
    """
    llm_resp = query_llm(prompt)
    if not llm_resp:
        return {"status": "error", "message": "LLM did not return a plan"}

    # 3. Parse & Validate LLM output
    try:
        plan = json.loads(llm_resp)
        if not isinstance(plan, list):
            raise ValueError("Expected JSON array")
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"LLM returned invalid JSON: {e}"}

    # Normalize paths in plan
    operations = []
    for item in plan:
        s = Path(src) / item.get("source", "")
        d = Path(tgt) / item.get("destination", Path(item.get("destination", s.name)).name)
        operations.append({"source": str(s), "destination": str(d)})

    # 4. Execute via batch_move
    return batch_move(operations, dry_run=dry_run, dialog_id=d_id)

# ─── Auto-Scan & Rename ─────────────────────────────────────────────────────
def auto_scan(path: str, rules: Dict[str, List[str]] = None, 
              dry_run: bool = False, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    p = Path(normalize_path(path))
    _ensure_allowed(p, "auto_scan")

    if not rules:
        rules = {
            "Images": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"],
            "Documents": [".pdf", ".doc", ".docx", ".txt", ".xlsx", ".pptx"],
            "Archives": [".zip", ".rar", ".7z", ".tar", ".gz"],
            "Installers": [".exe", ".msi", ".dmg", ".pkg"],
            "Code": [".py", ".js", ".html", ".css", ".json", ".md"]
        }

    dir_listing = list_directory_sync(str(p))
    if "error" in dir_listing:
        return {"status": "error", "message": dir_listing["error"]}

    operations = []
    for entry in dir_listing.get("entries", []):
        if not entry["is_file"]:
            continue
        ext = Path(entry["name"]).suffix.lower()
        for category, exts in rules.items():
            if ext in exts:
                dest_dir = p / category
                operations.append({
                    "source": entry["path"],
                    "destination": str(dest_dir / entry["name"])
                })
                break

    return batch_move(operations, dry_run=dry_run, dialog_id=d_id)

def batch_rename(path: str, pattern: str, replacement: str = "", 
                 dry_run: bool = False, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    p = Path(normalize_path(path))
    _ensure_allowed(p, "batch_rename")
    
    dir_listing = list_directory_sync(str(p))
    if "error" in dir_listing:
        return {"status": "error", "message": dir_listing["error"]}

    results = {"renamed": 0, "skipped": 0, "errors": 0, "details": []}
    start = time.time()

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return {"status": "error", "message": f"Invalid regex: {e}"}

    for entry in dir_listing.get("entries", []):
        if not entry["is_file"]:
            continue
        old_name = entry["name"]
        new_name = regex.sub(replacement, old_name)
        if old_name == new_name:
            results["skipped"] += 1
            continue
        
        old_path = Path(entry["path"])
        new_path = old_path.parent / new_name
        
        try:
            if dry_run:
                results["details"].append({"from": old_name, "to": new_name, "status": "dry_run"})
                results["renamed"] += 1
                continue
            if new_path.exists():
                raise FileExistsError("Target file already exists")
            old_path.rename(new_path)
            results["details"].append({"from": old_name, "to": new_name, "status": "renamed"})
            results["renamed"] += 1
        except Exception as e:
            results["errors"] += 1
            results["details"].append({"from": old_name, "to": new_name, "error": str(e)})

    conversation_memory.add(
        op="batch_rename", paths={"path": str(p)}, status="completed",
        dialog=d_id, context=f"Renamed {results['renamed']} files matching '{pattern}'"
    )
    results["status"] = "completed"
    results["elapsed_sec"] = round(time.time() - start, 2)
    return results

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("filesystem-batch", "3.5")
server.register_tool("batch_move", {
    "description": "Move multiple files/directories with validation and chunking",
    "inputSchema": {
        "type": "object",
        "properties": {
            "operations": {"type": "array", "items": {"type": "object", "properties": {"source": {"type": "string"}, "destination": {"type": "string"}}}},
            "dry_run": {"type": "boolean", "default": False},
            "strict_validation": {"type": "boolean", "default": True},
            "chunk_size": {"type": "integer", "default": 50},
            "dialog_id": {"type": "string"}
        },
        "required": ["operations"]
    }
}, lambda **kw: batch_move(
    kw["operations"], kw.get("dry_run", False), kw.get("strict_validation", True),
    kw.get("chunk_size", 50), kw.get("dialog_id")
))

server.register_tool("batch_copy", {
    "description": "Copy multiple files/directories",
    "inputSchema": {
        "type": "object",
        "properties": {
            "operations": {"type": "array", "items": {"type": "object"}},
            "dry_run": {"type": "boolean", "default": False},
            "chunk_size": {"type": "integer", "default": 50},
            "dialog_id": {"type": "string"}
        },
        "required": ["operations"]
    }
}, lambda **kw: batch_copy(
    kw["operations"], kw.get("dry_run", False), kw.get("chunk_size", 50), kw.get("dialog_id")
))

server.register_tool("batch_delete", {
    "description": "Delete multiple files/directories (optionally to trash)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "paths": {"type": "array", "items": {"type": "string"}},
            "use_trash": {"type": "boolean", "default": True},
            "dry_run": {"type": "boolean", "default": False},
            "dialog_id": {"type": "string"}
        },
        "required": ["paths"]
    }
}, lambda **kw: batch_delete(
    kw["paths"], kw.get("use_trash", True), kw.get("dry_run", False), kw.get("dialog_id")
))

server.register_tool("smart_move", {
    "description": "Move files based on natural language query using LLM planning",
    "inputSchema": {
        "type": "object",
        "properties": {
            "natural_query": {"type": "string"},
            "target_dir": {"type": "string"},
            "source_dir": {"type": "string"},
            "dry_run": {"type": "boolean", "default": False},
            "dialog_id": {"type": "string"}
        },
        "required": ["natural_query", "target_dir"]
    }
}, lambda **kw: smart_move(
    kw["natural_query"], kw["target_dir"], kw.get("source_dir"),
    kw.get("dry_run", False), kw.get("dialog_id")
))

server.register_tool("auto_scan", {
    "description": "Automatically sort files into categories based on extensions",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "rules": {"type": "object"},
            "dry_run": {"type": "boolean", "default": False},
            "dialog_id": {"type": "string"}
        },
        "required": ["path"]
    }
}, lambda **kw: auto_scan(
    kw["path"], kw.get("rules"), kw.get("dry_run", False), kw.get("dialog_id")
))

server.register_tool("batch_rename", {
    "description": "Rename multiple files using regex pattern",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "pattern": {"type": "string"},
            "replacement": {"type": "string", "default": ""},
            "dry_run": {"type": "boolean", "default": False},
            "dialog_id": {"type": "string"}
        },
        "required": ["path", "pattern"]
    }
}, lambda **kw: batch_rename(
    kw["path"], kw["pattern"], kw.get("replacement", ""),
    kw.get("dry_run", False), kw.get("dialog_id")
))

if __name__ == "__main__":
    server.run()