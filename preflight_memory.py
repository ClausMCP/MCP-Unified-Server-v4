import asyncio
from mcp_client import MCPClient  # ваш клиент для общения с MCP-сервером

async def handle_user_input(user_input: str):
    async with MCPClient() as client:
        # 1. Поиск в памяти
        mem = await client.call_tool("conversation_memory", {"action": "search", "query": user_input})
        docs = await client.call_tool("mempalace_search", {"query": user_input})
        rag = await client.call_tool("rag_search", {"query": user_input})
        
        # 2. Формируем расширенный контекст
        enriched_prompt = f"""
[ПАМЯТЬ ИЗ ПРОШЛЫХ ДИАЛОГОВ]:
{mem}

[СОХРАНЁННЫЕ ДОКУМЕНТЫ]:
{docs}

[ИНДЕКСИРОВАННЫЕ ЗНАНИЯ]:
{rag}

[НОВЫЙ ВОПРОС ПОЛЬЗОВАТЕЛЯ]:
{user_input}
"""
        # 3. Отправляем LLM (через MCP или напрямую)
        response = await client.call_llm(enriched_prompt)
        return response