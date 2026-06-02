#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP RAG Engine v1.3 – инкрементальная индексация, метаданные в SQLite,
правильная статистика, список файлов + проверка размерности эмбеддингов.
Исправлено: rag_add_document теперь корректно сохраняет doc_id в метаданные чанков.
"""
import os
import json
import hashlib
import sqlite3
import time
import tempfile
import uuid
import shutil
from pathlib import Path
from typing import List, Dict, Optional, Any, Set, Tuple
import threading
import chromadb
from sentence_transformers import SentenceTransformer
import pypdf
import docx
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from mcp_shared import (
    _log, BaseMCPServer, dialog_ctx, conversation_memory,
    normalize_path, _ensure_allowed, query_llm
)

# ─── Конфигурация ──────────────────────────────────────────────────────────
RAG_DB_PATH = os.environ.get("MCP_RAG_DB_PATH", "./mcp_rag_db")
CHUNK_SIZE = int(os.environ.get("MCP_RAG_CHUNK_SIZE", "1000"))
CHUNK_OVERLAP = int(os.environ.get("MCP_RAG_CHUNK_OVERLAP", "100"))
EMBEDDING_MODEL = os.environ.get("MCP_RAG_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
TOP_K = int(os.environ.get("MCP_RAG_TOP_K", "5"))
META_DB_PATH = os.path.join(RAG_DB_PATH, "rag_meta.db")

# Глобальные объекты
_chroma_client = None
_embedder = None
_collection_lock = threading.Lock()
_meta_conn = None
_meta_lock = threading.Lock()

def _get_client() -> chromadb.ClientAPI:
    global _chroma_client
    if _chroma_client is None:
        os.makedirs(RAG_DB_PATH, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=RAG_DB_PATH)
        _init_metadata_db()
    return _chroma_client

def _get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        _log(f"[RAG] Loading embedding model '{EMBEDDING_MODEL}'...")
        _embedder = SentenceTransformer(EMBEDDING_MODEL)
        _log("[RAG] Embedder ready")
    return _embedder

def _init_metadata_db():
    os.makedirs(RAG_DB_PATH, exist_ok=True)
    with sqlite3.connect(META_DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS rag_files (
            source TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            indexed_at REAL NOT NULL,
            last_modified REAL NOT NULL,
            PRIMARY KEY (source, collection_name)
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_collection ON rag_files(collection_name)")
        conn.commit()

def _get_indexed_metadata(collection_name: str) -> Dict[str, Tuple[str, float]]:
    with sqlite3.connect(META_DB_PATH) as conn:
        cur = conn.execute(
            "SELECT source, file_hash, last_modified FROM rag_files WHERE collection_name = ?",
            (collection_name,)
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}

def _update_file_metadata(source: str, file_hash: str, collection_name: str, mtime: float):
    with sqlite3.connect(META_DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO rag_files (source, file_hash, collection_name, indexed_at, last_modified)
            VALUES (?, ?, ?, ?, ?)""",
            (source, file_hash, collection_name, time.time(), mtime)
        )
        conn.commit()

def _delete_file_metadata(source: str, collection_name: str):
    with sqlite3.connect(META_DB_PATH) as conn:
        conn.execute(
            "DELETE FROM rag_files WHERE source = ? AND collection_name = ?",
            (source, collection_name)
        )
        conn.commit()

def _remove_file_from_collection(collection, source: str):
    try:
        result = collection.get(where={"source": source}, include=[])
        ids = result["ids"]
        if ids:
            collection.delete(ids=ids)
            _log(f"[RAG] Removed {len(ids)} chunks for {source}")
    except Exception as e:
        _log(f"[RAG] Failed to remove {source}: {e}")

def _file_hash_and_mtime(file_path: Path) -> Tuple[str, float]:
    try:
        stat = file_path.stat()
        mtime = stat.st_mtime
        with open(file_path, 'rb') as f:
            content = f.read()
        h = hashlib.md5(content).hexdigest()
        return h, mtime
    except Exception as e:
        _log(f"[RAG] Error hashing {file_path}: {e}")
        return "", 0.0

# ─── Извлечение текста и чанкинг ──────────────────────────────────────────
def extract_text(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext == '.pdf':
        text = []
        with open(file_path, 'rb') as f:
            reader = pypdf.PdfReader(f)
            for page in reader.pages:
                if page_text := page.extract_text():
                    text.append(page_text)
        return '\n'.join(text)
    elif ext == '.docx':
        doc = docx.Document(file_path)
        return '\n'.join(p.text for p in doc.paragraphs)
    elif ext == '.epub':
        book = epub.read_epub(file_path)
        text = []
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), 'html.parser')
                text.append(soup.get_text())
        return '\n'.join(text)
    elif ext in ('.txt', '.md'):
        return file_path.read_text(encoding='utf-8', errors='replace')
    else:
        return ""

