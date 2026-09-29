"""
Interfaz común de herramientas, tanto las predefinidas (adaptadores
multimodales) como las creadas dinámicamente por el agente, como las
que declara una Skill de terceros vía skill.yaml. Parte del SDK
público (ver sdk/__init__.py) — toda Skill subclasea `Tool` y usa
`ToolManifest`/`Artifact` (sdk/artifacts.py) importándolos de acá,
nunca de una ruta interna del kernel.

Toda herramienta declara un Manifest explícito de permisos. El sandbox
y el registro usan ese manifiesto para decidir aislamiento y si requiere
aprobación humana (ver config.yaml: tool_integration.require_human_approval_for).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sdk.artifacts import Artifact
from sdk.permissions import Permission


@dataclass
class ToolManifest:
    name: str
    description: str
    requires_network: bool = False
    requires_filesystem_write: bool = False
    # HALLAZGO REAL DE AUDITORÍA EXTERNA (M-6 en kal, 2026-09-27,
    # portado acá vía scripts/check_kernel_drift.py): declarado y
    # serializado (kernel/registry/registry.py), pero NO ENFORZADO
    # todavía en ningún lugar — Permission.NETWORK concede
    # network_mode="bridge" completo (todo internet, sin allowlist ni
    # proxy de egreso), sin mirar este campo para nada. Documentado
    # acá para que declarar esto en un manifest no dé una falsa
    # sensación de estar restringiendo algo — hoy es solo información,
    # no un control real. Implementarlo de verdad (proxy de egreso o
    # iptables dentro del contenedor) es un cambio de infraestructura
    # más grande, no una corrección puntual.
    allowed_domains: list[str] = field(default_factory=list)
    # Superconjunto explícito de permisos (modelo estilo Android). Los dos
    # booleans de arriba son la forma abreviada de declarar los dos casos
    # más comunes; permissions es la fuente de verdad canónica que usa
    # el resto del pipeline (registry, sandbox). Permisos sin equivalente
    # en booleans (GPU/CAMERA/MICROPHONE/CLIPBOARD/BROWSER/DOCKER) solo
    # se declaran aquí.
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    created_by: str = "system"  # "system" | "agent" (dinámica)
    source_context: str = ""     # qué tarea/prompt originó esta herramienta (auditoría)
    # JSON Schema de los argumentos de execute(), usado para exponer la
    # herramienta al LLM (ver agent_core/llm/agent_loop.py). Default
    # permisivo (sin argumentos requeridos) para herramientas dinámicas
    # cuyo autor (el propio agente) todavía no declara un schema explícito.
    parameters_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})

    def __post_init__(self) -> None:
        derived = set(self.permissions)
        if self.requires_network:
            derived.add(Permission.NETWORK)
        if self.requires_filesystem_write:
            derived.add(Permission.FILESYSTEM_WRITE)
        derived.add(Permission.FILESYSTEM_READ)  # implícito: todo el workspace es legible
        self.permissions = frozenset(derived)


class Tool(ABC):
    manifest: ToolManifest

    @abstractmethod
    def execute(self, **kwargs) -> Artifact:
        ...
