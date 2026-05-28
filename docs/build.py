#!/usr/bin/env python3
"""Render brand-bearing docs from *.md.tmpl Jinja2 sources. (#316, #334)

Reads `docs/_config.yml`, walks every `docs/tmpl/**/*.md.tmpl`, and
writes the rendered output to the corresponding location in the docs
tree (or repo root for README). Both source template and rendered `.md`
are committed — the template is what humans edit, the rendered `.md` is
what GitHub / GitLab serve to readers.

Path mapping:

- `docs/tmpl/user-guide/operating.md.tmpl` → `docs/user-guide/operating.md`
- `docs/tmpl/reference/architecture.md.tmpl` → `docs/reference/architecture.md`
- `docs/tmpl/README.md.tmpl` → `README.md` (root level — the repo's
  front page lives at the conventional location even though its source
  is bucketed with the rest of the templates)

## Modes

- `python docs/build.py` — render mode. Writes rendered `.md` files.
- `python docs/build.py --check` — drift mode. Renders to memory and
  compares against the committed `.md`; exits non-zero if any file is
  out of sync. CI runs this to fail merges that edited the template
  but forgot to rebuild (or edited the rendered file directly).

## Adding a new variable

Add to `docs/_config.yml`. Reference in templates as `{{ var_name }}`.
Jinja2 filters are available (`{{ app_name | upper }}`, etc.).
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

DOCS_DIR = Path(__file__).resolve().parent
REPO_ROOT = DOCS_DIR.parent
TMPL_DIR = DOCS_DIR / "tmpl"
CONFIG_PATH = DOCS_DIR / "_config.yml"


def load_config() -> dict:
    with CONFIG_PATH.open() as fh:
        return yaml.safe_load(fh)


def find_templates() -> list[Path]:
    """Walk docs/tmpl/**/*.md.tmpl — the mirror tree of all template sources."""
    return sorted(TMPL_DIR.rglob("*.md.tmpl"))


def render(template_path: Path, env: Environment, config: dict) -> str:
    rel = template_path.relative_to(REPO_ROOT)
    template = env.get_template(str(rel))
    return template.render(**config)


def rendered_path(template_path: Path) -> Path:
    """Map a template under docs/tmpl/ to its rendered output location.

    Templates at `docs/tmpl/<subdir>/foo.md.tmpl` render to
    `docs/<subdir>/foo.md`. Top-level templates directly under
    `docs/tmpl/` (only README today) render to the repo root — README
    needs to live at the conventional repo-root location for GitHub /
    GitLab to recognize it as the project front page.
    """
    rel = template_path.relative_to(TMPL_DIR)
    if rel.parent == Path("."):
        return REPO_ROOT / rel.with_suffix("")
    return DOCS_DIR / rel.with_suffix("")


def main(argv: list[str]) -> int:
    check_mode = "--check" in argv

    config = load_config()
    # Loader rooted at REPO_ROOT so both `docs/foo.md.tmpl` and
    # `README.md.tmpl` resolve via their repo-relative path. StrictUndefined
    # turns `{{ missing_var }}` into an error rather than silently producing
    # an empty string — keeps the templates honest.
    env = Environment(
        loader=FileSystemLoader(REPO_ROOT),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )

    templates = find_templates()
    if not templates:
        print("no *.md.tmpl files found under docs/ or repo root; nothing to do")
        return 0

    drift: list[Path] = []
    for tmpl in templates:
        rendered = render(tmpl, env, config)
        out = rendered_path(tmpl)

        if check_mode:
            existing = out.read_text() if out.exists() else ""
            if existing != rendered:
                drift.append(out.relative_to(REPO_ROOT))
        else:
            out.write_text(rendered)
            print(f"rendered {out.relative_to(REPO_ROOT)}")

    if check_mode:
        if drift:
            print(
                "doc drift detected — these rendered files don't match what "
                "would be produced from their .md.tmpl source + _config.yml:",
                file=sys.stderr,
            )
            for p in drift:
                print(f"  {p}", file=sys.stderr)
            print(
                "\nrun `python docs/build.py` and commit the regenerated *.md.",
                file=sys.stderr,
            )
            return 1
        print(f"checked {len(templates)} templates — all rendered files in sync")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
