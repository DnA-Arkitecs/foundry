"""Foundry gates: the hard checks each station runs before a dish may move on."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from foundry_db import (GENERIC_HOOKS, MAX_LINES, RUNNER_NAMES, SECRET_PATTERNS, SPEC_SECTIONS, STARTER_SCENARIO,
                        default_banned_words, holdout_files, seal_holdout, sha256_file)

SCAN_SKIP = {".git", "__pycache__", ".pytest_cache"}  # everything else is scanned, node_modules and .venv included
SOURCE_SUFFIXES = (".py", ".js", ".mjs", ".ts")
VENDOR_DIRS = {"node_modules", ".venv", "venv", "dist", "build"}  # scanned for words and keys, never judged as source
MAX_SCAN_BYTES = 20_000_000
SAFE_ENV = ["PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "TEMP", "TMP", "LANG"]  # no keys reach the builder


def text_files(workspace):
    for p in sorted(Path(workspace).rglob("*")):
        if p.is_file() and not (set(p.relative_to(workspace).parts) & SCAN_SKIP):
            yield p


def read_text(p):
    """Best-effort decode so a key hidden as UTF-16 or in a stray byte still gets scanned."""
    try:
        b = Path(p).read_bytes()
    except OSError:
        return ""
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16", errors="ignore")
    if len(b) >= 4 and b.count(0) * 3 >= len(b):  # UTF-16 with no byte-order mark: NULs fill one side of each pair
        return b.decode("utf-16-le" if b[1::2].count(0) >= b[0::2].count(0) else "utf-16-be", errors="ignore")
    return b.decode("utf-8", errors="ignore")


def word_pattern(word):
    """A case-insensitive match for `word` standing alone: no letter, digit or underscore on either side, so a
    banned word inside a longer English word never trips the gate."""
    word = word.strip()
    start = r"\b" if re.match(r"\w", word[:1]) else r"(?<!\w)"
    end = r"\b" if re.match(r"\w", word[-1:]) else r"(?!\w)"
    return re.compile(start + re.escape(word) + end, re.IGNORECASE)


def unscannable(workspace):
    return [p.name for p in text_files(workspace) if p.stat().st_size > MAX_SCAN_BYTES]


def scan_banned(workspace, words):
    hits = []
    patterns = [(w, word_pattern(w)) for w in words if w and w.strip()]
    for p in text_files(workspace):
        text = read_text(p)
        hits += ["{}: '{}'".format(p.name, w) for w, rx in patterns if rx.search(text)]
    return hits


def source_files(workspace):
    """Hand-written source (Python, JavaScript, TypeScript); vendored and built trees are not the dish's code."""
    ws = Path(workspace)
    return [p for p in text_files(ws) if p.suffix in SOURCE_SUFFIXES and not p.name.endswith(".min.js")
            and not (set(p.relative_to(ws).parts) & VENDOR_DIRS)]


def is_test_file(p):
    """`test_*.py` / `test_*.js`, or the JavaScript conventions `*.test.ts` and `*.spec.mjs`."""
    stem = p.name[:-len(p.suffix)]
    return p.name.startswith("test_") or stem.endswith((".test", ".spec"))


def scan_secrets(workspace):
    hits = []
    for p in text_files(workspace):
        text = read_text(p)
        hits += ["{}: {}".format(p.name, label) for label, rx in SECRET_PATTERNS if re.search(rx, text)]
    return hits


def run_cmd(cmd, cwd, timeout=60):
    cmd = [sys.executable if c == "{python}" else c for c in cmd]
    env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV}
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # no stale .pyc between a fix and its re-run
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 124, "", str(exc)


def banned_list(root):
    f = Path(root) / "banned_words.txt"
    lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else default_banned_words()
    return [w.strip() for w in lines if w.strip()]


def gate(name, ok, detail=""):
    return (name, bool(ok), detail)


def holdout_intact(db, d):
    sealed = {r["path"]: r["sha256"] for r in
              db.execute("SELECT path, sha256 FROM artifacts WHERE slug=? AND kind='holdout'", (d["slug"],))}
    now = {p.name: sha256_file(p) for p in holdout_files(d["holdout"])}
    bad = ["modified " + n for n in sealed if n in now and now[n] != sealed[n]]
    bad += ["missing " + n for n in sealed if n not in now] + ["added " + n for n in now if n not in sealed]
    return gate("holdout_intact", not bad, "; ".join(bad) or "{} sealed files match".format(len(sealed)))


def load_scenarios(holdout):
    """Return (scenarios, problems). A malformed scenario is a problem, never a crash."""
    found, problems = [], []
    for p in holdout_files(holdout):
        try:
            sc = json.loads(p.read_text(encoding="utf-8"))
            ok = isinstance(sc, dict) and isinstance(sc.get("cmd"), list) and sc["cmd"] and all(
                isinstance(c, str) for c in sc["cmd"])
        except (ValueError, OSError):
            ok = False
        if ok:
            found.append((p.name, sc))
        else:
            problems.append(p.name)
    return found, problems


def g_menu(db, d, root):
    hook, words = d["hook"].strip(), len(d["hook"].split())
    slop = [g for g in GENERIC_HOOKS if g in hook.lower()]
    return [gate("hook_has_substance", words >= 4 and 15 <= len(hook) <= 280, "{} words".format(words)),
            gate("not_generic_slop", not slop, ", ".join(slop) or "ok")]


