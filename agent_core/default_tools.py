"""
Registro de las herramientas estáticas de primera parte de kal-in sobre
el ToolRegistry del kernel (kernel/registry/registry.py) — antes vivía
dentro de kernel/registry/registry.py, movido acá porque decidir QUÉ
herramientas concretas existen por defecto (multimedia, navegador, etc.)
es política de agente, no mecanismo de kernel. kernel/registry/registry.py
solo expone el mecanismo genérico de registro; este módulo decide qué
registrar. Llamado explícitamente desde agent_core/orchestrator.py al
arrancar.
"""
from __future__ import annotations

from kernel.api.bus import kernel_service_bus
from kernel.registry.registry import tool_registry
from tool_integration.adapters.audio_gen import AudioGenerationTool
from tool_integration.adapters.browser import BrowserTool
from tool_integration.adapters.image_analysis import ImageAnalysisTool
from tool_integration.adapters.image_composition import ImageCompositionTool
from tool_integration.adapters.image_editing import ImageEditingTool
from tool_integration.adapters.image_gen import ImageGenerationTool
from tool_integration.adapters.ocr import TextExtractionTool
from tool_integration.adapters.skill_creator_tool import ProposeSkillTool
from tool_integration.adapters.speech_to_text import SpeechToTextTool
from tool_integration.adapters.text_file import CreateTextFileTool
from tool_integration.adapters.video_gen import VideoCompositionTool
from tool_integration.adapters.vscode_android import AndroidBuildScreenshotTool
from tool_integration.adapters.vscode_files import ImportResourceTool, ProposeProjectFilesTool, ReadWorkspaceFileTool
from tool_integration.services import AudioService, DownloadService, ImageService, STTService


def register_default_static_tools() -> None:
    """
    Registra los adaptadores de primera parte (multimodales + navegador)
    como herramientas estáticas disponibles por defecto. Instanciarlos es
    liviano — no importan diffusers/piper/moviepy/playwright hasta que se
    llama execute() (carga perezosa, ver cada adaptador) — así que esto es
    seguro incluso si esas librerías pesadas no están instaladas todavía:
    el error solo aparecería al intentar usarlas, no al arrancar el agente.
    """
    # UNA sola instancia de cada servicio, compartida de verdad entre el
    # adaptador de primera parte (llamada Python directa) y cualquier
    # skill que declare el kernel_services correspondiente (llamada vía
    # el socket Unix del Kernel Service Bus, ver kernel/__init__.py)
    # — el modelo se carga una sola vez para ambos caminos, no una copia
    # por cada uno. ImageEditingTool recibe la MISMA shared_image_service
    # que ImageGenerationTool (mismo dominio "image", dos acciones:
    # generate/inpaint).
    #
    # Único lugar del proceso que registra servicios en el
    # kernel_service_bus (antes kernel/api/bus.py también lo hacía por su
    # cuenta al importarse — mecanismo del bus mezclado con la política de
    # qué servicios existen por defecto; ver el comentario en
    # kernel/api/bus.py). DownloadService no tenía Tool de primera parte
    # que lo construyera, así que quedaba SOLO en el registro eager de
    # bus.py — al sacar ese registro de ahí, se agrega acá explícitamente
    # para no perder en silencio el Kernel Download Service.
    shared_image_service = ImageService()
    shared_audio_service = AudioService()
    shared_stt_service = STTService()
    shared_download_service = DownloadService()
    kernel_service_bus.register("image", shared_image_service)
    kernel_service_bus.register("audio", shared_audio_service)
    kernel_service_bus.register("stt", shared_stt_service)
    kernel_service_bus.register("download", shared_download_service)

    tool_registry.register_static_tool(ImageGenerationTool(image_service=shared_image_service))
    tool_registry.register_static_tool(AudioGenerationTool(audio_service=shared_audio_service))
    tool_registry.register_static_tool(VideoCompositionTool())
    tool_registry.register_static_tool(BrowserTool())
    tool_registry.register_static_tool(SpeechToTextTool(stt_service=shared_stt_service))
    tool_registry.register_static_tool(ImageEditingTool(image_service=shared_image_service))
    tool_registry.register_static_tool(ImageCompositionTool())
    tool_registry.register_static_tool(ImageAnalysisTool())
    tool_registry.register_static_tool(TextExtractionTool())
    tool_registry.register_static_tool(ProposeProjectFilesTool())
    tool_registry.register_static_tool(ImportResourceTool())
    tool_registry.register_static_tool(ReadWorkspaceFileTool())
    tool_registry.register_static_tool(AndroidBuildScreenshotTool())
    tool_registry.register_static_tool(ProposeSkillTool())
    tool_registry.register_static_tool(CreateTextFileTool())
    tool_registry.load_skills()
