"""
Tests de kernel/permissions/network_safety.py — extraído de
tool_integration/adapters/browser.py para que download_manager.py
reuse la misma protección (allowlist de dominios + rechazo de IPs
privadas/reservadas) sin duplicar la lógica.
"""
from __future__ import annotations

from kernel.permissions.network_safety import (
    is_domain_allowed,
    is_hostname_allowed,
    is_unsafe_ip,
)


def test_is_unsafe_ip_accepts_normal_public_addresses():
    assert is_unsafe_ip("93.184.216.34") is False
    assert is_unsafe_ip("8.8.8.8") is False


def test_is_unsafe_ip_rejects_private_loopback_linklocal_and_garbage():
    assert is_unsafe_ip("127.0.0.1") is True
    assert is_unsafe_ip("10.1.2.3") is True
    assert is_unsafe_ip("169.254.169.254") is True
    assert is_unsafe_ip("::1") is True
    assert is_unsafe_ip(None) is True
    assert is_unsafe_ip("no-es-una-ip") is True


def test_is_domain_allowed_denies_everything_when_list_is_empty():
    assert is_domain_allowed("https://unsplash.com/photos/x", []) is False


def test_is_domain_allowed_accepts_exact_match():
    assert is_domain_allowed("https://unsplash.com/photos/x", ["unsplash.com"]) is True


def test_is_domain_allowed_accepts_subdomain():
    assert is_domain_allowed("https://images.unsplash.com/photo-1", ["unsplash.com"]) is True


def test_is_domain_allowed_rejects_unrelated_domain():
    assert is_domain_allowed("https://evil.com/x", ["unsplash.com"]) is False


def test_is_domain_allowed_is_case_insensitive():
    assert is_domain_allowed("https://Unsplash.COM/x", ["unsplash.com"]) is True


def test_is_hostname_allowed_same_matching_as_is_domain_allowed():
    """Extraída de is_domain_allowed() para kernel/permissions/network_access_manager.py,
    que ya tiene el hostname (no una URL) como resource_key."""
    assert is_hostname_allowed("unsplash.com", ["unsplash.com"]) is True
    assert is_hostname_allowed("images.unsplash.com", ["unsplash.com"]) is True
    assert is_hostname_allowed("evil.com", ["unsplash.com"]) is False
    assert is_hostname_allowed("unsplash.com", []) is False
    assert is_hostname_allowed("Unsplash.COM", ["unsplash.com"]) is True
