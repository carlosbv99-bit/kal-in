"""
Evaluación determinista de condiciones de reapertura.

Este módulo es deliberadamente `re` + comparaciones, y nada más. La
propiedad que tiene que cumplir es una sola, y es la que hace que todo el
diseño funcione: **decidir si una hipótesis se reabre NO requiere llamar al
modelo**. Si en algún camino hiciera falta, el contrato de `ReopenCondition`
está mal y hay que volver a los modelos.

La regla estructural (no configurable) es la primera que se evalúa:
`MODEL_GENERATED` NUNCA dispara una reapertura. Se chequea acá, y no como un
valor por defecto de `ReopenCondition.origins`, justamente para que no se
pueda desactivar construyendo una condición permisiva. Reabrir una hipótesis
con texto que generó el propio modelo es un bucle de auto-confirmación con
pasos extra.

Todo lo demás también falla cerrado: `origins` vacío no matchea nada (no
"cualquiera"), un patrón inválido no matchea nada, y un origen no declarado
no matchea. Un matcher que falla abierto convierte el ledger en una máquina
de reactivar cosas al azar.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Container

from kal_epistemics.models import (
    Evidence,
    EvidenceOrigin,
    ReopenAction,
    ReopenCondition,
)
from kal_epistemics.safety import MAX_CONTENT_SCAN, pattern_is_unsafe

# MAX_CONTENT_SCAN se define en safety.py (junto al resto de los topes) y se
# re-exporta acá para no partir los límites en dos archivos.


@dataclass(frozen=True)
class ReopenDecision:
    """
    Resultado de evaluar una condición contra una evidencia.

    `reason` es un código mecánico y estable (no una frase generada), para
    que se pueda agregar, contar y testear sin interpretar lenguaje natural.
    """

    matched: bool
    action: ReopenAction
    reason: str
    condition_id: str = ""
    evidence_id: str = ""


def _match(pattern: str, text: str) -> bool | None:
    """True/False, o None si el patrón es inválido (fail-closed)."""
    if not pattern:
        return True
    try:
        compilado = re.compile(pattern)
    except re.error:
        return None
    return compilado.search(text) is not None


def should_reopen(
    condition: ReopenCondition,
    evidence: Evidence,
    *,
    already_applied: Container[str] = frozenset(),
) -> ReopenDecision:
    """
    ¿Esta evidencia dispara esta condición?

    Orden de evaluación, de lo estructural a lo específico. El orden importa
    porque el `reason` reportado tiene que ser el motivo más fundamental, no
    el último que se chequeó.

    `already_applied` es el conjunto de claves de dedup ya aplicadas **a ESTA
    condición**. El alcance lo decide el llamador, y no es un detalle: el ledger
    lo lleva por (condición, evidencia), porque una misma evidencia puede
    disparar otra condición distinta y eso es información nueva, no una
    repetición. Si el llamador pasara todas las claves aplicadas a la hipótesis,
    la segunda condición se descartaría sola y el resultado dependería del orden
    en que se declararon las condiciones.
    """
    def no(reason: str) -> ReopenDecision:
        return ReopenDecision(
            matched=False,
            action=condition.action,
            reason=reason,
            condition_id=condition.id,
            evidence_id=evidence.id,
        )

    # 1. Estructural: el texto del propio modelo no reabre nada, nunca.
    if evidence.origin is EvidenceOrigin.MODEL_GENERATED:
        return no("endogenous_never_reopens")

    # 1b. Admisión: lo que está en cuarentena se conserva para auditoría, pero
    #     no entra al circuito epistémico (ver EvidenceStatus).
    if not evidence.is_usable:
        return no("evidence_quarantined")

    # 2. Dedup: la misma evidencia otra vez no es evidencia nueva.
    if evidence.dedup_key in already_applied:
        return no("evidence_already_applied")

    # 3. Origen declarado (vacío = ninguno, fail-closed).
    if evidence.origin not in condition.origins:
        return no("origin_not_allowed")

    # 4. Tipo de ancla, si la condición exige uno.
    if condition.anchor_kinds and evidence.anchor_kind not in condition.anchor_kinds:
        return no("anchor_kind_not_allowed")

    # 5. Fuente: coincidencia EXACTA primero (camino no confiable, sin motor
    #    de regex), después el patrón del camino confiable.
    if condition.source_in and evidence.source not in condition.source_in:
        return no("source_not_allowed")
    if condition.source_pattern:
        motivo = pattern_is_unsafe(condition.source_pattern)
        if motivo == "invalid_pattern":
            return no("invalid_pattern")
        if motivo:
            return no(f"unsafe_pattern:{motivo}")
        if not _match(condition.source_pattern, evidence.source):
            return no("source_mismatch")

    # 6. Contenido: palabras clave (substring, sin motor de regex, sin ReDoS
    #    posible) y después el patrón del camino confiable. Se matchea, jamás
    #    se interpreta — ver MAX_CONTENT_SCAN y safety.py.
    if condition.content_keywords:
        texto = evidence.content[:MAX_CONTENT_SCAN].casefold()
        if not all(k.casefold() in texto for k in condition.content_keywords):
            return no("keywords_not_found")
    if condition.content_pattern:
        motivo = pattern_is_unsafe(condition.content_pattern)
        if motivo == "invalid_pattern":
            return no("invalid_pattern")
        if motivo:
            return no(f"unsafe_pattern:{motivo}")
        if not _match(condition.content_pattern, evidence.content[:MAX_CONTENT_SCAN]):
            return no("content_mismatch")

    return ReopenDecision(
        matched=True,
        action=condition.action,
        reason="matched",
        condition_id=condition.id,
        evidence_id=evidence.id,
    )
