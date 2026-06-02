#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Rate Limiter + Circuit Breaker v1.3.2
- Исправлена очистка БД (cursor.rowcount вместо conn.rowcount)
- Убраны параметры level из вызовов _log
- Улучшен health-check
"""

import time
import threading
import sqlite3
import json
from pathlib import Path

from mcp_shared import _log

DB_PATH = Path("mcp_rate_limiter.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rate_history (
            service TEXT PRIMARY KEY,
            timestamps TEXT,
            failures INTEGER DEFAULT 0,
            last_failure REAL DEFAULT 0,
            last_success REAL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

init_db()


class PersistentRateLimiter:
    def __init__(self, max_calls: int = 25, window_sec: float = 60.0):
        self.max_calls = max_calls
        self.window = window_sec
        self.lock = threading.Lock()

    def allow(self, service: str) -> tuple[bool, float | None]:
        now = time.monotonic()
        conn = sqlite3.connect(DB_PATH)
        try:
            with self.lock:
                row = conn.execute(
                    "SELECT timestamps FROM rate_history WHERE service = ?", (service,)
                ).fetchone()

                timestamps = json.loads(row[0]) if row and row[0] else []

                timestamps = [ts for ts in timestamps if ts > now - self.window]

                if len(timestamps) >= self.max_calls:
                    retry_after = (timestamps[0] + self.window - now) if timestamps else self.window
                    return False, round(max(0.0, retry_after), 1)

                timestamps.append(now)
                conn.execute(
                    "REPLACE INTO rate_history (service, timestamps) VALUES (?, ?)",
                    (service, json.dumps(timestamps))
                )
                conn.commit()
                return True, None
        finally:
            conn.close()


class EnhancedCircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 45.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.lock = threading.Lock()
        self._start_health_check()

    def _start_health_check(self):
        def health_check_loop():
            while True:
                time.sleep(300)  # каждые 5 минут
                self._perform_health_check()

        thread = threading.Thread(target=health_check_loop, daemon=True)
        thread.start()

    def _perform_health_check(self):
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                "SELECT service, failures, last_failure FROM rate_history"
            ).fetchall()
            
            for service, failures, last_failure in rows:
                if failures >= self.failure_threshold:
                    now = time.monotonic()
                    if now - (last_failure or 0) > self.recovery_timeout * 2:
                        conn.execute(
                            "UPDATE rate_history SET failures = 0 WHERE service = ?", 
                            (service,)
                        )
                        _log(f"[HealthCheck] Сервис {service} восстановлен")
            conn.commit()
        finally:
            conn.close()

    def can_execute(self, service: str) -> bool:
        conn = sqlite3.connect(DB_PATH)
        try:
            row = conn.execute(
                "SELECT failures, last_failure FROM rate_history WHERE service = ?", (service,)
            ).fetchone()
            if not row:
                return True
            failures, last_failure = row
            if failures < self.failure_threshold:
                return True
            # Если превышен порог, проверяем, не прошло ли время восстановления
            now = time.monotonic()
            if now - (last_failure or 0) > self.recovery_timeout:
                return True
            return False
        finally:
            conn.close()

    def record_success(self, service: str):
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "UPDATE rate_history SET failures = 0, last_success = ? WHERE service = ?",
                (time.monotonic(), service)
            )
            conn.commit()
        finally:
            conn.close()

    def record_failure(self, service: str):
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "UPDATE rate_history SET failures = failures + 1, last_failure = ? WHERE service = ?",
                (time.monotonic(), service)
            )
            conn.commit()
        finally:
            conn.close()


# ====================== ГЛОБАЛЬНЫЕ ЭКЗЕМПЛЯРЫ ======================
rate_limiter = PersistentRateLimiter(max_calls=25, window_sec=60)
circuit_breaker = EnhancedCircuitBreaker(failure_threshold=5, recovery_timeout=45)


def safe_call(service: str, func, *args, **kwargs):
    allowed, retry_after = rate_limiter.allow(service)
    
    if not allowed:
        return {
            "error": f"Rate limit exceeded for {service}",
            "retry_after": retry_after,
            "message": f"Повторите через {retry_after} секунд"
        }

    if not circuit_breaker.can_execute(service):
        return {
            "error": f"Сервис {service} временно отключён (Circuit Breaker)",
            "retry_after": 30
        }

    try:
        result = func(*args, **kwargs)
        circuit_breaker.record_success(service)
        return result
    except Exception as e:
        circuit_breaker.record_failure(service)
        return {"error": str(e), "service": service}


# ====================== ИСПРАВЛЕННАЯ ОЧИСТКА БД ======================
def cleanup_old_records(max_age_hours: int = 24):
    """Очистка старых записей"""
    cutoff = time.time() - (max_age_hours * 3600)
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute("""
            DELETE FROM rate_history 
            WHERE (last_failure < ? OR last_failure IS NULL)
              AND (last_success < ? OR last_success IS NULL)
        """, (cutoff, cutoff))
        deleted = cursor.rowcount
        if deleted > 0:
            _log(f"[DB Cleanup] Удалено {deleted} старых записей rate limiter")
        conn.commit()
    except Exception as e:
        _log(f"[DB Cleanup] Ошибка очистки: {e}")
    finally:
        conn.close()


# Запуск очистки при старте
cleanup_old_records()

def start_cleanup_scheduler():
    """Планировщик очистки"""
    while True:
        time.sleep(21600)  # каждые 6 часов
        cleanup_old_records()

cleanup_thread = threading.Thread(target=start_cleanup_scheduler, daemon=True)
cleanup_thread.start()

_log("[RateLimiter] Инициализирован v1.3.2 с исправленной очисткой БД")