import sqlite3
import json

def search_memory(query):
    # Поиск в conversation_memory
    conn = sqlite3.connect("mcp_memory.db")
    c = conn.cursor()
    c.execute("SELECT context FROM entries WHERE context LIKE ? LIMIT 5", (f"%{query}%",))
    rows = c.fetchall()
    conn.close()
    return "\n".join([r[0] for r in rows])

user_query = input("Ваш вопрос: ")
memory_context = search_memory(user_query)

if memory_context:
    enriched = f"[ИНФОРМАЦИЯ ИЗ ПАМЯТИ]:\n{memory_context}\n\n[ВОПРОС]:\n{user_query}"
else:
    enriched = user_query

# Отправить enriched в модель (через LM Studio API)