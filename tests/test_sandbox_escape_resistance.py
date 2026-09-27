"""
Tests de resistencia a fuga (escape resistance) del sandbox.

Diferencia con test_sandbox_integration.py: aquellos prueban las
garantías "de manual" (red, fs, memoria, pids) con código relativamente
simple. Estos prueban vectores de ataque más específicos, asumiendo el
PEOR CASO: que el código llegó a ejecutarse dentro del contenedor sin
haber sido detenido por code_analysis/ast_validator.py (denylist
evadida, ofuscación, lo que sea). La pregunta que responden no es
"¿el validador estático lo detecta?" sino "¿aunque no lo detecte, el
contenedor sigue conteniendo el daño?" — que es la garantía que
realmente importa (ver docstring de code_analysis/denylist.py).

Por eso estos tests llaman a runner.run() directamente (la clase de
bajo nivel), no a SandboxExecutor.execute() (que sí pasa por el
validador) — se salta la primera capa a propósito para probar la
segunda de forma aislada.
"""
from __future__ import annotations

from tests.conftest import requires_docker

pytestmark = requires_docker


def test_privilege_escalation_via_setuid_is_blocked(runner):
    """
    Sin CAP_SETUID (cap_drop=ALL), intentar convertirse en root debe
    fallar aunque el proceso encuentre la forma de llamar a os.setuid.
    """
    code = (
        "import os\n"
        "try:\n"
        "    os.setuid(0)\n"
        "    print('ESCALADA_EXITOSA')\n"
        "except PermissionError as e:\n"
        "    print('BLOQUEADO:', e)\n"
    )
    result = runner.run(code)
    assert "ESCALADA_EXITOSA" not in result.stdout
    assert "BLOQUEADO" in result.stdout


def test_raw_socket_requires_capability_not_present(runner):
    """
    Crear un socket raw (típico para sniffing/spoofing) requiere
    CAP_NET_RAW, que está eliminado por cap_drop=ALL, independientemente
    de network_mode. Debe fallar en la creación del socket, no en el
    envío de datos.
    """
    code = (
        "import socket\n"
        "try:\n"
        "    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)\n"
        "    print('SOCKET_RAW_CREADO')\n"
        "except PermissionError as e:\n"
        "    print('BLOQUEADO:', e)\n"
    )
    result = runner.run(code)
    assert "SOCKET_RAW_CREADO" not in result.stdout
    assert "BLOQUEADO" in result.stdout


def test_docker_socket_not_visible_inside_container(runner):
    """
    El socket de Docker del host NUNCA debe ser visible dentro del
    contenedor efímero de ejecución (solo el servicio sandbox_runner,
    fuera de este contenedor, debe tener ese acceso). Si esto falla,
    es una fuga de configuración crítica: el código ejecutado podría
    lanzar sus propios contenedores sin límites.
    """
    code = (
        "import os\n"
        "exists = os.path.exists('/var/run/docker.sock')\n"
        "print('DOCKER_SOCK_VISIBLE' if exists else 'DOCKER_SOCK_AUSENTE')\n"
    )
    result = runner.run(code)
    assert "DOCKER_SOCK_AUSENTE" in result.stdout
    assert "DOCKER_SOCK_VISIBLE" not in result.stdout


def test_pid_namespace_is_isolated(runner):
    """
    El contenedor debe correr en su propio namespace de PIDs: no debe
    ver el árbol de procesos completo del host. Si el host tiene
    cientos de procesos y el contenedor los ve todos, el namespace de
    PIDs no está aislado (fuga de información + superficie de ataque
    para intentar señalizar/matar procesos del host).
    """
    code = (
        "import os\n"
        "pids = [p for p in os.listdir('/proc') if p.isdigit()]\n"
        "print('PID_COUNT:', len(pids))\n"
    )
    result = runner.run(code)
    assert result.status == "success"
    count_line = next(l for l in result.stdout.splitlines() if "PID_COUNT" in l)
    pid_count = int(count_line.split(":")[1].strip())
    # Un contenedor aislado debería ver solo su propio proceso (y quizá
    # un puñado más). Un host real normalmente tiene decenas o cientos.
    # Umbral generoso para evitar falsos positivos por procesos internos
    # de Python, pero suficiente para detectar una fuga real de namespace.
    assert pid_count < 15, f"Se esperaban pocos PIDs (namespace aislado), se vieron {pid_count}"


def test_no_sensitive_environment_variables_leaked(runner):
    """
    Las credenciales de proveedores multimodales (IMAGE_GEN_API_KEY,
    etc.) NUNCA deben llegar al proceso ejecutado en el sandbox — ver
    diseño en tool_integration/. Este test confirma esa garantía desde
    el lado del sandbox: el entorno del contenedor no debe contener
    ninguna variable con esos nombres ni patrones típicos de secretos.
    """
    code = (
        "import os\n"
        "sensitive_markers = ('API_KEY', 'SECRET', 'TOKEN', 'PASSWORD')\n"
        "leaked = [k for k in os.environ if any(m in k.upper() for m in sensitive_markers)]\n"
        "print('LEAKED:', leaked)\n"
    )
    result = runner.run(code)
    assert result.status == "success"
    assert "LEAKED: []" in result.stdout


def test_container_filesystem_is_its_own_not_hosts(runner):
    """
    Sanity check de aislamiento de filesystem: /etc/hostname dentro del
    contenedor debe ser el hostname aleatorio del contenedor (un ID
    corto de Docker), no el hostname real de la máquina host. Confirma
    que el namespace de filesystem/UTS está aislado, no solo que
    /workspace es read-only.
    """
    code = "print(open('/etc/hostname').read().strip())"
    result = runner.run(code)
    assert result.status == "success"
    # El hostname de un contenedor Docker es el container ID corto
    # (12 caracteres hexadecimales), nunca el hostname real del NUC.
    hostname = result.stdout.strip()
    assert len(hostname) == 12
    assert all(c in "0123456789abcdef" for c in hostname)


def test_cannot_read_host_shadow_file(runner):
    """
    Ni siquiera con read_only=True hay garantía explícita contra leer
    archivos sensibles del propio contenedor si por error corriera como
    root. Este test confirma que corre como usuario no-root real: leer
    /etc/shadow (permisos típicos 640 root:shadow) debe fallar con
    PermissionError.
    """
    code = (
        "try:\n"
        "    content = open('/etc/shadow').read()\n"
        "    print('LECTURA_EXITOSA')\n"
        "except PermissionError as e:\n"
        "    print('BLOQUEADO:', e)\n"
        "except FileNotFoundError:\n"
        "    print('BLOQUEADO: archivo no existe en esta imagen')\n"
    )
    result = runner.run(code)
    assert "LECTURA_EXITOSA" not in result.stdout
    assert "BLOQUEADO" in result.stdout

