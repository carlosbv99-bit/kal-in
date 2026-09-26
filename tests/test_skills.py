"""
Tests de kernel/registry/skills.py: descubrimiento y carga de skills
(plugins) instaladas como carpetas con su propio skill.yaml.

Construye carpetas de skills sintéticas bajo tmp_path (no toca skills/
real del proyecto). El manifiesto (nombre/descripción/permisos/
parameters_schema) es el único que load_skills() lee — el `.py` de la
skill NUNCA se importa en este proceso, ni para leer nada de él (ver
docstring de kernel/registry/skills.py): por eso muchos de los tests
de "skill rota" de abajo confirman que la skill se REGISTRA igual
(porque el error solo puede detectarse ejecutando de verdad, dentro de
un contenedor) en vez de fallar en la carga, a diferencia de antes.
"""
from __future__ import annotations

import pytest

from kernel.lifecycle.docker_runner import DockerSandboxRunner
from kernel.lifecycle.executor import SandboxExecutor
from tests.conftest import requires_docker
from kernel.registry.registry import ToolRegistry
from kernel.registry.sandboxed_skill import SandboxedSkillTool


@pytest.fixture
def registry():
    # sandbox=object(): load_skills()/register_static_tool() nunca invocan
    # nada del sandbox al CARGAR una skill sin requirements — un dummy
    # alcanza y evita requerir Docker. Estos tests prueban descubrimiento
    # (manifiesto, enabled/disabled, errores), no ejecución real — para
    # eso ver los tests marcados @requires_docker abajo y
    # tests/test_sandboxed_skill.py.
    return ToolRegistry(sandbox=object())


VALID_TOOL_SOURCE = """
from sdk.skill import Tool
from sdk.artifacts import Artifact


class GreetTool(Tool):
    def execute(self, **kwargs) -> Artifact:
        return Artifact(modality="text", uri="", metadata={"summary": "hola"})
"""

# A propósito NO usa "GreetTool" ni ningún nombre que _make_skill espere
# por defecto — existe solo para demostrar que el módulo JAMÁS se
# importa al cargar (si se importara, esto reventaría el load_skills()
# de cualquier skill habilitada con este código).
RAISES_AT_MODULE_LEVEL_SOURCE = """
raise RuntimeError("esta skill nunca debería importarse, ni siquiera al leer su manifiesto")
"""

NOT_A_TOOL_SOURCE = """
class NotATool:
    pass
"""

BROKEN_SYNTAX_SOURCE = """
def esto no es python valido(
"""


def _make_skill(base_dir, name, tool_source, enabled=True, entry_point="tool:GreetTool", extra_manifest=""):
    skill_dir = base_dir / name
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text(tool_source, encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        f"""
name: {name}
description: "una skill de prueba"
version: "0.1.0"
entry_point: "{entry_point}"
enabled: {"true" if enabled else "false"}
{extra_manifest}
""",
        encoding="utf-8",
    )
    return skill_dir


def test_enabled_skill_is_loaded_and_registered(tmp_path, registry):
    _make_skill(tmp_path, "greeter", VALID_TOOL_SOURCE, enabled=True)

    results = registry.load_skills(skills_dir=tmp_path)

    assert len(results) == 1
    assert results[0].status == "loaded"
    tool = registry.get("greeter")
    assert tool is not None
    # Aislamiento real: lo registrado NUNCA es la clase real de la skill
    # (GreetTool) instanciada directa — es el wrapper que la ejecuta
    # dentro de un contenedor. El manifest se arma ENTERO desde
    # skill.yaml (nunca de la clase Python, que ni se importa) — el
    # nombre con el que se registra la tool es el mismo `name` de
    # skill.yaml, no uno independiente que la clase pudiera declarar.
    assert isinstance(tool, SandboxedSkillTool)
    assert tool.manifest.name == "greeter"
    assert tool.manifest.description == "una skill de prueba"  # viene del YAML, no del .py


