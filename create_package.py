#!/usr/bin/env python3
"""
Script para empaquetar los archivos importantes de kal-in en un archivo
ZIP — pensado para entregarle una visión completa del proyecto a un
auditor/especialista externo, sin que tenga que clonar el repo entero
(ni arrastrar `.venv/`, `data/`, `logs/`, `.env` con claves reales, ni
`__pycache__`).

Revisado 2026-09-27 preparando una auditoría externa: la lista estaba
desactualizada desde el split kal/kal-in (referenciaba "setup.py",
que no existe en este repo — vive en el repo nuevo `kal`, el kernel
puro), tenía "code_analysis/" duplicado, y le faltaban documentos
clave para un auditor (docs/HISTORY.md, LICENSE, las versiones en
español de README/CONTRIBUTING).
"""

import zipfile
from datetime import datetime, timezone
from pathlib import Path

# Nunca empaquetar estos, aunque aparezcan dentro de un directorio
# incluido más abajo — ruido (bytecode compilado) o, en el caso de
# ".env", un riesgo real: puede tener claves de API reales de uso
# local (ver docs/HISTORY.md, revisión de auditoría 2026-09-27).
_EXCLUDED_NAMES = {"__pycache__", ".pytest_cache", ".env", "node_modules"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def _is_excluded(path: Path) -> bool:
    return any(part in _EXCLUDED_NAMES for part in path.parts) or path.suffix in _EXCLUDED_SUFFIXES


def create_kal_package():
    """Función principal para crear el paquete ZIP de kal-in."""

    # Directorio raíz del proyecto
    project_root = Path(__file__).parent
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    package_name = f"kal-in_project_package_{timestamp}_utc.zip"

    # Lista de archivos y directorios importantes a incluir
    important_files_and_dirs = [
        # Documentación principal
        "README.md",
        "README.es.md",
        "CONTRIBUTING.md",
        "CONTRIBUTING.es.md",
        "LICENSE",
        "docs/HISTORY.md",
        "scripts/usage_guide.md",

        # Configuración
        "config/config.yaml",
        "Dockerfile",
        "docker-compose.yml",

        # Directorios principales
        # "kernel/" ya cubre api/broker/lifecycle/permissions/registry
        # (rglob de más abajo trae todo lo de adentro solo). "sdk/" es
        # la base pura de la que kernel/ depende (Tool/Artifact/
        # Permission) — un paquete generado sin esto no podía ni
        # importar una Skill.
        "agent_core/",
        "kernel/",
        "sdk/",
        "skills/",

        # Scripts importantes
        "scripts/run_kal.sh",
        "scripts/setup_all.sh",
        "scripts/verify_environment.sh",
        "scripts/test_installation.sh",
        "scripts/generate_market_page.py",
        "scripts/install_from_market.py",
        "scripts/enable_skill.py",
        "scripts/sign_skill.py",
        "scripts/validate_skills.py",
        "scripts/verify_sandbox.sh",

        # Componentes de seguridad
        "code_analysis/",
        "error_handling/",
        "audit/",

        # Núcleo de integración de herramientas
        "tool_integration/",

        # Integración con VS Code
        "vscode-extension/",
        "vscode-extension/README.md",
        "vscode-extension/package.json",

        # Pruebas
        "tests/",

        # Utilidades
        "utils/",

        # Carpeta frontend si existe
        "frontend/",

        # Archivos de configuración adicionales — las 4 listas reales
        # de este repo (core/multimodal/dev + la umbrella), no solo
        # requirements.txt.
        "requirements.txt",
        "requirements-core.txt",
        "requirements-multimodal.txt",
        "requirements-dev.txt",
        ".gitignore",
    ]

    # Crear el archivo ZIP
    with zipfile.ZipFile(package_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        files_added = 0
        added_relative_paths = set()  # evita duplicados si una entrada se repite en la lista

        for item in important_files_and_dirs:
            full_path = project_root / item

            if not full_path.exists():
                print(f"Advertencia: {item} no existe")
                continue

            if full_path.is_file():
                if _is_excluded(full_path):
                    continue
                relative_path = full_path.relative_to(project_root)
                if relative_path in added_relative_paths:
                    continue
                zipf.write(full_path, relative_path)
                added_relative_paths.add(relative_path)
                print(f"Agregado archivo: {relative_path}")
                files_added += 1
            elif full_path.is_dir():
                for file_path in full_path.rglob('*'):
                    if not file_path.is_file() or _is_excluded(file_path):
                        continue
                    relative_path = file_path.relative_to(project_root)
                    if relative_path in added_relative_paths:
                        continue
                    zipf.write(file_path, relative_path)
                    added_relative_paths.add(relative_path)
                    print(f"Agregado archivo: {relative_path}")
                    files_added += 1

    print(f"\nPaquete creado exitosamente: {package_name}")
    print(f"Total de archivos agregados: {files_added}")
    print("El paquete contiene los archivos principales de kal-in para análisis por un especialista.")


if __name__ == "__main__":
    create_kal_package()