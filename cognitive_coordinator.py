#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cognitive Coordinator – подписывается на события Cognitive Bus
и реализует кросс-модульные сценарии.
"""
from mcp_cognitive_bus import subscribe, publish
from mcp_shared import _log

# Ленивые импорты, чтобы избежать циклических зависимостей
def _get_world_model():
    from mcp_world_model import world_add_fact, world_run_forward_chaining
    return world_add_fact, world_run_forward_chaining

def _get_hypothesis_engine():
    from mcp_hypothesis_engine import hyp_create_hypothesis, hyp_update_status
    return hyp_create_hypothesis, hyp_update_status

def _get_goal_manager():
    from mcp_goal_manager import goal_update
    return goal_update

def _get_reflection():
    from mcp_reflection_engine import run_reflection
    return run_reflection

def on_hypothesis_verified(data):
    """Когда гипотеза подтверждена, добавляем её как факт в World Model."""
    _log(f"[Coordinator] Hypothesis verified: {data['statement'][:100]} (conf={data['confidence']})")
    add_fact, _ = _get_world_model()
    try:
        add_fact(data["statement"], confidence=data["confidence"], source_tool="hypothesis_engine")
        # Также запускаем forward chaining
        _, run_fc = _get_world_model()
        run_fc()
    except Exception as e:
        _log(f"[Coordinator] Failed to promote hypothesis to fact: {e}")

def on_hypothesis_rejected(data):
    """Когда гипотеза отвергнута, запускаем рефлексию, чтобы понять причину."""
    _log(f"[Coordinator] Hypothesis rejected: {data['hypothesis_id']} reason={data.get('reason')}")
    run_reflection = _get_reflection()
    try:
        run_reflection(limit=100)
    except Exception as e:
        _log(f"[Coordinator] Reflection trigger failed: {e}")

def on_goal_completed(data):
    """Когда цель завершена, запускаем рефлексию для анализа успеха."""
    _log(f"[Coordinator] Goal completed: {data['goal_id']} -> triggering reflection")
    run_reflection = _get_reflection()
    try:
        run_reflection(limit=50)
    except Exception as e:
        _log(f"[Coordinator] Reflection trigger failed: {e}")

def on_plan_failed(data):
    """Когда план провалился, создаём гипотезу о причине."""
    _log(f"[Coordinator] Plan failed: {data['plan_id']} -> generating hypothesis")
    create_hyp, _ = _get_hypothesis_engine()
    try:
        create_hyp(
            statement=f"Plan {data['plan_id']} failed due to {data.get('reason', 'unknown')}",
            confidence=0.4,
            verification_plan=[{"action": "analyze_logs", "params": {"plan_id": data['plan_id']}}],
            source_tool="coordinator"
        )
    except Exception as e:
        _log(f"[Coordinator] Hypothesis creation failed: {e}")

def on_fact_added(data):
    """При добавлении нового факта запускаем forward chaining в World Model."""
    _log(f"[Coordinator] Fact added: {data['statement'][:100]}")
    _, run_fc = _get_world_model()
    try:
        new_facts = run_fc()
        if new_facts:
            _log(f"[Coordinator] Forward chaining produced {len(new_facts)} new facts")
    except Exception as e:
        _log(f"[Coordinator] Forward chaining error: {e}")

def on_rule_added(data):
    """При добавлении нового правила также запускаем forward chaining."""
    _log(f"[Coordinator] Rule added: {data['rule_id']}")
    _, run_fc = _get_world_model()
    try:
        run_fc()
    except Exception as e:
        _log(f"[Coordinator] Forward chaining after rule error: {e}")

def register_coordinator():
    """Регистрирует все обработчики событий."""
    subscribe("hypothesis_verified", on_hypothesis_verified)
    subscribe("hypothesis_rejected", on_hypothesis_rejected)
    subscribe("goal_completed", on_goal_completed)
    subscribe("plan_failed", on_plan_failed)
    subscribe("fact_added", on_fact_added)
    subscribe("rule_added", on_rule_added)
    _log("[Coordinator] Registered all event handlers")

# Автоматическая регистрация при импорте
register_coordinator()