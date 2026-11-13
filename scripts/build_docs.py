#!/usr/bin/env python3
"""Build the docs and mirror the static site into the committed repo-root site/ folder.

Modeled after chameleon-rs (https://github.com/mikeckennedy/chameleon-rs).
Great Docs renders the site to ``great-docs/_site/``, an ephemeral, git-ignored build
directory that is regenerated on every build. For git-push static hosting we keep a
committed copy of the site at the repo root in ``site/`` (which nginx serves at the
subpath; ``docs/`` or ``plans/`` hold the design documents). This script runs the build,
mirrors ``_site/`` into ``site/`` (full replace), then applies post-processing fixes (see
``_postprocess`` below), and fails if ``llms.txt``, ``llms-full.txt`` or the hand-written
skill is missing from the output (see ``_check_outputs``).

Great Docs takes the version badge in the navbar and the Changelog page from the repository's
latest GitHub Release, fetched at build time. The docs are built and committed before the release
exists, so the badge would show the previous version (or nothing, before the first release).
``_local_releases`` builds the same release records from Cargo.toml and CHANGELOG.md instead, and
``main`` installs it in place of the GitHub fetch, so the badge and the Changelog page always
describe this checkout. ``_check_outputs`` fails the build if no page got the badge.

Great Docs writes ``llms-full.txt`` by importing the package and reading each object's
``__doc__`` and signature. The compiled extension carries one-line docstrings and bare
signatures; the full ones live in ``_core.pyi``, which the reference pages already use through
griffe. ``_install_stub_twins`` swaps the stub's classes in for the compiled ones in this
process only, so ``llms-full.txt`` shows the same docstrings, Examples included, as the reference.

Run it with the project's virtualenv interpreter (``uv pip install --python venv/bin/python --group docs``
installs Great Docs into it), e.g.::

    venv/bin/python scripts/build_docs.py
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import griffe

_SCRIPTS = Path(__file__).resolve().parent
PKG_DIR = _SCRIPTS.parent  # dir holding pyproject.toml + great-docs.yml
REPO_ROOT = PKG_DIR  # the package lives at the git root (single-tier)
SITE = PKG_DIR / 'great-docs' / '_site'
DEST = REPO_ROOT / 'site'
SKILL = PKG_DIR / 'skills' / 'diskcache-oxide' / 'SKILL.md'
# The files an agent reads, and the paths Great Docs publishes the skill to. Each must exist after
# a build, and each skill copy must be the hand-written file, or the build failed quietly.
REQUIRED = ('llms.txt', 'llms-full.txt', 'skill.md')
# The measured case for the library, inserted near the top of llms.txt so an agent choosing a cache sees it before
# the API index. Each line names the page or results file its numbers come from; update them together.
WHY_CHOOSE_IT = (
    'Measured against the defunct python-diskcache 5.6.3 (last release August 2023), same machine, same session:',
    '',
    '- A cached `get` in 0.76 µs at p50 (0.42× diskcache), a `set` in 22.6 µs (0.77×), on Python 3.14; the '
    'free-threaded build is within a few percent. See [Performance]({base}user-guide/performance.html).',
    '- Eight threads sharing one `Cache` at a 99:1 read:write mix: about 650,000 operations a second, flat from 1 to 8 '
    'threads, where diskcache falls from 406,000 to 19,000 (Apple M5 Pro, Python 3.14 with the GIL; '
    '`bench/results/macos/gate-3.14.txt`). See the chart on the [home page]({base}).',
    '- Eight worker processes writing to a `FanoutCache(shards=8)`: about 1.5 million operations a second, against '
    "262,000 for a plain `Cache` and 1.24 million for diskcache's own `FanoutCache`. See "
    '[FanoutCache]({base}user-guide/fanoutcache.html).',
    '- A prefix scan over 400,000 keys in about 10 ms, against about 25 seconds in diskcache, because '
    '`iterkeys(prefix=)` is an indexed range scan. See '
    '[Getting started]({base}user-guide/getting-started.html#list-keys).',
    '- Beyond speed: safe across `fork()` and on free-threaded CPython, no pickle on disk unless asked for, bounded '
    'connections, memory and write-ahead log, and three diskcache bugs fixed (`clear()` losing concurrent writes, '
    '`expire()` leaving entries behind, spurious misses during an overwrite). See '
    '[Divergences]({base}user-guide/divergences.html).',
)
CARGO_TOML = REPO_ROOT / 'Cargo.toml'
CHANGELOG = REPO_ROOT / 'CHANGELOG.md'
_VERSION_BADGE = re.compile(r'<span class="version-badge"[^>]*>v([^<]*)</span>')
# `## [0.1.0] - 2026-11-09`, with a trailing marker such as `[YANKED]` allowed.
_CHANGELOG_HEADING = re.compile(r'^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?.*$', re.MULTILINE)
_LINK_DEFINITION = re.compile(r'^\[[^\]]+\]:\s+\S+\s*$', re.MULTILINE)
SKILL_COPIES = ('skill.md', '.well-known/agent-skills/diskcache-oxide/SKILL.md', '.well-known/skills/default/SKILL.md')


def _package_version() -> str:
    """The version from ``[workspace.package]`` in Cargo.toml, which pyproject.toml reads dynamically."""
    import tomllib

    return str(tomllib.loads(CARGO_TOML.read_text(encoding='utf-8'))['workspace']['package']['version'])


def _local_releases(repo_url: str, max_releases: int = 50) -> list[dict[str, Any]]:
    """Release records shaped like the GitHub API's, built from Cargo.toml and CHANGELOG.md.

    Great Docs reads ``tag_name``, ``name``, ``body``, ``published_at``, ``html_url``, ``prerelease`` and
    ``draft`` from each record: the first one feeds the navbar's version badge, and all of them the
    Changelog page. The first record is always the version in Cargo.toml. When CHANGELOG.md has a dated
    heading for it, that date and body are used; otherwise it is the ``Unreleased`` section, marked as
    such and undated, so a build between releases still shows the version it was built from. The
    remaining records are the changelog's other dated entries, newest first. Link-reference
    definitions at the end of the file are dropped from the last body.
    """
    version = _package_version()
    text = CHANGELOG.read_text(encoding='utf-8') if CHANGELOG.is_file() else ''
    text = _LINK_DEFINITION.sub('', text)
    headings = list(_CHANGELOG_HEADING.finditer(text))
    sections: dict[str, tuple[str | None, str]] = {}
    for i, m in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        sections[m.group('version')] = (m.group('date'), text[m.end() : end].strip())
    base = repo_url.rstrip('/')

    def record(ver: str, date: str | None, body: str, *, unreleased: bool = False) -> dict[str, Any]:
        return {
            'tag_name': f'v{ver}',
            'name': f'v{ver} (unreleased)' if unreleased else f'v{ver}',
            'body': body,
            'published_at': f'{date}T00:00:00Z' if date else '',
            'html_url': f'{base}/blob/main/CHANGELOG.md' if unreleased else f'{base}/releases/tag/v{ver}',
            'prerelease': False,
            'draft': False,
        }

    releases: list[dict[str, Any]] = []
    date, body = sections.get(version, (None, ''))
    if date:
        releases.append(record(version, date, body))
    else:
        unreleased_body = sections.get('Unreleased', (None, ''))[1]
        releases.append(record(version, None, unreleased_body, unreleased=True))
    for ver, (date, body) in sections.items():
        if ver == version or not date:
            continue
        releases.append(record(ver, date, body))
    return releases[:max_releases]


def _site_url() -> str:
    """Read ``site_url`` from great-docs.yml so the canonical base never drifts from config."""
    cfg = PKG_DIR / 'great-docs.yml'
    if not cfg.is_file():
        return ''
    m = re.search(r'(?m)^\s*site_url:\s*["\']?([^"\'\n]+)', cfg.read_text(encoding='utf-8'))
    return m.group(1).strip() if m else ''


_FRONTMATTER = re.compile(r'\A---\n(.*?)\n---\n', re.S)


def _frontmatter(qmd: Path) -> dict[str, str]:
    """The ``key: value`` lines of a page's YAML front matter, quotes stripped. Only flat string keys."""
    if not qmd.is_file():
        return {}
    m = _FRONTMATTER.match(qmd.read_text(encoding='utf-8'))
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        km = re.match(r'^([\w-]+):\s*(.*)$', line)
        if km:
            out[km.group(1)] = km.group(2).strip().strip('"').strip("'")
    return out