@requires_docker
def test_end_to_end_execution_works_with_real_docker(tmp_path):
    registry = ToolRegistry(sandbox=SandboxExecutor(runner=DockerSandboxRunner()))
    _make_skill(tmp_path, "greeter", VALID_TOOL_SOURCE, enabled=True)

    registry.load_skills(skills_dir=tmp_path)
    artifact = registry.get("greeter").execute()

    assert artifact.metadata["summary"] == "hola"


def test_disabled_skill_is_never_imported_even_if_it_would_explode(tmp_path, registry):
    _make_skill(tmp_path, "bomba", RAISES_AT_MODULE_LEVEL_SOURCE, enabled=False)

    results = registry.load_skills(skills_dir=tmp_path)

    assert len(results) == 1
    assert results[0].status == "disabled"
    assert registry.get("bomba") is None


def test_enabled_skill_with_dangerous_module_level_code_never_executes_it_at_load(tmp_path, registry):
    """
    La garantía nueva y real de esta iteración: ni siquiera una skill
    HABILITADA con código a nivel de módulo que revienta se importa al
    cargar — antes (`_import_entry_point`) esto SÍ se ejecutaba en el
    proceso principal (y fallaba con "import_failed"); ahora
    load_skills() nunca abre el .py, así que la skill se registra
    normalmente. Si el módulo se hubiera importado de verdad, este
    RuntimeError habría propagado y este test ni llegaría a la
    aserción — el hecho de que "loaded" se alcance ES la prueba.
    """
    _make_skill(tmp_path, "bomba_habilitada", RAISES_AT_MODULE_LEVEL_SOURCE, enabled=True)

    results = registry.load_skills(skills_dir=tmp_path)

    assert results[0].status == "loaded"
    assert registry.get("bomba_habilitada") is not None


def test_directory_without_manifest_is_silently_ignored(tmp_path, registry):
    (tmp_path / "no_es_skill").mkdir()
    (tmp_path / "no_es_skill" / "README.md").write_text("no soy una skill", encoding="utf-8")
    _make_skill(tmp_path, "greeter", VALID_TOOL_SOURCE, enabled=True)

    results = registry.load_skills(skills_dir=tmp_path)

    assert len(results) == 1
    assert results[0].skill_dir == "greeter"


def test_invalid_yaml_manifest_reports_error_without_crashing(tmp_path, registry):
    skill_dir = tmp_path / "roto"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text(VALID_TOOL_SOURCE, encoding="utf-8")
    (skill_dir / "skill.yaml").write_text("esto: [no cierra el corchete", encoding="utf-8")

    results = registry.load_skills(skills_dir=tmp_path)

    assert len(results) == 1
    assert results[0].status == "invalid_manifest"


def test_manifest_missing_required_field_reports_error(tmp_path, registry):
    skill_dir = tmp_path / "incompleto"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text(VALID_TOOL_SOURCE, encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        'name: incompleto\ndescription: "falta entry_point"\nversion: "0.1.0"\nenabled: true\n',
        encoding="utf-8",
    )

    results = registry.load_skills(skills_dir=tmp_path)

    assert results[0].status == "invalid_manifest"


def test_path_traversal_in_manifest_name_reports_invalid_manifest_and_never_registers(tmp_path, registry):
    """
    K-1 (auditoría externa Likay-OS, 2026-09-26): un name de skill.yaml
    como "../../../../tmp/pwned" se usaba sin sanitizar para armar
    SandboxedSkillTool.artifact_dir, permitiendo escribir directorios
    fuera de data/artifacts/skills/. El directorio de la skill en sí
    ("malicioso") es un nombre normal — el ataque está en el CAMPO
    `name` de adentro del manifiesto, no en el nombre de la carpeta.
    """
    skill_dir = tmp_path / "malicioso"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text(VALID_TOOL_SOURCE, encoding="utf-8")
    (skill_dir / "skill.yaml").write_text(
        'name: "../../../../tmp/pwned"\ndescription: "d"\nversion: "0.1.0"\n'
        'entry_point: "tool:GreetTool"\nenabled: true\n',
        encoding="utf-8",
    )

    results = registry.load_skills(skills_dir=tmp_path)

    assert results[0].status == "invalid_manifest"
    assert registry.get("../../../../tmp/pwned") is None
    assert not (tmp_path.parent / "tmp" / "pwned").exists()


