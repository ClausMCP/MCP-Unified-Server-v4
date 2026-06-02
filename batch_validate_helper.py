#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Batch Validation Helper v2.0
Strict, type-safe validation for batch filesystem operations.
Delegates path checks to mcp_shared, supports UNC format validation, 
removes duplicate regex patterns, and logs to conversation memory.
"""
import os
from pathlib import Path
from typing import List, Dict, Any, Tuple, TypedDict
from mcp_shared import (
    _log, normalize_path, _ensure_allowed, is_placeholder_path,
    conversation_memory, dialog_ctx
)

# ─── Type Definitions ────────────────────────────────────────────────────────
class BatchOperation(TypedDict, total=False):
    op: str
    source: str
    destination: str
    overwrite: bool
    recursive: bool

# ─── Validation Logic ────────────────────────────────────────────────────────
def _validate_unc_format(path_str: str) -> Tuple[bool, str]:
    """Check UNC path syntax before security checks."""
    p = normalize_path(path_str)
    if p.startswith('\\\\'):
        parts = p[2:].split('\\')
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return False, "Invalid UNC format. Expected \\\\server\\share\\..."
        # Reject illegal characters in server/share names
        if any(c in parts[0] for c in r'\/:*?"<>|'):
            return False, "Invalid characters in UNC server/share name"
        return True, ""
    return True, ""  # Local path, syntax validation passed

def validate_single_path(path: str, op_type: str) -> Dict[str, Any]:
    """Validate a single path: placeholder → syntax → security."""
    if not path or not isinstance(path, str):
        return {"valid": False, "error": "Path is empty or invalid type", "path": path}

    # 1. Placeholder & hallucination check (delegated to core)
    is_ph, reason = is_placeholder_path(path)
    if is_ph:
        return {"valid": False, "error": f"Placeholder/AI-hallucination detected: {reason}", "path": path}

    # 2. UNC format validation
    unc_ok, unc_err = _validate_unc_format(path)
    if not unc_ok:
        return {"valid": False, "error": unc_err, "path": path}

    # 3. Normalization & Security boundary check
    try:
        norm_path = normalize_path(path)
        _ensure_allowed(Path(norm_path), op_type)
        return {"valid": True, "path": norm_path}
    except PermissionError as e:
        return {"valid": False, "error": str(e), "path": path}
    except Exception as e:
        return {"valid": False, "error": f"Validation error: {e}", "path": path}

def validate_operations(operations: List[Dict[str, Any]], strict: bool = True) -> Dict[str, Any]:
    """
    Validate a list of batch operations.
    Returns split valid/invalid lists with normalized paths for safe execution.
    """
    dialog_id = dialog_ctx.get()
    valid_ops: List[Dict[str, Any]] = []
    invalid_ops: List[Dict[str, Any]] = []
    errors: List[str] = []

    for idx, op in enumerate(operations):
        op_type = op.get("op", "unknown").lower()
        src = op.get("source", "")
        dst = op.get("destination", "")

        # Validate source
        src_check = validate_single_path(src, f"src_{op_type}")
        if not src_check["valid"]:
            invalid_ops.append({**op, "validation_error": src_check["error"], "field": "source"})
            errors.append(f"Op {idx}: {src_check['error']}")
            continue

        # Validate destination for write operations
        if op_type in ("move", "copy", "rename") and dst:
            dst_check = validate_single_path(dst, f"dst_{op_type}")
            if not dst_check["valid"]:
                invalid_ops.append({**op, "validation_error": dst_check["error"], "field": "destination"})
                errors.append(f"Op {idx}: {dst_check['error']}")
                continue
            # Attach normalized paths to operation
            validated_op = {**op, "source": src_check["path"], "destination": dst_check["path"]}
        else:
            validated_op = {**op, "source": src_check["path"]}

        valid_ops.append(validated_op)

    result = {
        "valid": len(invalid_ops) == 0,
        "valid_count": len(valid_ops),
        "invalid_count": len(invalid_ops),
        "valid_ops": valid_ops,
        "invalid_ops": invalid_ops,
        "errors": errors
    }

    # Context-aware logging
    conversation_memory.add(
        op="validate_operations",
        paths={"total": len(operations)},
        status="success" if result["valid"] else "partial_failure",
        dialog=dialog_id,
        context=f"Batch validation: {result['valid_count']} valid, {result['invalid_count']} rejected."
    )
    return result

# ─── Self-Test / CLI Entry ──────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    _log("Running batch_validate_helper self-test...")
    test_ops = [
        {"op": "copy", "source": "C:\\temp\\data.txt", "destination": "\\\\nas\\backup\\data.txt"},
        {"op": "delete", "source": "[File_1_AI_Generated]"},
        {"op": "move", "source": "D:\\projects", "destination": "C:\\Windows\\System32"}
    ]
    res = validate_operations(test_ops)
    print(json.dumps(res, indent=2, ensure_ascii=False))