# Servicio aislado: es el ÚNICO componente con acceso al socket de Docker
# del host (ver docker-compose.yml). Superficie de ataque reducida a
# propósito: si este servicio se ve comprometido, el daño está acotado
# a lo que docker_runner.py permite (contenedores efímeros con límites
# duros), no a acceso arbitrario al agente principal.
FROM python:3.11-slim

RUN groupadd -r sandbox && useradd -r -g sandbox sandbox

WORKDIR /app

# VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (M-7 en kal,
# 2026-09-27, portado acá vía scripts/check_kernel_drift.py): el
# usuario `sandbox` de arriba se creaba pero nunca se ACTIVABA (sin
# `USER sandbox`) — este servicio corría como root. Cualquier RCE acá
# (código ejecutable vía /execute, con un denylist AST evadible como
# única barrera adicional — ver code_analysis/ast_validator.py) pasaba
# directo de "proceso comprometido" a root del contenedor.
#
# NOTA (divergencia deliberada con kal): en kal, este servicio monta el
# socket de Docker del host directo, así que además necesita
# pertenecer al grupo dueño de ese socket (`--group-add`/`group_add:`).
# Acá NO — docker-compose.yml ya aísla eso mejor: `sandbox_runner`
# nunca ve el socket real, solo habla por TCP con `docker_socket_proxy`
# (`DOCKER_HOST=tcp://docker_socket_proxy:2375`, ver ese archivo), que
# es el único que monta `/var/run/docker.sock`. `USER sandbox` acá no
# necesita ningún grupo extra por esto.
#
# NOTA (divergencia deliberada con kal): kal portó este fix instalando
# TODO requirements-core.txt — ahí es el kernel puro, así que ese
# archivo YA es exactamente lo que este servicio necesita. Acá
# requirements-core.txt es el del PROYECTO completo (agent_core
# incluido: chromadb/sqlalchemy para memoria, sklearn, etc.) — usarlo
# tal cual acá infla la imagen del sandbox_runner con dependencias que
# nunca toca (verificado con grep: sandbox_api.py/docker_runner.py
# solo requieren docker/fastapi/uvicorn). Se mantiene la lista mínima
# explícita, ya presente acá desde antes del fix.
RUN pip install --no-cache-dir docker fastapi "uvicorn[standard]"

# El contexto de build de este Dockerfile es la raíz del proyecto
# (ver docker-compose.yml: build.context), no ./kernel/lifecycle, para
# poder copiar también sdk/, code_analysis/, audit/ y utils/ que este
# servicio reutiliza (Permission, validación estática, auditoría, config).
COPY kernel/ ./kernel/
COPY sdk/ ./sdk/
COPY code_analysis/ ./code_analysis/
COPY audit/ ./audit/
COPY utils/ ./utils/
COPY config/ ./config/

# Evaluar Docker-in-Docker rootless o gVisor como alternativa más
# segura antes de producción (ver plan de Fase 0/5) — USER sandbox de
# abajo es la mitigación de hoy, no la solución final.
#
# BUG REAL ENCONTRADO VERIFICANDO ESTE FIX EN kal (2026-09-27): agregar
# SOLO `USER sandbox` sin esto rompía el servicio por completo — /app
# quedó root:root por los COPY de arriba, y utils/logger.py crea logs/
# (con mkdir relativo al cwd) al importarse, así que el proceso ni
# siquiera arrancaba (`PermissionError: [Errno 13] Permission denied:
# 'logs'`). Verificado con un build real en kal: sin este chown,
# `docker run ... python -c "from kernel.api.sandbox_api import app"`
# falla; con él, importa bien y corre como `sandbox`, no como root
# (`whoami` confirmado).
RUN chown -R sandbox:sandbox /app
EXPOSE 9000
USER sandbox

CMD ["uvicorn", "kernel.api.sandbox_api:app", "--host", "0.0.0.0", "--port", "9000"]
