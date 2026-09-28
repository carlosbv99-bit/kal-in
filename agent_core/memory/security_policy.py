"""
Memory Security Policy Engine (Fase 1) — el flujo remember() -> corto
-> mediano -> largo -> recall() -> LLM no tenía, en ningún punto, el
concepto de "¿es apropiado que esta información sea persistente/
compartible?". Dos riesgos reales motivaron esto: (1) una credencial
pegada por el usuario para depurar algo podía terminar promovida a
largo plazo PARA SIEMPRE sin ningún filtro ni forma de borrarla; (2)
algo guardado con un LLM 100% local podía terminar inyectado como
contexto a un proveedor en la NUBE si el usuario cambiaba
`llm.provider` más adelante, sin darse cuenta.

Dos ejes ortogonales, cada uno resuelto en un punto distinto del flujo
(ver agent_core/memory/manager.py y tool_integration/adapters/core_tools.py):
  - `MemoryClassification`: ¿esto merece persistir para siempre? Se
    decide SOLO en la promoción a mediano->largo plazo — nunca en
    remember()/consolidate_short_to_mid(), que necesitan poder
    contener una credencial pegada para que el agente la use en la
    tarea inmediata.
  - `MemorySharing`: ¿esto puede salir de esta máquina? Se decide en
    remember() (default fail-closed) y se aplica en recall() contra
    CUALQUIER tier, sin importar si ya se clasificó o no.

Fase 1 es deliberadamente angosta: solo patrones de formato CONOCIDO
(nunca heurísticas genéricas tipo "string largo parece un token", alto
riesgo de falsos positivos) y solo dos valores reales de clasificación
(PUBLIC/SECRET — SENSITIVE queda reservado para cuando exista un
clasificador real, ver docs/HISTORY.md).
"""
from __future__ import annotations

import re
from enum import Enum


class MemoryClassification(str, Enum):
    PUBLIC = "public"
    # Reservado para cuando exista un clasificador real (Fase 4) que
    # pueda poblarlo con criterio — Fase 1 nunca lo asigna, no hay
    # forma determinística de distinguir "sensible" de "público" sin él.
    SENSITIVE = "sensitive"
    SECRET = "secret"


class MemorySharing(str, Enum):
    LOCAL_ONLY = "local_only"
    CLOUD_OK = "cloud_ok"
    # Reservado para cuando exista una UX de aprobación — Fase 1 nunca
    # lo asigna.
    ASK = "ask"


# Patrones de formato CONOCIDO y específico, no heurísticas genéricas —
# cada uno con un label corto usado en el placeholder de redact().
#
# VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
# 2026-09-26), A-6: la lista original solo cubría 4 formatos (OpenAI
# clásico, AWS, GitHub clásico, JWT) — ni siquiera las variantes
# ACTUALES del mismo OpenAI (sk-proj-/sk-ant-, en uso real desde 2024)
# pasaban, dejándolas promoverse a memoria persistente sin redactar.
# Deliberadamente se sigue sin detección por entropía (mismo criterio
# ya documentado arriba: alto riesgo de falsos positivos sobre
# cualquier string que "parezca aleatorio") — cada patrón nuevo es,
# igual que los originales, un prefijo de formato público y conocido.
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("openai_project_key", re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws_temp_key", re.compile(r"ASIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("stripe_key", re.compile(r"(sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("google_api_key", re.compile(r"AIzaSy[A-Za-z0-9_-]{33}")),
    ("groq_key", re.compile(r"gsk_[A-Za-z0-9]{20,}")),
    ("xai_key", re.compile(r"xai-[A-Za-z0-9]{20,}")),
    ("huggingface_token", re.compile(r"hf_[A-Za-z0-9]{20,}")),
    ("npm_token", re.compile(r"npm_[A-Za-z0-9]{30,}")),
    ("sendgrid_key", re.compile(r"SG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")),
    ("pypi_token", re.compile(r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{20,}")),
    ("gitlab_pat", re.compile(r"glpat-[A-Za-z0-9_-]{20,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")),
    ("private_key_block", re.compile(
        r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?-----END (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
        re.DOTALL,
    )),
]


def classify(content: str) -> MemoryClassification:
    """SECRET si algún patrón de credencial conocida matchea, PUBLIC si
    no — nunca devuelve SENSITIVE (ver docstring del módulo)."""
    for _, pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            return MemoryClassification.SECRET
    return MemoryClassification.PUBLIC


def redact(content: str) -> str:
    """
    Reemplaza CADA span que matchee un patrón conocido por
    "[REDACTED:<label>]", preservando el resto del texto tal cual —
    nunca resume ni infiere un motivo ("para depuración") que un regex
    no puede confirmar de verdad (decisión explícita: preservar el
    conocimiento real, eliminar solo el secreto).
    """
    redacted = content
    for label, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{label}]", redacted)
    return redacted


def is_cloud_provider(provider: str) -> bool:
    """
    True para "openai_compatible". Nota conocida: un backend LOCAL que
    hable el wire format OpenAI (LM Studio/vLLM, ver
    ConversationEngineConfig.provider) también cae acá — sobre-bloquea
    ese caso (recall() trae menos de lo que podría), nunca al revés
    (nunca deja pasar de más). Limitación segura, documentada, no una
    fuga — refinarla con network_safety.py::is_unsafe_ip() sobre el
    base_url queda para cuando haga falta de verdad.
    """
    return provider == "openai_compatible"