def _source_qmd(rel: str) -> Path:
    """The source page for a site path such as ``user-guide/caches`` or ``recipes/counters``."""
    section, _, name = rel.partition('/')
    return PKG_DIR / section.replace('-', '_') / f'{name}.qmd'


def _page_entry(base: str, rel: str, title: str) -> str:
    """An ``llms.txt`` line for a page, with its front-matter description when it has one."""
    desc = _frontmatter(_source_qmd(rel)).get('description', '')
    return f'- [{title}]({base}{rel}.html)' + (f': {desc.rstrip(".")}' if desc else '')


def _clean_quarto(text: str) -> str:
    """Make a page's Quarto source read as plain Markdown: drop fenced-div markers (the chart wrappers), turn
    executable cells into Python code blocks, and drop their ``#|`` options and the hidden cleanup cells."""
    out: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(':::'):
            i += 1
            continue
        if line.strip() == '```{python}':
            j = i + 1
            while j < len(lines) and lines[j].strip() != '```':
                j += 1
            cell = lines[i + 1 : j]
            if not any(c.strip() == '#| echo: false' for c in cell):
                out.append('```python')
                out.extend(c for c in cell if not c.startswith('#|'))
                out.append('```')
            i = j + 1
            continue
        out.append(line)
        i += 1
    return '\n'.join(out).strip() + '\n'


