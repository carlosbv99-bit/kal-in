#!/usr/bin/env bash
# Arranca kal (API + frontend en el mismo proceso, puerto 8000).
#
# Requisitos que deben estar YA corriendo antes de este script (la
# conexión es inmediata al arrancar, no perezosa):
#   - Docker daemon (TaskExecutor lo necesita para el sandbox)
#   - Ollama (para el chat; si no está, /status lo marcará pero kal
#     igual arranca — solo /chat fallará hasta que Ollama esté arriba)
#
# Uso:
#   chmod +x scripts/run_kal.sh
#   ./scripts/run_kal.sh

set -euo pipefail

echo "== Verificando Docker =="
if ! docker info > /dev/null 2>&1; then
  echo "ERROR: Docker no está corriendo. kal lo necesita para arrancar (TaskExecutor se conecta al iniciar, no de forma perezosa)."
  exit 1
fi

echo "== Verificando Ollama (opcional, solo afecta /chat) =="

# version_lt A B: verdadero si A < B comparando numéricamente los tres primeros
# componentes (corta sufijos tipo -rc1). Sin `sort -V`, que no existe en BSD/macOS.
version_lt() {
  local IFS='.'
  local -a a=($1) b=($2)
  local i x y
  for i in 0 1 2; do
    x=${a[i]:-0}; y=${b[i]:-0}
    x=${x%%[!0-9]*}; y=${y%%[!0-9]*}
    if ((10#${x:-0} < 10#${y:-0})); then return 0; fi
    if ((10#${x:-0} > 10#${y:-0})); then return 1; fi
  done
  return 1
}

if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
  # El piso importa: por debajo de 0.17.1 un modelo fabricado puede hacer que Ollama
  # devuelva trozos de su propia MEMORIA (Bleeding Llama), incluidas variables de
  # entorno y claves de API; por debajo de 0.23.3 el actualizador automático de
  # Windows no verifica firmas. Aviso, no bloqueo: el servidor arranca igual.
  OLLAMA_VERSION=$(curl -sf http://localhost:11434/api/version 2>/dev/null \
    | sed -nE 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p')
  if [ -z "$OLLAMA_VERSION" ]; then
    echo "Ollama disponible (no pude leer la versión: /api/version no respondió)."
  elif version_lt "$OLLAMA_VERSION" "0.17.1"; then
    echo "AVISO: Ollama $OLLAMA_VERSION — por debajo de 0.17.1 tiene una vulnerabilidad conocida"
    echo "  (Bleeding Llama: un modelo fabricado puede hacer que Ollama devuelva memoria del"
    echo "  proceso, incluidas variables de entorno y claves de API). Actualizá Ollama."
  elif version_lt "$OLLAMA_VERSION" "0.23.3"; then
    echo "AVISO: Ollama $OLLAMA_VERSION — por debajo de 0.23.3 el actualizador automático de"
    echo "  Windows no verifica firmas. Actualizá Ollama."
  else
    echo "Ollama $OLLAMA_VERSION disponible."
  fi
else
  echo "AVISO: Ollama no responde en localhost:11434 — kal arrancará igual, pero /chat fallará hasta que esté arriba."
fi

# VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
# 2026-09-26), C-5: --host 0.0.0.0 expone TODA la API sin autenticar
# (salvo los ~13 endpoints que sí piden token admin) a cualquier
# dispositivo de la LAN, no solo a esta máquina — contradice el
# binding a loopback que docker-compose.yml ya aplica a propósito
# (mismo hallazgo de 2026-07-09 citado en ese archivo). KAL_HOST
# permite optar explícitamente por 0.0.0.0 (p.ej. para acceder desde
# otro dispositivo en la misma LAN de confianza), pero el default
# ahora es 127.0.0.1 — nunca expuesto sin que alguien lo pida a propósito.
KAL_HOST="${KAL_HOST:-127.0.0.1}"
if [ "$KAL_HOST" = "0.0.0.0" ]; then
  echo "AVISO: KAL_HOST=0.0.0.0 — la API queda expuesta a TODA la LAN. La mayoría de los endpoints no piden autenticación."
fi

echo "== Arrancando kal en http://${KAL_HOST}:8000 =="
# BUG REAL ENCONTRADO EN USO: --reload sin restricciones vigila TODO el
# proyecto, incluida data/ — un artefacto real (una imagen generada, un
# archivo de versión de herramienta dinámica) o la suite de tests
# corriendo en paralelo (que escribe en data/tool_versions/) dispara un
# reinicio completo del servidor A MITAD de una conversación real,
# cortando la respuesta al usuario con un error de red en el frontend.
# Los --reload-exclude de abajo son solo RUNTIME (nunca código fuente
# que sí deba recargarse), así que esto no esconde ningún cambio real.
uvicorn agent_core.orchestrator:app --host "$KAL_HOST" --port 8000 --reload \
  --reload-exclude 'data/*' \
  --reload-exclude 'logs/*' \
  --reload-exclude 'docs/*' \
  --reload-exclude 'tests/*' \
  --reload-exclude '*.log'