def chunk_text(text: str, chunk_size: int, overlap: int) -> List[str]:
    if not text:
        return []
    paragraphs = text.split('\n')
    chunks = []
    current = []
    current_len = 0
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        para_len = len(para)
        if current_len + para_len > chunk_size and current:
            chunks.append('\n'.join(current))
            overlap_text = []
            overlap_len = 0
            for p in reversed(current):
                if overlap_len + len(p) <= overlap:
                    overlap_text.insert(0, p)
                    overlap_len += len(p)
                else:
                    break
            current = overlap_text
            current_len = overlap_len
        current.append(para)
        current_len += para_len
    if current:
        chunks.append('\n'.join(current))
    return chunks

# ─── Инкрементальная индексация ───────────────────────────────────────────
def rag_index_folder(folder_path: str, collection_name: str = "default",
                     force_reindex: bool = False, incremental: bool = True,
                     cleanup_deleted: bool = False) -> Dict:
    dialog_id = dialog_ctx.get()
    root = Path(normalize_path(folder_path))
    try:
        _ensure_allowed(root, "rag_index_folder")
    except PermissionError as e:
        return {"status": "error", "message": str(e)}
    if not root.is_dir():
        return {"status": "error", "message": "Not a directory"}

    client = _get_client()
    embedder = _get_embedder()
    
    with _collection_lock:
        try:
            collection = client.get_collection(collection_name)
            if force_reindex:
                client.delete_collection(collection_name)
                collection = client.create_collection(collection_name)
                with sqlite3.connect(META_DB_PATH) as conn:
                    conn.execute("DELETE FROM rag_files WHERE collection_name = ?", (collection_name,))
                    conn.commit()
        except Exception:
            collection = client.create_collection(collection_name)

        embedding_dim = embedder.get_sentence_embedding_dimension()
        try:
            sample = collection.get(limit=1, include=["embeddings"])
            if sample["embeddings"]:
                existing_dim = len(sample["embeddings"][0])
                if existing_dim != embedding_dim:
                    _log(f"[RAG] Embedding dimension mismatch: existing {existing_dim}, new {embedding_dim}. Recreating collection.")
                    client.delete_collection(collection_name)
                    collection = client.create_collection(collection_name)
                    with sqlite3.connect(META_DB_PATH) as conn:
                        conn.execute("DELETE FROM rag_files WHERE collection_name = ?", (collection_name,))
                        conn.commit()
        except Exception:
            pass

        indexed_files = _get_indexed_metadata(collection_name) if not force_reindex else {}
        
    supported_ext = {'.pdf', '.epub', '.docx', '.txt', '.md'}
    current_files: List[Path] = []
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in supported_ext:
            current_files.append(f)

    total = len(current_files)
    new_count = 0
    changed_count = 0
    deleted_count = 0
    errors = 0
    total_chunks_added = 0

    for file_path in current_files:
        src = str(file_path)
        current_hash, current_mtime = _file_hash_and_mtime(file_path)
        if not current_hash:
            errors += 1
            continue

        need_index = False
        if src not in indexed_files:
            need_index = True
            new_count += 1
        else:
            stored_hash, stored_mtime = indexed_files[src]
            if current_hash != stored_hash:
                need_index = True
                changed_count += 1

        if need_index:
            if src in indexed_files:
                _remove_file_from_collection(collection, src)
                _delete_file_metadata(src, collection_name)

            try:
                raw_text = extract_text(file_path)
                if not raw_text:
                    errors += 1
                    continue

                chunks = chunk_text(raw_text, CHUNK_SIZE, CHUNK_OVERLAP)
                if not chunks:
                    continue

                chunk_ids = []
                embeddings = []
                metadatas = []
                documents = []
                for idx, chunk in enumerate(chunks):
                    chunk_id = hashlib.md5(f"{src}_{idx}_{current_hash}".encode()).hexdigest()[:16]
                    embedding = embedder.encode(chunk).tolist()
                    metadata = {
                        "source": src,
                        "filename": file_path.name,
                        "chunk_index": idx,
                        "total_chunks": len(chunks),
                        "file_hash": current_hash,
                    }
                    chunk_ids.append(chunk_id)
                    embeddings.append(embedding)
                    metadatas.append(metadata)
                    documents.append(chunk)

                collection.add(
                    ids=chunk_ids,
                    embeddings=embeddings,
                    metadatas=metadatas,
                    documents=documents
                )
                total_chunks_added += len(chunks)

                _update_file_metadata(src, current_hash, collection_name, current_mtime)
            except Exception as e:
                _log(f"[RAG] Error indexing {file_path}: {e}")
                errors += 1

    if cleanup_deleted:
        current_sources = {str(f) for f in current_files}
        for src in indexed_files:
            if src not in current_sources:
                _remove_file_from_collection(collection, src)
                _delete_file_metadata(src, collection_name)
                deleted_count += 1

    conversation_memory.add(
        op="rag_index_folder",
        paths={"folder": str(root), "collection": collection_name},
        status="completed",
        dialog=dialog_id,
        context=f"Indexed: {new_count} new, {changed_count} changed, {deleted_count} deleted, {errors} errors"
    )

    return {
        "status": "success",
        "collection": collection_name,
        "files_scanned": total,
        "new_files": new_count,
        "changed_files": changed_count,
        "deleted_files": deleted_count,
        "chunks_added": total_chunks_added,
        "errors": errors,
        "incremental": incremental,
        "force_reindex": force_reindex,
        "cleanup_deleted": cleanup_deleted
    }

