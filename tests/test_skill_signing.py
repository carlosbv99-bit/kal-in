"""
Tests de kernel/registry/skill_signing.py (F3 del plan de
marketplace): firma/verificación de integridad de un paquete de
skill, con la clave de un AUTOR externo (nunca la clave propia de kal
de kernel/registry/signing.py).

Alcance deliberado (ver docstring del módulo bajo test): esto prueba
integridad del paquete, no autoridad del autor.
"""
from __future__ import annotations

from kernel.registry.skill_signing import SkillSigner, verify_skill_signature
from kernel.registry.skills import set_skill_enabled


def _make_skill_dir(tmp_path, name="mi_skill", extra_files: dict[str, str] | None = None):
    skill_dir = tmp_path / name
    skill_dir.mkdir()
    (skill_dir / "skill.yaml").write_text("name: mi_skill\nversion: '0.1.0'\nenabled: true\n", encoding="utf-8")
    (skill_dir / "tool.py").write_text("class MiTool:\n    pass\n", encoding="utf-8")
    for rel_path, content in (extra_files or {}).items():
        path = skill_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return skill_dir


def test_unsigned_skill_directory_is_reported_as_unsigned(tmp_path):
    skill_dir = _make_skill_dir(tmp_path)

    assert verify_skill_signature(skill_dir) == "unsigned"


def test_sign_and_verify_roundtrip(tmp_path):
    skill_dir = _make_skill_dir(tmp_path)
    signer = SkillSigner(key_dir=tmp_path / "keys")
    signer.write_signature(skill_dir)

    assert verify_skill_signature(skill_dir) == "verified"


def test_tampering_with_code_after_signing_is_detected(tmp_path):
    skill_dir = _make_skill_dir(tmp_path)
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)

    (skill_dir / "tool.py").write_text("class MiTool:\n    def execute(self): return 'alterado'\n", encoding="utf-8")

    assert verify_skill_signature(skill_dir) == "tampered"


def test_tampering_with_skill_yaml_after_signing_is_detected(tmp_path):
    """
    Crítico: permissions/kernel_services viven en skill.yaml — si esto
    no quedara cubierto por la firma, alguien podría mantener el
    código intacto pero escalar permisos sin invalidar nada.
    """
    skill_dir = _make_skill_dir(tmp_path)
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)

    (skill_dir / "skill.yaml").write_text(
        "name: mi_skill\nversion: '0.1.0'\nenabled: true\npermissions: [network]\n", encoding="utf-8"
    )

    assert verify_skill_signature(skill_dir) == "tampered"


def test_toggling_enabled_after_signing_does_not_invalidate_the_signature(tmp_path):
    """
    BUG REAL encontrado probando el Skill Creator (2026-07-20): antes
    de este fix, esto daba "tampered" — habilitar/deshabilitar una
    skill YA firmada (set_skill_enabled(), lo mismo que hace
    scripts/enable_skill.py) rompía su propia firma en el acto, aunque
    `enabled` sea explícitamente una decisión de instalación LOCAL, no
    del autor (ver kernel/registry/skills.py).
    """
    skill_dir = _make_skill_dir(tmp_path)
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)
    assert verify_skill_signature(skill_dir) == "verified"

    set_skill_enabled(skill_dir, False)
    assert verify_skill_signature(skill_dir) == "verified"

    set_skill_enabled(skill_dir, True)
    assert verify_skill_signature(skill_dir) == "verified"


def test_tampering_with_a_field_other_than_enabled_in_skill_yaml_still_detected_after_toggling_enabled(tmp_path):
    """Que 'enabled' esté excluido del hash no debe abrir la puerta a
    colar OTROS cambios en skill.yaml (p.ej. escalar permissions) al
    mismo tiempo que se activa/desactiva la skill."""
    skill_dir = _make_skill_dir(tmp_path)
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)

    (skill_dir / "skill.yaml").write_text(
        "name: mi_skill\nversion: '0.1.0'\nenabled: true\npermissions: [network]\n", encoding="utf-8"
    )

    assert verify_skill_signature(skill_dir) == "tampered"


