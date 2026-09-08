"""Apply P2.4, P2.6, P2.7, P3.2, P3.3 to contract_flow.py in one pass.

The file has accumulated mixed line endings (CR-only / LF / CRLF).
We rewrite it as LF + clean blank lines, then re-apply the
size pre-check from P2.1 and add the new fixes.
"""

import re

path = "src/lhgp/flow/contract_flow.py"
raw = open(path, "rb").read().decode("utf-8")

# Normalize to LF.
text = raw.replace("\r\n", "\n").replace("\r", "\n")

# Collapse runs of 3+ blank lines down to 2 (visual breathing room).
text = re.sub(r"\n{3,}", "\n\n", text)

# 1) Add _MAX_SOURCE_BYTES constant after _MAX_SEED_LEN.
if "_MAX_SOURCE_BYTES" not in text:
    text = text.replace(
        "_MAX_SEED_LEN = 256",
        "_MAX_SEED_LEN = 256\n\n"
        "# Mirror the cli.py pre-check so we never read a multi-GB file\n"
        "# into memory just to be rejected by walk_source's own 2 MiB cap.\n"
        "_MAX_SOURCE_BYTES = 2 * 1024 * 1024",
        1,
    )

# 2) Add logging import + logger.
if "import logging" not in text:
    text = text.replace(
        "import sqlite3",
        "import logging\nimport sqlite3",
        1,
    )
    # Add the logger right after the lhgp imports block.
    text = text.replace(
        "from lhgp.flow.ast_walker import Flow, FlowEdge, FlowNode, walk_source",
        "from lhgp.flow.ast_walker import Flow, FlowEdge, FlowNode, walk_source\n\n"
        "logger = logging.getLogger(__name__)",
        1,
    )

# 3) Add the size pre-check before read_text.
text = text.replace(
    '                source_text = file_path.read_text(encoding="utf-8")',
    "                # Pre-check size before read_text so a multi-GB file\n"
    "                # is refused without ever allocating the buffer.\n"
    "                if file_path.stat().st_size > _MAX_SOURCE_BYTES:\n"
    "                    continue\n"
    '                source_text = file_path.read_text(encoding="utf-8")',
    1,
)

# 4) Substring cap warning (P2.4). When we hit the cap, log a warning
#    naming the seed.
text = text.replace(
    "        if len(matches) >= _MAX_SUBSTRING_MATCHES:\n\n            return matches",
    "        if len(matches) >= _MAX_SUBSTRING_MATCHES:\n"
    "            logger.warning(\n"
    '                "contract_flow: seed %r hit substring cap of %d "\n'
    '                "matches; later matches not tried (lexical order, not "\n'
    '                "relevance-ranked). Use a more specific seed or a "\n'
    '                "module-path entry in acceptance.checks.",\n'
    "                seed,\n"
    "                _MAX_SUBSTRING_MATCHES,\n"
    "            )\n"
    "            return matches",
    1,
)

# 5) P2.7: import / from-import seed extraction. Add a regex pass
#    that converts `import X` / `from X import Y` seeds to a module
#    path so _resolve_by_module_path can try them.
text = text.replace(
    "def _coerce_seed(value: object) -> str | None:\n",
    "def _coerce_seed(value: object) -> str | None:\n",
    1,
)
# The actual extraction happens in _build_seeds (where the original
# string lives). Inject there.
text = text.replace(
    "def _build_seeds(view: ContractView) -> list[str]:",
    "def _coerce_seed(value: object) -> str | None:\n"
    '    """Normalise a seed to a non-empty string, or None to skip."""\n'
    "    if isinstance(value, str):\n"
    "        s = value.strip()\n"
    "        if not s or len(s) > _MAX_SEED_LEN:\n"
    "            return None\n"
    "        return s\n"
    "    # CheckSpec-like: expose a `target` attribute.\n"
    '    target = getattr(value, "target", None)\n'
    "    if isinstance(target, str):\n"
    "        s = target.strip()\n"
    "        if s and len(s) <= _MAX_SEED_LEN:\n"
    "            return s\n"
    "    return None\n\n\n"
    "def _import_statement_to_module_path(seed: str) -> str | None:\n"
    '    """If ``seed`` is a Python import statement, return the\n'
    "    module path it references. Otherwise None.\n"
    "    - ``import lhgp.flow.contract_flow`` -> ``lhgp.flow.contract_flow``\n"
    "    - ``from lhgp.flow.contract_flow import walk_contract`` -> same\n"
    "    - ``from . import sibling`` (relative) -> None (ambiguous)\n"
    '    """\n'
    "    s = seed.strip()\n"
    '    if s.startswith("import "):\n'
    '        rest = s[len("import "):].split(" as ", 1)[0].split(" as ", 1)[0]\n'
    '        rest = rest.split(",", 1)[0].strip()\n'
    "        return rest or None\n"
    '    if s.startswith("from "):\n'
    "        # ``from <module> import <names>``\n"
    "        try:\n"
    '            _, after = s.split("from ", 1)\n'
    "        except ValueError:\n"
    "            return None\n"
    '        module = after.split(" import ", 1)[0].strip()\n'
    '        if module.startswith("."):\n'
    "            return None\n"
    "        return module or None\n"
    "    return None\n\n\n"
    "def _build_seeds(view: ContractView) -> list[str]:",
    1,
)

# Inside _build_seeds, after we collect the raw seeds, expand any
# that look like import statements. The original function ends with
# ``return seeds``; insert the expansion just before that return.
text = text.replace(
    "    if not seeds:\n        return seeds\n    return seeds",
    "    expanded: list[str] = []\n"
    "    for s in seeds:\n"
    "        expanded.append(s)\n"
    "        as_module = _import_statement_to_module_path(s)\n"
    "        if as_module is not None and as_module != s:\n"
    "            expanded.append(as_module)\n"
    "    return expanded",
    1,
)

# P3.3 was about TERMINAL_STATES in wiki/sync.py; that's a separate
# file. Skip here.

# P3.2 (_empty_flow heuristic) lives in flow/cli.py; skip here.

open(path, "w", encoding="utf-8", newline="\n").write(text)
print("rewrote", path, "len", len(text))