def test_unknown_permission_in_manifest_reports_invalid_manifest(tmp_path, registry):
    _make_skill(
        tmp_path, "permiso_raro", VALID_TOOL_SOURCE, enabled=True,
        extra_manifest='permissions:\n  - "este_permiso_no_existe"\n',
    )

    results = registry.load_skills(skills_dir=tmp_path)

    assert results[0].status == "invalid_manifest"
    assert "este_permiso_no_existe" in results[0].detail


# --- entry_point: solo se valida FORMATO y EXISTENCIA del archivo al
# cargar (sin ejecutar nada) — el resto (¿existe la clase?, ¿es un
# Tool?, ¿tiene errores de sintaxis?) se descubre recién al ejecutar
# de verdad, dentro de Docker. ---


def test_malformed_entry_point_format_is_entry_point_invalid(tmp_path, registry):
    _make_skill(tmp_path, "formato_malo", VALID_TOOL_SOURCE, enabled=True, entry_point="sin_dos_puntos")

    results = registry.load_skills(skills_dir=tmp_path)

    assert results[0].status == "entry_point_invalid"


def test_entry_point_referencing_missing_file_is_entry_point_invalid(tmp_path, registry):
    _make_skill(tmp_path, "archivo_inexistente", VALID_TOOL_SOURCE, enabled=True, entry_point="no_existe:Clase")

    results = registry.load_skills(skills_dir=tmp_path)

    assert results[0].status == "entry_point_invalid"
    assert "no_existe.py" in results[0].detail


@requires_docker
def test_entry_point_class_not_found_registers_fine_but_fails_at_execution(tmp_path):
    registry = ToolRegistry(sandbox=SandboxExecutor(runner=DockerSandboxRunner()))
    _make_skill(tmp_path, "clase_inexistente", VALID_TOOL_SOURCE, enabled=True, entry_point="tool:NoExiste")

    results = registry.load_skills(skills_dir=tmp_path)
    assert results[0].status == "loaded"  # el archivo existe, el formato es válido — se registra

    artifact = registry.get("clase_inexistente").execute()
    assert artifact.metadata["status"] == "error"
    assert "NoExiste" in artifact.metadata["stderr"]


@requires_docker
def test_entry_point_not_a_tool_subclass_fails_at_execution(tmp_path):
    registry = ToolRegistry(sandbox=SandboxExecutor(runner=DockerSandboxRunner()))
    _make_skill(tmp_path, "no_tool", NOT_A_TOOL_SOURCE, enabled=True, entry_point="tool:NotATool")

    results = registry.load_skills(skills_dir=tmp_path)
    assert results[0].status == "loaded"

    artifact = registry.get("no_tool").execute()
    assert artifact.metadata["status"] == "error"


@requires_docker
def test_broken_module_syntax_fails_at_execution(tmp_path):
    registry = ToolRegistry(sandbox=SandboxExecutor(runner=DockerSandboxRunner()))
    _make_skill(tmp_path, "sintaxis_rota", BROKEN_SYNTAX_SOURCE, enabled=True)

    results = registry.load_skills(skills_dir=tmp_path)
    assert results[0].status == "loaded"  # sintaxis nunca se revisa sin ejecutar

    artifact = registry.get("sintaxis_rota").execute()
    assert artifact.metadata["status"] == "error"


