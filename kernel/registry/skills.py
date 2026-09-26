"""
Sistema de skills (plugins): habilidades instalables como carpetas bajo
skills/<nombre>/, cada una con su manifiesto (skill.yaml) y su propio
código Tool.

Diferencia de confianza con las herramientas dinámicas que el AGENTE
propone (kernel/registry/registry.py::propose_dynamic_tool, con sandbox
y aprobación humana): una skill la instala un HUMANO copiando una carpeta
al proyecto — mismo nivel de confianza que los adaptadores de primera
parte (image_gen.py, browser.py). Por eso el CÓDIGO de la skill (import
de os/subprocess/etc. dentro de su propio .py) no pasa por el validador
AST de code_analysis/denylist.py — ese denylist bloquea os/subprocess/etc.
pensando en código no confiable ejecutándose sin aislamiento real; una
skill real (leer un Excel, llamar una API) necesita esas capacidades
legítimamente para hacer algo útil.

El control real es DENY-BY-DEFAULT a nivel de manifiesto: cada skill.yaml
trae enabled: false, y una skill nunca se importa —ni una sola línea de su
código se ejecuta— hasta que un humano edita el manifiesto a mano a
enabled: true. Una skill rota (manifiesto inválido, import que falla,
entry_point que no es una Tool) nunca tira abajo el arranque del agente ni
afecta a las demás skills — se reporta como fallo auditado y se sigue.

AISLAMIENTO REAL DE EJECUCIÓN: el `.py` de una skill NUNCA se importa
en este proceso, bajo ninguna circunstancia — ni para leer su
manifiesto, ni por supuesto para instanciar la clase o llamar a
`.execute()`. Todo lo que `load_skills()` necesita
(nombre/descripción/permisos/parameters_schema) se declara en el
propio `skill.yaml`, que es la ÚNICA fuente de verdad — `entry_point`
es solo un string ("archivo:Clase") que recién se resuelve DENTRO del
contenedor, al momento de ejecutar de verdad (ver
kernel/lifecycle/skill_runner.py). Lo que queda registrado en el ToolRegistry es
un `SandboxedSkillTool` (kernel/registry/sandboxed_skill.py): cada
`.execute()` corre en un contenedor Docker efímero, con las mismas
garantías que ya tiene `run_code` (sin red por defecto, filesystem
read-only salvo /workspace, límites de recursos, usuario sin
privilegios). Si `skill.yaml` declara `requirements` (paquetes de pip),
se construye/reusa una imagen Docker derivada solo para esa skill (ver
kernel/lifecycle/skill_image_builder.py); sin requirements, se usa la imagen ya
endurecida `kal-sandbox-minimal`.

Trade-off real y aceptado: al no importar nunca el `.py`, `load_skills()`
ya no puede detectar a tiempo de carga una sintaxis rota, una clase de
`entry_point` inexistente, o que no sea subclase de `Tool` — esos casos
se descubren recién en la PRIMERA ejecución real (dentro del
contenedor), donde ya se manejan con gracia (error prolijo, nunca un
crash). Sí se sigue validando en la carga, sin ejecutar nada: que el
formato de `entry_point` sea válido y que el archivo referenciado
exista.

INTEGRIDAD DEL PAQUETE (F3 del plan de marketplace, ver
kernel/registry/skill_signing.py): "un humano leyó el código antes de
poner enabled: true" no dice nada sobre si ESE código es el mismo que
el autor original publicó — un paquete de skill de un tercero puede
alterarse entre que se publica y que este usuario lo instala. Antes de
cargar cualquier skill habilitada, se verifica su firma (si tiene una
— `skill.sig`, opcional, no rompe skills existentes sin firmar). Una
firma que no verifica contra el contenido ACTUAL de la carpeta
("tampered") rechaza la carga por completo, fail closed — nunca se
llega ni a validar `entry_point`. Alcance deliberadamente acotado: esto
prueba integridad ("¿llegó intacto?"), NUNCA autoridad ("¿debería
confiar en este autor?") — eso requeriría un registro/reputación real,
fuera de esta iteración.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from audit.audit_log import AuditEvent, audit_log
from sdk.skill import ToolManifest
from sdk.permissions import Permission
from kernel.registry.skill_signing import verify_skill_signature
from utils.logger import get_logger

if TYPE_CHECKING:
    from kernel.lifecycle.skill_image_builder import SkillImageBuilder
    from kernel.registry.registry import ToolRegistry

logger = get_logger(__name__)

DEFAULT_SKILLS_DIR = Path("skills")
MANIFEST_FILENAME = "skill.yaml"


@dataclass
class SkillManifest:
    name: str
    description: str
    version: str
    entry_point: str  # "<archivo_sin_.py>:<NombreClase>", relativo a la carpeta de la skill
    enabled: bool = False
    # Fuente de verdad real del ToolManifest con el que se registra la
    # skill (ver load_skills() más abajo) — nunca se lee de la clase
    # Python, justamente para no tener que importarla nunca en el host.
    permissions: list[str] = field(default_factory=list)
    # Specs de pip (p.ej. "qrcode==7.4.2") que esta skill necesita además
    # de la stdlib. Si no está vacío, se construye (o reusa, cacheada por
    # hash de esta lista) una imagen Docker derivada de
    # kernel/lifecycle/images/minimal/ solo para esta skill — ver
    # kernel/lifecycle/skill_image_builder.py. Vacío por defecto: la mayoría de las
    # skills (como system_info) no necesita nada más que stdlib.
    requirements: list[str] = field(default_factory=list)
    # JSON Schema de los argumentos de execute(), expuesto al LLM (ver
    # agent_core/llm/agent_loop.py) — antes se leía de
    # tool_cls.manifest.parameters_schema (requería importar la skill en
    # el host); ahora es el propio skill.yaml el que lo declara.
    parameters_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    # Métodos del Kernel Service Bus ("<servicio>.<acción>", p.ej.
    # "image.generate") que esta skill puede llamar — ver
    # kernel/__init__.py y sdk/context.py.
    # Allowlist plana: cualquier llamado a un método no listado acá se
    # rechaza ANTES de tocar el servicio real (fail closed), aunque el
    # servicio exista. Vacío por defecto: la mayoría de las skills no
    # necesita ningún servicio del kernel.
    kernel_services: list[str] = field(default_factory=list)


@dataclass
class SkillStatus:
    skill_dir: str
    manifest: SkillManifest | None
    # "disabled" | "loaded" | "invalid_manifest" | "entry_point_invalid" |
    # "image_build_failed" | "signature_invalid"
    status: str
    detail: str = ""
    # "unsigned" | "verified" | "tampered" (ver kernel/registry/skill_signing.py
    # — F3 del plan de marketplace: integridad del paquete, no confianza en
    # el autor). "unsigned" para toda skill sin skill.sig, compatibilidad
    # total con las skills existentes.
    signature_status: str = "unsigned"


def parse_manifest(manifest_path: Path) -> SkillManifest:
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    return SkillManifest(
        name=raw["name"],
        description=raw["description"],
        version=str(raw["version"]),
        entry_point=raw["entry_point"],
        enabled=bool(raw.get("enabled", False)),
        permissions=list(raw.get("permissions", [])),
        requirements=list(raw.get("requirements", [])),
        parameters_schema=raw.get("parameters_schema", {"type": "object", "properties": {}}),
        kernel_services=list(raw.get("kernel_services", [])),
    )


_VALID_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _validate_skill_name(name: str) -> str | None:
    """
    VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
    2026-09-26), K-1: `manifest.name` (el `name` crudo de skill.yaml)
    se usaba SIN NINGUNA sanitización para armar
    `SandboxedSkillTool.artifact_dir` (kernel/registry/sandboxed_skill.py)
    vía un simple `Path / manifest.name`, seguido de
    `mkdir(parents=True, exist_ok=True)`. Un `name` como
    "../../../../.venv/lib/pythonX.Y/site-packages" hace que el propio
    proceso del agente cree directorios FUERA de data/artifacts/skills/
    — path traversal que, combinado con cualquier escritura posterior
    ahí (un .pth, un paquete que se autoimporta), es RCE diferido en el
    próximo arranque del intérprete. Devuelve el mensaje de error, o
    None si el nombre es válido — mismo patrón que
    `_validate_entry_point_reference` de abajo.
    """
    if not _VALID_SKILL_NAME.match(name):
        return (
            f"name '{name}' inválido — solo minúsculas, dígitos, '_' y '-', debe empezar con "
            "minúscula o dígito, máximo 64 caracteres (^[a-z0-9][a-z0-9_-]{0,63}$)"
        )
    return None


def _validate_entry_point_reference(skill_dir: Path, entry_point: str) -> str | None:
    """
    Valida el `entry_point` SIN importar ni ejecutar nada: solo formato
    ("archivo:Clase", ambas partes no vacías) y que el archivo
    referenciado exista de verdad. Devuelve el mensaje de error, o None
    si está todo bien. El `.py` en sí (¿existe la clase? ¿es una
    subclase de Tool? ¿tiene errores de sintaxis?) recién se resuelve
    DENTRO del contenedor, en la primera ejecución real — ver
    kernel/lifecycle/skill_runner.py.
    """
    module_part, _, class_name = entry_point.partition(":")
    if not module_part or not class_name:
        return f"entry_point '{entry_point}' inválido, formato esperado '<archivo>:<Clase>'"

    module_path = skill_dir / f"{module_part}.py"
    if not module_path.exists():
        return f"No se encontró '{module_path.name}' en {skill_dir}"

    return None


def load_skills(
    registry: "ToolRegistry",
    skills_dir: Path = DEFAULT_SKILLS_DIR,
    image_builder: "SkillImageBuilder | None" = None,
) -> list[SkillStatus]:
    """
    `image_builder` es inyectable (mismo motivo que `sandbox=` en
    ToolRegistry): solo se construye un `SkillImageBuilder` real (que
    requiere Docker) para skills que declaran `requirements` — una skill
    sin dependencias nunca toca Docker en tiempo de carga, así que tests
    de descubrimiento puro (manifiesto, enabled/disabled, errores) siguen
    sin necesitar un daemon Docker real, igual que antes.
    """
    if not skills_dir.exists():
        return []

    results: list[SkillStatus] = []
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        manifest_path = skill_dir / MANIFEST_FILENAME
        if not manifest_path.exists():
            continue  # no es una skill (p.ej. __pycache__), se ignora en silencio

        try:
            manifest = parse_manifest(manifest_path)
        except (yaml.YAMLError, KeyError, OSError) as e:
            detail = f"Manifiesto inválido: {e}"
            logger.warning(f"Skill en {skill_dir}: {detail}")
            results.append(SkillStatus(skill_dir=skill_dir.name, manifest=None, status="invalid_manifest", detail=detail))
            _audit(skill_dir.name, "invalid_manifest", detail)
            continue

        name_error = _validate_skill_name(manifest.name)
        if name_error is not None:
            logger.warning(f"Skill en {skill_dir}: {name_error}")
            results.append(SkillStatus(skill_dir=skill_dir.name, manifest=manifest, status="invalid_manifest", detail=name_error))
            _audit(skill_dir.name, "invalid_manifest", name_error)
            continue

        if not manifest.enabled:
            results.append(SkillStatus(skill_dir=skill_dir.name, manifest=manifest, status="disabled"))
            continue  # deshabilitada: ni una línea de su código se ejecuta

        signature_status = verify_skill_signature(skill_dir)
        if signature_status == "tampered":
            detail = (
                "skill.sig no verifica contra el contenido actual de la carpeta — el paquete "
                "fue alterado desde que se firmó (o skill.sig está corrupto)."
            )
            logger.warning(f"Skill '{manifest.name}': {detail}")
            results.append(
                SkillStatus(
                    skill_dir=skill_dir.name, manifest=manifest, status="signature_invalid",
                    detail=detail, signature_status=signature_status,
                )
            )
            _audit(manifest.name, "signature_invalid", detail)
            continue  # fail closed: nunca se registra una skill con firma inválida

        entry_point_error = _validate_entry_point_reference(skill_dir, manifest.entry_point)
        if entry_point_error is not None:
            logger.warning(f"Skill '{manifest.name}': {entry_point_error}")
            results.append(
                SkillStatus(skill_dir=skill_dir.name, manifest=manifest, status="entry_point_invalid", detail=entry_point_error)
            )
            _audit(manifest.name, "entry_point_invalid", entry_point_error)
            continue

        try:
            tool_manifest = ToolManifest(
                name=manifest.name,
                description=manifest.description,
                permissions=frozenset(Permission(p) for p in manifest.permissions),
                created_by="skill",  # informativo únicamente: trust_tier_for() decide el
                                      # nivel de confianza real por el TIPO de wrapper, nunca por esto.
                parameters_schema=manifest.parameters_schema,
            )
        except ValueError as e:
            detail = f"Permiso desconocido en skill.yaml: {e}"
            logger.warning(f"Skill '{manifest.name}': {detail}")
            results.append(SkillStatus(skill_dir=skill_dir.name, manifest=manifest, status="invalid_manifest", detail=detail))
            _audit(manifest.name, "invalid_manifest", detail)
            continue

        from kernel.lifecycle.skill_image_builder import MINIMAL_IMAGE

        image = MINIMAL_IMAGE
        if manifest.requirements:
            try:
                if image_builder is None:
                    from kernel.lifecycle.skill_image_builder import SkillImageBuilder as _SkillImageBuilder

                    image_builder = _SkillImageBuilder()
                image = image_builder.build_or_get_image(manifest.name, manifest.requirements)
            except Exception as e:
                detail = str(e)
                logger.warning(f"Skill '{manifest.name}': no se pudo preparar su imagen: {detail}")
                results.append(
                    SkillStatus(skill_dir=skill_dir.name, manifest=manifest, status="image_build_failed", detail=detail)
                )
                _audit(manifest.name, "image_build_failed", detail)
                continue

        from kernel.registry.sandboxed_skill import SandboxedSkillTool

        registry.register_static_tool(
            SandboxedSkillTool(
                manifest=tool_manifest,
                skill_dir=skill_dir,
                entry_point=manifest.entry_point,
                image=image,
                sandbox=registry.sandbox,
                kernel_services=manifest.kernel_services,
            )
        )
        results.append(
            SkillStatus(
                skill_dir=skill_dir.name, manifest=manifest, status="loaded",
                signature_status=signature_status,
            )
        )
        _audit(
            manifest.name, "loaded",
            f"Skill '{manifest.name}' v{manifest.version} cargada (aislada, imagen={image}, "
            f"firma={signature_status})",
        )

    return results


def _audit(name: str, status: str, detail: str) -> None:
    audit_log.record(
        AuditEvent(
            event_type="skill_loaded",
            summary=f"Skill '{name}': {detail}",
            context={"skill": name, "status": status},
            outcome="success" if status == "loaded" else "failure",
        )
    )


_ENABLED_LINE_RE = re.compile(r"(?m)^enabled:\s*(true|false)\s*$")


def set_skill_enabled(skill_dir: Path, enabled: bool) -> None:
    """
    Activa/desactiva una skill (F4 del plan de marketplace, ver
    scripts/enable_skill.py). Edita la línea `enabled:` de su
    skill.yaml con un reemplazo de texto dirigido — nunca con
    `yaml.dump()`, que destruiría los comentarios explicativos que ya
    trae cada manifest real. Si la línea no existe (manifest
    minimalista que la omite, default `False` en parse_manifest()), la
    agrega al final.
    """
    manifest_path = skill_dir / MANIFEST_FILENAME
    text = manifest_path.read_text(encoding="utf-8")
    new_value = "true" if enabled else "false"
    new_text, count = _ENABLED_LINE_RE.subn(f"enabled: {new_value}", text)
    if count == 0:
        new_text = text.rstrip("\n") + f"\nenabled: {new_value}\n"
    manifest_path.write_text(new_text, encoding="utf-8")


def audit_skill_enable_change(manifest_name: str, skill_dir_name: str, enabled: bool, source: str = "local") -> None:
    """
    Rastro auditado de una decisión humana explícita vía
    scripts/enable_skill.py (source="local") o
    scripts/install_from_market.py (source="market", Fase A del plan
    de comunidad) — mismo evento, con el origen como contexto extra.
    """
    audit_log.record(
        AuditEvent(
            event_type="skill_enabled" if enabled else "skill_disabled",
            summary=f"Skill '{manifest_name}' {'habilitada' if enabled else 'deshabilitada'} manualmente ({source})",
            context={"skill": manifest_name, "skill_dir": skill_dir_name, "source": source},
            outcome="success",
        )
    )
