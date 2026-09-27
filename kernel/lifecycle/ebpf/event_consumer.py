"""
Fase E2 (diseño del esquema de eventos + integración con la
auditoría): parsea las líneas JSON que emite
kernel/lifecycle/ebpf/syscall_events.bt y las convierte en AuditEvent —
escritas SIEMPRE a través del mismo audit_log.record() que usa el
resto del proyecto, nunca un segundo escritor paralelo con su propio
lock. Justificación de por qué esta fase existe: ya se corrigió una
condición de carrera real en audit_log.py causada por múltiples
escritores sin coordinar — agregar eBPF como fuente de eventos nueva
sin pensar este punto la hubiera reintroducido.

DECISIÓN DE DISEÑO — no todo syscall observado es una "violación de
política": connect/setuid/setresuid/ptrace dentro de un contenedor del
sandbox SIEMPRE son inesperados (network_mode=none, cap_drop=ALL,
usuario no-root — ver kernel/lifecycle/docker_runner.py — no hay ningún
escenario legítimo), así que verlos ya es una violación real, sin
necesitar reglas adicionales. execve NO lo es: la corrida real de la
Fase E1 (ver README.md) mostró que la mayoría de los execve observados
son la propia maquinaria de Docker (docker/runc/containerd-shim)
lanzando el contenedor, no algo sospechoso. Auditar cada execve como
"violación" diluiría el evento hasta volverlo inútil para alertar de
verdad — por eso execve se descarta acá (queda como observabilidad
pura de la Fase E1, no llega al log de auditoría en esta fase). Si más
adelante hace falta visibilidad de qué se ejecuta dentro del sandbox,
debería ser un event_type propio ("syscall_observed" o similar), nunca
mezclado con "syscall_policy_violation" — mezclarlos haría que un
humano revisando el log dejara de confiar en que ese tipo de evento
siempre significa algo real.

SOBRE QUIÉN CORRE ESTO — el consumidor de estos eventos (leer el
stdout de un bpftrace que corrió con root) es una superficie
privilegiada nueva en la arquitectura. Debería vivir en el mismo
proceso/tier de confianza que sandbox_runner (que ya es el único con
acceso al socket de Docker, ver docker-compose.yml), no como un cuarto
componente privilegiado distinto. Esta fase es solo el diseño + la
lógica de parseo/registro — 100% testeable sin ningún proceso ni
bpftrace real corriendo. La Fase E4 es donde esto se conecta a un
bpftrace de verdad, dentro de ese límite de confianza.

FILTRO DE CGROUP (adelantado de la Fase E7, 2026-07-10) — HALLAZGO
REAL de la evaluación en vivo de E4: el filtro de `comm` en los `.bt`
(python/python3) no distingue una skill SANDBOXEADA (donde
connect/setuid/etc. siempre son una violación real, `network_mode=none`
+ `cap_drop=ALL`) del proceso PRINCIPAL de kal (que sí tiene red real
— hablar con Ollama es normal). En la evaluación real, la propia
actividad de kal se registró como "violación" — el falso positivo que
esta fase buscaba encontrar. `is_sandboxed_container_process()`
resuelve esto del lado de Python (más simple que pelear con matching
de patrones de cgroup dentro de bpftrace): lee `/proc/<pid>/cgroup` y
confirma que el proceso vive bajo un cgroup de Docker antes de tratar
la syscall como violación real.

Esto es una heurística de MEJOR ESFUERZO para modo OBSERVACIÓN, no la
verificación que respaldaría un enforcement real: se lee /proc DESPUÉS
de que bpftrace ya reportó el evento (por el pipe), así que hay una
ventana chica donde el proceso pudo haber terminado — en ese caso se
descarta (False), priorizando no generar ruido antes que no perder
ningún evento (correcto para observación, no para bloqueo). Un
enforcement de verdad (Fase E5) necesitaría este mismo chequeo hecho
DENTRO del programa eBPF en el momento de la syscall
(`bpf_get_current_cgroup_id()`), no desde afuera vía /proc.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audit.audit_log import AuditEvent, audit_log
from utils.logger import get_logger

logger = get_logger(__name__)

# Syscalls que, dentro de un contenedor del sandbox, no tienen NINGÚN
# escenario legítimo — verlos ya es una violación, sin heurísticas
# adicionales. execve queda deliberadamente afuera (ver docstring).
VIOLATION_SYSCALLS = frozenset({"connect", "setuid", "setresuid", "ptrace"})

# Patrones de ruta de cgroup de un contenedor Docker — "docker-<id>.scope"
# es el driver systemd (confirmado en esta máquina, ver README.md Fase
# E-1/E3), "/docker/<id>" es el driver cgroupfs (otras instalaciones).
# Sirve igual para cgroup v1 (varias líneas, una por jerarquía) y v2
# (una sola línea "0::<ruta>") — el patrón aparece en la ruta sin
# importar el formato de la jerarquía.
_DOCKER_CGROUP_MARKERS = ("docker-", "/docker/")


def is_sandboxed_container_process(pid: int, cgroup_file: Path | None = None) -> bool:
    """
    True si `pid` corre dentro del cgroup de un contenedor Docker (ver
    HALLAZGO DE FILTRO DE CGROUP en el docstring del módulo). False si
    no se pudo determinar (el proceso ya terminó, sin permiso para
    leer, etc.) — fail-closed hacia "no es una violación", no hacia
    "sí lo es", para minimizar falsos positivos en modo observación.
    """
    path = cgroup_file or Path(f"/proc/{pid}/cgroup")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(marker in content for marker in _DOCKER_CGROUP_MARKERS)


@dataclass
class SyscallEvent:
    syscall: str
    pid: int
    detail: dict[str, Any] = field(default_factory=dict)


def parse_event_line(line: str) -> SyscallEvent | None:
    """
    Parsea una línea JSON emitida por kernel/lifecycle/ebpf/syscall_events.bt.
    None (nunca una excepción) para líneas vacías o que no son JSON
    válido — bpftrace también emite líneas propias que no son eventos
    ("Attached N probes", warnings, etc.), hay que poder ignorarlas sin
    interrumpir el consumo del resto del stream.
    """
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "syscall" not in data or "pid" not in data:
        return None
    return SyscallEvent(syscall=data["syscall"], pid=data["pid"], detail=data)


def record_syscall_event(event: SyscallEvent) -> bool:
    """
    Registra `event` en la cadena de auditoría SI (y solo si) es una
    violación real: el syscall está en VIOLATION_SYSCALLS Y el proceso
    corre dentro del cgroup de un contenedor del sandbox (ver
    is_sandboxed_container_process — sin esto, la propia actividad de
    red del proceso principal de kal se registraría como violación,
    el falso positivo real que encontró la evaluación de la Fase E4).
    Siempre vía audit_log.record(), el mismo escritor único con lock
    de fcntl.flock que usa el resto del proyecto, nunca un lock aparte.
    Devuelve True si se registró, False si se descartó.
    """
    if event.syscall not in VIOLATION_SYSCALLS:
        return False
    if not is_sandboxed_container_process(event.pid):
        return False

    audit_log.record(
        AuditEvent(
            event_type="syscall_policy_violation",
            summary=f"Syscall inesperado dentro del sandbox: {event.syscall} (pid={event.pid})",
            context={"syscall": event.syscall, "pid": event.pid, **event.detail},
            outcome="failure",
        )
    )
    return True


def consume_stream(lines: Iterable[str]) -> int:
    """
    Procesa un stream completo de líneas (p.ej. el stdout de un
    bpftrace real, en la Fase E4) y devuelve cuántos eventos se
    registraron como violación. Una línea inválida o un evento que no
    es violación no interrumpen el consumo del resto.
    """
    recorded = 0
    for line in lines:
        event = parse_event_line(line)
        if event is None:
            continue
        if record_syscall_event(event):
            recorded += 1
    return recorded
