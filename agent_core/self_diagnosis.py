"""
Auto-diagnóstico bajo demanda: kal investiga por qué un invariante de
salud del sistema falló (hoy: la cadena de auditoría) y propone una
corrección de código real vía agent_core/self_modification.py — nunca la
aplica sola. La propuesta resultante queda "pending_human_approval" (o
"rejected_unsafe"/"regression_detected" si el pipeline de
self_modification ya la descarta) exactamente igual que cualquier otra
propuesta de self-modification; aplicarla sigue siendo
POST /self-modification/apply con un approved_by explícito.

Nace de un caso real: la cadena de auditoría se rompió por una condición
de carrera en audit/audit_log.py, diagnosticada y corregida a mano (ver
AuditLog.diagnose_chain()). Este módulo generaliza ese proceso —
diagnóstico MECÁNICO del síntoma (determinista, sin LLM) + el LLM
proponiendo el parche a partir de ese diagnóstico— para que kal pueda
intentarlo por su cuenta la próxima vez, siempre bajo demanda (nunca
disparado automáticamente) y siempre con aprobación humana antes de
tocar disco real.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent_core.llm.ollama_client import OllamaClient
from agent_core.llm.provider import LLMProvider, ProviderError
from agent_core.self_modification import (
    SelfModificationManager,
    SelfModProposal,
    self_modification_manager,
)
from audit.audit_log import AuditEvent, audit_log
from utils.logger import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_CODE_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


@dataclass
class InvariantCheckResult:
    name: str
    healthy: bool
    detail: str
    target_path: str  # archivo relevante para proponer un fix, si no healthy
    diagnostic_context: dict = field(default_factory=dict)


def check_audit_chain() -> InvariantCheckResult:
    diagnosis = audit_log.diagnose_chain()
    return InvariantCheckResult(
        name="audit_chain",
        healthy=diagnosis.is_valid,
        detail=diagnosis.summary(),
        target_path="audit/audit_log.py",
        diagnostic_context={"total_entries": diagnosis.total_entries, "breaks": len(diagnosis.breaks)},
    )


# Registro de invariantes verificables. Agregar uno nuevo a futuro es una
# función check_xxx() + una entrada acá — el resto del pipeline (diagnóstico
# vía LLM, propuesta de fix) es genérico y no necesita cambios.
INVARIANT_CHECKS: dict[str, Callable[[], InvariantCheckResult]] = {
    "audit_chain": check_audit_chain,
}

SELF_REPAIR_SYSTEM_PROMPT = """Eres el módulo de auto-diagnóstico de kal. Se te da un invariante de \
salud del sistema que falló, un diagnóstico mecánico de qué está mal, y el contenido completo del \
archivo responsable. Tu tarea:
1. Explicá la causa raíz en 2-3 oraciones, en base al diagnóstico y al código — no inventes causas \
que el diagnóstico no sustente.
2. Después de la explicación, dame el archivo COMPLETO ya corregido (no un fragmento ni un diff) en \
un único bloque ```python ... ```.
No agregues nada después del bloque de código. Si no podés determinar una corrección segura, no \
incluyas ningún bloque de código — solo tu explicación.
"""


@dataclass
class SelfDiagnosisResult:
    invariant: str
    diagnosis: str
    proposal: SelfModProposal | None
    status: str  # "no_issue" | "diagnosed" | "llm_error" | "no_fix_proposed"


class SelfDiagnosisAgent:
    def __init__(
        self,
        llm_client: LLMProvider | None = None,
        self_modification: SelfModificationManager | None = None,
        project_root: Path | None = None,
        invariant_checks: dict[str, Callable[[], InvariantCheckResult]] | None = None,
    ):
        self.llm = llm_client or OllamaClient()
        self.self_modification = self_modification or self_modification_manager
        self.project_root = project_root or PROJECT_ROOT
        self.invariant_checks = invariant_checks or INVARIANT_CHECKS

    def diagnose_and_propose_fix(self, invariant: str, model: str | None = None) -> SelfDiagnosisResult:
        check = self.invariant_checks.get(invariant)
        if check is None:
            raise ValueError(f"Invariante desconocido: '{invariant}'")

        result = check()
        if result.healthy:
            return SelfDiagnosisResult(invariant=invariant, diagnosis=result.detail, proposal=None, status="no_issue")

        current_source = (self.project_root / result.target_path).read_text(encoding="utf-8")
        messages = [
            {"role": "system", "content": SELF_REPAIR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Invariante violado: {invariant}\n"
                    f"Diagnóstico mecánico: {result.detail}\n\n"
                    f"Archivo actual ({result.target_path}):\n```python\n{current_source}\n```"
                ),
            },
        ]

        try:
            response = self.llm.chat(messages, model=model)
        except ProviderError as e:
            logger.warning(f"Self-diagnosis de '{invariant}': no se pudo contactar al proveedor de LLM: {e}")
            self._audit(invariant, "llm_error", str(e))
            return SelfDiagnosisResult(invariant=invariant, diagnosis=str(e), proposal=None, status="llm_error")

        fixed_source = self._extract_code_fence(response.content)
        diagnosis_text = self._extract_explanation(response.content)

        if fixed_source is None:
            logger.info(f"Self-diagnosis de '{invariant}': el modelo no propuso un parche parseable")
            self._audit(invariant, "no_fix_proposed", diagnosis_text)
            return SelfDiagnosisResult(invariant=invariant, diagnosis=diagnosis_text, proposal=None, status="no_fix_proposed")

        proposal = self.self_modification.propose(
            target_path=result.target_path,
            proposed_source=fixed_source,
            justification=f"Auto-diagnóstico de invariante '{invariant}': {diagnosis_text}",
        )
        self._audit(invariant, "diagnosed", f"{diagnosis_text} (propuesta {proposal.id}: {proposal.status})")
        return SelfDiagnosisResult(invariant=invariant, diagnosis=diagnosis_text, proposal=proposal, status="diagnosed")

    @staticmethod
    def _extract_code_fence(content: str) -> str | None:
        match = _CODE_FENCE_RE.search(content)
        return match.group(1) if match else None

    @staticmethod
    def _extract_explanation(content: str) -> str:
        before_fence = content.split("```")[0].strip()
        return before_fence or content.strip()

    @staticmethod
    def _audit(invariant: str, status: str, detail: str) -> None:
        audit_log.record(
            AuditEvent(
                event_type="self_diagnosis_run",
                summary=f"Auto-diagnóstico de '{invariant}': {status}",
                context={"invariant": invariant, "status": status, "detail": detail},
                outcome="success" if status == "diagnosed" else "failure",
            )
        )


self_diagnosis_agent = SelfDiagnosisAgent()