def test_one_broken_skill_does_not_prevent_others_from_loading(tmp_path, registry):
    _make_skill(tmp_path, "rota", VALID_TOOL_SOURCE, enabled=True, entry_point="no_existe:Clase")
    _make_skill(tmp_path, "greeter", VALID_TOOL_SOURCE, enabled=True)

    results = registry.load_skills(skills_dir=tmp_path)
    statuses = {r.skill_dir: r.status for r in results}

    assert statuses["rota"] == "entry_point_invalid"
    assert statuses["greeter"] == "loaded"
    assert registry.get("greeter") is not None


def test_nonexistent_skills_dir_returns_empty_list(tmp_path, registry):
    results = registry.load_skills(skills_dir=tmp_path / "no_existe")
    assert results == []


def test_list_skills_reflects_last_load(tmp_path, registry):
    _make_skill(tmp_path, "greeter", VALID_TOOL_SOURCE, enabled=True)
    registry.load_skills(skills_dir=tmp_path)

    listed = registry.list_skills()

    assert len(listed) == 1
    assert listed[0]["name"] == "greeter"  # nombre declarado en skill.yaml, no el del Tool interno
    assert listed[0]["status"] == "loaded"


def test_successful_load_is_audited(tmp_path, registry, monkeypatch):
    from audit.audit_log import audit_log

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    skills_dir = tmp_path / "skills_src"
    skills_dir.mkdir()
    _make_skill(skills_dir, "greeter", VALID_TOOL_SOURCE, enabled=True)

    registry.load_skills(skills_dir=skills_dir)

    entries = audit_log.tail(5)
    assert entries[0]["event_type"] == "skill_loaded"
    assert entries[0]["outcome"] == "success"


def test_failed_load_is_audited(tmp_path, registry, monkeypatch):
    from audit.audit_log import audit_log

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    skills_dir = tmp_path / "skills_src"
    skills_dir.mkdir()
    _make_skill(skills_dir, "rota", VALID_TOOL_SOURCE, enabled=True, entry_point="no_existe:Clase")

    registry.load_skills(skills_dir=skills_dir)

    entries = audit_log.tail(5)
    assert entries[0]["event_type"] == "skill_loaded"
    assert entries[0]["outcome"] == "failure"


def test_disabled_skill_is_not_audited(tmp_path, registry, monkeypatch):
    from audit.audit_log import audit_log

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    skills_dir = tmp_path / "skills_src"
    skills_dir.mkdir()
    _make_skill(skills_dir, "bomba", RAISES_AT_MODULE_LEVEL_SOURCE, enabled=False)

    registry.load_skills(skills_dir=skills_dir)

    assert audit_log.tail(5) == []


# --- requirements (imagen Docker derivada por skill) ---


def test_requirements_parsed_from_manifest(tmp_path, registry):
    # image_builder falso: esto prueba parseo de YAML, no build real —
    # sin esto, load_skills() dispara un `docker build` real (lento,
    # además deja una imagen kal-skill-con-deps huérfana en el host).
    class FakeImageBuilder:
        def build_or_get_image(self, name, requirements):
            return f"fake-image-for-{name}"

    _make_skill(
        tmp_path, "con_deps", VALID_TOOL_SOURCE, enabled=True,
        extra_manifest='requirements:\n  - "six==1.16.0"\n',
    )

    results = registry.load_skills(skills_dir=tmp_path, image_builder=FakeImageBuilder())

    assert results[0].manifest.requirements == ["six==1.16.0"]


def test_skill_without_requirements_never_touches_image_builder(tmp_path, registry):
    """
    Sin `requirements`, load_skills() no debe siquiera construir un
    SkillImageBuilder (que requiere Docker) — se usa MINIMAL_IMAGE
    directo. sandbox=object() en el fixture ya lo garantiza para
    execute(), esto confirma que también vale para la CARGA en sí.
    """
    _make_skill(tmp_path, "sin_deps", VALID_TOOL_SOURCE, enabled=True)

    class ExplodingImageBuilder:
        def build_or_get_image(self, *a, **kw):
            raise AssertionError("no debería llamarse sin requirements")

    results = registry.load_skills(skills_dir=tmp_path, image_builder=ExplodingImageBuilder())

    assert results[0].status == "loaded"


