#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Code Debugger v3.1 (Context-Isolated & Cross-Platform Sandbox)
Syntax checking and isolated code execution with strict environment stripping,
Unix resource limits, AST-based safety checks, and persistent hypothesis memory.
Uses contextvars for secure dialog isolation.
"""
import os
import sys
import json
import subprocess
import tempfile
import ast
import time
from pathlib import Path
from typing import Dict, Any, Tuple
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx
)

# ─── Cross-Platform Resource Limits ─────────────────────────────────────────
HAS_RESOURCE = False
if sys.platform != 'win32':
    try:
        import resource
        HAS_RESOURCE = True
    except ImportError:
        pass

def _set_limits():
    """Apply strict resource limits (Unix only)."""
    if not HAS_RESOURCE:
        return
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (20, 20))
    except Exception:
        pass

# ─── Security & Safety ──────────────────────────────────────────────────────
_BANNED_MODULES = {
    'os', 'sys', 'subprocess', 'socket', 'urllib', 'http', 'requests',
    'ftplib', 'smtplib', 'telnetlib', 'poplib', 'imaplib', 'shutil',
    'pathlib', 'tempfile', 'multiprocessing', 'threading', 'ctypes',
    'mmap', 'pickle', 'marshal', 'importlib', 'pkgutil', 'io'
}

def _check_code_safety(code: str) -> Tuple[bool, str]:
    """Static AST analysis to block dangerous imports and built-in overrides."""
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split('.')[0] in _BANNED_MODULES:
                        return False, f"Restricted import: '{alias.name}'"
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split('.')[0] in _BANNED_MODULES:
                    return False, f"Restricted import from: '{node.module}'"
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ('__builtins__', 'open', 'eval', 'exec'):
                        return False, f"Attempt to override restricted built-in: '{target.id}'"
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} (line {e.lineno})"
    except Exception as e:
        return False, f"AST analysis failed: {e}"

def _strip_environment() -> dict:
    """Return a whitelisted environment to prevent secret/env leaks."""
    allowed = {'PATH', 'PYTHONPATH', 'HOME', 'USERPROFILE', 'TEMP', 'TMP', 'SYSTEMROOT', 'COMSPEC'}
    return {k: v for k, v in os.environ.items() if k in allowed}

# ─── Core Operations ────────────────────────────────────────────────────────
def syntax_check(code: str, language: str = "python", dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    if language.lower() != "python":
        return {"valid": None, "error": f"Language '{language}' not supported for syntax check", "language": language}
    
    safe, reason = _check_code_safety(code)
    if not safe:
        return {"valid": False, "error": reason, "language": language}
    
    try:
        compile(code, '<string>', 'exec')
        conversation_memory.add(
            op="syntax_check", paths={"lang": language}, status="valid", dialog=d_id,
            context=f"Syntax check passed for {language}"
        )
        return {"valid": True, "error": None, "language": language}
    except SyntaxError as e:
        return {
            "valid": False, "error": f"Line {e.lineno}, Col {e.offset}: {e.msg}",
            "line": e.lineno, "column": e.offset, "text": e.text, "language": language
        }
    except Exception as e:
        return {"valid": False, "error": f"{type(e).__name__}: {e}", "language": language}

def test_hypothesis(code: str, timeout: int = 5, dialog_id: str = None) -> Dict:
    d_id = dialog_id or dialog_ctx.get()
    # 1. Safety check
    safe, reason = _check_code_safety(code)
    if not safe:
        return {"error": f"Security violation: {reason}", "blocked": True, "dialog": d_id}
    
    # 2. Syntax pre-check
    syntax = syntax_check(code)
    if not syntax["valid"]:
        return {"error": f"Syntax error: {syntax['error']}", "blocked": False, "dialog": d_id}
    
    tmpname = None
    start = time.time()
    try:
        fd, tmpname = tempfile.mkstemp(suffix='.py', prefix='mcp_exec_')
        os.close(fd)
        os.chmod(tmpname, 0o600)
        with open(tmpname, 'w', encoding='utf-8') as f:
            f.write("# Safe MCP Sandbox Environment\nimport sys; sys.path = ['.']\n" + code)
        
        # Prepare execution command
        cmd = [sys.executable, '-u', '-B', tmpname]
        env = _strip_environment()
        
        # Platform-specific sandboxing
        kwargs = {
            "capture_output": True, "text": True, "timeout": timeout,
            "env": env, "cwd": os.path.dirname(tmpname)
        }
        if sys.platform != 'win32':
            kwargs["preexec_fn"] = _set_limits
        else:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB

        proc = subprocess.run(cmd, **kwargs)
        elapsed = time.time() - start
        
        stdout = proc.stdout[:2000] if proc.stdout else ""
        stderr = proc.stderr[:1000] if proc.stderr else ""
        
        conversation_memory.add(
            op="test_hypothesis", paths={"temp_file": tmpname},
            status="executed" if proc.returncode == 0 else f"exit_{proc.returncode}",
            dialog=d_id, context=f"Executed in {elapsed:.2f}s, exit={proc.returncode}, out={len(stdout)} chars"
        )
        return {
            "exit_code": proc.returncode, "stdout": stdout, "stderr": stderr,
            "elapsed_sec": round(elapsed, 2),
            "truncated": len(proc.stdout or "") > 2000 or len(proc.stderr or "") > 1000,
            "blocked": False, "dialog": d_id
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Execution timed out after {timeout}s", "timeout": timeout, "blocked": False, "dialog": d_id}
    except Exception as e:
        return {"error": str(e), "exception": type(e).__name__, "blocked": False, "dialog": d_id}
    finally:
        if tmpname and os.path.exists(tmpname):
            try:
                os.unlink(tmpname)
            except Exception:
                pass

# ─── Server Setup ────────────────────────────────────────────────────────────
server = BaseMCPServer("code-debugger", "3.1")
server.register_tool("syntax_check", {
    "description": "Check Python code syntax and block restricted imports",
    "inputSchema": {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "language": {"type": "string", "default": "python"},
            "dialog_id": {"type": "string"}
        },
        "required": ["code"]
    }
}, lambda **kw: syntax_check(kw["code"], kw.get("language", "python"), kw.get("dialog_id")))

server.register_tool("test_hypothesis", {
    "description": "Run Python code in isolated sandbox with strict env stripping and timeout",
    "inputSchema": {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "timeout": {"type": "integer", "default": 5},
            "dialog_id": {"type": "string"}
        },
        "required": ["code"]
    }
}, lambda **kw: test_hypothesis(kw["code"], kw.get("timeout", 5), kw.get("dialog_id")))

if __name__ == "__main__":
    server.run()