def g_spec(db, d, root):
    spec = Path(d["workspace"]) / "SPEC.md"
    text = spec.read_text(encoding="utf-8", errors="ignore") if spec.exists() else ""
    out = [gate("spec_exists", spec.exists())]
    for s in SPEC_SECTIONS:
        m = re.search(r"##\s*" + re.escape(s) + r"\s*\n(.*?)(?=\n##\s|\Z)", text, re.S)
        body = m.group(1).strip() if m else ""
        out.append(gate("spec_section_" + s.lower().replace(" ", "_"),
                        len(body) >= 20 and "tbd" not in body.lower(), "{} chars".format(len(body))))
    return out


def g_mise(db, d, root):
    ws, ho = Path(d["workspace"]).resolve(), Path(d["holdout"]).resolve()
    outside = ho != ws and ws not in ho.parents and ho not in ws.parents
    runner = [n for n in RUNNER_NAMES if (ws / n).exists()] + [p.name for p in ws.glob("Start-*.ps1")]
    scen, problems = load_scenarios(ho)
    placeholder = [n for n, sc in scen if sc == STARTER_SCENARIO]
    ok = bool(scen) and not problems and not placeholder and outside
    if ok:
        seal_holdout(db, d["slug"], ho)  # the authoring window closes here
    return [gate("holdout_outside_sandbox", outside, str(ho.name)), gate("runner_present", runner, ",".join(runner)),
            gate("scenarios_valid", scen and not problems, "invalid: " + ", ".join(problems) if problems else
                 "{} scenarios".format(len(scen))),
            gate("scenarios_not_placeholder", not placeholder, ", ".join(placeholder) or "ok"),
            gate("holdout_sealed", ok, "{} scenarios sealed".format(len(scen)) if ok else "not sealed")]


def g_cooks(db, d, root):
    ws = Path(d["workspace"])
    src = source_files(ws)
    tests = [p for p in src if is_test_file(p)]
    long_ = ["{} ({} lines)".format(p.name, n) for p in src if (n := len(p.read_bytes().splitlines())) > MAX_LINES]
    return [gate("has_source", len(src) > len(tests), "{} source files".format(len(src))),
            gate("has_tests", tests, "{} test files".format(len(tests))),
            gate("modules_stay_liquid", not long_, ", ".join(long_) or "all <= {} lines".format(MAX_LINES)),
            holdout_intact(db, d)]


def peeked(ws, d):
    """Files that mention the holdout location, however the path is written (slashes and case ignored)."""
    needles = {str(Path(d["holdout"]).resolve()).replace("\\", "/").lower(), "holdouts/" + d["slug"]}
    return [p.name for p in text_files(ws) if any(n in read_text(p).replace("\\", "/").lower() for n in needles)]


def g_validators(db, d, root):
    ws = Path(d["workspace"])
    rc, out, err = run_cmd(json.loads(d["test_cmd"]), ws)
    ran_none = "Ran 0 tests" in err or "NO TESTS RAN" in err
    banned, secrets, big, peek = scan_banned(ws, banned_list(root)), scan_secrets(ws), unscannable(ws), peeked(ws, d)
    tail = (err or out).strip().splitlines()[-1:] or [""]
    return [gate("tests_pass", rc == 0 and not ran_none, "rc={} {}".format(rc, tail[0])),
            gate("no_banned_words", not banned, "; ".join(banned) or "clean"),
            gate("no_secret_patterns", not secrets, "; ".join(secrets) or "clean"),
            gate("all_files_scannable", not big, ", ".join(big) or "ok"),
            gate("builder_never_read_holdout", not peek, ", ".join(peek) or "no holdout path in workspace"),
            holdout_intact(db, d)]


def blind_gates(d):
    found, problems = load_scenarios(d["holdout"])
    failed = list(problems)
    for name, sc in found:
        rc, stdout, _ = run_cmd(sc["cmd"], d["workspace"])
        if rc != sc.get("expect_rc", 0) or str(sc.get("expect_stdout_contains", "")) not in stdout:
            failed.append(sc.get("name", name))
    review = Path(d["holdout"]) / "taste_review.json"
    verdict = "missing"
    if review.exists():
        try:
            data = json.loads(review.read_text(encoding="utf-8"))
            verdict = data.get("verdict") if isinstance(data, dict) else "invalid"
        except (ValueError, OSError):
            verdict = "invalid"
    return [gate("blind_scenarios_pass", found and not failed,
                 "failed: " + ", ".join(failed) if failed else "{} passed".format(len(found))),
            gate("taste_review_pass", verdict == "pass", "verdict={}".format(verdict))]


def g_taste(db, d, root):
    intact = holdout_intact(db, d)
    if not intact[1]:  # a tampered scenario is never executed
        return [gate("blind_scenarios_pass", False, "skipped: holdout tampered"),
                gate("taste_review_pass", False, "skipped: holdout tampered"), intact]
    return blind_gates(d) + [intact]


def g_plating(db, d, root):
    ws = Path(d["workspace"])
    readme = ws / "README.md"
    out = [gate("readme_present", readme.exists() and len(read_text(readme)) >= 80),
           gate("license_present", (ws / "LICENSE").exists())]
    out += g_validators(db, d, root)
    return out + [g for g in g_taste(db, d, root) if g[0] != "holdout_intact"]


GATES = [g_menu, g_spec, g_mise, g_cooks, g_validators, g_taste, g_plating]
