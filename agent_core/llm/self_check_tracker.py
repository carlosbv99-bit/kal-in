"""
SelfCheckTracker: rastrea cuándo una herramienta que generó un artefacto
en este turno fue luego revisada con analyze_image sobre su propio
resultado — extraído de agent_core/llm/agent_loop.py (2026-07-27) para
bajar la densidad de ese archivo, sin cambiar ningún comportamiento.

Dos BUG REAL ENCONTRADOS EN USO motivaron esto (no un diseño teórico):

1. "crea una naranja (solo una)" generó bien la primera vez, pero
   analyze_image sobre esa MISMA imagen detectó que en realidad
   mostraba un grupo (SDXL-Turbo, a solo 2 pasos, no respeta de forma
   confiable cantidades exactas de objetos — una limitación real del
   modelo de imágenes, no del razonamiento) y el modelo intentó
   corregirlo regenerando una y otra vez, sin límite, hasta agotar
   max_steps sin responder nunca.
2. "crea una calle concurrida de moscú" — tras el ciclo normal de
   autochequeo (generar -> revisar -> regenerar UNA vez), el modelo
   volvió a intentar la misma herramienta una TERCERA vez pese al
   rechazo explícito ("da tu respuesta final AHORA") — y siguió
   intentando 3 veces MÁS, agotando max_steps SIN llegar nunca a una
   respuesta, pese a haber generado dos veces un resultado real.

`checked_tools` resuelve (1): aplica un tope MÁS ESTRICTO (ver
agent_core/llm/tool_repeat_limiter.py::ToolRepeatLimiter.evaluate())
a cualquier herramienta que ya se autochequeó. `rejection_counts`
resuelve (2): si el modelo IGNORA el rechazo una segunda vez, corta el
turno con una respuesta sintetizada honesta en vez de seguir gastando
pasos reales de cómputo esperando que reaccione.
"""
from __future__ import annotations

from sdk.artifacts import Artifact


class SelfCheckTracker:
    def __init__(self):
        # uri -> nombre de la herramienta que generó ese artefacto EN
        # ESTE turno — permite detectar cuándo analyze_image se llama
        # sobre un resultado propio (autochequeo) en vez de una imagen
        # externa cualquiera.
        self.artifact_paths_this_turn: dict[str, str] = {}
        self.checked_tools: set[str] = set()
        self.rejection_counts: dict[str, int] = {}

    def record_artifact(self, artifact: Artifact | None, tool_name: str) -> None:
        if artifact is not None and artifact.uri:
            self.artifact_paths_this_turn[artifact.uri] = tool_name

    def note_check_if_applicable(self, tool_name: str, arguments: dict) -> None:
        """Llamar tras cada ejecución REAL y exitosa (nunca tras un
        rechazo) — si `tool_name` es analyze_image sobre un artefacto
        generado en este mismo turno, marca la herramienta de ORIGEN
        como autochequeada."""
        if tool_name == "analyze_image":
            origin_tool = self.artifact_paths_this_turn.get(arguments.get("image_path"))
            if origin_tool is not None:
                self.checked_tools.add(origin_tool)

    def is_checked(self, name: str) -> bool:
        return name in self.checked_tools

    def as_frozenset(self) -> frozenset[str]:
        return frozenset(self.checked_tools)

    def record_rejection(self, name: str) -> int:
        """Llamar cada vez que ToolRepeatLimiter rechaza una llamada a
        una herramienta ya autochequeada — devuelve el nuevo conteo."""
        self.rejection_counts[name] = self.rejection_counts.get(name, 0) + 1
        return self.rejection_counts[name]

    def should_cut_turn(self, name: str) -> bool:
        return self.rejection_counts.get(name, 0) >= 2

    def build_cut_short_final_answer(self, name: str) -> str:
        """Respuesta sintetizada cuando el modelo ignora 2 veces el
        rechazo de una herramienta autochequeada (ver bug 2 arriba) —
        cita el último artefacto real generado si existe, en vez de
        dejar al usuario sin ninguna respuesta pese a tener un
        resultado real ya generado."""
        last_uri = next(
            (uri for uri, tool in reversed(list(self.artifact_paths_this_turn.items())) if tool == name),
            None,
        )
        if last_uri is not None:
            return (
                f"Generé el resultado con '{name}', pero no logré confirmar que coincida "
                "exactamente con lo pedido (los modelos de generación no siempre son exactos) — "
                "intenté corregirlo pero seguí insistiendo de más, así que corté aquí. Puedes ver el "
                "resultado arriba."
            )
        return (
            f"No logré completar el pedido con '{name}' — insistí de más sin éxito, "
            "así que corté aquí en vez de seguir gastando tiempo."
        )
