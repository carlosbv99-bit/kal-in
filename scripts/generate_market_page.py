"""
Genera docs/index.html: una página estática navegable sobre el mismo
catálogo que ya usa el market (Fase B del plan de comunidad, ver
kernel/registry/skill_market.py para la Fase A — instalación
remota). Sin backend ni JS: se corre a mano, se commitea el HTML
resultante, y GitHub Pages lo sirve directamente desde /docs en main.

A diferencia de load_skills() (kernel/registry/skills.py), esto NO
filtra por `enabled` — el listado del market muestra todo lo que hay
publicado en el repo; `enabled` es una decisión de instalación local
de cada usuario, no una propiedad del catálogo.

Uso:
    python3 scripts/generate_market_page.py
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kernel.registry.skill_signing import verify_skill_signature
from kernel.registry.skills import (
    DEFAULT_SKILLS_DIR,
    MANIFEST_FILENAME,
    SkillManifest,
    parse_manifest,
)

_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kal — Skill Market</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0; padding: 0 1.5rem 4rem;
    background: #0d1117; color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }}
  header {{ max-width: 860px; margin: 0 auto; padding: 3rem 0 2rem; }}
  h1 {{ font-size: 2.2rem; margin: 0 0 0.5rem; color: #e6edf3; }}
  .tagline {{ font-size: 1.05rem; color: #8b949e; max-width: 620px; line-height: 1.5; }}
  .install-hint {{ color: #8b949e; }}
  code {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 0.15rem 0.4rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  main.grid {{ max-width: 860px; margin: 0 auto; display: grid; gap: 1rem; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 1.25rem 1.5rem; }}
  .card h2 {{ margin: 0 0 0.4rem; font-size: 1.25rem; color: #e6edf3; }}
  .version {{ color: #8b949e; font-size: 0.85rem; font-weight: normal; }}
  .description {{ color: #c9d1d9; margin: 0 0 0.75rem; }}
  .badges {{ margin-bottom: 0.75rem; }}
  .badge {{ display: inline-block; font-size: 0.78rem; border-radius: 999px; padding: 0.2rem 0.7rem; margin-right: 0.4rem; }}
  .badge.verified {{ background: #0f3d21; color: #7ee2a8; border: 1px solid #1a5c34; }}
  .badge.unsigned {{ background: #3d2c0f; color: #e2b77e; border: 1px solid #5c451a; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 0.25rem 0.75rem; margin: 0 0 0.9rem; font-size: 0.88rem; }}
  dt {{ color: #8b949e; }}
  dd {{ margin: 0; }}
  pre.install {{ margin: 0; }}
  pre.install code {{ display: block; padding: 0.5rem 0.75rem; overflow-x: auto; }}
  .empty {{ color: #8b949e; }}
  footer {{ max-width: 860px; margin: 2.5rem auto 0; color: #8b949e; font-size: 0.85rem; }}
  footer a {{ color: #7ee2a8; }}
</style>
</head>
<body>
<header>
  <h1>Kal — Skill Market</h1>
  <p class="tagline">
    Sandboxed capabilities for the Kal microkernel. Every skill listed
    here is signature-verified before install — installing from this
    market never proceeds on an unsigned or altered package.
  </p>
  <p class="install-hint">Install any of these with:<br><code>python3 scripts/install_from_market.py &lt;name&gt;</code></p>
</header>
<main class="grid">
{cards}
</main>
<footer>
  <p>{count} skill(s) published · <a href="https://github.com/carlosbv99-bit/kal">Source</a> · <a href="https://github.com/carlosbv99-bit/kal/blob/main/README.md">About Kal</a></p>
</footer>
</body>
</html>
"""

_CARD_TEMPLATE = """<article class="card">
  <h2>{name} <span class="version">v{version}</span></h2>
  <p class="description">{description}</p>
  <div class="badges">{signature_badge}</div>
  <dl>
    <dt>Permissions</dt><dd>{permissions}</dd>
    <dt>Requirements</dt><dd>{requirements}</dd>
    <dt>Kernel services</dt><dd>{kernel_services}</dd>
  </dl>
  <pre class="install"><code>python3 scripts/install_from_market.py {name}</code></pre>
</article>"""


def _badge_for_signature(signature_status: str) -> str:
    if signature_status == "verified":
        return '<span class="badge verified">&check; signature verified</span>'
    return '<span class="badge unsigned">unsigned</span>'


def _render_card(manifest: SkillManifest, signature_status: str) -> str:
    return _CARD_TEMPLATE.format(
        name=html.escape(manifest.name),
        version=html.escape(manifest.version),
        description=html.escape(manifest.description),
        signature_badge=_badge_for_signature(signature_status),
        permissions=html.escape(", ".join(manifest.permissions)) or "(none)",
        requirements=html.escape(", ".join(manifest.requirements)) or "(none, stdlib only)",
        kernel_services=html.escape(", ".join(manifest.kernel_services)) or "(none)",
    )


def render_market_html(skills_dir: Path = DEFAULT_SKILLS_DIR) -> str:
    entries: list[tuple[SkillManifest, str]] = []
    if skills_dir.exists():
        for manifest_path in sorted(skills_dir.glob(f"*/{MANIFEST_FILENAME}")):
            try:
                manifest = parse_manifest(manifest_path)
            except Exception as e:  # noqa: BLE001 — un manifiesto roto no debe romper la página entera
                print(f"Advertencia: manifiesto roto en {manifest_path}, se ignora al listar: {e}", file=sys.stderr)
                continue
            signature_status = verify_skill_signature(manifest_path.parent)
            entries.append((manifest, signature_status))

    cards = "\n".join(_render_card(m, s) for m, s in entries) if entries else '<p class="empty">No skills published yet.</p>'
    return _PAGE_TEMPLATE.format(cards=cards, count=len(entries))


def main() -> None:
    html_content = render_market_html()
    out_path = Path(__file__).parent.parent / "docs" / "index.html"
    out_path.write_text(html_content, encoding="utf-8")
    print(f"Escrito {out_path}")


if __name__ == "__main__":
    main()