def test_adding_a_new_file_after_signing_is_detected(tmp_path):
    skill_dir = _make_skill_dir(tmp_path)
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)

    (skill_dir / "helper.py").write_text("# archivo nuevo, no firmado\n", encoding="utf-8")

    assert verify_skill_signature(skill_dir) == "tampered"


def test_removing_a_file_after_signing_is_detected(tmp_path):
    skill_dir = _make_skill_dir(tmp_path, extra_files={"helper.py": "# ayudante\n"})
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)

    (skill_dir / "helper.py").unlink()

    assert verify_skill_signature(skill_dir) == "tampered"


def test_corrupt_signature_file_is_treated_as_tampered_not_a_crash(tmp_path):
    skill_dir = _make_skill_dir(tmp_path)
    (skill_dir / "skill.sig").write_text("esto no es json valido {{{", encoding="utf-8")

    assert verify_skill_signature(skill_dir) == "tampered"


def test_signature_file_missing_required_fields_is_tampered(tmp_path):
    skill_dir = _make_skill_dir(tmp_path)
    (skill_dir / "skill.sig").write_text('{"algorithm": "ed25519"}', encoding="utf-8")

    assert verify_skill_signature(skill_dir) == "tampered"


def test_pycache_and_pyc_files_are_ignored_by_the_manifest(tmp_path):
    """
    __pycache__/.pyc no son contenido real de la skill — no deberían
    poder invalidar una firma válida si aparecen después (p.ej. al
    ejecutar algo localmente antes de instalar de verdad).
    """
    skill_dir = _make_skill_dir(tmp_path)
    SkillSigner(key_dir=tmp_path / "keys").write_signature(skill_dir)

    pycache = skill_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "tool.cpython-311.pyc").write_bytes(b"\x00\x01\x02")

    assert verify_skill_signature(skill_dir) == "verified"


def test_author_keypair_persists_across_signer_instances(tmp_path):
    skill_dir = _make_skill_dir(tmp_path)
    key_dir = tmp_path / "keys"

    signer_a = SkillSigner(key_dir=key_dir)
    signer_a.write_signature(skill_dir)
    fingerprint_a = signer_a.public_key_hex()

    signer_b = SkillSigner(key_dir=key_dir)
    assert signer_b.public_key_hex() == fingerprint_a
    # Re-firmar con la "misma identidad" (instancia nueva, mismo key_dir)
    # sigue verificando bien.
    signer_b.write_signature(skill_dir)
    assert verify_skill_signature(skill_dir) == "verified"


def test_private_key_file_has_restrictive_permissions(tmp_path):
    key_dir = tmp_path / "keys"
    SkillSigner(key_dir=key_dir)

    key_path = key_dir / "skill_author_key"
    assert key_path.exists()
    assert oct(key_path.stat().st_mode)[-3:] == "600"


def test_signature_from_a_different_author_key_does_not_verify_someone_elses_content(tmp_path):
    """
    Dos autores distintos, cada uno con su propio keypair — la firma
    de uno no debería poder pasar como si fuera de otro (no hay
    reutilización de material entre identidades).
    """
    skill_dir = _make_skill_dir(tmp_path)
    signer_1 = SkillSigner(key_dir=tmp_path / "author1")
    signer_2 = SkillSigner(key_dir=tmp_path / "author2")

    assert signer_1.public_key_hex() != signer_2.public_key_hex()

    signer_1.write_signature(skill_dir)
    assert verify_skill_signature(skill_dir) == "verified"

    # Re-firmar con el autor 2 reemplaza la firma — sigue siendo válida,
    # pero con una clave pública distinta (la del segundo autor).
    sig = signer_2.sign_skill(skill_dir)
    assert sig["author_public_key"] == signer_2.public_key_hex()
    assert sig["author_public_key"] != signer_1.public_key_hex()
