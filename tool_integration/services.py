"""
Servicios reales expuestos en el Kernel Service Bus (ver
kernel/__init__.py). Cada servicio es un objeto de Python normal,
vive en el proceso principal — nunca se serializa ni se envía a un
contenedor; lo único que cruza la frontera es el resultado (un dict
JSON-serializable), vía kernel/api/socket_server.py.

ImageService es, en la práctica, el "Model Manager" que motivó todo
este bus: antes, cada llamador (image_gen.py) tenía su PROPIA
instancia de pipeline cacheada — ahora hay una sola, compartida tanto
por el adaptador de primera parte (llamada Python directa, mismo
proceso) como por cualquier skill que declare `kernel_services:
["image.generate"]` (vía el socket).

AudioService/STTService (mismo patrón, agregados 2026-07-11 al
terminar de desacoplar audio_gen.py/speech_to_text.py) e
ImageService.inpaint() (misma clase que .generate(), agregado al
terminar de desacoplar image_editing.py) siguen el mismo criterio: la
carga perezosa del modelo, movida tal cual desde el adaptador
correspondiente, ahora compartida entre la llamada Python directa y
cualquier skill que declare el `kernel_services` correspondiente.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from audit.audit_log import AuditEvent, audit_log
from kernel.broker.resource_broker import resource_broker
from kernel.permissions.network_access_manager import network_access_manager as default_network_access_manager
from kernel.permissions.network_permissions import NetworkAction, NetworkScope
from tool_integration.download_manager import (
    DownloadValidationError,
    download_manager as default_download_manager,
)
from utils.config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_VOICES_DIR = Path("data/models/piper")


class KernelServiceError(Exception):
    """Un servicio no pudo completar la acción pedida — nunca deja
    escapar la excepción original (que podría filtrar detalles internos
    del host) hacia el llamador, sea Python directo o vía el bus."""


class ImageService:
    """
    Genera imágenes localmente (SDXL-Turbo, CPU) — misma lógica que
    tenía tool_integration/adapters/image_gen.py::_generate_locally,
    ahora compartida. El pipeline se carga una sola vez (primera
    llamada, de cualquier origen) y se mantiene en memoria.

    También expone `inpaint()` — misma lógica que tenía
    tool_integration/adapters/image_editing.py::_inpaint, con su PROPIA
    config (`editing_cfg`, distinta de `cfg`: son dos secciones
    separadas de settings.multimodal, `image` e `image_editing`) y su
    propio pipeline lazy (diffusers de inpainting, un modelo COMPLETO,
    distinto del de generación). Comparten esta clase por ser el mismo
    dominio ("image"), no la misma config ni el mismo pipeline.
    """

    # Lista explícita de acciones invocables vía el Kernel Service Bus
    # (ver kernel/api/bus.py::dispatch) — cualquier otro método público
    # de esta clase NO es una acción del bus, aunque exista.
    ALLOWED_ACTIONS = frozenset({"generate", "inpaint"})

    def __init__(self, cfg=None, editing_cfg=None):
        # cfg/editing_cfg inyectables: mismo motivo que en el resto del
        # proyecto — un test que monkeypatchea settings.multimodal.image
        # o settings.multimodal.image_editing ANTES de instanciar el
        # Tool correspondiente sigue funcionando exactamente igual. La
        # instancia compartida real de producción (usada por el Kernel
        # Service Bus) es una instancia aparte, registrada una sola vez
        # al arrancar — ver
        # agent_core/default_tools.py::register_default_static_tools().
        self.cfg = cfg or settings.multimodal.image
        self.editing_cfg = editing_cfg or settings.multimodal.image_editing
        self._pipeline = None
        self._inpaint_pipeline = None
        # Hallazgo de la revisión de seguridad 2026-07-09: sin esto, el
        # agente y cualquier cantidad de skills concurrentes (vía el
        # bus) podían llamar al mismo objeto de pipeline de PyTorch al
        # mismo tiempo, sin ninguna sincronización — diffusers no
        # garantiza que una misma instancia de pipeline sea segura para
        # llamadas concurrentes desde threads distintos (estado interno
        # compartido del scheduler/generador). Un lock por pipeline (no
        # uno solo para toda la clase) para no serializar generate() e
        # inpaint() entre sí sin necesidad — son pipelines distintos.
        self._generate_lock = threading.Lock()
        self._inpaint_lock = threading.Lock()
        Path(self.cfg.artifact_dir).mkdir(parents=True, exist_ok=True)
        Path(self.editing_cfg.artifact_dir).mkdir(parents=True, exist_ok=True)

        # Ver kernel/broker/resource_broker.py — libera estos pipelines solo
        # tras un rato sin uso, o de inmediato si la RAM del sistema está
        # baja (nunca se descargaban antes, bug real confirmado en uso).
        resource_broker.register(
            "image.generate", is_loaded=lambda: self._pipeline is not None, unload=self._unload_pipeline
        )
        resource_broker.register(
            "image.inpaint",
            is_loaded=lambda: self._inpaint_pipeline is not None,
            unload=self._unload_inpaint_pipeline,
        )

    def _unload_pipeline(self) -> None:
        self._pipeline = None

    def _unload_inpaint_pipeline(self) -> None:
        self._inpaint_pipeline = None

    def _get_pipeline(self):
        # BUG REAL ENCONTRADO EN USO (2026-07-27): este pipeline (13GB
        # fp32) podía intentar cargar con el modelo de chat de Ollama
        # todavía residente en RAM, superando la RAM física total de la
        # máquina y congelando el sistema entero. evict_idle_and_pressured()
        # ahora también sabe liberar el modelo de Ollama (ver
        # agent_core/orchestrator.py::build_llm_client()) — llamarlo ACÁ,
        # antes de cargar, cierra el hueco simétrico al que ya existía
        # del lado de OllamaClient.chat().
        resource_broker.evict_idle_and_pressured()
        resource_broker.mark_used("image.generate")
        if self._pipeline is None:
            import torch
            from diffusers import AutoPipelineForText2Image

            logger.info(f"Cargando modelo de generación de imágenes: {self.cfg.model} (puede tardar la primera vez)")
            self._pipeline = AutoPipelineForText2Image.from_pretrained(
                self.cfg.model,
                torch_dtype=torch.float32,  # float16 no es fiable en CPU
            )
            self._pipeline.to("cpu")
        return self._pipeline

    def generate(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        if not prompt:
            raise KernelServiceError("'prompt' es requerido")

        with self._generate_lock:
            pipeline = self._get_pipeline()

            # BUG REAL ENCONTRADO EN USO: el log y los metadatos citaban
            # self.cfg.num_inference_steps (el default de config.yaml) aunque
            # se haya pasado un override por kwargs — la imagen se generaba
            # con el valor correcto, pero el metadato devuelto MENTÍA sobre
            # cuántos pasos se usaron de verdad.
            actual_steps = kwargs.get("num_inference_steps", self.cfg.num_inference_steps)
            logger.info(f"Generando imagen ({actual_steps} pasos): {prompt!r}")
            result = pipeline(
                prompt=prompt,
                num_inference_steps=actual_steps,
                guidance_scale=kwargs.get("guidance_scale", self.cfg.guidance_scale),
                height=kwargs.get("height", self.cfg.height),
                width=kwargs.get("width", self.cfg.width),
            )
            image = result.images[0]

        artifact_id = str(uuid4())
        path = Path(self.cfg.artifact_dir) / f"{artifact_id}.png"
        image.save(path)

        return {
            "artifact": f"artifact://image/{artifact_id}",
            "path": str(path),
            "metadata": {
                "prompt": prompt,
                "model": self.cfg.model,
                "num_inference_steps": actual_steps,
            },
        }

    def _get_inpaint_pipeline(self):
        # Ver el mismo comentario en _get_pipeline() más arriba.
        resource_broker.evict_idle_and_pressured()
        resource_broker.mark_used("image.inpaint")
        if self._inpaint_pipeline is None:
            import torch
            from diffusers import AutoPipelineForInpainting

            logger.info(
                f"Cargando modelo de inpainting: {self.editing_cfg.inpaint_model} "
                "(puede tardar la primera vez, ~5.5GB de descarga)"
            )
            self._inpaint_pipeline = AutoPipelineForInpainting.from_pretrained(
                self.editing_cfg.inpaint_model,
                torch_dtype=torch.float32,  # float16 no es fiable en CPU
                safety_checker=None,
                requires_safety_checker=False,
            )
            self._inpaint_pipeline.to("cpu")
        return self._inpaint_pipeline

    def inpaint(self, image_path: str, box: list[int], prompt: str, **kwargs: Any) -> dict[str, Any]:
        if not box or len(box) != 4:
            raise KernelServiceError("'box' es requerido, con exactamente 4 valores [izquierda, arriba, derecha, abajo]")
        if not prompt:
            raise KernelServiceError("'prompt' es requerido, describiendo qué debe aparecer en 'box'")

        from PIL import Image, ImageDraw

        with self._inpaint_lock:
            pipeline = self._get_inpaint_pipeline()

            with Image.open(image_path) as source:
                img = source.convert("RGB")

            mask = Image.new("L", img.size, 0)  # negro = mantener, blanco = regenerar
            ImageDraw.Draw(mask).rectangle(tuple(box), fill=255)

            logger.info(f"Inpainting ({self.editing_cfg.inpaint_num_inference_steps} pasos): {prompt!r}")
            result = pipeline(
                prompt=prompt,
                image=img,
                mask_image=mask,
                height=img.height,
                width=img.width,
                num_inference_steps=self.editing_cfg.inpaint_num_inference_steps,
                guidance_scale=self.editing_cfg.inpaint_guidance_scale,
            )
            edited = result.images[0]

        artifact_id = str(uuid4())
        path = Path(self.editing_cfg.artifact_dir) / f"{artifact_id}.png"
        edited.save(path)

        return {
            "artifact": f"artifact://image/{artifact_id}",
            "path": str(path),
            "metadata": {
                "operation": "inpaint",
                "source_path": image_path,
                "prompt": prompt,
                "box": box,
                "model": self.editing_cfg.inpaint_model,
            },
        }


class AudioService:
    """
    Sintetiza voz localmente (piper-tts, CPU) — misma lógica que tenía
    tool_integration/adapters/audio_gen.py::_generate_locally (incluidos
    los 3 bugs reales ya corregidos ahí: parámetros del WAV explícitos,
    voice.synthesize() como generador de chunks, formato de AudioChunk
    no confirmado de antemano). Solo el backend "local" — "api" (OpenAI
    TTS) se queda en el adaptador, nunca pasa por el kernel (mismo
    criterio que ImageGenerationTool._generate_via_api).

    Implementa tool_integration.provider.TTSProvider estructuralmente
    (conformidad de Protocol, sin heredar de nada) — los adaptadores
    que la usan (tool_integration/adapters/audio_gen.py) declaran su
    dependencia como TTSProvider, no como esta clase concreta.
    """

    ALLOWED_ACTIONS = frozenset({"synthesize"})

    def __init__(self, cfg=None):
        self.cfg = cfg or settings.multimodal.audio
        self._voice = None
        # Mismo motivo que ImageService._generate_lock: la voz de piper
        # es un único objeto compartido entre el agente y cualquier
        # skill concurrente vía el bus.
        self._lock = threading.Lock()
        Path(self.cfg.artifact_dir).mkdir(parents=True, exist_ok=True)
        _VOICES_DIR.mkdir(parents=True, exist_ok=True)

        resource_broker.register("audio.synthesize", is_loaded=lambda: self._voice is not None, unload=self._unload_voice)

    def _unload_voice(self) -> None:
        self._voice = None

    def _ensure_voice_files(self) -> tuple[Path, Path]:
        model_path = _VOICES_DIR / f"{self.cfg.voice_model}.onnx"
        config_path = _VOICES_DIR / f"{self.cfg.voice_model}.onnx.json"

        if model_path.exists() and config_path.exists():
            return model_path, config_path

        logger.info(f"Descargando modelo de voz de piper: {self.cfg.voice_model} (primera vez, requiere red)")
        from huggingface_hub import hf_hub_download

        # Convención de subcarpetas de rhasspy/piper-voices:
        # <idioma>/<idioma_país>/<voz>/<calidad>/.
        parts = self.cfg.voice_model.split("-")
        if len(parts) != 3:
            raise KernelServiceError(
                f"voice_model '{self.cfg.voice_model}' no tiene el formato esperado "
                "'<idioma_país>-<voz>-<calidad>' (p.ej. 'es_ES-davefx-medium')"
            )
        lang_full, voice_name, quality = parts
        lang_short = lang_full.split("_")[0]
        subfolder = f"{lang_short}/{lang_full}/{voice_name}/{quality}"

        downloaded_model = hf_hub_download(
            repo_id="rhasspy/piper-voices", filename=f"{self.cfg.voice_model}.onnx", subfolder=subfolder
        )
        downloaded_config = hf_hub_download(
            repo_id="rhasspy/piper-voices", filename=f"{self.cfg.voice_model}.onnx.json", subfolder=subfolder
        )
        return Path(downloaded_model), Path(downloaded_config)

    def _get_voice(self):
        # Ver el comentario en ImageService._get_pipeline() más arriba.
        resource_broker.evict_idle_and_pressured()
        resource_broker.mark_used("audio.synthesize")
        if self._voice is None:
            from piper.voice import PiperVoice

            model_path, config_path = self._ensure_voice_files()
            logger.info(f"Cargando voz de piper: {model_path.name}")
            self._voice = PiperVoice.load(str(model_path), config_path=str(config_path), use_cuda=False)
        return self._voice

    def synthesize(self, text: str, **kwargs: Any) -> dict[str, Any]:
        if not text:
            raise KernelServiceError("'text' es requerido")

        import wave

        with self._lock:
            voice = self._get_voice()

            artifact_id = str(uuid4())
            path = Path(self.cfg.artifact_dir) / f"{artifact_id}.wav"

            sample_rate = getattr(getattr(voice, "config", None), "sample_rate", 22050)

            logger.info(f"Sintetizando audio ({len(text)} caracteres) con voz {self.cfg.voice_model}")
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)  # PCM de 16 bits
                wav_file.setframerate(sample_rate)
                for chunk in voice.synthesize(text):
                    wav_file.writeframes(self._audio_chunk_to_pcm_bytes(chunk))

        return {
            "artifact": f"artifact://audio/{artifact_id}",
            "path": str(path),
            "metadata": {"text": text, "voice_model": self.cfg.voice_model},
        }

    @staticmethod
    def _audio_chunk_to_pcm_bytes(chunk) -> bytes:
        import numpy as np

        if hasattr(chunk, "audio_int16_bytes"):
            return chunk.audio_int16_bytes

        if hasattr(chunk, "audio_int16_array"):
            return chunk.audio_int16_array.astype(np.int16).tobytes()

        if hasattr(chunk, "audio_float_array"):
            return (chunk.audio_float_array * 32767).astype(np.int16).tobytes()

        if isinstance(chunk, np.ndarray):
            if np.issubdtype(chunk.dtype, np.floating):
                return (chunk * 32767).astype(np.int16).tobytes()
            return chunk.astype(np.int16).tobytes()

        available = [a for a in dir(chunk) if not a.startswith("_")]
        raise KernelServiceError(
            f"No se reconoce el formato de AudioChunk de piper-tts (tipo {type(chunk).__name__}). "
            f"Atributos/métodos disponibles: {available}."
        )


class STTService:
    """
    Transcribe audio localmente (faster-whisper, CPU) — misma lógica
    que tenía tool_integration/adapters/speech_to_text.py::execute.
    Sin backend "api" (el adaptador tampoco lo tenía).

    Implementa tool_integration.provider.STTProvider estructuralmente
    (conformidad de Protocol, sin heredar de nada) — los adaptadores
    que la usan (tool_integration/adapters/speech_to_text.py) declaran
    su dependencia como STTProvider, no como esta clase concreta.
    """

    ALLOWED_ACTIONS = frozenset({"transcribe"})

    def __init__(self, cfg=None):
        self.cfg = cfg or settings.multimodal.stt
        self._model = None
        # Mismo motivo que ImageService._generate_lock: el modelo de
        # Whisper es un único objeto compartido entre el agente y
        # cualquier skill concurrente vía el bus.
        self._lock = threading.Lock()

        resource_broker.register("stt.transcribe", is_loaded=lambda: self._model is not None, unload=self._unload_model)

    def _unload_model(self) -> None:
        self._model = None

    def _get_model(self):
        # Ver el comentario en ImageService._get_pipeline() más arriba.
        resource_broker.evict_idle_and_pressured()
        resource_broker.mark_used("stt.transcribe")
        if self._model is None:
            from faster_whisper import WhisperModel

            logger.info(f"Cargando modelo de Whisper: {self.cfg.model_size} (puede tardar la primera vez)")
            self._model = WhisperModel(self.cfg.model_size, device="cpu", compute_type="int8")
        return self._model

    def transcribe(self, audio_path: str, **kwargs: Any) -> dict[str, Any]:
        if not Path(audio_path).exists():
            raise KernelServiceError(f"No existe el archivo de audio: {audio_path}")

        with self._lock:
            model = self._get_model()
            logger.info(f"Transcribiendo audio: {audio_path}")
            segments, info = model.transcribe(audio_path, language=self.cfg.language)
            # faster-whisper devuelve un generador perezoso — el cómputo
            # real ocurre al iterarlo, así que hay que consumirlo DENTRO
            # del lock (si no, la sincronización sería un espejismo: el
            # trabajo pesado seguiría corriendo fuera de la sección
            # protegida).
            text = " ".join(segment.text.strip() for segment in segments).strip()

        return {
            "metadata": {
                "summary": text,
                "audio_path": audio_path,
                "detected_language": info.language,
                "model_size": self.cfg.model_size,
            },
        }


class DownloadService:
    """
    Kernel Download Service: para que una Skill de terceros pueda bajar
    un archivo real de Internet SIN necesitar `Permission.NETWORK`
    (que le daría red cruda sin validar nada, ver
    kernel/registry/sandboxed_skill.py:134 — network_mode="bridge").
    Reusa tal cual tool_integration/download_manager.py::DownloadManager
    (IP-safety anti-DNS-rebinding, tope de tamaño, escaneo ClamAV,
    validación de contenido real) — hoy el único otro consumidor es
    ImportResourceTool (agente de VS Code, proceso host, con su PROPIA
    UX de aprobación de dos gates: red + filesystem, genuinamente
    distinta de lo que necesita una Skill acá, por eso no comparten
    código más allá de DownloadManager).

    Primer servicio del bus que necesita saber QUÉ Skill lo llama (el
    permiso de red es por-skill) — ver kernel/api/bus.py::dispatch(),
    que inyecta `skill_name` solo a acciones que lo declaran como
    parámetro, así ImageService/AudioService/STTService no se ven
    afectados.
    """

    ALLOWED_ACTIONS = frozenset({"fetch"})

    def __init__(self, cfg=None, download_manager=None, network_access_manager=None):
        self.cfg = cfg or settings.downloads
        self.download_manager = download_manager or default_download_manager
        self.network_access_manager = network_access_manager or default_network_access_manager
        Path(self.cfg.artifact_dir).mkdir(parents=True, exist_ok=True)

    def fetch(self, url: str, expected_type: str, skill_name: str) -> dict[str, Any]:
        hostname = urlparse(url).hostname or url
        decision = self.network_access_manager.evaluate(
            skill_name=skill_name, scope=NetworkScope.INTERNET,
            action=NetworkAction.DOWNLOAD, resource_key=hostname,
        )
        if decision != "auto_allowed":
            pending = self.network_access_manager.create_pending_request(
                skill_name=skill_name, scope=NetworkScope.INTERNET,
                action=NetworkAction.DOWNLOAD, resource_key=hostname,
            )
            raise KernelServiceError(
                f"Descargar desde '{hostname}' requiere aprobación humana (pedido #{pending.id} "
                "registrado) — probá de nuevo después de que se apruebe."
            )

        try:
            resource = self.download_manager.download_and_validate(url, expected_type)
        except DownloadValidationError as e:
            raise KernelServiceError(str(e)) from e

        artifact_id = str(uuid4())
        path = Path(self.cfg.artifact_dir) / f"{artifact_id}.{expected_type}"
        path.write_bytes(resource.content)

        audit_log.record(
            AuditEvent(
                event_type="artifact_downloaded",
                summary=f"Skill '{skill_name}' descargó '{url}' vía el Kernel Download Service",
                context={
                    "skill": skill_name, "url": url, "sha256": resource.sha256,
                    "mime": resource.mime, "size_bytes": resource.size_bytes,
                },
                outcome="success",
            )
        )

        return {
            "artifact": f"artifact://download/{artifact_id}",
            "path": str(path),
            "metadata": {
                "url": url, "mime": resource.mime, "sha256": resource.sha256, "size_bytes": resource.size_bytes,
            },
        }
