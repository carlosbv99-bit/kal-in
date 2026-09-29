"""
Tests de utils/secure_dir.py::ensure_private_dir() — usado por
kernel/registry/signing.py, kernel/registry/skill_signing.py,
kernel/permissions/access_manager.py, audit/audit_log.py y
utils/admin_token.py (M-4/B-6, auditoría externa 2026-09-27).
"""
from __future__ import annotations

from utils.secure_dir import ensure_private_dir


def test_creates_a_missing_directory_with_0700(tmp_path):
    target = tmp_path / "nested" / "keys"

    ensure_private_dir(target)

    assert target.is_dir()
    assert oct(target.stat().st_mode)[-3:] == "700"


def test_tightens_an_existing_directory_with_looser_permissions(tmp_path):
    """El caso real que motivó el fix: data/keys/ ya existía (de una
    instalación anterior a este fix) con permisos de grupo/otros."""
    tmp_path.chmod(0o775)

    ensure_private_dir(tmp_path)

    assert oct(tmp_path.stat().st_mode)[-3:] == "700"


def test_is_idempotent(tmp_path):
    target = tmp_path / "keys"
    ensure_private_dir(target)
    ensure_private_dir(target)

    assert oct(target.stat().st_mode)[-3:] == "700"
