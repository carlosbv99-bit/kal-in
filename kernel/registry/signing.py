"""
Firma digital de herramientas dinámicas creadas por el propio agente.

No es autenticación de usuario ni prueba de que un humano aprobó la
herramienta (eso ya lo hace el pipeline de aprobación en registry.py).
Es la identidad criptográfica de kal como creador: firma cada versión
activada de una herramienta con una clave Ed25519 local, así que
cualquier edición del .py en disco fuera de ese pipeline (por ejemplo,
alguien tocando data/tool_versions/<name>/<name>_v3.py a mano) se
vuelve detectable — la firma deja de verificar contra el contenido real.

La clave privada nunca sale de data/keys/ (fuera de git, ver
.gitignore: data/) y no protege contra un atacante con acceso de
escritura a ese directorio; protege contra modificación accidental o
fuera de banda del código de una herramienta ya aprobada.
"""
from __future__ import annotations

from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from utils.config import settings

DEFAULT_KEY_DIR = Path("data/keys")


def _canonical_payload(name: str, version: int, source_code: str) -> bytes:
    return f"{name}\n{version}\n{source_code}".encode()


class ToolSigner:
    def __init__(self, key_dir: Path | str = DEFAULT_KEY_DIR):
        self.key_dir = Path(key_dir)
        self.key_dir.mkdir(parents=True, exist_ok=True)
        self._private_key_path = self.key_dir / "tool_signing_key"
        self._public_key_path = self.key_dir / "tool_signing_key.pub"
        self._private_key = self._load_or_create_keypair()

    def _load_or_create_keypair(self) -> Ed25519PrivateKey:
        if self._private_key_path.exists():
            raw = self._private_key_path.read_bytes()
            return Ed25519PrivateKey.from_private_bytes(raw)

        private_key = Ed25519PrivateKey.generate()
        raw_private = private_key.private_bytes(
            encoding=Encoding.Raw, format=PrivateFormat.Raw, encryption_algorithm=NoEncryption()
        )
        raw_public = private_key.public_key().public_bytes(
            encoding=Encoding.Raw, format=PublicFormat.Raw
        )
        self._private_key_path.write_bytes(raw_private)
        self._private_key_path.chmod(0o600)
        self._public_key_path.write_bytes(raw_public)
        return private_key

    def public_key_hex(self) -> str:
        raw_public = self._private_key.public_key().public_bytes(
            encoding=Encoding.Raw, format=PublicFormat.Raw
        )
        return raw_public.hex()

    def sign(self, name: str, version: int, source_code: str) -> str:
        signature = self._private_key.sign(_canonical_payload(name, version, source_code))
        return signature.hex()

    def verify(self, name: str, version: int, source_code: str, signature_hex: str) -> bool:
        public_key: Ed25519PublicKey = self._private_key.public_key()
        try:
            public_key.verify(
                bytes.fromhex(signature_hex),
                _canonical_payload(name, version, source_code),
            )
            return True
        except (InvalidSignature, ValueError):
            return False


tool_signer = ToolSigner(key_dir=settings.signing.key_dir)
