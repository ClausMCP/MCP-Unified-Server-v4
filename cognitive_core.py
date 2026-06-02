#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cognitive Core v3.0 – полностью интегрирован с реальными модулями:
- mcp_planning_engine (планирование, replanning, иерархия)
- mcp_task_manager (асинхронные задачи, пауза/возобновление)
- mcp_world_model (факты, правила, forward chaining)
- mcp_hypothesis_engine (гипотезы, верификация)
- mcp_reflection_engine (рефлексия, если доступна)
- mcp_shared (память диалогов, логирование)

Цикл: Observe → Remember → Reflect → Hypothesize → Plan → Act → Evaluate
"""
import os
import sys
import json
import time
import threading
import traceback
from typing import Dict, List, Any, Optional

from mcp_shared import _log, BaseMCPServer, dialog_ctx, conversation_memory
from mcp_world_model import WorldModel, world_add_fact, world_run_forward_chaining
from mcp_hypothesis_engine import HypothesisEngine, hyp_create_hypothesis, hyp_list_hypotheses
from mcp_planning_engine import ActionPlanner, planning_create_plan, planning_get_plan, planning_list_plans
from mcp_task_manager import submit_task, task_status, task_list, TaskProgress

# Попытка импортировать рефлексию (если модуль отсутствует – заглушка)
try:
    from mcp_reflection_engine import run_reflection, get_reflections
    REFLECTION_AVAILABLE = True
except ImportError:
    REFLECTION_AVAILABLE = False
    _log("[CognitiveCore] mcp_reflection_engine not found, reflection disabled")
    def run_reflection(limit=100):
        _log("[CognitiveCore] Reflection skipped (module missing)")
        return {"status": "skipped"}
    def get_reflections(limit=20):
        return {"reflections": []}


class CognitiveCore:
    """
    Оркестратор когнитивного цикла. Использует глобальные экземпляры планировщика,
    менеджера задач, модели мира, гипотез.
    """
    def __init__(self):
        # Используем уже существующие глобальные экземпляры (из модулей)
        self.planner = ActionPlanner()          # из mcp_planning_engine (глобальный _planner)
        self.world = WorldModel()               # глобальный экземпляр из mcp_world_model
        self.hypothesis = HypothesisEngine()    # глобальный _hypothesis_engine
        self.memory = conversation_memory

        # Состояние цикла
        self.current_goal = None
        self.current_plan_id = None
        self.running = False
        self._stop_event = threading.Event()
        self._cycle_thread = None
        self._cycle_interval = 2.0

    def start(self):
        """Запускает фоновый цикл когнитивной обработки."""
        if self.running:
            return
        self.running = True
        self._stop_event.clear()
        self._cycle_thread = threading.Thread(target=self._cycle_loop, daemon=True, name="cognitive_cycle")
        self._cycle_thread.start()
        _log("[CognitiveCore] Cognitive cycle started")

    def stop(self):
        self.running = False
        self._stop_event.set()
        if self._cycle_thread:
            self._cycle_thread.join(timeout=5)
        _log("[CognitiveCore] Cognitive cycle stopped")

    def set_goal(self, goal: str, goal_type: str = "atomic", metadata: Dict = None) -> str:
        """
        Устанавливает новую цель и создаёт план через планировщик.
        Для atomic цели metadata должен содержать "tool_name" и опционально "tool_args",
        "precondition", "postcondition". Для composite – можно указать "steps" или полагаться
        на GoalManager (если он подключён).
        Возвращает plan_id.
        """
        # Временно сохраняем цель в локальном состоянии
        self.current_goal = {
            "description": goal,
            "goal_type": goal_type,
            "metadata": metadata or {}
        }

        # Генерируем шаги плана на основе цели
        steps = []
        if goal_type == "atomic":
            tool_name = metadata.get("tool_name") if metadata else None
            if not tool_name:
                _log("[CognitiveCore] Atomic goal requires tool_name in metadata")
                return None
            steps.append({
                "tool_name": tool_name,
                "args": metadata.get("tool_args", {}),
                "precondition": metadata.get("precondition", {}),
                "postcondition": metadata.get("postcondition", {}),
                "expected_effects": metadata.get("expected_effects", {})
            })
        elif goal_type == "composite":
            if metadata and "steps" in metadata:
                steps = metadata["steps"]
            else:
                _log("[CognitiveCore] Composite goal requires 'steps' in metadata")
                return None
        else:
            _log(f"[CognitiveCore] Unknown goal type: {goal_type}")
            return None

        if not steps:
            return None

        # Создаём план через планировщик (требуется goal_id, но у нас его нет – сгенерим)
        import hashlib
        goal_id = hashlib.md5(f"{goal}_{time.time()}".encode()).hexdigest()[:12]
        # Прямое создание плана через БД планировщика (без GoalManager)
        plan_id = f"plan_{goal_id}"
        ok = self.planner.db.create_plan(
            plan_id=plan_id,
            goal_id=goal_id,
            name=f"Plan for {goal[:50]}",
            steps=steps,
            parent_plan_id=None,
            hierarchy_level=0
        )
        if ok:
            self.current_plan_id = plan_id
            _log(f"[CognitiveCore] Created plan {plan_id} for goal: {goal}")
            return plan_id
        else:
            _log(f"[CognitiveCore] Failed to create plan for goal: {goal}")
            return None

    def _cycle_loop(self):
        """Основной цикл когнитивной обработки."""
        while not self._stop_event.is_set():
            try:
                if self.current_plan_id:
                    self._run_active_cycle()
                else:
                    self._idle_cycle()
            except Exception as e:
                _log(f"[CognitiveCore] Cycle error: {e}\n{traceback.format_exc()}")
            self._stop_event.wait(self._cycle_interval)

    def _run_active_cycle(self):
        """Одна итерация при активной цели/плане."""
        plan = self.planner.db.get_plan(self.current_plan_id)
        if not plan:
            self.current_plan_id = None
            return

        if plan["status"] in ("completed", "failed", "cancelled"):
            _log(f"[CognitiveCore] Plan {self.current_plan_id} finished with status {plan['status']}")
            self.current_plan_id = None
            return

        # 1. Observe – собираем новые данные из памяти
        observations = self._observe()

        # 2. Remember – обогащаем модель мира фактами
        self._remember(observations)

        # 3. Reflect – ищем противоречия
        reflections = self._reflect()
        if reflections:
            _log(f"[CognitiveCore] Found {len(reflections)} reflections")

        # 4. Hypothesize – генерируем гипотезы
        new_hypotheses = self._hypothesize(reflections)
        for hyp in new_hypotheses:
            _log(f"[CognitiveCore] New hypothesis: {hyp['statement'][:80]}")

        # 5. Plan – проверяем, нужно ли перепланирование
        if plan["status"] == "replanning":
            pass  # планировщик сам обработает
        elif plan["status"] in ("pending", "in_progress"):
            self._check_need_replan(plan, reflections)

        # 6. Act – планировщик сам выполняет шаги в фоне (через TaskManager)
        #    Здесь мы только запускаем планировщик, если он ещё не активен
        if plan["status"] == "pending":
            # Запускаем выполнение плана (метод планировщика _start_plan)
            self.planner._start_plan(self.current_plan_id)

    def _observe(self) -> List[Dict]:
        """Собирает последние записи из памяти диалога (последние 5 операций)."""
        thread = self.memory.get_dialog_thread(limit=5)
        observations = []
        for entry in thread.get("entries", []):
            if entry["op"] == "conversation":
                observations.append({
                    "type": "message",
                    "role": entry.get("paths", {}).get("role"),
                    "content": entry.get("context", "")
                })
            elif entry["op"] in ("tool_call", "tool_result"):
                observations.append({
                    "type": "tool",
                    "tool": entry.get("paths", {}).get("tool"),
                    "status": entry.get("status"),
                    "context": entry.get("context", "")
                })
        return observations

    def _remember(self, observations: List[Dict]):
        """Преобразует наблюдения в факты и добавляет их в World Model."""
        for obs in observations:
            if obs["type"] == "message" and obs["role"] == "user":
                # Можно извлечь ключевые факты через LLM, но упростим: добавляем как факт-наблюдение
                world_add_fact(obs["content"], confidence=0.6, source_tool="cognitive_core")
            elif obs["type"] == "tool" and obs["status"] == "completed":
                world_add_fact(f"Tool {obs['tool']} executed successfully", confidence=0.9, source_tool="cognitive_core")

    def _reflect(self) -> List[Dict]:
        """Запускает рефлексию и возвращает найденные конфликты/противоречия."""
        run_reflection(limit=100)
        result = get_reflections(limit=20)
        return result.get("reflections", [])

    def _hypothesize(self, reflections: List[Dict]) -> List[Dict]:
        """На основе рефлексий создаёт новые гипотезы."""
        new_hypotheses = []
        for ref in reflections:
            ref_type = ref.get("reflection_type", "")
            if ref_type in ("antonym_conflict", "version_conflict", "relation_conflict"):
                statement = f"Possible explanation: {ref.get('description', 'unknown conflict')}"
                hyp_result = hyp_create_hypothesis(
                    statement=statement,
                    confidence=0.4,
                    source_tool="cognitive_core"
                )
                hyp_id = hyp_result.get("hypothesis_id")
                if hyp_id:
                    new_hypotheses.append({"statement": statement, "hypothesis_id": hyp_id})
        return new_hypotheses

    def _check_need_replan(self, plan: Dict, reflections: List[Dict]):
        """Если рефлексия обнаружила критическое противоречие, инициируем replan."""
        for ref in reflections:
            if ref.get("reflection_type") in ("antonym_conflict", "relation_conflict"):
                # Проверяем, относится ли конфликт к текущему плану (упрощённо – всегда)
                _log(f"[CognitiveCore] Triggering replan for {plan['plan_id']} due to reflection")
                self.planner.replan_manually(plan["plan_id"], plan["current_step"], "reflection_conflict")
                break

    def _idle_cycle(self):
        """Фоновый режим без активной цели: рефлексия, гипотезы, forward chaining."""
        run_reflection(limit=50)
        reflections = get_reflections(limit=10).get("reflections", [])
        self._hypothesize(reflections)
        world_run_forward_chaining()

    # ------ MCP-инструменты ------
    def set_goal_tool(self, goal: str, goal_type: str = "atomic", metadata: Dict = None) -> Dict:
        plan_id = self.set_goal(goal, goal_type, metadata)
        if plan_id:
            return {"status": "success", "goal": goal, "plan_id": plan_id}
        else:
            return {"status": "error", "message": "Failed to create plan for goal"}

    def get_status(self) -> Dict:
        plan_info = {}
        if self.current_plan_id:
            plan = self.planner.db.get_plan(self.current_plan_id)
            if plan:
                plan_info = {
                    "plan_id": plan["plan_id"],
                    "status": plan["status"],
                    "current_step": plan["current_step"],
                    "steps_total": len(plan["steps"]) if "steps" in plan else 0
                }
        return {
            "running": self.running,
            "current_goal": self.current_goal,
            "plan": plan_info,
            "memory_stats": self.memory.get_stats() if hasattr(self.memory, "get_stats") else {}
        }

    def list_tasks(self, status: str = None, limit: int = 20) -> Dict:
        return task_list(status, limit)


# ========== Глобальный экземпляр когнитивного ядра ==========
_cognitive = CognitiveCore()
_cognitive.start()

# ---------- MCP-инструменты ----------
def cog_set_goal(goal: str, goal_type: str = "atomic", metadata: Dict = None) -> Dict:
    return _cognitive.set_goal_tool(goal, goal_type, metadata)

def cog_get_status() -> Dict:
    return _cognitive.get_status()

def cog_run_reflection() -> Dict:
    run_reflection(limit=100)
    return {"status": "reflection_completed"}

def cog_list_hypotheses() -> Dict:
    return hyp_list_hypotheses()

def cog_create_hypothesis(statement: str, verification_plan: List[Dict] = None) -> Dict:
    return hyp_create_hypothesis(statement, verification_plan=verification_plan or [], source_tool="cognitive_core")

def cog_list_plans(status: str = None) -> Dict:
    return planning_list_plans(status)

def cog_get_plan(plan_id: str) -> Dict:
    return planning_get_plan(plan_id)

def cog_list_tasks(status: str = None, limit: int = 20) -> Dict:
    return _cognitive.list_tasks(status, limit)

def cog_submit_task(tool_name: str, args: Dict, dialog_id: str = None) -> Dict:
    return submit_task(tool_name, args, dialog_id)

def cog_task_status(task_id: str) -> Dict:
    return task_status(task_id)

# MCP-сервер
server = BaseMCPServer("cognitive-core", "3.0")

server.register_tool("cog_set_goal", {
    "description": "Установить цель (atomic или composite). Для atomic укажите metadata.tool_name",
    "inputSchema": {
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "goal_type": {"type": "string", "enum": ["atomic", "composite"], "default": "atomic"},
            "metadata": {"type": "object"}
        },
        "required": ["goal"]
    }
}, lambda **kw: cog_set_goal(kw["goal"], kw.get("goal_type", "atomic"), kw.get("metadata")))

server.register_tool("cog_get_status", {
    "description": "Получить статус когнитивной системы",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: cog_get_status())

server.register_tool("cog_run_reflection", {
    "description": "Запустить цикл рефлексии вручную",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: cog_run_reflection())

server.register_tool("cog_list_hypotheses", {
    "description": "Список всех гипотез",
    "inputSchema": {"type": "object", "properties": {}}
}, lambda **kw: cog_list_hypotheses())

server.register_tool("cog_create_hypothesis", {
    "description": "Создать новую гипотезу",
    "inputSchema": {
        "type": "object",
        "properties": {
            "statement": {"type": "string"},
            "verification_plan": {"type": "array", "items": {"type": "object"}}
        },
        "required": ["statement"]
    }
}, lambda **kw: cog_create_hypothesis(kw["statement"], kw.get("verification_plan")))

server.register_tool("cog_list_plans", {
    "description": "Список планов (можно фильтровать по статусу)",
    "inputSchema": {
        "type": "object",
        "properties": {"status": {"type": "string"}}
    }
}, lambda **kw: cog_list_plans(kw.get("status")))

server.register_tool("cog_get_plan", {
    "description": "Детали плана по ID",
    "inputSchema": {
        "type": "object",
        "properties": {"plan_id": {"type": "string"}},
        "required": ["plan_id"]
    }
}, lambda **kw: cog_get_plan(kw["plan_id"]))

server.register_tool("cog_list_tasks", {
    "description": "Список асинхронных задач Task Manager",
    "inputSchema": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "limit": {"type": "integer", "default": 20}
        }
    }
}, lambda **kw: cog_list_tasks(kw.get("status"), kw.get("limit", 20)))

server.register_tool("cog_submit_task", {
    "description": "Запустить длительную операцию в фоне (через Task Manager)",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "args": {"type": "object"},
            "dialog_id": {"type": "string"}
        },
        "required": ["tool_name", "args"]
    }
}, lambda **kw: cog_submit_task(kw["tool_name"], kw.get("args", {}), kw.get("dialog_id")))

server.register_tool("cog_task_status", {
    "description": "Статус задачи по ID",
    "inputSchema": {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"]
    }
}, lambda **kw: cog_task_status(kw["task_id"]))

__mcp_plugin__ = {
    "name": "cognitive-core",
    "version": "3.0",
    "description": "Когнитивное ядро с интеграцией планировщика, менеджера задач, World Model, гипотез и рефлексии",
    "dependencies": ["planning-engine", "task-manager", "world-model", "hypothesis-engine"],
    "on_load": lambda: _log("[CognitiveCore] v3.0 loaded and running"),
    "on_unload": lambda: _cognitive.stop()
}

if __name__ == "__main__":
    server.run()