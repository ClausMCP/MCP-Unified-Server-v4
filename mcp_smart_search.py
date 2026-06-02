#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Smart Search v1.2 – приоритетный поиск с офлайн-адаптацией
"""
import json
from typing import List, Dict, Optional
from mcp_shared import (
    _log, BaseMCPServer, conversation_memory, dialog_ctx, is_online
)

# В самом верху файла (после импортов)
try:
    from mcp_background_indexer import _indexer  # запускает фоновый поток
except ImportError:
    pass
__mcp_plugin__ = {
    "name": "smart-search",
    "version": "1.2.0",
    "description": "Unified keyword search over web, KB, memory, and memPalace with offline adaptation",
    "dependencies": [],
    "on_load": lambda: _log("[smart-search] v1.2 loaded (offline adaptation enabled)"),
    "on_unload": lambda: _log("[smart-search] Unloaded.")
}

def smart_search(query: str, sources: Optional[List[str]] = None,
                 limit: int = 5, dialog_id: Optional[str] = None,
                 mempalace_project: Optional[str] = None,
                 prefer_offline: bool = True) -> Dict:
    """
    Search across multiple sources with automatic offline adaptation.
    Sources: "web", "kb", "memory", "mempalace"
    """
    d_id = dialog_id or dialog_ctx.get()

    # 1. Автоматическая адаптация источников при отсутствии подключения
    if sources is None:
        if is_online():
            sources = ["web", "kb", "memory", "mempalace"]
        else:
            sources = ["kb", "memory", "mempalace"]  # web исключается офлайн

    # 2. Приоритет офлайн-источников, если включён флаг
    if prefer_offline:
        offline_sources = {"kb", "memory", "mempalace"}
        # Сортируем: сначала офлайн-источники, затем остальные (сохраняя исходный порядок внутри групп)
        sources = sorted(sources, key=lambda s: (0 if s in offline_sources else 1, sources.index(s)))

    results = {}
    errors = []

    # 1. Web search
    if "web" in sources:
        try:
            from mcp_web_reader import web_search
            web_res = web_search(query, max_results=limit)
            if web_res.get("status") == "success":
                results["web"] = {
                    "status": "success",
                    "count": web_res.get("count", 0),
                    "results": web_res.get("results", [])
                }
            else:
                errors.append(f"Web search: {web_res.get('error', 'Unknown error')}")
        except ImportError:
            errors.append("Web search not available: mcp_web_reader module missing")
        except Exception as e:
            errors.append(f"Web search error: {e}")

    # 2. Knowledge base
    if "kb" in sources:
        try:
            from knowledge_base_server import search_notes
            kb_res = search_notes(query, limit=limit)
            if kb_res.get("status") == "success":
                results["knowledge_base"] = {
                    "status": "success",
                    "count": len(kb_res.get("results", [])),
                    "results": kb_res.get("results", [])
                }
            else:
                errors.append(f"KB search: {kb_res.get('message', 'Unknown error')}")
        except ImportError:
            errors.append("Knowledge base not available: knowledge_base_server missing")
        except Exception as e:
            errors.append(f"KB search error: {e}")

    # 3. Memory (conversation)
    if "memory" in sources:
        try:
            from context_manager_server import recall_fact
            mem_res = recall_fact(query, store_if_missing=False, dialog_id=d_id)
            if mem_res.get("found"):
                results["memory"] = {
                    "status": "success",
                    "confidence": mem_res.get("confidence"),
                    "fact": mem_res.get("fact"),
                    "source": mem_res.get("source"),
                    "related_count": mem_res.get("related_count", 0)
                }
            else:
                results["memory"] = {"status": "not_found", "message": "No matching fact in memory"}
        except ImportError:
            errors.append("Memory recall not available: context_manager_server missing")
        except Exception as e:
            errors.append(f"Memory search error: {e}")

    # 4. memPalace
    if "mempalace" in sources:
        try:
            from mcp_mempalace import mempalace_search
            mp_res = mempalace_search(query, project_path=mempalace_project, limit=limit)
            if mp_res.get("status") == "success":
                results["mempalace"] = {
                    "status": "success",
                    "count": mp_res.get("count", 0),
                    "results": mp_res.get("results", [])
                }
            else:
                errors.append(f"memPalace search: {mp_res.get('error', 'Unknown error')}")
        except ImportError:
            errors.append("memPalace not available: mcp_mempalace module missing")
        except Exception as e:
            errors.append(f"memPalace search error: {e}")

    # Логирование в память диалога
    conversation_memory.add(
        op="smart_search",
        paths={"query": query, "sources": sources, "prefer_offline": prefer_offline},
        status="success" if results else "partial",
        dialog=d_id,
        context=f"Smart search for '{query}' over {sources}. Found {len(results)} sources with data."
    )

    return {
        "status": "success" if results else "error",
        "query": query,
        "sources_used": sources,
        "results": results,
        "errors": errors if errors else None,
        "dialog_id": d_id,
        "online_status": is_online()
    }

def register_tools(server: BaseMCPServer):
    server.register_tool("smart_search", {
        "description": "Unified search across web, knowledge base, conversation memory, and memPalace with offline adaptation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["web", "kb", "memory", "mempalace"]},
                    "default": ["web", "kb", "memory"],
                    "description": "Sources to search (auto-adapts if None)"
                },
                "limit": {"type": "integer", "default": 5},
                "dialog_id": {"type": "string"},
                "mempalace_project": {"type": "string", "description": "Project path for memPalace (if used)"},
                "prefer_offline": {"type": "boolean", "default": True, "description": "Prioritize offline sources"}
            },
            "required": ["query"]
        }
    }, lambda **kw: smart_search(
        kw["query"],
        kw.get("sources"),
        kw.get("limit", 5),
        kw.get("dialog_id"),
        kw.get("mempalace_project"),
        kw.get("prefer_offline", True)
    ))

if __name__ == "__main__":
    server = BaseMCPServer("smart-search", "1.2")
    register_tools(server)
    server.run()