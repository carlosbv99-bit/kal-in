"""
Tests del sanitizador de evidencia (`agent_core/epistemics/sanitize.py`).

Tercer eje, independiente de admisión y de creencia: **¿puedo dejar que esto
salga hacia el modelo?** Un dato puede ser real, estar admitido y sostener una
conclusión, y aun así no poder viajar a un proveedor en la nube.

Lo que se fija acá:

1. La política de secretos NO se reinventa: se reusa `security_policy.py`, con
   el mismo criterio que `recall()` (nube → no se incluye; local → se redacta).
2. El marcador de no confiable viaja con el dato.
3. **El sumidero no es solo `Evidence.content`**: el claim de una hipótesis, la
   pregunta y el déficit son texto que también puede contener una credencial
   pegada por el usuario, y también pasan por el sanitizador.
"""
from __future__ import annotations

from agent_core.epistemics import (
    EpistemicStore,
    sanitize_for_context,
    sanitize_text,
)
from kal_epistemics import (
    Evidence,
    EvidenceOrigin,
    EvidenceStatus,
    ReopenCondition,
)

# Credencial con formato CONOCIDO (mismo criterio que security_policy.py: nunca
# detección por entropía), sintética y sin ningún valor real.
_SECRETO = "ghp_" + "A" * 36

_LOCAL = "ollama"
_NUBE = "openai_compatible"


def _store() -> EpistemicStore:
    return EpistemicStore(clock=lambda: 1000.0)


def _evidencia(**kwargs) -> Evidence:
    return Evidence.from_content(
        kwargs.pop("content", "dato observado"),
        origin=kwargs.pop("origin", EvidenceOrigin.KERNEL_OBSERVED),
        source="run_code",
        anchor="hash",
        **kwargs,
    )


# --- política de secretos: reusada, no reinventada ------------------------


def test_una_credencial_se_redacta_con_proveedor_local():
    texto = sanitize_text(f"la clave es {_SECRETO}", provider=_LOCAL)

    assert _SECRETO not in texto
    assert "[REDACTED:github_token]" in texto
    # Preserva el resto del texto: no resume ni inventa un motivo.
    assert "la clave es" in texto


def test_una_credencial_no_vuelve_al_contexto_con_proveedor_en_la_nube():
    """Fail-closed, mismo criterio que recall()."""
    assert sanitize_text(f"la clave es {_SECRETO}", provider=_NUBE) is None


def test_texto_vacio_o_en_blanco_no_vuelve():
    assert sanitize_text("   ", provider=_LOCAL) is None
    assert sanitize_text("", provider=_LOCAL) is None


def test_el_texto_largo_se_acota():
    texto = sanitize_text("x" * 1000, provider=_LOCAL, max_chars=50)

    assert len(texto) == 51
    assert texto.endswith("…")


# --- el marcador viaja con el dato ---------------------------------------


def test_el_marcador_de_no_confiable_acompana_al_contenido_externo():
    ev = _evidencia(origin=EvidenceOrigin.TOOL_EXTERNAL, content="una página dice X")

    saneada = sanitize_for_context(ev, provider=_LOCAL)

    assert "CONTENIDO EXTERNO NO CONFIABLE" in saneada.text
    assert "no instrucciones" in saneada.text


def test_la_evidencia_en_cuarentena_no_vuelve_al_contexto():
    ev = _evidencia(status=EvidenceStatus.QUARANTINED, quarantine_reason="inyección")

    assert sanitize_for_context(ev, provider=_LOCAL) is None


def test_el_snippet_de_evidencia_esta_acotado():
    ev = _evidencia(content="x" * 5000)

    saneada = sanitize_for_context(ev, provider=_LOCAL)

    assert saneada.truncated is True
    assert len(saneada.text) < 400


# --- los sumideros que NO son Evidence.content ----------------------------


def test_el_bloque_no_reinyecta_una_credencial_pegada_en_el_claim():
    store = _store()
    store.ledger("s").record_hypothesis(f"el deploy falló y la clave {_SECRETO} estaba vencida")

    bloque = store.context_block("s", provider=_LOCAL)

    assert _SECRETO not in bloque
    assert "[REDACTED:github_token]" in bloque


def test_el_bloque_omite_la_hipotesis_con_proveedor_en_la_nube():
    store = _store()
    store.ledger("s").record_hypothesis(f"el deploy falló y la clave {_SECRETO} estaba vencida")

    bloque = store.context_block("s", provider=_NUBE)

    assert _SECRETO not in bloque
    assert "omitida(s) por política de exposición" in bloque


def test_el_bloque_cita_la_evidencia_exogena_sanitizada():
    store = _store()
    ledger = store.ledger("s")
    condicion = ReopenCondition.from_untrusted(content_keywords=("Out of memory",))
    ledger.record_hypothesis("Se cayó por OOM", reopen_conditions=[condicion])

    store.ingest_tool_result(
        session_id="s",
        tool_name="run_code",
        observation="kernel: Out of memory: Killed process 4821",
    )
    bloque = store.context_block("s", provider=_LOCAL)

    assert "evidencia:" in bloque
    assert "observado por el kernel" in bloque
    assert "Out of memory" in bloque


def test_el_bloque_no_cita_evidencia_propia_como_respaldo():
    """Una hipótesis que solo tiene evidencia endógena no debe mostrar cita:
    mostrarla la haría parecer respaldada, que es justo lo que no es."""
    store = _store()
    ledger = store.ledger("s")
    ev = Evidence.from_content("lo dijo el modelo", origin=EvidenceOrigin.MODEL_GENERATED)
    ledger.add_evidence(ev)
    ledger.record_hypothesis("Una conjetura", evidence_links=[ev.id])

    bloque = store.context_block("s", provider=_LOCAL)

    assert "evidencia:" not in bloque
