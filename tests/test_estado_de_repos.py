"""
Tests de scripts/estado_de_repos.py — el chequeo que evita mezclar commits entre kal-in y
kal.

Contexto del bug real que lo motivó: un port a medio hacer en archivos COMPARTIDOS
(`kernel/`, `sdk/`, `audit/`, `code_analysis/`) quedó sin commitear mientras se commiteaba
en el otro repo con `git add -A`. El trabajo ajeno se pudo haber colado en ese commit, o
haber quedado huérfano. La regla (stages explícitos, terminar el port en los dos repos)
está en CONTRIBUTING.es.md; este chequeo la hace visible antes de commitear.
"""
from __future__ import annotations

import subprocess

from scripts.estado_de_repos import compartidos_sucios, estado


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@e.com", "-c", "user.name=T", *args],
        check=True, capture_output=True,
    )


def _repo_con_commit(tmp_path, nombre, archivos: dict[str, str]):
    repo = tmp_path / nombre
    (repo).mkdir()
    _git(repo, "init", "-b", "main")
    for rel, contenido in archivos.items():
        ruta = repo / rel
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(contenido, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "inicial")
    return repo


def test_un_compartido_sin_commitear_se_reporta(tmp_path):
    repo = _repo_con_commit(tmp_path, "kal-in", {"kernel/x.py": "x = 1\n"})
    (repo / "kernel" / "x.py").write_text("x = 2  # port a medio hacer\n", encoding="utf-8")

    e = estado(repo)

    assert e["compartidos_sucios"] == ["kernel/x.py"]
    assert compartidos_sucios({"kal-in": e}) == {"kal-in": ["kernel/x.py"]}


def test_un_archivo_no_compartido_no_bloquea(tmp_path):
    """`agent_core/` es de kal-in: un cambio ahí no puede mezclarse con kal."""
    repo = _repo_con_commit(tmp_path, "kal-in", {"agent_core/x.py": "x = 1\n"})
    (repo / "agent_core" / "x.py").write_text("x = 2\n", encoding="utf-8")

    e = estado(repo)

    assert e["compartidos_sucios"] == []
    assert e["sucios"] == ["agent_core/x.py"]
    assert compartidos_sucios({"kal-in": e}) == {}


def test_los_directorios_compartidos_son_los_del_drift_checker():
    """Si estas dos listas divergen, el chequeo vigila donde el drift checker no mira."""
    from scripts.check_kernel_drift import SHARED_DIRS
    from scripts.estado_de_repos import DIRECTORIOS_COMPARTIDOS

    assert set(DIRECTORIOS_COMPARTIDOS) == set(SHARED_DIRS)


def test_una_skill_compartida_sucia_tambien_se_reporta(tmp_path):
    """CONTRIBUTING nombra el caso: una skill con el mismo nombre en ambos repos es port."""
    repo = _repo_con_commit(tmp_path, "kal-in", {"skills/eco/tool.py": "x = 1\n"})
    (repo / "skills" / "eco" / "tool.py").write_text("x = 2\n", encoding="utf-8")

    e = estado(repo, skills_compartidas=("eco",))

    assert e["compartidos_sucios"] == ["skills/eco/tool.py"]
