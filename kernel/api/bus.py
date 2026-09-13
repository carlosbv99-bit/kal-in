"""
Registro de servicios del Kernel Service Bus + despacho por nombre de
método ("<servicio>.<acción>"). Puramente en memoria del proceso
principal — nunca sabe nada de sockets ni de contenedores (eso es
kernel/api/socket_server.py, que envuelve esto para exponerlo a una
skill aislada).
"""
from __future__ import annotations

import inspect
from typing import Any


class ServiceNotFoundError(Exception):
    """El servicio nombrado en 'method' no está registrado."""


class ActionNotFoundError(Exception):
    """El servicio existe pero no tiene esa acción."""


class ArtifactNotFoundError(Exception):
    """Un parámetro 'artifact://...' no corresponde a ningún artefacto conocido."""


class KernelServiceBus:
    def __init__(self):
        self._services: dict[str, Any] = {}
        # Mapeo "artifact://..." -> ruta real de host. Una skill (dentro
        # del contenedor) solo conoce la referencia opaca — nunca la
        # ruta real del filesystem del host, que no significa nada
        # adentro y no debería exponerse a código de terceros sin
        # necesidad. SandboxedSkillTool._to_artifact() resuelve acá
        # cuando la skill devuelve el mismo "artifact://" que recibió
        # como resultado propio (ver tool_integration/services.py::ImageService.generate()).
        # Mecanismo deliberadamente mínimo — no un sistema de artefactos
        # completo (eso es la visión más grande de "Proyectos", no
        # construida todavía).
        self.artifact_paths: dict[str, str] = {}

    def register(self, name: str, service: Any) -> None:
        self._services[name] = service

    def dispatch(self, method: str, params: dict[str, Any], skill_name: str | None = None) -> dict[str, Any]:
        service_name, _, action_name = method.partition(".")
        service = self._services.get(service_name)
        if service is None:
            raise ServiceNotFoundError(f"servicio desconocido: '{service_name}'")

        # Hallazgo de la revisión de seguridad 2026-07-09: antes esto
        # resolvía la acción con getattr genérico sobre CUALQUIER
        # atributo público del servicio — inofensivo mientras cada
        # servicio solo tuviera los métodos pensados como "acciones",
        # pero sin ninguna lista explícita, un método público agregado
        # a futuro para otro propósito (no pensado como acción del bus)
        # quedaría invocable igual, por accidente. ALLOWED_ACTIONS es la
        # lista explícita y con intención de cada servicio (ver
        # tool_integration/services.py) — dispatch() ya no confía en que
        # "es público" signifique "es una acción segura".
        allowed_actions = getattr(service, "ALLOWED_ACTIONS", frozenset())
        if action_name not in allowed_actions:
            raise ActionNotFoundError(f"acción desconocida: '{method}'")

        action = getattr(service, action_name, None)
        if action is None or not callable(action):
            raise ActionNotFoundError(f"acción desconocida: '{method}'")

        params = self._resolve_input_artifacts(params)
        # Único caso hoy que necesita saber QUIÉN llama (DownloadService,
        # el permiso de red es por-skill) — inyectado solo si la propia
        # acción lo declara como parámetro, así ImageService/AudioService/
        # STTService (que no lo esperan) siguen recibiendo exactamente lo
        # mismo que siempre.
        if skill_name is not None and "skill_name" in inspect.signature(action).parameters:
            params = {**params, "skill_name": skill_name}
        result = action(**params)

        # "path" es un detalle de host — nunca cruza al otro lado del
        # socket (una skill no necesita ni debería ver rutas reales del
        # filesystem del host). Se registra acá para poder resolver el
        # "artifact://" de vuelta más tarde (ver resolve_artifact()).
        artifact_uri = result.get("artifact")
        real_path = result.pop("path", None)
        if artifact_uri and real_path:
            self.artifact_paths[artifact_uri] = real_path

        return result

    def resolve_artifact(self, uri: str) -> str | None:
        return self.artifact_paths.get(uri)

    def _resolve_input_artifacts(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Resolución de artefactos de ENTRADA — contraparte de la resolución
        de salida de arriba. `image.generate`/`audio.synthesize` nunca
        reciben un artefacto como parámetro (solo texto), pero
        `stt.transcribe`/`image.inpaint` sí necesitan uno ya existente
        (un audio/imagen generado por una llamada anterior EN LA MISMA
        ejecución de la skill). Cualquier valor de `params` que sea un
        `"artifact://..."` se reemplaza acá por la ruta real de host —
        la skill sigue sin ver nunca esa ruta, solo la referencia opaca
        que ya tenía de un resultado anterior.
        """
        resolved = {}
        for key, value in params.items():
            if isinstance(value, str) and value.startswith("artifact://"):
                real_path = self.artifact_paths.get(value)
                if real_path is None:
                    raise ArtifactNotFoundError(f"artefacto desconocido: '{value}'")
                resolved[key] = real_path
            else:
                resolved[key] = value
        return resolved


# Singleton, mismo patrón que tool_registry (kernel/registry/registry.py)
# / audit_log (audit/audit_log.py) / permission_cascade
# (kernel/permissions/permission_cascade.py). Nombrado `kernel_service_bus`,
# no `kernel` a secas — evita la colisión confusa con el nombre del
# propio paquete `kernel` que lo contiene.
#
# BUG REAL ENCONTRADO EN REVISIÓN (2026-09-13, separando el "kernel puro"
# para Likay-OS): antes, este módulo construía y registraba acá mismo
# ImageService/AudioService/STTService/DownloadService al importarse
# (_build_default_bus(), eliminada) — mecanismo (el bus en sí, genérico)
# mezclado con política (QUÉ servicios concretos se registran por
# defecto). El bus es kernel puro (dispatch genérico por nombre, sin
# saber nada de imagen/audio/modelos); decidir qué servicios multimedia
# existen es capacidad de agente, no del kernel — eso ahora lo hace
# kernel/registry/registry.py::_register_static_tools(), el único lugar
# que ya construía ImageService/AudioService/STTService para las Tools
# de primera parte (antes duplicaba el trabajo: se construían UNA VEZ
# acá al importar, sin usarse, y OTRA VEZ ahí de verdad). El bus queda
# vacío hasta que algo lo registre explícitamente.
kernel_service_bus = KernelServiceBus()