def _recipe_order() -> list[str]:
    """Recipe page names in the order the recipes index presents them."""
    index = PKG_DIR / 'recipes' / 'index.qmd'
    names = re.findall(r'\]\(([\w-]+)\.qmd\)', index.read_text(encoding='utf-8')) if index.is_file() else []
    seen = list(dict.fromkeys(n for n in names if n != 'index'))
    for qmd in sorted((PKG_DIR / 'recipes').glob('*.qmd')):
        if qmd.stem != 'index' and qmd.stem not in seen:
            seen.append(qmd.stem)
    return seen


def _fix_llms_full(dest: Path) -> None:
    """great-docs 0.13.0 pastes the User Guide's Quarto source into ``llms-full.txt`` (fenced-div markers and
    ``{python}`` cells included) and leaves the recipes out. Clean the source, and append the recipes."""
    path = dest / 'llms-full.txt'
    if not path.is_file():
        return
    text = _clean_quarto(path.read_text(encoding='utf-8'))
    if '\n## Recipes\n' not in text:
        parts = ['', '## Recipes', '']
        for name in _recipe_order():
            qmd = PKG_DIR / 'recipes' / f'{name}.qmd'
            fm = _frontmatter(qmd)
            body = _FRONTMATTER.sub('', qmd.read_text(encoding='utf-8'), count=1)
            parts.append(f'### {fm.get("title", name)}')
            parts.append('')
            if fm.get('description'):
                parts.extend([fm['description'], ''])
            parts.append(_clean_quarto(body))
        text = text.rstrip() + '\n' + '\n'.join(parts)
        print(f'  llms-full.txt: cleaned Quarto markup and appended {len(_recipe_order())} recipes')
    else:
        print('  llms-full.txt: cleaned Quarto markup')
    path.write_text(text, encoding='utf-8')


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
                extra.append(_page_entry(base, rel, title))
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
            extra.append(_page_entry(base, rel, title))
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


