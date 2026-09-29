"""
Expone un KernelServiceBus a UNA skill aislada, para UNA ejecución, vía
un socket Unix — nunca vía red (el contenedor de la skill sigue con
network_mode="none"). kernel/registry/sandboxed_skill.py arranca uno
de estos por cada `.execute()` de una skill que declare
`kernel_services` en su skill.yaml, y lo para al terminar (éxito o
error) — nunca sobrevive más allá de esa única ejecución.

Permisos: `allowed_methods` es la lista plana que la propia skill
declaró (`kernel_services` en skill.yaml) — un chequeo de membresía
simple, NO pasa por PermissionCascade (esa cascada es para recursos
del sistema tipo filesystem/red; esto es más parecido a "qué
herramientas tiene disponibles un agente", ya un registro explícito
por diseño). Cualquier pedido a un método no declarado se rechaza
ANTES de tocar el bus real, auditado.

Límites: `max_requests` y `idle_timeout` acotan cuánto puede insistir
o cuánto puede tardar una skill — sin esto, una skill en loop podría
acaparar un servicio compartido (p.ej. el pipeline de generación de
imágenes) para todas las demás.
"""
from __future__ import annotations

import socket
import threading
from pathlib import Path

from audit.audit_log import AuditEvent, audit_log
from kernel.api.bus import (
    ActionNotFoundError,
    ArtifactNotFoundError,
    KernelServiceBus,
    ServiceNotFoundError,
)
from kernel.api.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PERMISSION_DENIED,
    ProtocolError,
    error_response,
    parse_request,
    success_response,
)
from utils.correlation import set_correlation_id
from utils.logger import get_logger

logger = get_logger(__name__)

# Hallazgo de la revisión de seguridad 2026-07-09, corregido ahora: sin
# esto, una skill (la confianza MÁS BAJA del sistema, instalable hoy
# desde el market) podía mandar bytes sin salto de línea indefinidamente
# y el proceso HOST (de confianza) los acumulaba sin límite en memoria —
# DoS de memoria alcanzable por un tercero real, no solo teórico. Los
# pedidos legítimos de hoy (prompt de texto + referencias artifact://,
# nunca bytes binarios inline) pesan unos pocos KB — 1 MiB deja más de
# 100x de margen sin dejar de acotar el peor caso.
_MAX_LINE_BYTES = 1_048_576


class LineTooLongError(Exception):
    """Una conexión mandó más de _MAX_LINE_BYTES sin un salto de línea."""


class InvalidLineEncodingError(Exception):
    """
    VULNERABILIDAD/BUG REAL ENCONTRADO EN AUDITORÍA EXTERNA (Likay-OS,
    2026-09-26), B-4: line.decode("utf-8") sin este chequeo lanzaba
    UnicodeDecodeError sin capturar — como _serve() corre en un
    threading.Thread daemon, una excepción no atrapada ahí no tira
    abajo el proceso, pero SÍ mata el thread en silencio, dejando el
    socket de esta skill muerto sin ningún error claro. Una skill (la
    confianza MÁS BAJA del sistema) podía lograr esto mandando bytes
    que no son UTF-8 válido antes del primer salto de línea.
    """


