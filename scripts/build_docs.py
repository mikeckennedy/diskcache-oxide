#!/usr/bin/env python3
"""Build the docs and mirror the static site into the committed repo-root site/ folder.

Modeled after chameleon-rs (https://github.com/mikeckennedy/chameleon-rs).
Great Docs renders the site to ``great-docs/_site/``, an ephemeral, git-ignored build
directory that is regenerated on every build. For git-push static hosting we keep a
committed copy of the site at the repo root in ``site/`` (which nginx serves at the
subpath; ``docs/`` or ``plans/`` hold the design documents). This script runs the build,
mirrors ``_site/`` into ``site/`` (full replace), then applies post-processing fixes (see
``_postprocess`` below).

Run it with the project's virtualenv interpreter, e.g.::

    venv/bin/python scripts/build_docs.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
PKG_DIR = _SCRIPTS.parent  # dir holding pyproject.toml + great-docs.yml
REPO_ROOT = PKG_DIR  # the package lives at the git root (single-tier)
SITE = PKG_DIR / 'great-docs' / '_site'
DEST = REPO_ROOT / 'site'


def _site_url() -> str:
    """Read ``site_url`` from great-docs.yml so the canonical base never drifts from config."""
    cfg = PKG_DIR / 'great-docs.yml'
    if not cfg.is_file():
        return ''
    m = re.search(r'(?m)^\s*site_url:\s*["\']?([^"\'\n]+)', cfg.read_text(encoding='utf-8'))
    return m.group(1).strip() if m else ''


def _md_title(md_file: Path, fallback: str) -> str:
    """Extract the first ``# Heading`` from a rendered ``.md`` page in ``site/``."""
    if md_file.is_file():
        for line in md_file.read_text(encoding='utf-8').splitlines():
            if line.startswith('# '):
                return line[2:].strip()
    return fallback


def _append_llms_sections(dest: Path, base: str) -> None:
    """Append User Guide, custom sections, and skill links to ``llms.txt``.

    great-docs 0.13.0 generates ``llms.txt`` before adding the User Guide and custom
    sections to ``_quarto.yml``, so ``llms.txt`` only lists ``### API Reference``.
    """
    llms = dest / 'llms.txt'
    quarto_yml = PKG_DIR / 'great-docs' / '_quarto.yml'
    if not llms.is_file() or not quarto_yml.is_file():
        return
    text = llms.read_text(encoding='utf-8')
    if '### User Guide' in text:
        return

    qtext = quarto_yml.read_text(encoding='utf-8')
    extra: list[str] = []

    ug_match = re.search(r'(?ms)^\s*-\s+id:\s+user-guide\b(.*?)(?=^\s*-\s+id:|\Z)', qtext)
    if ug_match:
        extra.extend(['### User Guide', ''])
        for token in re.finditer(
            r'^\s*-\s+section:\s*(.+?)\s*$|^\s*-\s+(user-guide/\S+?)\.qmd\s*$',
            ug_match.group(1),
            re.MULTILINE,
        ):
            sec, rel = token.group(1), token.group(2)
            if sec:
                if extra[-1] != '':
                    extra.append('')
                extra.extend([f'#### {sec.strip()}', ''])
            elif rel:
                title = _md_title(dest / f'{rel}.md', rel.rsplit('/', 1)[-1])
                extra.append(f'- [{title}]({base}{rel}.html)')
        extra.append('')

    for sec_id, heading in (('divergences', 'Divergences'), ('recipes', 'Recipes')):
        sec_match = re.search(
            rf'(?ms)^\s*-\s+id:\s+{sec_id}\b(.*?)(?=^\s*-\s+id:|^\s*page-footer:|\Z)',
            qtext,
        )
        if not sec_match:
            continue
        extra.extend([f'### {heading}', ''])
        for rel in re.findall(rf'href:\s*({sec_id}/\S+?)\.qmd\b', sec_match.group(1)):
            if rel.endswith('/index'):
                continue
            title = _md_title(dest / f'{rel}.md', rel.rsplit('/', 1)[-1])
            extra.append(f'- [{title}]({base}{rel}.html)')
        extra.append('')

    extra.extend(
        [
            '### Agent Skill and Full Context',
            '',
            (
                f'- [Agent Skill (SKILL.md)]({base}skill.md): '
                'Compact guide, decision table, gotchas and examples for AI coding agents'
            ),
            (
                f'- [Full Documentation (llms-full.txt)]({base}llms-full.txt): '
                'Complete API reference and User Guide in a single file'
            ),
            '',
        ]
    )
    llms.write_text(text.rstrip() + '\n\n' + '\n'.join(extra), encoding='utf-8')
    print('  llms.txt: appended User Guide, custom sections, and skill links')