def _describe_llms_entries(dest: Path) -> None:
    """Give each bare ``- [Name](url)`` line in ``llms.txt`` its docstring's first line.

    great-docs 0.13.0 describes the classes and functions in ``llms.txt`` but not their methods, so an
    agent reading the index sees ``Cache.set`` with nothing after it. The first line of each
    docstring is read from the imported package, where ``_install_stub_twins`` has already put the
    stub's docstrings in place of the compiled extension's.
    """
    import inspect

    import diskcache_oxide

    llms = dest / 'llms.txt'
    if not llms.is_file():
        return
    described = 0
    lines = llms.read_text(encoding='utf-8').splitlines()
    for i, line in enumerate(lines):
        m = re.fullmatch(r'- \[([\w.]+)\]\(([^)]+)\)', line)
        if not m:
            continue
        obj: object = diskcache_oxide
        try:
            for part in m.group(1).split('.'):
                obj = getattr(obj, part)
        except AttributeError:
            continue
        doc = inspect.getdoc(obj) or ''
        if isinstance(obj, property) and obj.fget is not None:
            doc = inspect.getdoc(obj.fget) or doc
        first = doc.strip().split('\n', 1)[0].strip()
        if first and not first.startswith('str(object='):  # a bare `str` constant has `str`'s docstring
            lines[i] = f'{line}: {first.rstrip(".")}'
            described += 1
    if described:
        llms.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        print(f'  llms.txt: described {described} method entries from their docstrings')


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
    4. Describe the method entries in ``llms.txt``, which great-docs leaves bare.
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

    # 3b) The Changelog page is built from CHANGELOG.md (see _local_releases), not GitHub Releases.
    changelog = dest / 'changelog.html'
    if changelog.is_file():
        html = changelog.read_text(encoding='utf-8')
        new = re.sub(
            r'This changelog is generated automatically from\s*<a href="[^"]*/releases">GitHub Releases</a>\.',
            'This changelog is the repository\'s <a href="https://github.com/mikeckennedy/diskcache-oxide/blob/main/'
            'CHANGELOG.md">CHANGELOG.md</a>, rendered at build time.',
            html,
        )
        if new != html:
            changelog.write_text(new, encoding='utf-8')
            print('  changelog.html: credited CHANGELOG.md instead of GitHub Releases')

    # 4) One-line descriptions for the method entries.
    _describe_llms_entries(dest)

    # 5) The measured case for the library, right after the one-line blurb; and a `str` constant inherits str's
    #    docstring, so say what it is instead.
    llms = dest / 'llms.txt'
    if llms.is_file():
        text = llms.read_text(encoding='utf-8')
        if '## Why choose it' not in text:
            why = '\n'.join(line.format(base=base) for line in WHY_CHOOSE_IT)
            text, n = re.subn(r'(?m)^(> .+)$', lambda m: f'{m.group(1)}\n\n## Why choose it\n\n{why}', text, count=1)
            if n:
                llms.write_text(text, encoding='utf-8')
                print('  llms.txt: added the measured case under the blurb')
        fixed = re.sub(
            r'(- \[BUNDLED_SQLITE_VERSION\]\([^)]+\)): str\(object=.*$',
            r'\1: The SQLite version compiled into this build, as a string',
            text,
            flags=re.MULTILINE,
        )
        if fixed != text:
            llms.write_text(fixed, encoding='utf-8')
            print('  llms.txt: described BUNDLED_SQLITE_VERSION')

    # 6) llms-full.txt: plain Markdown, recipes included.
    _fix_llms_full(dest)


