"""
Tests de scripts/install_from_market.py.

VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (A-1 en kal,
2026-09-27, portada acá vía scripts/check_kernel_drift.py):
args.skill_name (input de línea de comandos) se usaba SIN NINGUNA
sanitización para armar local_dest = DEFAULT_SKILLS_DIR / skill_name —
un --skill-name "../.algo" escribía y HABILITABA una skill fuera de
skills/ por completo. Mismo patrón exacto que K-1 (skills.py) y K-6
(registry.py/versioning.py), solo que en el instalador de market.

PoC reproducido con un repo "market" LOCAL sintético (git clone
funciona igual contra un path local que contra una URL real, mismo
patrón que tests/test_skill_market.py) — sin red.
"""
from __future__ import annotations

import pytest
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import install_from_market

from kernel.registry.skill_signing import SkillSigner

_SKILL_YAML_TEMPLATE = """name: {name}
description: "una skill de prueba"
version: "0.1.0"
entry_point: "tool:GreetTool"
enabled: true
permissions: []
"""

_TOOL_SOURCE = "class GreetTool:\n    pass\n"


def _git(repo_dir, *args):
    subprocess.run(["git", *args], cwd=str(repo_dir), check=True, capture_output=True)


def _write_skill(skill_dir, name, key_dir):
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(_SKILL_YAML_TEMPLATE.format(name=name), encoding="utf-8")
    (skill_dir / "tool.py").write_text(_TOOL_SOURCE, encoding="utf-8")
    SkillSigner(key_dir=key_dir).write_signature(skill_dir)


def _make_market_repo_with_traversal_skill(tmp_path):
    """
    Repo "market" con una skill LEGÍTIMA en skills/greeter/, más una
    "maliciosa" en un directorio HERMANO de skills/ (no adentro) — el
    ataque real: pedir --skill-name "../.audit_pwned" hace que
    fetch_skill_from_market busque en repo_dir/skills/../.audit_pwned,
    que resuelve a repo_dir/.audit_pwned (un nivel arriba de skills/).
    """
    repo_dir = tmp_path / "market_repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test")

    key_dir = tmp_path / "attacker_keys"
    _write_skill(repo_dir / "skills" / "greeter", "greeter", key_dir)
    _write_skill(repo_dir / ".audit_pwned", "pwned", key_dir)

    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "market con un intento de path traversal")
    return repo_dir


def test_traversal_skill_name_is_rejected_before_touching_the_network(tmp_path, monkeypatch, capsys):
    from audit.audit_log import audit_log

    market_repo = _make_market_repo_with_traversal_skill(tmp_path)
    workdir = tmp_path / "kal_instalacion"
    workdir.mkdir()
    # El rechazo AHORA audita (ver _audit_rechazo en el script): sin aislar el
    # archivo, este test escribiría en el audit log REAL del repo.
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit-rechazo.log")
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        sys, "argv",
        ["install_from_market.py", "../.audit_pwned", "--market", str(market_repo), "--yes"],
    )

    try:
        install_from_market.main()
        raised = False
    except SystemExit:
        raised = True

    assert raised, "debería rechazar el nombre inválido con SystemExit"
    assert "inválido" in capsys.readouterr().out
    # Nada se escribió ni dentro ni fuera de skills/ — se rechazó ANTES
    # de tocar la red (fetch_skill_from_market ni se llegó a invocar).
    assert not (workdir / "skills").exists()
    assert not (workdir / ".audit_pwned").exists()
    assert not (workdir.parent / ".audit_pwned").exists()


def test_a_legitimate_skill_name_still_installs_normally(tmp_path, monkeypatch):
    from audit.audit_log import audit_log

    market_repo = _make_market_repo_with_traversal_skill(tmp_path)
    workdir = tmp_path / "kal_instalacion_legitima"
    workdir.mkdir()
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit.log")
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        sys, "argv",
        ["install_from_market.py", "greeter", "--market", str(market_repo), "--yes"],
    )

    install_from_market.main()

    assert (workdir / "skills" / "greeter" / "tool.py").exists()


# --- el rastro de los rechazos por seguridad (2026-09-29) -------------------
#
# El script auditaba SOLO el éxito (`audit_skill_enable_change`, línea 160): se podía
# intentar instalar una skill maliciosa desde un market comprometido, el script la
# rechazaba bien por firma, y no quedaba registro del intento. Mismo desbalance que
# `verify_tool_integrity()` y el circuit breaker; mismo criterio que `rollback_tool()`
# con `tool_tamper_detected`.


