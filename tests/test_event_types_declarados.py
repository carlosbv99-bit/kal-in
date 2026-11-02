"""
Test: todo `event_type="..."` que se registra en el código tiene que estar
declarado en el `Literal` `EventType` de `audit/audit_log.py`.

Motivo real (encontrado el 2026-11-02): `sandbox_workdir_no_visible` se
registraba desde `kernel/lifecycle/docker_runner.py` y **no** estaba en el
Literal. `record()` no valida en runtime —el Literal es chequeo estático— así
que no fallaba nada: el Literal quedaba desalineado con la realidad y perdía
la única razón de existir. El HISTORIAL ya documenta el mismo hallazgo una vez
antes (tres `event_type` usados en `sandboxed_skill.py` que no estaban
declarados), y volvió a pasar: por eso ahora hay un test en vez de una nota.

Se compara por AST (no por regex) para no confundir un `event_type` real con
uno mencionado en un comentario o un docstring.
"""
from __future__ import annotations

import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
ARCHIVO_AUDIT = RAIZ / "audit" / "audit_log.py"
IGNORAR = {".venv", "__pycache__", ".git", "data", "logs"}


def _declarados() -> set[str]:
    arbol = ast.parse(ARCHIVO_AUDIT.read_text(encoding="utf-8"))
    for nodo in arbol.body:
        if isinstance(nodo, ast.Assign) and any(
            isinstance(destino, ast.Name) and destino.id == "EventType"
            for destino in nodo.targets
        ):
            corte = nodo.value
            assert isinstance(corte, ast.Subscript), "EventType no es un Literal[...]"
            return {
                elemento.value
                for elemento in corte.slice.elts
                if isinstance(elemento, ast.Constant) and isinstance(elemento.value, str)
            }
    raise AssertionError("no encontré `EventType = Literal[...]` en audit/audit_log.py")


def _usados() -> dict[str, set[str]]:
    usados: dict[str, set[str]] = {}
    for ruta in RAIZ.rglob("*.py"):
        if IGNORAR & set(ruta.parts) or "tests" in ruta.parts:
            continue
        try:
            arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            # SOLO construcciones de AuditEvent: `ChainBreak(event_type=...)` usa
            # marcadores de diagnóstico ("<línea corrupta>") que no son eventos
            # registrados y no tienen por qué estar en el Literal.
            callee = nodo.func
            nombre = (
                callee.id if isinstance(callee, ast.Name)
                else callee.attr if isinstance(callee, ast.Attribute)
                else ""
            )
            if nombre != "AuditEvent":
                continue
            for clave in nodo.keywords:
                valor = clave.value
                if (
                    clave.arg == "event_type"
                    and isinstance(valor, ast.Constant)
                    and isinstance(valor.value, str)
                ):
                    usados.setdefault(valor.value, set()).add(
                        str(ruta.relative_to(RAIZ))
                    )
    return usados


def test_todo_event_type_usado_esta_declarado_en_el_literal():
    declarados = _declarados()
    usados = _usados()

    faltantes = {
        tipo: sorted(archivos)
        for tipo, archivos in usados.items()
        if tipo not in declarados
    }

    assert faltantes == {}, (
        "Hay `event_type` que se registran pero no están declarados en "
        f"`EventType` (audit/audit_log.py): {faltantes}. Agregalos al Literal: "
        "no se valida en runtime, así que la desalineación es silenciosa."
    )
