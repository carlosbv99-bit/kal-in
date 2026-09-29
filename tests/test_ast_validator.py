from code_analysis.ast_validator import validate_code


def test_safe_code_passes():
    result = validate_code("x = 1 + 2\nprint(x)")
    assert result.is_safe
    assert result.is_valid_syntax


def test_syntax_error_detected():
    result = validate_code("def broken(:\n    pass")
    assert not result.is_valid_syntax
    assert not result.is_safe


def test_forbidden_call_detected():
    result = validate_code("eval('1+1')")
    assert not result.is_safe
    assert any("eval" in v for v in result.violations)


def test_forbidden_import_detected():
    result = validate_code("import os\nos.system('ls')")
    assert not result.is_safe


def test_forbidden_attribute_detected():
    result = validate_code("x.__globals__")
    assert not result.is_safe


def test_getattr_bypass_of_attribute_check_is_blocked():
    """
    getattr(x, '__subclasses__') no genera un nodo ast.Attribute, así
    que sin bloquear getattr explícitamente, este bypass pasaría el
    chequeo de FORBIDDEN_ATTRIBUTES sin ser detectado.
    """
    result = validate_code("getattr(object(), '__subclasses__')")
    assert not result.is_safe


def test_setattr_is_blocked():
    result = validate_code("setattr(object(), 'x', 1)")
    assert not result.is_safe


def test_globals_locals_vars_are_blocked():
    for call in ("globals()", "locals()", "vars()"):
        result = validate_code(call)
        assert not result.is_safe, f"{call} debería estar bloqueado"


def test_importlib_dynamic_import_is_blocked():
    """
    importlib.import_module('os') permite importar cualquier módulo por
    nombre en runtime, evadiendo el chequeo de `import os` literal.
    """
    result = validate_code("import importlib\nimportlib.import_module('os')")
    assert not result.is_safe


def test_pathlib_file_io_bypass_is_blocked():
    """
    BUG REAL ENCONTRADO EN REVISIÓN (2026-08-24, auditoría de un
    colaborador): "open" está en FORBIDDEN_CALLS, pero
    pathlib.Path(...).write_text()/.read_text() da el mismo acceso a
    filesystem sin generar un ast.Call a "open" — se colaba.
    """
    result = validate_code("from pathlib import Path\nPath('/etc/passwd').read_text()")
    assert not result.is_safe


def test_calling_a_forbidden_builtin_via_the_builtins_module_is_blocked():
    """
    `builtins.__import__('os')`: el nodo Call tiene func=ast.Attribute
    (`builtins.__import__`), no ast.Name — visit_Call() solo compara
    node.func.id para nodos Name, así que esta forma pasaba sin ser
    detectada (B-1 en kal, auditoría externa 2026-09-27, portado acá
    vía scripts/check_kernel_drift.py). Cerrado agregando "builtins" a
    FORBIDDEN_IMPORTS (sin poder importar el módulo, no se puede
    llegar a su atributo).
    """
    result = validate_code("import builtins\nbuiltins.__import__('os')")
    assert not result.is_safe


def test_calling_a_forbidden_builtin_via_bare_dunder_builtins_is_blocked():
    """
    `__builtins__.eval(...)`: disponible SIN ningún import (es el
    namespace global implícito), así que bloquear "builtins" como
    import no alcanza acá — el propio nombre __builtins__ como BASE de
    un atributo ahora se bloquea sin importar qué atributo puntual sea.
    """
    result = validate_code("__builtins__.eval('1+1')")
    assert not result.is_safe

    result2 = validate_code("__builtins__.__import__('os')")
    assert not result2.is_safe


def test_aliasing_a_forbidden_builtin_evades_the_static_check():
    """
    Hueco documentado, NO corregido (ver denylist.py): `e = eval;
    e('1+1')` evade FORBIDDEN_CALLS porque el chequeo compara el
    nombre literal en el sitio de la llamada, sin resolver a qué
    objeto está atado ese nombre — resolverlo de verdad requeriría
    análisis de alias real, desproporcionado para un filtro barato de
    primera línea. La garantía real es Docker (ver
    test_sandbox_escape_resistance.py), no este validador.
    """
    result = validate_code("e = eval\ne('1+1')")
    assert result.is_safe  # documentado como hueco conocido, no un bypass "corregido"


def test_known_residual_gap_documented_not_silently_fixed():
    """
    Este test documenta (no oculta) un hueco conocido: el chequeo actual
    no analiza literales de string para detectar construcción dinámica
    de nombres de atributos prohibidos por concatenación pura de strings
    sin pasar por getattr/setattr (p.ej. una f-string usada luego en un
    mecanismo distinto). Se deja explícito aquí para que cualquier
    cambio futuro al validador sea deliberado, no una regresión
    silenciosa. La garantía real contra esto vive en el aislamiento de
    Docker, no en este validador — ver test_sandbox_escape_resistance.py.
    """
    # Sin getattr/setattr/eval/exec de por medio, no hay forma directa
    # de convertir una string construida dinámicamente en un acceso a
    # atributo o llamada en Python puro — por eso este caso es más
    # teórico que explotable, pero se documenta igual.
    result = validate_code("s = '__subclasses__'\nprint(s)")
    assert result.is_safe  # esto es código inocuo real, no un bypass


def test_class_traversal_trick_blocked_by_static_layer():
    """
    ().__class__.__bases__[0].__subclasses__() es el escape clásico de
    Python (llegar a todas las subclases cargadas, incluida potencialmente
    subprocess.Popen, sin ningún import literal). Usa nodos ast.Attribute
    con nombres literales (__class__, __bases__, __subclasses__), que sí
    están en FORBIDDEN_ATTRIBUTES. Confirma que el filtro barato captura
    al menos la variante no ofuscada de este ataque conocido.
    """
    code = "().__class__.__bases__[0].__subclasses__()"
    result = validate_code(code)
    assert not result.is_safe