# ─── Поиск, статистика, список файлов ────────────────────────────────────────
def rag_search(query: str, collection_name: str = "default",
               top_k: int = TOP_K) -> Dict:
    if not query.strip():
        return {"status": "error", "message": "Empty query"}

    client = _get_client()
    embedder = _get_embedder()
    try:
        collection = client.get_collection(collection_name)
    except Exception:
        return {"status": "error", "message": f"Collection '{collection_name}' not found"}

    query_embedding = embedder.encode(query).tolist()
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        include=["documents", "metadatas", "distances"]
    )

    chunks = []
    if results["ids"] and results["ids"][0]:
        for i, doc_id in enumerate(results["ids"][0]):
            chunks.append({
                "id": doc_id,
                "text": results["documents"][0][i],
                "source": results["metadatas"][0][i].get("source", "unknown"),
                "filename": results["metadatas"][0][i].get("filename", "unknown"),
                "distance": results["distances"][0][i]
            })

    return {
        "status": "success",
        "query": query,
        "collection": collection_name,
        "chunks": chunks,
        "count": len(chunks)
    }

def rag_ask(question: str, collection_name: str = "default",
            top_k: int = TOP_K, model: Optional[str] = None) -> Dict:
    search_result = rag_search(question, collection_name, top_k)
    if search_result.get("status") != "success":
        return search_result

    chunks = search_result["chunks"]
    if not chunks:
        return {
            "status": "no_results",
            "question": question,
            "answer": "Не найдено релевантных фрагментов в индексе."
        }

    context_parts = []
    for i, ch in enumerate(chunks, 1):
        context_parts.append(f"[Фрагмент {i} из {ch['filename']}]\n{ch['text']}")
    context = "\n---\n".join(context_parts)

    prompt = f"""Ты — помощник, отвечающий на вопросы, используя только предоставленный контекст.
Если ответа нет в контексте, скажи: «В индексированных документах нет информации об этом».
При ответе указывай источник (имя файла).
ВОПРОС: {question}
КОНТЕКСТ:
{context}
ОТВЕТ:"""

    llm_response = query_llm(prompt, model=model)
    if not llm_response:
        llm_response = "Не удалось получить ответ от LLM. Проверьте LLM_ENDPOINT."

    dialog_id = dialog_ctx.get()
    conversation_memory.add(
        op="rag_ask",
        paths={"question": question[:100], "collection": collection_name},
        status="answered",
        dialog=dialog_id,
        context=f"Used {len(chunks)} chunks, answer length {len(llm_response)}"
    )

    return {
        "status": "success",
        "question": question,
        "answer": llm_response,
        "used_chunks": len(chunks),
        "collection": collection_name,
        "chunks": chunks
    }

def rag_stats(collection_name: str = "default") -> Dict:
    client = _get_client()
    try:
        coll = client.get_collection(collection_name)
        total_chunks = coll.count()
        with sqlite3.connect(META_DB_PATH) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM rag_files WHERE collection_name = ?", (collection_name,))
            unique_files = cur.fetchone()[0]
        return {
            "collection": collection_name,
            "chunks": total_chunks,
            "unique_files": unique_files,
            "embedding_model": EMBEDDING_MODEL,
            "chunk_size": CHUNK_SIZE,
            "db_path": RAG_DB_PATH
        }
    except Exception as e:
        return {"error": f"Collection '{collection_name}' not found or error: {e}"}

