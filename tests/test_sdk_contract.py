"""
Tests de contrato de `sdk/` — la ÚNICA superficie pública que una
Skill (propia o de terceros) importa (ver sdk/__init__.py). Este
archivo no prueba comportamiento (eso ya lo cubren los tests de cada
Tool/Skill real) — fija la FORMA del contrato: nombres exportados,
campos de los dataclasses, miembros del enum de permisos. Si alguno de
estos tests falla, es una señal de que un cambio rompe compatibilidad
hacia atrás con cualquier Skill ya escrita contra `sdk/` — tratarlo
como se trataría un cambio de versión mayor, no un refactor interno
cualquiera (mismo criterio que agent_core/llm/provider.py::LLMProvider).

Paso 3 del roadmap de consolidación del Kernel ("Estabilizar SDK"),
alcance acordado con el usuario: pinnear el contrato ACTUAL con tests,
no versionado formal ni un SDK separable todavía — eso queda para
cuando haya un consumidor externo real.
"""
from __future__ import annotations

import dataclasses
import inspect
from abc import ABC

import pytest

import sdk
from sdk.artifacts import Artifact
from sdk.context import SOCKET_PATH, KernelError, call
from sdk.permissions import (
    RUNTIME_ENFORCED,
    UNSUPPORTED_RUNTIME_PERMISSIONS,
    Permission,
)
from sdk.skill import Tool, ToolManifest


def test_sdk_dunder_all_is_exactly_these_six_names():
    # Un nombre agregado o quitado acá es, por definición, un cambio de
    # contrato — cualquier Skill que haga `from sdk import X` depende
    # de que X siga estando en esta lista.
    assert set(sdk.__all__) == {"Artifact", "KernelError", "Permission", "Tool", "ToolManifest", "call"}


def test_sdk_top_level_import_exposes_the_six_names():
    from sdk import Artifact as A
    from sdk import KernelError as KE
    from sdk import Permission as P
    from sdk import Tool as T
    from sdk import ToolManifest as TM
    from sdk import call as c

    assert A is Artifact
    assert KE is KernelError
    assert P is Permission
    assert T is Tool
    assert TM is ToolManifest
    assert c is call


def test_artifact_has_exactly_the_documented_fields():
    field_names = {f.name for f in dataclasses.fields(Artifact)}
    assert field_names == {"modality", "uri", "metadata"}


def test_artifact_metadata_defaults_to_an_empty_dict():
    artifact = Artifact(modality="text", uri="")
    assert artifact.metadata == {}


def test_tool_manifest_has_exactly_the_documented_fields():
    field_names = {f.name for f in dataclasses.fields(ToolManifest)}
    assert field_names == {
        "name", "description", "requires_network", "requires_filesystem_write",
        "allowed_domains", "permissions", "created_by", "source_context", "parameters_schema",
    }


def test_tool_manifest_defaults():
    manifest = ToolManifest(name="x", description="y")
    assert manifest.requires_network is False
    assert manifest.requires_filesystem_write is False
    assert manifest.allowed_domains == []
    assert manifest.created_by == "system"
    assert manifest.source_context == ""
    assert manifest.parameters_schema == {"type": "object", "properties": {}}
    # FILESYSTEM_READ es implícito para TODA herramienta (ver __post_init__).
    assert Permission.FILESYSTEM_READ in manifest.permissions


def test_tool_is_an_abstract_base_class_requiring_execute():
    assert issubclass(Tool, ABC)
    assert "execute" in Tool.__abstractmethods__

    class Incomplete(Tool):
        manifest = ToolManifest(name="incompleto", description="sin execute")

    with pytest.raises(TypeError):
        Incomplete()  # no puede instanciarse sin implementar execute()


def test_permission_has_exactly_these_nine_members():
    assert {p.value for p in Permission} == {
        "filesystem_read", "filesystem_write", "network", "gpu",
        "camera", "microphone", "clipboard", "browser", "docker",
    }


def test_runtime_enforced_and_unsupported_partition_all_permissions():
    assert RUNTIME_ENFORCED | UNSUPPORTED_RUNTIME_PERMISSIONS == set(Permission)
    assert RUNTIME_ENFORCED & UNSUPPORTED_RUNTIME_PERMISSIONS == set()


def test_kernel_error_is_an_exception():
    assert issubclass(KernelError, Exception)


def test_call_is_callable_with_the_documented_signature():
    sig = inspect.signature(call)
    params = list(sig.parameters.values())
    assert params[0].name == "method"
    assert params[1].kind == inspect.Parameter.VAR_KEYWORD


def test_socket_path_is_the_documented_fixed_path():
    # kernel/lifecycle/skill_runner.py y kernel/registry/sandboxed_skill.py
    # se ponen de acuerdo en este mismo valor — un cambio acá rompe esa
    # coordinación implícita entre 3 archivos.
    assert SOCKET_PATH == "/workspace/.kal/kernel.sock"
