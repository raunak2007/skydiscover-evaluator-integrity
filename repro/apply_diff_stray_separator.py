#!/usr/bin/env python3
"""
Reproduce the SEARCH/REPLACE corruption seen in runlogs/control_15_b.log
(10:46:20) and runlogs/control_15_c.log (11:01:27), with no LLM call.

Both runs died with:

    File "/tmp/<hex>.py", line 126
        =======
        ^^
    SyntaxError: invalid syntax

The apply path is skydiscover/utils/code_utils.py:

    extract_diffs()  line 56:  r"<<<<<<< SEARCH\n(.*?)=======\n(.*?)>>>>>>> REPLACE"
    apply_diff()     line 13:  splices group 2 into the parent verbatim

Group 2 is lazy but has no "no more separators" guard, so it will happily
span a second `=======` line. Whatever sits between the first separator and
`>>>>>>> REPLACE` becomes replacement text -- conflict markers included.
Nothing downstream re-validates: the controller
(search/default_discovery_controller.py:793-825) only rejects when zero
blocks parsed or the result is byte-identical to the parent, and there is no
ast.parse/compile anywhere in the package. So the marker rides into
container_evaluator._inject_file() (line 262) and blows up at import.

Run:  python3 repro/apply_diff_stray_separator.py
Exits 0 when the bug reproduces.
"""

import ast
import importlib.util
import json
import os
import pathlib
import sys

SKYDISCOVER = pathlib.Path(
    os.environ.get("SKYDISCOVER_ROOT", pathlib.Path.home() / "Downloads" / "skydiscover")
)


