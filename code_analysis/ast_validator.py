"""
Validación estática de código antes de que llegue al sandbox.

Uso típico:
    result = validate_code(source_code)
    if not result.is_safe:
        # rechazar / escalar a humano, NUNCA ejecutar
        ...

Esto valida sintaxis (vía ast.parse) y patrones prohibidos (denylist).
No sustituye al aislamiento del sandbox — ver docstring de denylist.py.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field

from code_analysis.denylist import (
    FORBIDDEN_ATTRIBUTES,
    FORBIDDEN_CALLS,
    FORBIDDEN_IMPORTS,
)


@dataclass
class ValidationResult:
    is_safe: bool
    is_valid_syntax: bool
    violations: list[str] = field(default_factory=list)
    syntax_error: str | None = None


class _DenylistVisitor(ast.NodeVisitor):
    def __init__(self):
        self.violations: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            self.violations.append(f"Llamada prohibida: {node.func.id}() en línea {node.lineno}")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root_module = alias.name.split(".")[0]
            if root_module in FORBIDDEN_IMPORTS:
                self.violations.append(f"Import prohibido: {alias.name} en línea {node.lineno}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.split(".")[0] in FORBIDDEN_IMPORTS:
            self.violations.append(f"Import prohibido: {node.module} en línea {node.lineno}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in FORBIDDEN_ATTRIBUTES:
            self.violations.append(f"Acceso a atributo prohibido: .{node.attr} en línea {node.lineno}")
        # HALLAZGO REAL DE AUDITORÍA EXTERNA (B-1 en kal, 2026-09-27,
        # verificado empíricamente, portado acá vía
        # scripts/check_kernel_drift.py): `__builtins__.eval(...)`/
        # `__builtins__.__import__(...)` NO pasaba por ningún chequeo —
        # no es un import literal (`__builtins__` ya está disponible
        # sin importar nada), ".eval"/".__import__" no están en
        # FORBIDDEN_ATTRIBUTES (solo el NOMBRE "__builtins__" lo está,
        # para cuando aparece como atributo DE otra cosa, p.ej.
        # `func.__builtins__`), y el nodo Call tiene func=ast.Attribute,
        # no ast.Name, así que FORBIDDEN_CALLS tampoco lo ve. Cualquier
        # acceso a un atributo CUYA BASE sea el nombre `__builtins__` es
        # en sí mismo sospechoso — se bloquea acá sin importar qué
        # atributo puntual sea.
        elif isinstance(node.value, ast.Name) and node.value.id == "__builtins__":
            self.violations.append(f"Acceso a __builtins__.{node.attr} en línea {node.lineno}")
        self.generic_visit(node)


def validate_code(source: str) -> ValidationResult:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return ValidationResult(is_safe=False, is_valid_syntax=False, syntax_error=str(e))

    visitor = _DenylistVisitor()
    visitor.visit(tree)

    return ValidationResult(
        is_safe=len(visitor.violations) == 0,
        is_valid_syntax=True,
        violations=visitor.violations,
    )
