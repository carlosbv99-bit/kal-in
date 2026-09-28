"""
Fase E4 (despliegue en modo solo-observación): consume en vivo el
stream de eventos de kernel/lifecycle/ebpf/syscall_events.bt durante uso REAL de
kal (no solo la suite de tests), para conocer la tasa de falsos
positivos antes de considerar enforcement (E5) — nunca bloquea nada,
solo registra en la auditoría.

Arranque elegido explícitamente por el usuario (ver README.md, Fase
E4): manual, con sudo, por sesión — ni una regla sudoers permanente ni
un servicio systemd todavía (eso queda para evaluar en E7 si esta
evaluación resulta útil). Uso:

    sudo bpftrace kernel/lifecycle/ebpf/syscall_events.bt | python3 -m kernel.lifecycle.ebpf.observer

(o `bash kernel/lifecycle/ebpf/run_observer.sh`, que arma este mismo pipe).

El pipe separa el privilegio correctamente: bpftrace corre con sudo
(lo mínimo que necesita para cargar los programas en el kernel — ver
kernel/lifecycle/ebpf/prototype_syscalls.bt para por qué Claude no puede
correrlo él mismo en esta máquina). Este script corre SIN privilegios
— todo lo que hace es leer líneas de texto de stdin y llamar a
audit_log.record() (que ya sabe protegerse solo con su propio lock de
fcntl.flock, ver audit/audit_log.py) — nunca al revés, este proceso no
necesita ni debe correr con sudo.

Sobre dónde vive esto en un despliegue futuro (E7): mismo tier de
confianza que sandbox_runner (el único componente que ya toca el
socket de Docker) — no un cuarto proceso privilegiado nuevo. Ver
kernel/lifecycle/ebpf/event_consumer.py para esa discusión completa.
"""
from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import TextIO

from kernel.lifecycle.ebpf.event_consumer import parse_event_line, record_syscall_event
from utils.logger import get_logger

logger = get_logger(__name__)


def run(lines: Iterable[str]) -> tuple[int, int]:
    """
    Procesa `lines` (típicamente sys.stdin, un pipe desde bpftrace)
    hasta que se agote o se interrumpa con Ctrl+C — a diferencia de
    event_consumer.consume_stream(), que devuelve el conteo solo al
    final, esto cuenta de forma incremental para no perder el número
    real si se corta a mitad de una sesión de observación larga (el
    caso normal de uso: el usuario corta con Ctrl+C, no un stream que
    termina solo). Devuelve (líneas procesadas, violaciones registradas).
    """
    processed = 0
    recorded = 0
    try:
        for line in lines:
            processed += 1
            event = parse_event_line(line)
            if event is not None and record_syscall_event(event):
                recorded += 1
    except KeyboardInterrupt:
        pass
    return processed, recorded


def main(stream: TextIO | None = None) -> None:
    logger.info("Observador eBPF (Fase E4) activo — leyendo eventos, Ctrl+C para terminar")
    processed, recorded = run(stream if stream is not None else sys.stdin)
    logger.info(f"Observador eBPF detenido. Líneas procesadas: {processed}, violaciones registradas: {recorded}")


if __name__ == "__main__":
    main()