def load_code_utils():
    """Import code_utils by path; the package __init__ drags in `openai`."""
    path = SKYDISCOVER / "skydiscover" / "utils" / "code_utils.py"
    if not path.exists():
        sys.exit(f"code_utils.py not found at {path}; set SKYDISCOVER_ROOT")
    spec = importlib.util.spec_from_file_location("_code_utils", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cu = load_code_utils()

PARENT = '''def similarity(a, b):
    GROUPS = [
        {"cat", "feline"},
        {"dog", "canine"},
    ]
    return 0.0
'''

SEARCH = '''        {"cat", "feline"},
        {"dog", "canine"},'''

REPLACE = '''        {"cat", "feline", "kitty"},
        {"dog", "canine", "puppy"},'''


def controller_guards(parent, response):
    """The only two checks _parse_llm_response() applies (lines 793-825)."""
    blocks = cu.extract_diffs(response)
    if not blocks:
        return None, "rejected: no valid diffs found"
    child = cu.apply_diff(parent, response)
    if child == parent:
        return None, "rejected: SEARCH blocks did not match parent"
    return child, "accepted"


def report(name, response):
    child, verdict = controller_guards(PARENT, response)
    print(f"--- {name}")
    print(f"    blocks parsed : {len(cu.extract_diffs(response))}")
    print(f"    controller    : {verdict}")
    if child is None:
        print("    written       : nothing\n")
        return None
    markers = [i + 1 for i, l in enumerate(child.split("\n")) if l.strip() == "======="]
    print(f"    marker lines  : {markers or 'none'}")
    try:
        ast.parse(child)
        print("    ast.parse     : ok\n")
    except SyntaxError as e:
        print(f"    ast.parse     : SyntaxError line {e.lineno}: {e.msg}  <-- corrupt\n")
    return child


print("=" * 68)
print("1. Minimal reproduction")
print("=" * 68)

# Well-formed: the baseline.
report(
    "well-formed block",
    f"<<<<<<< SEARCH\n{SEARCH}\n=======\n{REPLACE}\n>>>>>>> REPLACE\n",
)

# THE BUG: a second `=======` inside the replace body. Group 2 swallows it.
malformed = (
    f"<<<<<<< SEARCH\n{SEARCH}\n"
    f"=======\n{REPLACE}\n"
    f"=======\n{REPLACE}\n"           # stray separator, model repeated itself
    f">>>>>>> REPLACE\n"
)
bug = report("stray second '=======' in replace body", malformed)
assert bug is not None and "=======" in bug, "expected corruption, got none"

# Contrast: a truncated block (no terminator) parses to zero blocks and is
# rejected cleanly. Truncation is NOT the failure mode -- an extra separator is.
report(
    "truncated block (no '>>>>>>> REPLACE')",
    f"<<<<<<< SEARCH\n{SEARCH}\n=======\n{REPLACE}\n",
)

print("=" * 68)
print("2. Replay of the two recorded failures")
print("=" * 68)

RUNS = [
    ("control_15_b  10:46:20", "text_similarity_control_0830_1037",
     "8819917a-e701-4fe3-8ede-dfd168d6583a", 126),
    ("control_15_c  11:01:27", "text_similarity_control_0830_1058",
     "2b50b77d-8998-4bd9-8512-ae1756152c43", 124),
]

for label, run, pid, expected_line in RUNS:
    progs = SKYDISCOVER / "outputs/topk" / run / "checkpoints/checkpoint_15/programs"
    if not (progs / f"{pid}.json").exists():
        print(f"--- {label}: checkpoint not present, skipped\n")
        continue
    child = json.loads((progs / f"{pid}.json").read_text())
    markers = [i + 1 for i, l in enumerate(child["solution"].split("\n"))
               if l.strip() == "======="]
    print(f"--- {label}  program {pid[:8]}")
    print(f"    marker lines  : {markers}  (traceback said line {expected_line})")
    print(f"    metrics       : {child['metrics']}")
    print(f"    changes       : {child['metadata']['changes'].splitlines()[0]}")
    print(f"                    {child['metadata']['changes'].splitlines()[1]}")
    print("                    ^ two blocks, identical SEARCH text, replace")
    print("                      lengths differ by the swallowed separator\n")

# Byte-exact reconstruction of control_15_b from its recorded parent.
progs = SKYDISCOVER / ("outputs/topk/text_similarity_control_0830_1037"
                       "/checkpoints/checkpoint_15/programs")
if progs.exists():
    print("=" * 68)
    print("3. Byte-exact reconstruction of control_15_b")
    print("=" * 68)
    C = json.loads((progs / "8819917a-e701-4fe3-8ede-dfd168d6583a.json").read_text())
    P = json.loads((progs / f"{C['parent_id']}.json").read_text())["solution"]
    Pl, Cl = P.split("\n"), C["solution"].split("\n")

    anchor = "\n".join(Pl[103:107])    # 4-line SEARCH
    body = "\n".join(Cl[103:125])      # 22-line intended REPLACE
    tail_s = "\n".join(Pl[371:382])
    tail_r = "\n".join(Cl[412:424])

    response = (
        f"<<<<<<< SEARCH\n{anchor}\n=======\n{body}\n=======\n{body}\n>>>>>>> REPLACE\n\n"
        f"<<<<<<< SEARCH\n{anchor}\n=======\n{body}\n>>>>>>> REPLACE\n\n"
        f"<<<<<<< SEARCH\n{tail_s}\n=======\n{tail_r}\n>>>>>>> REPLACE\n"
    )
    out = cu.apply_diff(P, response)
    print(f"    reproduced == recorded solution : {out == C['solution']}")
    print(f"    summary matches recorded metadata: "
          f"{cu.format_diff_summary(cu.extract_diffs(response)).splitlines()[:2] == C['metadata']['changes'].splitlines()[:2]}")
    try:
        ast.parse(out)
        print("    ast.parse: ok (unexpected)")
    except SyntaxError as e:
        print(f"    ast.parse: SyntaxError line {e.lineno}: {e.msg}")
    assert out == C["solution"], "reconstruction diverged from recorded program"

print("\nReproduced.")
