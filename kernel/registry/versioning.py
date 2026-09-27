"""
Persistencia versionada de herramientas dinámicas.

Antes de esto, una herramienta dinámica activada solo existía como un
string `source_code` en memoria de proceso (ver PendingTool/
DynamicSandboxedTool en registry.py) — nada llegaba a disco, y por
tanto no había manera de "volver a la versión anterior" ni de firmar
algo persistente. Cada activación (inicial o re-propuesta con código
nuevo) queda como un archivo `<name>_v<N>.py` inmutable — nunca se
sobreescribe una versión ya escrita, igual espíritu que el audit log
append-only.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

DEFAULT_VERSIONS_DIR = Path("data/tool_versions")

_VALID_TOOL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def is_valid_tool_name(name: str) -> bool:
    """
    Expuesto para que el LLAMADOR (kernel/registry/registry.py::
    propose_dynamic_tool()) pueda rechazar un nombre inválido TEMPRANO
    — antes de validar código, correr el sandbox de prueba, o tocar
    disco — con el mismo criterio exacto que la segunda capa de
    defensa de _tool_dir() de abajo. Una sola fuente de verdad para el
    charset válido, nunca dos regex que puedan divergir.
    """
    return bool(_VALID_TOOL_NAME.match(name))


class ToolVersionStore:
    def __init__(self, base_dir: Path | str = DEFAULT_VERSIONS_DIR):
        self.base_dir = Path(base_dir)

    def _tool_dir(self, name: str, create: bool = False) -> Path:
        """
        VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
        2026-09-26), K-6: `name` (elegido por el LLM vía
        ToolRegistry.propose_dynamic_tool(), kernel/registry/
        registry.py) se usaba SIN sanitizar para armar
        `self.base_dir / name`, seguido de mkdir(parents=True,
        exist_ok=True) en save_version() — mismo patrón que K-1
        (kernel/registry/sandboxed_skill.py). El sufijo `_vN.py` de los
        archivos en sí acota el impacto (no pisa sitecustomize.py
        directo), pero igual permite escritura fuera del store e
        inyección de versiones ajenas. registry.py YA valida el nombre
        en propose_dynamic_tool() antes de llegar acá (fail rápido,
        sin tocar disco) — este chequeo es la segunda capa, mismo
        criterio de defensa en profundidad que sandboxed_skill.py,
        para CUALQUIER llamador de esta clase (todos los métodos
        públicos pasan por acá, ver list_versions/save_version/
        read_version de abajo).
        """
        if not _VALID_TOOL_NAME.match(name):
            raise ValueError(
                f"name '{name}' inválido para ToolVersionStore — solo minúsculas, dígitos, '_' y "
                "'-', debe empezar con minúscula o dígito, máximo 64 caracteres "
                "(^[a-z0-9][a-z0-9_-]{0,63}$)."
            )
        tool_dir = self.base_dir / name
        if not tool_dir.resolve().is_relative_to(self.base_dir.resolve()):
            raise ValueError(f"name '{name}' resuelve fuera de la raíz de versiones — rechazado.")
        if create:
            tool_dir.mkdir(parents=True, exist_ok=True)
        return tool_dir

    def list_versions(self, name: str) -> list[int]:
        tool_dir = self._tool_dir(name)
        if not tool_dir.exists():
            return []
        versions = []
        for path in tool_dir.glob(f"{name}_v*.py"):
            suffix = path.stem.rsplit("_v", 1)[-1]
            if suffix.isdigit():
                versions.append(int(suffix))
        return sorted(versions)

    def latest_version(self, name: str) -> int | None:
        versions = self.list_versions(name)
        return versions[-1] if versions else None

    def next_version(self, name: str) -> int:
        return (self.latest_version(name) or 0) + 1

    def save_version(
        self, name: str, version: int, source_code: str, manifest_dict: dict, signature: str
    ) -> None:
        """
        `version` se calcula previamente (ver next_version) y se pasa
        explícito en vez de recalcularse aquí, porque el llamador
        (ToolRegistry._activate) necesita ese mismo número para firmar
        el contenido ANTES de escribirlo — firmar y persistir deben
        usar el mismo número de versión, no dos cálculos independientes.
        """
        tool_dir = self._tool_dir(name, create=True)
        (tool_dir / f"{name}_v{version}.py").write_text(source_code, encoding="utf-8")
        (tool_dir / f"{name}_v{version}.manifest.json").write_text(
            json.dumps(
                {
                    "manifest": manifest_dict,
                    "signature": signature,
                    "version": version,
                    "created_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def read_version(self, name: str, version: int) -> tuple[str, dict]:
        tool_dir = self._tool_dir(name)
        source_path = tool_dir / f"{name}_v{version}.py"
        manifest_path = tool_dir / f"{name}_v{version}.manifest.json"
        if not source_path.exists() or not manifest_path.exists():
            raise FileNotFoundError(f"No existe la versión {version} de la herramienta '{name}'")
        source_code = source_path.read_text(encoding="utf-8")
        sidecar = json.loads(manifest_path.read_text(encoding="utf-8"))
        return source_code, sidecar


tool_version_store = ToolVersionStore()
