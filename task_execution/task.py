"""Modelo de datos de una tarea."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ESCALATED = "escalated"  # circuit breaker abierto, esperando humano


@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    result: str | None = None
    error: str | None = None