def _eventos_de_rechazo(audit_log):
    """Solo lee: cada test ya apuntó `audit_log.path` a su propio archivo temporal."""
    return [
        e for e in audit_log.tail(50) if e.get("event_type") == "market_install_rejected"
    ]


def test_el_nombre_malicioso_queda_auditado(tmp_path, monkeypatch, capsys):
    from audit.audit_log import audit_log

    market_repo = _make_market_repo_with_traversal_skill(tmp_path)
    workdir = tmp_path / "kal_instalacion_auditada"
    workdir.mkdir()
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit-rechazo.log")
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        sys, "argv",
        ["install_from_market.py", "../.audit_pwned", "--market", str(market_repo), "--yes"],
    )

    with pytest.raises(SystemExit):
        install_from_market.main()

    rechazos = _eventos_de_rechazo(audit_log)
    assert len(rechazos) == 1
    evento = rechazos[0]
    assert evento["context"]["reason"] == "nombre_invalido"
    assert evento["context"]["skill_name"] == "../.audit_pwned"
    assert evento["outcome"] == "failure"


def test_una_firma_manipulada_queda_auditada(tmp_path, monkeypatch, capsys):
    """
    El caso más importante: una skill firmada y después EDITADA (market comprometido
    o paquete manipulado en tránsito). `verify_skill_signature` verifica contra la
    clave embebida en el propio skill.sig, así que esto es exactamente lo que detecta.
    """
    from audit.audit_log import audit_log

    market_repo = _make_market_repo_with_traversal_skill(tmp_path)
    # Se firma en el builder y después se toca el archivo SIN volver a firmar.
    (market_repo / "skills" / "greeter" / "tool.py").write_text(
        "class GreetTool:\n    pass\n# línea agregada después de firmar\n", encoding="utf-8"
    )
    _git(market_repo, "add", "-A")
    _git(market_repo, "commit", "-m", "manipulada después de firmar")
    workdir = tmp_path / "kal_instalacion_firma"
    workdir.mkdir()
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit-rechazo.log")
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        sys, "argv",
        ["install_from_market.py", "greeter", "--market", str(market_repo), "--yes"],
    )

    with pytest.raises(SystemExit):
        install_from_market.main()

    assert not (workdir / "skills" / "greeter").exists(), "no se instala nada"
    rechazos = _eventos_de_rechazo(audit_log)
    assert len(rechazos) == 1
    assert rechazos[0]["context"]["reason"] == "firma_no_verificada"
    assert rechazos[0]["context"]["signature_status"] == "tampered"


def test_la_segunda_capa_de_defensa_queda_auditada_como_critica(tmp_path, monkeypatch):
    """
    Esa rama no debería alcanzarse nunca (la regex de validate_skill_name no admite
    '/'), así que se simula que la primera capa falló: llegar ahí significa que la
    defensa de arriba se rompió, y por eso se audita como crítica.
    """
    from audit.audit_log import audit_log

    market_repo = _make_market_repo_with_traversal_skill(tmp_path)
    workdir = tmp_path / "kal_instalacion_capa2"
    workdir.mkdir()
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit-rechazo.log")
    monkeypatch.setattr(install_from_market, "validate_skill_name", lambda name: None)
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        sys, "argv",
        ["install_from_market.py", "../.audit_pwned", "--market", str(market_repo), "--yes"],
    )

    with pytest.raises(SystemExit):
        install_from_market.main()

    assert not (workdir.parent / ".audit_pwned").exists()
    rechazos = _eventos_de_rechazo(audit_log)
    assert len(rechazos) == 1
    assert rechazos[0]["context"]["reason"] == "segunda_capa_de_defensa"
    assert rechazos[0]["context"]["severidad"] == "critica"


def test_un_rechazo_operacional_no_ensucia_el_audit_log(tmp_path, monkeypatch, capsys):
    """'Ya existe localmente' no es un intento adversarial: no se audita."""
    from audit.audit_log import audit_log

    market_repo = _make_market_repo_with_traversal_skill(tmp_path)
    workdir = tmp_path / "kal_instalacion_dup"
    (workdir / "skills" / "greeter").mkdir(parents=True)  # ya existe
    monkeypatch.setattr(audit_log, "path", tmp_path / "audit-rechazo.log")
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(
        sys, "argv",
        ["install_from_market.py", "greeter", "--market", str(market_repo), "--yes"],
    )

    with pytest.raises(SystemExit):
        install_from_market.main()

    assert "ya existe" in capsys.readouterr().out
    assert _eventos_de_rechazo(audit_log) == []