def _check_outputs(dest: Path) -> list[str]:
    """What's missing or wrong in the mirrored site. Empty when the build is good."""
    problems = [f'{name} is missing from {dest}' for name in REQUIRED if not (dest / name).is_file()]
    version = _package_version()
    badged = {
        page.relative_to(dest).as_posix(): set(_VERSION_BADGE.findall(page.read_text(encoding='utf-8')))
        for page in dest.rglob('*.html')
    }
    pages = [page for page, versions in badged.items() if versions]
    if not pages:
        problems.append(
            'no page has a version badge (Great Docs changed its markup, or _local_releases was not installed)'
        )
    elif wrong := sorted(page for page, versions in badged.items() if versions - {version}):
        problems.append(f'version badge is not v{version} on: {", ".join(wrong[:5])}')
    else:
        print(f'  version badge v{version} on {len(pages)} page(s)')
    if not SKILL.is_file():
        problems.append(f'the hand-written skill {SKILL} is missing')
        return problems
    expected = SKILL.read_text(encoding='utf-8')
    for rel in SKILL_COPIES:
        copy = dest / rel
        if not copy.is_file():
            problems.append(f'{rel} is missing from {dest}')
        elif copy.read_text(encoding='utf-8') != expected:
            problems.append(f'{rel} is not the hand-written skill (Great Docs generated one instead)')
    index = dest / '.well-known' / 'agent-skills' / 'index.json'
    if index.is_file():
        try:
            names = [s.get('name') for s in json.loads(index.read_text(encoding='utf-8')).get('skills', [])]
        except ValueError as exc:
            problems.append(f'{index.relative_to(dest)} is not JSON: {exc}')
        else:
            if names != ['diskcache-oxide']:
                problems.append(f'{index.relative_to(dest)} lists {names}, not ["diskcache-oxide"]')
    else:
        problems.append(f'.well-known/agent-skills/index.json is missing from {dest}')
    return problems


def _install_stub_twins() -> None:
    """Replace the compiled classes in the imported ``diskcache_oxide`` with ones built from ``_core.pyi``.

    The stub is valid Python: classes whose methods are ``...`` with docstrings. Executing it gives
    classes with the stub's signatures and docstrings, which is what ``llms-full.txt`` reads. Two
    adjustments: an ``@overload``-only method keeps its first overload, undecorated, so ``get`` is a
    real function with its docstring rather than ``typing``'s placeholder; and ``__new__`` stays,
    so ``inspect.signature(Cache)`` shows the constructor. tests/unit/test_stubs.py keeps the stub's
    signatures equal to the compiled module's, so nothing shown here differs from the runtime.

    Only this process sees the twins. Quarto runs the recipes' code in its own kernel, against the
    real extension.
    """
    import diskcache_oxide

    stub_path = Path(diskcache_oxide.__file__).parent / '_core.pyi'
    tree = ast.parse(stub_path.read_text(encoding='utf-8'), filename=str(stub_path))

    def is_overload(node: ast.AST) -> bool:
        return isinstance(node, ast.FunctionDef) and any(is_overload_decorator(d) for d in node.decorator_list)

    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        body: list[ast.stmt] = []
        seen: set[str] = set()
        for node in cls.body:
            if is_overload(node):
                assert isinstance(node, ast.FunctionDef)
                if node.name in seen:
                    continue
                seen.add(node.name)
                node.decorator_list = [d for d in node.decorator_list if not is_overload_decorator(d)]
            body.append(node)
        cls.body = body
    namespace: dict[str, object] = {'__name__': 'diskcache_oxide._core', '__file__': str(stub_path)}
    exec(compile(tree, str(stub_path), 'exec'), namespace)  # noqa: S102 (the project's own stub)

    swapped = []
    for name in diskcache_oxide.__all__:
        twin = namespace.get(name)
        if isinstance(twin, type) and getattr(getattr(diskcache_oxide, name), '__module__', '') == 'diskcache_oxide':
            setattr(diskcache_oxide, name, twin)
            swapped.append(name)
    print(f'  llms-full.txt reads docstrings from _core.pyi for: {", ".join(swapped)}')


def is_overload_decorator(d: ast.expr) -> bool:
    return (isinstance(d, ast.Name) and d.id == 'overload') or (isinstance(d, ast.Attribute) and d.attr == 'overload')


