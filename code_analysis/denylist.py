"""
Denylist de nodos/llamadas AST prohibidos en código no confiable.

Esto es un filtro barato de primera línea, NO la garantía de seguridad
(esa la da el aislamiento real en sandbox/). Ver kernel/lifecycle/docker_runner.py
para la capa que realmente contiene el daño si esta lista falla en
detectar algo (código ofuscado, por ejemplo).

HUECO CONOCIDO Y ACEPTADO: acceso por subíndice a __builtins__ dentro de
un script ejecutado como __main__ (p.ej. `__builtins__['eval']`, aunque
`__builtins__` como módulo no es subscriptable en ese contexto — pero
`getattr(__builtins__, 'e'+'val')` sí funcionaría si getattr no estuviera
bloqueado, y variantes futuras de ofuscación seguirán apareciendo). Esta
lista NO puede ser exhaustiva contra un adversario que construye el AST
para evadirla a propósito — es un filtro heurístico, no una prueba
formal. Por eso tests/test_sandbox_escape_resistance.py valida que,
incluso cuando el código llega a ejecutarse sin pasar por esta
validación, el aislamiento de Docker (sin red, fs read-only, cap_drop
ALL, usuario no-root, namespaces separados) sigue conteniendo el daño.

OTRO HUECO CONOCIDO Y ACEPTADO (B-1 en kal, auditoría externa
2026-09-27, verificado empíricamente, portado acá vía
scripts/check_kernel_drift.py): renombrar un builtin prohibido a una
nueva variable (`e = eval; e('1+1')`) evade FORBIDDEN_CALLS por
completo — el chequeo compara el NOMBRE LITERAL en el sitio de la
llamada (`node.func.id`) contra la lista, sin ningún análisis de flujo
de datos/alias (qué objeto está REALMENTE atado a ese nombre). Cerrar
esto de verdad requeriría resolución de alias real, desproporcionado
para lo que este módulo es (un filtro barato de primera línea, no la
garantía) — se documenta en vez de fingir una cobertura que no existe;
ver test_ast_validator.py::test_aliasing_a_forbidden_builtin_evades_the_static_check
y, otra vez, test_sandbox_escape_resistance.py para la garantía real.
"""

# Nombres de funciones/builtins prohibidos en cualquier contexto
FORBIDDEN_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",          # se permite solo vía wrapper controlado, no builtin directo
    "input",
    # Estas cuatro son el vector típico para saltarse el chequeo de
    # FORBIDDEN_ATTRIBUTES: getattr(x, '__subclasses__'+'') no es un
    # nodo ast.Attribute, así que el visitor de atributos no lo ve.
    # Bloquearlas aquí cierra ese hueco a nivel de llamada.
    "getattr",
    "setattr",
    "vars",
    "globals",
    "locals",
}

# Módulos cuya importación está prohibida por defecto (requieren
# aprobación humana explícita en el manifiesto de la herramienta,
# ver kernel/registry/registry.py)
FORBIDDEN_IMPORTS = {
    "os",
    "sys",
    "subprocess",
    "socket",
    "ctypes",
    "shutil",
    "pickle",     # deserialización insegura
    "marshal",
    "importlib",  # permite importar cualquier módulo (incluidos os/subprocess)
                  # por nombre en runtime, evitando el chequeo de import literal
    # HALLAZGO REAL DE AUDITORÍA EXTERNA (B-1 en kal, 2026-09-27,
    # verificado empíricamente, portado acá vía
    # scripts/check_kernel_drift.py): `import builtins` no estaba
    # prohibido, y `builtins.__import__('os')`/`builtins.eval(...)` son
    # nodos ast.Call cuyo func es un ast.Attribute (`builtins.eval`),
    # no un ast.Name — visit_Call() solo compara node.func.id contra
    # FORBIDDEN_CALLS para nodos ast.Name, así que esta forma de
    # llamar a eval/exec/__import__ pasaba sin ser detectada.
    "builtins",
    # BUG REAL ENCONTRADO EN REVISIÓN (2026-08-24): "open" está en
    # FORBIDDEN_CALLS, pero pathlib.Path(...).write_text()/.read_text()/
    # .open() da el mismo acceso a filesystem sin pasar por ese nombre
    # de función — un `from pathlib import Path` no aparece como
    # ast.Call a "open", así que se colaba. Mismo criterio que "shutil".
    "pathlib",
}

# Atributos peligrosos (p.ej. acceso a __globals__, __subclasses__ para
# sandbox escapes clásicos de Python)
FORBIDDEN_ATTRIBUTES = {
    "__globals__",
    "__subclasses__",
    "__bases__",
    "__mro__",
    "__builtins__",
}
