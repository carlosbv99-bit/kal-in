"""
VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (M-4/B-6,
2026-09-27): los directorios que guardan material sensible
(`data/keys/` — claves de firma, token admin, concesiones persistidas
de acceso a filesystem/red — y `logs/` — el log de auditoría) se
creaban con `Path.mkdir(parents=True, exist_ok=True)` sin fijar
permisos, quedando con lo que el umask del proceso diera (típicamente
0775 en un `venv`/CI) — cualquier OTRO usuario del mismo grupo podía
escribir ahí: reemplazar una clave privada de firma (las firmas
siguientes serían del atacante), escribir sus propias concesiones de
acceso (incluida una con `resource_key: null`, que autoriza CUALQUIER
recurso para esa skill/scope/acción), o alterar el log de auditoría
(el hash-chain detecta la manipulación, pero no la impide).
"""
from __future__ import annotations

from pathlib import Path


def ensure_private_dir(path: Path) -> None:
    """
    Crea `path` si no existe y fuerza permisos 0700 (solo el dueño lee/
    escribe/entra) — tanto si se acaba de crear como si ya existía con
    permisos más laxos de una instalación anterior a este fix.
    """
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
