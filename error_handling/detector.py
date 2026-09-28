"""
ErrorDetector: punto de entrada para capturar y reparar errores.

Flujo:
  1. Se captura una excepción durante la ejecución de una tarea.
  2. Se calcula su firma (error_signature) y se consulta el circuit breaker.
  3. Si el circuito está cerrado, se despacha a la estrategia correspondiente.
  4. El resultado (éxito/fracaso) se reporta de vuelta al circuit breaker
     y se audita, siempre.
"""
from __future__ import annotations

import re

from audit.audit_log import AuditEvent, audit_log
from error_handling.circuit_breaker import circuit_breaker, error_signature
from error_handling.strategies import (
    STRATEGY_REGISTRY,
    RepairContext,
    RepairResult,
    RepairStrategy,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# Nombres de excepción reconocidos explícitamente, en orden de chequeo.
# ModuleNotFoundError se revisa antes que ImportError porque en Python 3
# es la excepción real que se levanta (ImportError es su clase base) y
# ambos términos pueden aparecer en el mismo traceback.
_KNOWN_ERROR_TYPES = ("ModuleNotFoundError", "ImportError", "SyntaxError")


def classify_sandbox_error(stderr: str) -> tuple[str, str]:
    """
    Clasifica el stderr de una ejecución fallida en sandbox en uno de
    los tipos de error soportados por STRATEGY_REGISTRY.

    El sandbox devuelve texto plano (stdout/stderr de un proceso en un
    contenedor), no objetos de excepción Python — por eso esto es
    necesario: no hay un `except ImportError` posible sobre la salida
    de un subprocess. Heurística: Python siempre termina una traza no
    capturada con una línea de la forma "TipoDeError: mensaje", así que
    se busca esa línea y se identifica el tipo conocido más específico
    que aparezca en ella. Si no se reconoce nada, se clasifica como
    RuntimeError (catch-all genérico, ya que "algo falló en tiempo de
    ejecución" es la descripción más honesta cuando no se sabe más).

    BUG REAL ENCONTRADO EN USO: un rechazo de VALIDACIÓN ESTÁTICA (ver
    kernel/lifecycle/executor.py, mensaje "Validación estática falló: ..." —
    p.ej. un import prohibido por el denylist) caía en el fallback
    genérico de RuntimeError. RuntimeErrorStrategy reintenta el mismo
    código ciegamente (ver su docstring) asumiendo que el fallo podría
    ser transitorio — pero un rechazo de validación es DETERMINISTA: el
    código ni siquiera llegó a ejecutarse, y reintentarlo produce
    exactamente el mismo rechazo cada vez. En producción esto quemó 3
    intentos idénticos hasta abrir el circuit breaker para un fallo que
    ya se sabía, desde el primer intento, que nunca iba a cambiar. Se
    clasifica aparte, ANTES del resto de chequeos, para que
    ValidationErrorStrategy pueda fallar una sola vez sin reintentar.
    """
    if stderr.strip().startswith("Validación estática falló"):
        return ("ValidationError", stderr.strip())

    lines = [l for l in stderr.strip().splitlines() if l.strip()]
    if not lines:
        return ("RuntimeError", stderr.strip() or "error desconocido en sandbox (stderr vacío)")

    last_line = lines[-1]
    for error_type in _KNOWN_ERROR_TYPES:
        if re.search(rf"\b{error_type}\b", last_line):
            return (error_type, last_line.strip())

    return ("RuntimeError", last_line.strip())


class ErrorDetector:
    def __init__(self, strategies: dict[str, RepairStrategy] | None = None):
        """
        strategies: mapa opcional error_type -> instancia ya construida
        de RepairStrategy. Sin esto, cada estrategia se instanciaba con
        strategy_cls() a secas, lo que significa que SIEMPRE construía
        sus propias dependencias por defecto (p.ej. RuntimeErrorStrategy
        creando su propia MidTermMemory() apuntando al archivo real del
        proyecto), ignorando cualquier instancia que el llamador
        quisiera compartir (p.ej. el mid_term de TaskExecutor, o uno
        aislado para tests). Si no se provee nada, se cae de vuelta a
        instanciar desde STRATEGY_REGISTRY con sus defaults.
        """
        self._injected_strategies = strategies or {}

    def _resolve_strategy(self, error_type: str):
        if error_type in self._injected_strategies:
            return self._injected_strategies[error_type]
        strategy_cls = STRATEGY_REGISTRY.get(error_type)
        return strategy_cls() if strategy_cls else None

    def handle(self, ctx: RepairContext) -> RepairResult:
        signature = error_signature(ctx.error_type, ctx.error_message, ctx.location)

        if not circuit_breaker.allow_attempt(signature):
            logger.warning(f"Circuito abierto para {signature}, no se reintenta sin intervención humana")
            return RepairResult(success=False, detail="circuit_breaker_open")

        strategy = self._resolve_strategy(ctx.error_type)
        if strategy is None:
            logger.info(f"Sin estrategia registrada para {ctx.error_type}, escalando")
            audit_log.record(
                AuditEvent(
                    event_type="human_escalation",
                    summary=f"Sin estrategia de reparación para {ctx.error_type}",
                    context={"error_message": ctx.error_message, "location": ctx.location},
                    outcome="escalated",
                )
            )
            return RepairResult(success=False, detail="no_strategy_registered")

        try:
            result = strategy.repair(ctx)
        except NotImplementedError:
            logger.info(f"Estrategia para {ctx.error_type} aún no implementada (skeleton)")
            result = RepairResult(success=False, detail="strategy_not_implemented")
        except Exception as e:
            # logger.exception() ya agrega el traceback completo solo —
            # repetir {e} en el mensaje era redundante.
            logger.exception("Estrategia de reparación falló inesperadamente")
            result = RepairResult(success=False, detail=str(e))

        # Si la estrategia ya reejecutó el código (already_retried=True)
        # o falló directamente, el desenlace real ya se conoce: se
        # registra ahora. Si en cambio delegó el reintento al llamador
        # (already_retried=False, success=True), TODAVÍA no sabemos si
        # el reintento realmente funcionará — registrar "éxito" aquí
        # reiniciaría el contador de fallos del circuit breaker sin que
        # se haya confirmado nada, permitiendo que un error persistente
        # nunca dispare el breaker. En ese caso se difiere el registro:
        # ver record_outcome(), que el llamador debe invocar una vez
        # conocido el resultado real del reintento.
        if result.already_retried or not result.success:
            circuit_breaker.record_attempt(
                signature, success=result.success,
                context={"error_type": ctx.error_type, "location": ctx.location},
            )

        audit_log.record(
            AuditEvent(
                event_type="error_repair",
                summary=f"Intento de reparación de {ctx.error_type}",
                context={"location": ctx.location, "detail": result.detail},
                outcome="success" if result.success else "failure",
            )
        )
        return result

    def record_outcome(self, ctx: RepairContext, success: bool) -> None:
        """
        Para estrategias que delegan el reintento (already_retried=False,
        p.ej. RuntimeErrorStrategy: "hay un checkpoint, reintenta tú"),
        el llamador (task_execution/executor.py) debe reportar aquí el
        resultado REAL una vez conocido, para que el circuit breaker
        refleje si la reparación funcionó de verdad y no solo si la
        estrategia propuso algo plausible.
        """
        signature = error_signature(ctx.error_type, ctx.error_message, ctx.location)
        circuit_breaker.record_attempt(
            signature, success=success,
            context={"error_type": ctx.error_type, "location": ctx.location},
        )