def rag_list_files(collection_name: str = "default", limit: int = 100) -> Dict:
    with sqlite3.connect(META_DB_PATH) as conn:
        cur = conn.execute(
            "SELECT source, file_hash, last_modified, indexed_at FROM rag_files WHERE collection_name = ?",
            (collection_name,)
        )
        rows = cur.fetchall()
    files = [{"source": r[0], "hash": r[1], "last_modified": r[2], "indexed_at": r[3]} for r in rows]
    total = len(files)
    if limit:
        files = files[:limit]
    return {
        "status": "success",
        "collection": collection_name,
        "total_files": total,
        "files": files
    }

def rag_list_collections() -> Dict:
    client = _get_client()
    collections = client.list_collections()
    return {"collections": [c.name for c in collections], "count": len(collections)}

def rag_delete_collection(collection_name: str) -> Dict:
    client = _get_client()
    try:
        client.delete_collection(collection_name)
        with sqlite3.connect(META_DB_PATH) as conn:
            conn.execute("DELETE FROM rag_files WHERE collection_name = ?", (collection_name,))
            conn.commit()
        return {"status": "deleted", "collection": collection_name}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ─── НОВАЯ ФУНКЦИЯ: добавление одного документа (с doc_id в метаданных) ──────────
def rag_add_document(content: str, metadata: Dict = None, collection_name: str = "default") -> Dict:
    """
    Добавляет один документ в RAG-коллекцию, сохраняя doc_id в метаданные каждого чанка.
    """
    d_id = dialog_ctx.get()
    # Генерируем уникальный doc_id на основе содержимого
    doc_id = hashlib.md5(content.encode()).hexdigest()[:16]
    
    client = _get_client()
    embedder = _get_embedder()
    
    with _collection_lock:
        try:
            collection = client.get_collection(collection_name)
        except Exception:
            collection = client.create_collection(collection_name)
        
        # Разбиваем текст на чанки
        chunks = chunk_text(content, CHUNK_SIZE, CHUNK_OVERLAP)
        if not chunks:
            return {"status": "error", "message": "Empty content after chunking"}
        
        chunk_ids = []
        embeddings = []
        metadatas = []
        documents = []
        
        for idx, chunk in enumerate(chunks):
            chunk_id = hashlib.md5(f"{doc_id}_{idx}".encode()).hexdigest()[:16]
            embedding = embedder.encode(chunk).tolist()
            meta = {
                "doc_id": doc_id,                     # <- ключевое поле для поиска документа
                "chunk_index": idx,
                "total_chunks": len(chunks),
                "source": f"inline_doc_{doc_id}",
                "filename": f"doc_{doc_id}.txt"
            }
            if metadata:
                meta.update(metadata)
            chunk_ids.append(chunk_id)
            embeddings.append(embedding)
            metadatas.append(meta)
            documents.append(chunk)
        
        # Добавляем в коллекцию
        collection.add(
            ids=chunk_ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=documents
        )
        
        # Сохраняем метаинформацию о документе в SQLite (опционально)
        with sqlite3.connect(META_DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rag_files (source, file_hash, collection_name, indexed_at, last_modified) VALUES (?, ?, ?, ?, ?)",
                (f"doc_{doc_id}", doc_id, collection_name, time.time(), time.time())
            )
            conn.commit()
    
    conversation_memory.add(
        op="rag_add_document",
        paths={"collection": collection_name},
        status="success",
        dialog=d_id,
        context=f"Added document with doc_id {doc_id}, chunks: {len(chunks)}"
    )
    
    return {
        "status": "success",
        "collection": collection_name,
        "doc_id": doc_id,
        "chunks_added": len(chunks),
        "message": "Document indexed. Use write_file_from_rag to save to disk."
    }