@requires_docker
def test_image_build_failure_reports_image_build_failed_status(tmp_path):
    registry = ToolRegistry(sandbox=object())
    _make_skill(
        tmp_path, "deps_rotas", VALID_TOOL_SOURCE, enabled=True,
        extra_manifest='requirements:\n  - "este-paquete-no-existe-seguro-98765"\n',
    )

    results = registry.load_skills(skills_dir=tmp_path)

    assert results[0].status == "image_build_failed"
    assert registry.get("deps_rotas") is None


# --- parameters_schema declarado en skill.yaml (nunca leído de la clase) ---


def test_parameters_schema_from_manifest_is_used_in_tool_manifest(tmp_path, registry):
    _make_skill(
        tmp_path, "con_schema", VALID_TOOL_SOURCE, enabled=True,
        extra_manifest=(
            "parameters_schema:\n"
            "  type: object\n"
            "  properties:\n"
            "    texto:\n"
            "      type: string\n"
            "  required:\n"
            "    - texto\n"
        ),
    )

    registry.load_skills(skills_dir=tmp_path)

    tool = registry.get("con_schema")
    assert tool.manifest.parameters_schema == {
        "type": "object",
        "properties": {"texto": {"type": "string"}},
        "required": ["texto"],
    }


def test_no_parameters_schema_defaults_to_empty_object(tmp_path, registry):
    _make_skill(tmp_path, "sin_schema", VALID_TOOL_SOURCE, enabled=True)

    registry.load_skills(skills_dir=tmp_path)

    tool = registry.get("sin_schema")
    assert tool.manifest.parameters_schema == {"type": "object", "properties": {}}


# --- firma de integridad del paquete (F3 del plan de marketplace, ver
# kernel/registry/skill_signing.py) ---


def test_unsigned_skill_loads_exactly_as_before(tmp_path, registry):
    """Regresión cero para skills existentes sin skill.sig (qr_code,
    system_info, image_via_kernel — ninguna tiene firma)."""
    _make_skill(tmp_path, "greeter", VALID_TOOL_SOURCE, enabled=True)

    results = registry.load_skills(skills_dir=tmp_path)

    assert results[0].status == "loaded"
    assert results[0].signature_status == "unsigned"
    assert registry.get("greeter") is not None


def test_correctly_signed_skill_loads_and_reports_verified(tmp_path, registry):
    from kernel.registry.skill_signing import SkillSigner

    skill_dir = _make_skill(tmp_path, "greeter", VALID_TOOL_SOURCE, enabled=True)
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)

    results = registry.load_skills(skills_dir=tmp_path)

    assert results[0].status == "loaded"
    assert results[0].signature_status == "verified"
    assert registry.get("greeter") is not None


def test_tampered_signature_prevents_loading_entirely(tmp_path, registry):
    from kernel.registry.skill_signing import SkillSigner

    skill_dir = _make_skill(tmp_path, "greeter", VALID_TOOL_SOURCE, enabled=True)
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)
    # Alterar el código DESPUÉS de firmar — el paquete fue modificado
    # desde que su autor lo firmó.
    (skill_dir / "tool.py").write_text(VALID_TOOL_SOURCE + "\n# alterado\n", encoding="utf-8")

    results = registry.load_skills(skills_dir=tmp_path)

    assert results[0].status == "signature_invalid"
    assert results[0].signature_status == "tampered"
    assert registry.get("greeter") is None  # fail closed: nunca se registra


def test_tampered_signature_is_audited_as_failure(tmp_path, registry, monkeypatch):
    from audit.audit_log import audit_log
    from kernel.registry.skill_signing import SkillSigner

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    skill_dir = _make_skill(tmp_path, "greeter", VALID_TOOL_SOURCE, enabled=True)
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)
    (skill_dir / "tool.py").write_text(VALID_TOOL_SOURCE + "\n# alterado\n", encoding="utf-8")

    registry.load_skills(skills_dir=tmp_path)

    entries = audit_log.tail(5)
    assert entries[0]["event_type"] == "skill_loaded"
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["context"]["status"] == "signature_invalid"