def _stub_extension() -> griffe.Extension:
    """A griffe extension that fills two gaps in how great-docs 0.13.0 reads `_core.pyi`.

    1. A stub method that is only `@overload`s (no implementation is allowed in a stub) never
       becomes a member, so `Cache.get` got no page. Add one built from the overloads, using the
       docstring of whichever overload has it and widening each `Literal[...]` parameter to `bool`.
    2. PyO3 classes are built by `__new__`, and the stub says so (tests/unit/test_stubs.py checks it), but the usage
       line comes from `__init__`, so `Cache` showed as `Cache()`. Add an `__init__` that mirrors
       `__new__`'s parameters.
    """
    import griffe

    class StubFixes(griffe.Extension):
        def on_class_members(self, *, cls: griffe.Class, **kwargs: object) -> None:
            for name, overloads in cls.overloads.items():
                if name in cls.members or not overloads or name == '__new__':
                    continue
                first = overloads[0]
                params = griffe.Parameters(
                    *(
                        griffe.Parameter(
                            p.name,
                            annotation='bool' if str(p.annotation).startswith('Literal[') else p.annotation,
                            kind=p.kind,
                            default=p.default,
                        )
                        for p in first.parameters
                    )
                )
                docstring = next((o.docstring for o in overloads if o.docstring), None)
                func = griffe.Function(
                    name,
                    lineno=first.lineno,
                    endlineno=first.endlineno,
                    parameters=params,
                    docstring=docstring,
                )
                func.returns = ' | '.join(dict.fromkeys(str(o.returns) for o in overloads))
                cls.set_member(name, func)
            # great-docs renders `obj.overloads` as overload signatures when it's non-empty. On a class
            # it's griffe's name -> overloads dict, so the class's own usage line came out empty.
            cls.overloads.clear()

            new = cls.members.get('__new__')
            if isinstance(new, griffe.Function) and '__init__' not in cls.members:
                init_params = list(new.parameters)
                if init_params and init_params[0].name == 'cls':
                    init_params[0] = griffe.Parameter('self', kind=init_params[0].kind)
                cls.set_member(
                    '__init__',
                    griffe.Function(
                        '__init__',
                        lineno=new.lineno,
                        endlineno=new.endlineno,
                        parameters=griffe.Parameters(*init_params),
                        returns='None',
                    ),
                )

    return StubFixes()


def main() -> int:
    # Ensure GriffeLoader runs in pure static mode for compiled PyO3 classes
    # so that .pyi stubs are parsed without cyclic alias errors between
    # compiled extension re-exports.
    try:
        import griffe
        from griffe import GriffeLoader
    except ModuleNotFoundError:
        env = Path(sys.prefix).name
        print(
            f'The docs tooling is not installed in {env}/. It lives in the `docs` dependency group, which is not a '
            f'default group, so install it with:\n\n    uv pip install --python {env}/bin/python --group docs\n',
            file=sys.stderr,
        )
        return 1

    _orig_init = GriffeLoader.__init__
    stub_fixes = _stub_extension()

    def _safe_init(self: GriffeLoader, *args: Any, **kwargs: Any) -> None:
        kwargs['allow_inspection'] = False
        extensions = kwargs.get('extensions')
        if not isinstance(extensions, griffe.Extensions):
            extensions = griffe.Extensions()
        extensions.add(stub_fixes)
        kwargs['extensions'] = extensions
        _orig_init(self, *args, **kwargs)

    GriffeLoader.__init__ = _safe_init
    _install_stub_twins()

    # The version badge and the Changelog page come from this checkout, not from GitHub Releases.
    from great_docs.core import GreatDocs

    if not callable(getattr(GreatDocs, '_fetch_github_releases', None)):
        print('great-docs no longer has GreatDocs._fetch_github_releases; update _local_releases', file=sys.stderr)
        return 1

    def _releases_from_checkout(self: GreatDocs, owner: str, repo: str, max_releases: int = 50) -> list[dict[str, Any]]:
        return _local_releases(f'https://github.com/{owner}/{repo}', max_releases)

    GreatDocs._fetch_github_releases = _releases_from_checkout  # type: ignore[method-assign]

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
    problems = _check_outputs(DEST)
    if problems:
        for problem in problems:
            print(f'build output check failed: {problem}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