def rag_get_document(doc_id: str, collection_name: str = "default") -> Optional[str]:
    """
    Возвращает полный текст документа, проиндексированного в RAG, по его doc_id.
    """
    client = _get_client()
    try:
        collection = client.get_collection(collection_name)
        # Ищем все чанки с данным doc_id
        results = collection.get(where={"doc_id": doc_id}, include=["metadatas", "documents"])
        if not results["ids"]:
            return None
        # Сортируем по chunk_index
        chunks_with_index = []
        for meta, doc in zip(results["metadatas"], results["documents"]):
            idx = meta.get("chunk_index", 0)
            chunks_with_index.append((idx, doc))
        chunks_with_index.sort(key=lambda x: x[0])
        return "\n".join(doc for _, doc in chunks_with_index)
    except Exception as e:
        _log(f"[RAG] rag_get_document error: {e}")
        return None

# ─── Регистрация инструментов (обновлённая) ────────────────────────────────
def register_tools(server: BaseMCPServer):
    server.register_tool("rag_index_folder", {
        "description": "Индексировать папку с поддержкой инкрементального обновления",
        "inputSchema": {
            "type": "object",
            "properties": {
                "folder_path": {"type": "string"},
                "collection_name": {"type": "string", "default": "default"},
                "force_reindex": {"type": "boolean", "default": False},
                "incremental": {"type": "boolean", "default": True},
                "cleanup_deleted": {"type": "boolean", "default": False}
            },
            "required": ["folder_path"]
        }
    }, lambda **kw: rag_index_folder(
        kw["folder_path"], kw.get("collection_name", "default"),
        kw.get("force_reindex", False), kw.get("incremental", True),
        kw.get("cleanup_deleted", False)
    ))

    server.register_tool("rag_search", {
        "description": "Поиск релевантных фрагментов",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "collection_name": {"type": "string", "default": "default"},
                "top_k": {"type": "integer", "default": TOP_K}
            },
            "required": ["query"]
        }
    }, lambda **kw: rag_search(kw["query"], kw.get("collection_name", "default"), kw.get("top_k", TOP_K)))

    server.register_tool("rag_ask", {
        "description": "Задать вопрос, ответ через LLM",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "collection_name": {"type": "string", "default": "default"},
                "top_k": {"type": "integer", "default": TOP_K},
                "model": {"type": "string"}
            },
            "required": ["question"]
        }
    }, lambda **kw: rag_ask(kw["question"], kw.get("collection_name", "default"),
                            kw.get("top_k", TOP_K), kw.get("model")))

    server.register_tool("rag_stats", {
        "description": "Статистика индекса (чанки, уникальные файлы из мета-БД)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection_name": {"type": "string", "default": "default"}
            }
        }
    }, lambda **kw: rag_stats(kw.get("collection_name", "default")))

    server.register_tool("rag_list_files", {
        "description": "Список всех проиндексированных файлов в коллекции",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection_name": {"type": "string", "default": "default"},
                "limit": {"type": "integer", "default": 100}
            }
        }
    }, lambda **kw: rag_list_files(kw.get("collection_name", "default"), kw.get("limit", 100)))

    server.register_tool("rag_list_collections", {
        "description": "Список коллекций",
        "inputSchema": {"type": "object", "properties": {}}
    }, lambda **kw: rag_list_collections())

    server.register_tool("rag_delete_collection", {
        "description": "Удалить коллекцию",
        "inputSchema": {
            "type": "object",
            "properties": {"collection_name": {"type": "string"}},
            "required": ["collection_name"]
        }
    }, lambda **kw: rag_delete_collection(kw["collection_name"]))

    # Исправленный инструмент rag_add_document
    server.register_tool("rag_add_document", {
        "description": "Добавить один документ в RAG-коллекцию (текст, метаданные). Документ будет разбит на чанки и проиндексирован. Возвращает doc_id для последующего извлечения.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Текст документа"},
                "metadata": {"type": "object", "description": "Метаданные (например, source_file, title)"},
                "collection_name": {"type": "string", "default": "default", "description": "Имя коллекции"}
            },
            "required": ["content"]
        }
    }, lambda **kw: rag_add_document(kw["content"], kw.get("metadata"), kw.get("collection_name", "default")))

__mcp_plugin__ = {
    "name": "rag-engine",
    "version": "1.3",
    "description": "Инкрементальная индексация, метаданные в SQLite, проверка размерности эмбеддингов + исправленный rag_add_document",
    "dependencies": ["sentence_transformers", "chromadb", "pypdf", "docx", "ebooklib", "bs4"],
    "on_load": lambda: _log("[RAG] Engine v1.3 loaded. rag_add_document stores doc_id in chunk metadata."),
    "on_unload": lambda: _log("[RAG] Engine unloaded.")
}

if __name__ == "__main__":
    server = BaseMCPServer("rag-engine", "1.3")
    register_tools(server)
    server.run()