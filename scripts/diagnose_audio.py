"""
Diagnóstico: genera un WAV con AudioGenerationTool y lo deja en disco
(no en tmp_path de un test) para poder inspeccionarlo manualmente con
ffprobe/ffplay y aislar si el problema está en el WAV generado o en
cómo moviepy invoca ffmpeg para leerlo.

Uso:
    python3 scripts/diagnose_audio.py
    ffprobe data/artifacts/audio/diagnostico.wav
"""
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tool_integration.adapters.audio_gen import AudioGenerationTool

tool = AudioGenerationTool()
artifact = tool.execute(text="Esto es una prueba de diagnóstico de audio para revisar el archivo generado.")

print(f"Archivo generado en: {artifact.uri}")

with wave.open(artifact.uri, "rb") as f:
    print(f"  canales: {f.getnchannels()}")
    print(f"  ancho de muestra (bytes): {f.getsampwidth()}")
    print(f"  frecuencia: {f.getframerate()}")
    print(f"  nº de frames: {f.getnframes()}")
    print(f"  duración (s): {f.getnframes() / f.getframerate():.2f}")

print("\nAhora corre: ffprobe " + artifact.uri)
