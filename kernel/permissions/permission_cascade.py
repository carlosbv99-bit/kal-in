"""
Cascada de permisos de varios niveles — "más restrictivo gana" (ver
utils/config.py::PermissionCascadeConfig para el porqué y los
defaults). Usada por agent_core/llm/agent_loop.py ANTES de invocar el
handler de cualquier herramienta.

IMPORTANTE — separado de sdk/permissions.py a propósito:
ese otro archivo se copia TAL CUAL dentro de cada contenedor de skill
(toda skill necesita `Permission` a través de `base_tool.py`), así que
debe seguir siendo 100% stdlib. Este módulo SÍ depende de
`utils.config` (para leer la configuración de la cascada) — si viviera
en el mismo archivo que `Permission`, ejecutar una skill dentro de
Docker fallaría con `ModuleNotFoundError: utils` (bug real encontrado
probando esto: el contenedor no tiene `utils/` disponible). Este
módulo NUNCA se envía a un contenedor, solo lo usa el proceso principal.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sdk.permissions import Permission
from utils.config import settings

if TYPE_CHECKING:
    from sdk.skill import Tool


def trust_tier_for(tool: Tool) -> str:
    """
    Nivel de confianza de una herramienta ya registrada, para la cascada
    de permisos (PermissionCascade más abajo) — "system" | "agent" | "skill".

    IMPORTANTE: esto se decide por el TIPO del wrapper con el que la
    herramienta quedó registrada en el ToolRegistry, NUNCA leyendo
    manifest.created_by. Ese campo lo autodeclara la propia herramienta/
    skill en su propio código (skills/system_info/tool.py y
    skills/qr_code/tool.py, por ejemplo, ya ponen created_by="system" sin
    que nada lo valide) — si la cascada confiara en ese campo, cualquier
    autor de skill podría autodeclararse "system" y saltarse el techo más
    bajo pensado justo para código de terceros, anulando el propósito
    completo de tener niveles de confianza distintos.

    Imports diferidos: evitan el ciclo kernel.permissions.permission_cascade
    <-> kernel.registry.registry/sandboxed_skill (esos dos módulos
    importan Permission de sdk.permissions, no de acá, pero
    igual conviene no importarlos a nivel de módulo para no forzar su
    carga completa solo por importar esta función).
    """
    from kernel.registry.registry import DynamicSandboxedTool
    from kernel.registry.sandboxed_skill import SandboxedSkillTool

    if isinstance(tool, SandboxedSkillTool):
        return "skill"
    if isinstance(tool, DynamicSandboxedTool):
        return "agent"
    return "system"


class PermissionCascade:
    """
    Chequeo INDEPENDIENTE de UNSUPPORTED_RUNTIME_PERMISSIONS
    (sdk/permissions.py): aquel es "¿esto se puede confinar
    técnicamente?" (siempre activo, no configurable); este es "¿este
    contexto concreto (nivel de confianza + sesión) está autorizado a
    pedirlo?".
    """

    def __init__(self, cfg=None):
        cfg = cfg or settings.permissions
        self.globally_denied = frozenset(Permission(p) for p in cfg.globally_denied)
        self.trust_tier_caps: dict[str, frozenset[Permission]] = {
            tier: frozenset(Permission(p) for p in perms) for tier, perms in cfg.trust_tier_caps.items()
        }

    def missing_permissions(
        self,
        requested: frozenset[Permission],
        trust_tier: str,
        session_denied: frozenset[Permission] = frozenset(),
    ) -> frozenset[Permission]:
        """
        Permisos que `requested` necesita pero que NINGÚN nivel termina
        otorgando. Vacío significa: la ejecución puede seguir. No vacío
        significa: rechazar ANTES de ejecutar nada (fail closed) — nunca
        se ejecuta con menos permisos de los que la herramienta asume
        tener, eso produciría fallos confusos a mitad de camino en vez de
        un rechazo claro de entrada.
        """
        # tier desconocido (no declarado en config.yaml) -> nada permitido,
        # fail closed también ahí.
        tier_cap = self.trust_tier_caps.get(trust_tier, frozenset())
        allowed = (requested - self.globally_denied) & tier_cap
        allowed -= session_denied
        return requested - allowed


# Singleton, mismo patrón que tool_registry (registry.py) / audit_log
# (audit/audit_log.py) — una única cascada compartida por todo el proceso.
permission_cascade = PermissionCascade()
