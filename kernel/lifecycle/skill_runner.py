"""
Runner de confianza que ejecuta una skill DENTRO del contenedor
efímero — nunca se importa como módulo de este proyecto: se lee como
texto (ver kernel/registry/sandboxed_skill.py) y se manda tal cual
como `source_code` a SandboxExecutor.execute_trusted(), que lo corre
como /workspace/main.py dentro del sandbox de Docker.

Protocolo (ver kernel/registry/sandboxed_skill.py, que arma estos
archivos antes de cada ejecución):
  - /workspace/skill/<archivo>.py  -> código de la skill (copiado desde
    su carpeta real, tal cual está en skills/<nombre>/)
  - /workspace/_input.json         -> {"entry_point": "archivo:Clase",
    "kwargs": {...}}
  - /workspace/output/             -> carpeta escribible; si la skill
    genera un archivo real (imagen/audio/etc.), debe escribirlo ACÁ
    (ver KAL_SKILL_OUTPUT_DIR más abajo) y devolver en Artifact.uri
    solo el nombre de archivo relativo a esta carpeta, nunca una ruta
    absoluta — este contenedor no sabe nada de las rutas del host.
  - /workspace/output/_output.json -> resultado que este script escribe
    al final. Va DENTRO de output/ (no directo en /workspace) a
    propósito: es la única carpeta que DockerSandboxRunner.run() lee de
    vuelta al host (ver `output_dir=` en docker_runner.py) — así este
    JSON viaja de vuelta por el mismo mecanismo que un archivo real
    generado por la skill, sin necesitar un segundo canal. Nombre
    reservado: sandboxed_skill.py lo separa del resto de los archivos
    de salida antes de tratar lo demás como artefactos de la skill.

KAL_SKILL_OUTPUT_DIR: variable de entorno que este script define antes
de llamar a la skill, apuntando a /workspace/output. Cualquier skill
que quiera devolver un archivo debe leer esta variable (nunca asumir
una ruta fija) y escribir ahí.
"""
import importlib.util
import json
import os
import sys

with open("/workspace/_input.json", encoding="utf-8") as f:
    payload = json.load(f)

entry_point = payload["entry_point"]
kwargs = payload.get("kwargs", {})

module_part, _, class_name = entry_point.partition(":")
module_path = f"/workspace/skill/{module_part}.py"

# BUG REAL ENCONTRADO EN kal-in (2026-09-29, corriendo la skill
# facade epistémica contra un contenedor real en vez de en proceso):
# spec_from_file_location() carga tool.py por RUTA DE ARCHIVO, pero
# nunca agrega /workspace/skill/ a sys.path — cualquier skill que
# vendorice un módulo hermano junto a tool.py (import de un paquete
# propio, no de sdk/) revienta acá con ModuleNotFoundError, aunque un
# test en proceso (que sí tiene la carpeta en sys.path por otro
# motivo) pase limpio. Fix en la raíz en vez de que cada skill lo
# resuelva por su cuenta.
#
# append(), NUNCA insert(0, ...): con insert(0, ...) la carpeta de la
# skill queda POR DELANTE de la stdlib y de sdk/ en la resolución de
# imports — una skill (no confiable) podría vendorizar un archivo
# llamado, por ejemplo, `json.py` o una carpeta `sdk/` propia y
# shadowear al real, silenciosamente. Con append() el import
# vendorizado se sigue resolviendo igual (nada más lo provee), pero
# shadowear algo que ya existe en sys.path es imposible.
skill_dir = os.path.dirname(module_path)
if skill_dir not in sys.path:
    sys.path.append(skill_dir)

result = {}
try:
    spec = importlib.util.spec_from_file_location("kal_sandboxed_skill", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tool_cls = getattr(module, class_name)

    os.makedirs("/workspace/output", exist_ok=True)
    os.environ["KAL_SKILL_OUTPUT_DIR"] = "/workspace/output"

    tool = tool_cls()
    artifact = tool.execute(**kwargs)
    result = {
        "ok": True,
        "modality": artifact.modality,
        "uri": artifact.uri,
        "metadata": artifact.metadata,
    }
except Exception as e:  # noqa: BLE001 — se reporta cualquier fallo de la skill, no se re-lanza
    result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

os.makedirs("/workspace/output", exist_ok=True)
with open("/workspace/output/_output.json", "w", encoding="utf-8") as f:
    json.dump(result, f)