def _postprocess(dest: Path) -> None:
    """Work around great-docs 0.13.0 issues in the mirrored site.

    1. Prune dead entries from search.json. great-docs indexes a "skill" page as
       ``skill.html`` but only emits ``skill.md`` / ``skills.html``, so those search results
       404. We drop any entry whose target file does not exist under site/.
    2. Inject ``<link rel="canonical">``. great-docs' canonical-injection guard
       false-positives on a JS string containing ``rel="canonical"`` and so emits no real
       canonical element. We add one per page, taking the exact URL from sitemap.xml so it
       matches the sitemap.
    3. Append User Guide, custom sections, and skill links to ``llms.txt``.
    """
    # 1) Prune search.json entries that point at missing files.
    search = dest / 'search.json'
    if search.is_file():
        try:
            entries = json.loads(search.read_text(encoding='utf-8'))
            kept = [
                e for e in entries if not (href := str(e.get('href', '')).split('#', 1)[0]) or (dest / href).exists()
            ]
            dropped = len(entries) - len(kept)
            if dropped:
                search.write_text(json.dumps(kept), encoding='utf-8')
                print(f'  search.json: pruned {dropped} dead entr{"y" if dropped == 1 else "ies"}')
        except (ValueError, OSError) as exc:
            print(f'  search.json prune skipped: {exc}', file=sys.stderr)

    # 2) Inject canonical links derived from sitemap.xml.
    base = _site_url()
    sitemap = dest / 'sitemap.xml'
    if base and sitemap.is_file():
        injected = 0
        for loc in re.findall(r'<loc>([^<]+)</loc>', sitemap.read_text(encoding='utf-8')):
            if not loc.startswith(base):
                continue
            rel = loc[len(base) :]
            if rel == '' or rel.endswith('/'):
                rel += 'index.html'
            page = dest / rel
            if not page.is_file():
                continue
            html = page.read_text(encoding='utf-8')
            if re.search(r'<link[^>]*\brel="canonical"', html):
                continue
            new = html.replace('<head>', f'<head>\n<link rel="canonical" href="{loc}">', 1)
            if new != html:
                page.write_text(new, encoding='utf-8')
                injected += 1
        if injected:
            print(f'  canonical: injected <link rel=canonical> into {injected} page(s)')

    # 3) Append User Guide and skill links to llms.txt.
    _append_llms_sections(dest, base)


def main() -> int:
    # Ensure GriffeLoader runs in pure static mode for compiled PyO3 classes
    # so that .pyi stubs are parsed without cyclic alias errors between
    # compiled extension re-exports.
    import griffe
    from griffe import GriffeLoader

    _orig_init = GriffeLoader.__init__

    def _safe_init(self: GriffeLoader, *args: object, **kwargs: object) -> None:
        kwargs['allow_inspection'] = False
        _orig_init(self, *args, **kwargs)

    GriffeLoader.__init__ = _safe_init  # type: ignore[method-assign]

    from great_docs.cli import main as gd_main

    sys.argv = ['great-docs', 'build']
    try:
        gd_main()
    except SystemExit as exc:
        if exc.code not in (0, None):
            return int(exc.code) if isinstance(exc.code, int) else 1
    except Exception as exc:
        print(f'Build error: {exc}', file=sys.stderr)
        return 1

    if not SITE.is_dir():
        print(f'build output missing: {SITE}', file=sys.stderr)
        return 1

    if DEST.exists():
        shutil.rmtree(DEST)
    shutil.copytree(SITE, DEST)
    _postprocess(DEST)
    print(f'Mirrored -> {DEST} ({sum(1 for p in DEST.rglob("*") if p.is_file())} files)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