# --- set_skill_enabled() / audit_skill_enable_change() (F4: instalación guiada) ---


def test_set_skill_enabled_flips_false_to_true(tmp_path):
    from kernel.registry.skills import parse_manifest, set_skill_enabled

    skill_dir = _make_skill(tmp_path, "greeter", VALID_TOOL_SOURCE, enabled=False)

    set_skill_enabled(skill_dir, True)

    assert parse_manifest(skill_dir / "skill.yaml").enabled is True


def test_set_skill_enabled_flips_true_to_false(tmp_path):
    from kernel.registry.skills import parse_manifest, set_skill_enabled

    skill_dir = _make_skill(tmp_path, "greeter", VALID_TOOL_SOURCE, enabled=True)

    set_skill_enabled(skill_dir, False)

    assert parse_manifest(skill_dir / "skill.yaml").enabled is False


def test_set_skill_enabled_preserves_comments_and_other_fields(tmp_path):
    from kernel.registry.skills import parse_manifest, set_skill_enabled

    skill_dir = tmp_path / "greeter"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text(VALID_TOOL_SOURCE, encoding="utf-8")
    manifest_path = skill_dir / "skill.yaml"
    manifest_path.write_text(
        """name: greeter
description: "una skill de prueba"
version: "0.1.0"
entry_point: "tool:GreetTool"
# comentario importante que no debe desaparecer
enabled: false
permissions: []
""",
        encoding="utf-8",
    )

    set_skill_enabled(skill_dir, True)

    text = manifest_path.read_text(encoding="utf-8")
    assert "# comentario importante que no debe desaparecer" in text
    assert "enabled: true" in text
    manifest = parse_manifest(manifest_path)
    assert manifest.enabled is True
    assert manifest.permissions == []


def test_set_skill_enabled_inserts_missing_enabled_line(tmp_path):
    from kernel.registry.skills import parse_manifest, set_skill_enabled

    skill_dir = tmp_path / "greeter"
    skill_dir.mkdir()
    (skill_dir / "tool.py").write_text(VALID_TOOL_SOURCE, encoding="utf-8")
    manifest_path = skill_dir / "skill.yaml"
    manifest_path.write_text(
        'name: greeter\ndescription: "una skill de prueba"\nversion: "0.1.0"\nentry_point: "tool:GreetTool"\n',
        encoding="utf-8",
    )

    set_skill_enabled(skill_dir, True)

    assert parse_manifest(manifest_path).enabled is True


def test_audit_skill_enable_change_records_enabled_event(tmp_path, monkeypatch):
    from audit.audit_log import audit_log
    from kernel.registry.skills import audit_skill_enable_change

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")

    audit_skill_enable_change("greeter", "greeter_dir", True)

    entries = audit_log.tail(1)
    assert entries[0]["event_type"] == "skill_enabled"
    assert entries[0]["outcome"] == "success"
    assert entries[0]["context"] == {"skill": "greeter", "skill_dir": "greeter_dir", "source": "local"}


def test_audit_skill_enable_change_records_disabled_event(tmp_path, monkeypatch):
    from audit.audit_log import audit_log
    from kernel.registry.skills import audit_skill_enable_change

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")

    audit_skill_enable_change("greeter", "greeter_dir", False)

    entries = audit_log.tail(1)
    assert entries[0]["event_type"] == "skill_disabled"
    assert entries[0]["outcome"] == "success"


def test_audit_skill_enable_change_records_market_source(tmp_path, monkeypatch):
    from audit.audit_log import audit_log
    from kernel.registry.skills import audit_skill_enable_change

    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")

    audit_skill_enable_change("greeter", "greeter_dir", True, source="market")

    entries = audit_log.tail(1)
    assert entries[0]["context"]["source"] == "market"