class KernelBusSocketServer:
    def __init__(
        self,
        bus: KernelServiceBus,
        allowed_methods: list[str],
        socket_path: Path,
        skill_name: str,
        max_requests: int = 20,
        idle_timeout: float = 30.0,
        correlation_id: str | None = None,
    ):
        self.bus = bus
        self.allowed_methods = frozenset(allowed_methods)
        self.socket_path = socket_path
        self.skill_name = skill_name
        self.max_requests = max_requests
        self.idle_timeout = idle_timeout
        # Ver utils/correlation.py — capturado por el llamador en SU thread
        # (el que originó el pedido HTTP), porque _serve() corre en un
        # thread de background propio al que un contextvar nunca cruza
        # automáticamente.
        self.correlation_id = correlation_id
        self._server_socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self._server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_socket.bind(str(self.socket_path))
        self._server_socket.listen(1)
        self._server_socket.settimeout(self.idle_timeout)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _serve(self) -> None:
        # Este método corre en el thread de background lanzado por start()
        # — sin esto, todo lo que loguea/audita desde acá (_audit_call,
        # _audit_denied, logger.warning en _handle_line) quedaría sin
        # correlation_id pese a que start() se llamó dentro de un pedido
        # HTTP con uno bien seteado (ver utils/correlation.py).
        set_correlation_id(self.correlation_id)
        requests_handled = 0
        try:
            while requests_handled < self.max_requests and not self._stop_event.is_set():
                try:
                    conn, _ = self._server_socket.accept()
                except (TimeoutError, OSError):
                    return

                with conn:
                    conn.settimeout(self.idle_timeout)
                    try:
                        line = self._read_line(conn)
                    except (TimeoutError, OSError):
                        continue
                    except LineTooLongError:
                        self._audit_line_too_long()
                        continue
                    except InvalidLineEncodingError as e:
                        self._audit_invalid_encoding(str(e))
                        continue
                    if line is None:
                        continue

                    response = self._handle_line(line)
                    try:
                        conn.sendall((response + "\n").encode("utf-8"))
                    except OSError:
                        pass
                    requests_handled += 1
        finally:
            # Sin esto, alcanzar max_requests deja el socket ESCUCHANDO
            # sin nadie del otro lado leyéndolo — una conexión nueva
            # quedaría colgada en vez de fallar con un error claro
            # (ECONNREFUSED), ya que el archivo del socket seguiría
            # existiendo en el filesystem aunque nadie lo atienda.
            try:
                self._server_socket.close()
            except OSError:
                pass

    @staticmethod
    def _read_line(conn: socket.socket) -> str | None:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            buf += chunk
            if len(buf) > _MAX_LINE_BYTES:
                raise LineTooLongError(f"línea de más de {_MAX_LINE_BYTES} bytes sin salto de línea")
        line, _, _ = buf.partition(b"\n")
        try:
            return line.decode("utf-8")
        except UnicodeDecodeError as e:
            raise InvalidLineEncodingError(str(e)) from e

    def _handle_line(self, line: str) -> str:
        try:
            request = parse_request(line)
        except ProtocolError as e:
            return error_response(None, INVALID_PARAMS, str(e))

        if request.method not in self.allowed_methods:
            self._audit_denied(request.method)
            return error_response(
                request.id, PERMISSION_DENIED,
                f"'{request.method}' no está declarado en kernel_services de esta skill",
            )

        try:
            result = self.bus.dispatch(request.method, request.params, skill_name=self.skill_name)
        except (ServiceNotFoundError, ActionNotFoundError, ArtifactNotFoundError) as e:
            # Errores de protocolo del propio bus — el mensaje solo cita
            # el nombre de servicio/acción/artefacto que la SKILL misma
            # pasó como parámetro, nunca un detalle interno del host.
            # Seguros de devolver tal cual.
            self._audit_call(request.method, "failure", str(e))
            return error_response(request.id, METHOD_NOT_FOUND, str(e))
        except Exception as e:  # noqa: BLE001 — a propósito, tiene que atrapar CUALQUIER excepción (ver abajo)
            # Hallazgo de la revisión de seguridad 2026-07-09: antes se
            # devolvía str(e) crudo a la skill — un servicio real puede
            # fallar de formas que revelan detalles del host (rutas de
            # archivo reales ya resueltas desde un "artifact://" de
            # entrada, mensajes de librerías de terceros, etc.). Si esa
            # misma skill también tiene permiso de red, eso es una vía
            # de exfiltración. El detalle completo se sigue registrando
            # server-side (log + auditoría) para diagnóstico — solo deja
            # de cruzar el socket hacia el proceso de menor confianza.
            logger.warning(f"Servicio '{request.method}' falló para la skill '{self.skill_name}': {e}")
            self._audit_call(request.method, "failure", str(e))
            return error_response(
                request.id, INTERNAL_ERROR, f"el servicio '{request.method}' falló procesando el pedido"
            )

        self._audit_call(request.method, "success", "")
        return success_response(request.id, result)

    def _audit_call(self, method: str, outcome: str, detail: str) -> None:
        summary = f"Skill '{self.skill_name}' llamó a '{method}'"
        if detail:
            summary += f": {detail}"
        audit_log.record(
            AuditEvent(
                event_type="kernel_service_call",
                summary=summary,
                context={"skill": self.skill_name, "method": method},
                outcome=outcome,
            )
        )

    def _audit_denied(self, method: str) -> None:
        audit_log.record(
            AuditEvent(
                event_type="kernel_service_denied",
                summary=f"Skill '{self.skill_name}' intentó llamar a '{method}' sin declararlo en kernel_services",
                context={"skill": self.skill_name, "method": method},
                outcome="failure",
            )
        )

    def _audit_line_too_long(self) -> None:
        audit_log.record(
            AuditEvent(
                event_type="kernel_line_too_long",
                summary=f"Skill '{self.skill_name}' mandó una línea de más de {_MAX_LINE_BYTES} bytes sin salto de línea — conexión cortada",
                context={"skill": self.skill_name},
                outcome="failure",
            )
        )

    def _audit_invalid_encoding(self, detail: str) -> None:
        # Mejora real de diagnóstico portada de kal (2026-09-28, vía
        # scripts/check_kernel_drift.py): antes se descartaba el
        # detalle concreto de UnicodeDecodeError (qué byte, en qué
        # posición) — quedaba solo "no es UTF-8 válido", sin nada útil
        # para diagnosticar CUÁL fue el problema exacto de esa skill.
        audit_log.record(
            AuditEvent(
                event_type="kernel_line_invalid_encoding",
                summary=f"Skill '{self.skill_name}' mandó una línea que no es UTF-8 válido ({detail}) — conexión cortada",
                context={"skill": self.skill_name},
                outcome="failure",
            )
        )
