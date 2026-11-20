"""Foundry core: init, advance and status. Code owns the kitchen, agents are the line cooks.

An agent may propose files, but only a gate can move a dish forward; every decision lands in foundry.db.
Stdlib only. This module re-exports the ledger and gate names so callers import one place.
"""
import contextlib
import json
import re
import sys
import time
from pathlib import Path

from foundry_db import *  # noqa: F401,F403
from foundry_db import (DB_SCHEMA, LOOP_BACK_TO, SPEC_TEMPLATE, STARTER_SCENARIO, STATIONS, FoundryError, connect,
                        default_banned_words, get_dish, seal_holdout)
from foundry_gates import *  # noqa: F401,F403
from foundry_gates import GATES

def init_dish(root, slug, hook, test_cmd=None):
    with contextlib.closing(connect(root)) as db:
        return _init_dish(db, root, slug, hook, test_cmd)


def _init_dish(db, root, slug, hook, test_cmd):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", slug):
        raise FoundryError("slug must be lowercase letters, digits and dashes")
    root = Path(root).resolve()
    if db.execute("SELECT 1 FROM dishes WHERE slug=?", (slug,)).fetchone():
        raise FoundryError("dish already exists: " + slug)
    workspace, holdout = root / "dishes" / slug, root / "holdouts" / slug
    workspace.mkdir(parents=True)
    holdout.mkdir(parents=True)
    (workspace / "SPEC.md").write_text(SPEC_TEMPLATE.format(slug=slug, hook=hook), encoding="utf-8")
    (holdout / "scenario_001.json").write_text(json.dumps(STARTER_SCENARIO, indent=2), encoding="utf-8")
    banned = root / "banned_words.txt"
    if not banned.exists():
        banned.write_text("\n".join(default_banned_words()) + "\n", encoding="utf-8")
    cmd = test_cmd or [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"]
    db.execute("INSERT INTO dishes(slug,hook,workspace,holdout,test_cmd,created) VALUES(?,?,?,?,?,?)",
               (slug, hook, str(workspace), str(holdout), json.dumps(cmd), time.time()))
    seal_holdout(db, slug, holdout)
    db.commit()
    return get_dish(db, slug)



def ledger_gaps(db, slug, idx):
    """Stations before idx whose most recent run did not pass: a skipped or failed prior gate."""
    gaps = []
    for s in STATIONS[:idx]:
        row = db.execute("SELECT outcome FROM station_runs WHERE slug=? AND station=? ORDER BY id DESC LIMIT 1",
                         (slug, s)).fetchone()
        if row is None or row["outcome"] != "pass":
            gaps.append(s)
    return gaps


def advance(root, slug):
    """Run the gate for the dish's current station and move it. Returns a result dict."""
    with contextlib.closing(connect(root)) as db:
        return _advance(db, Path(root).resolve(), slug)


def _advance(db, root, slug):
    d = get_dish(db, slug)
    if d["status"] in ("plated", "quarantined"):
        raise FoundryError("dish is {}: nothing to advance".format(d["status"]))
    idx = d["stage_idx"]
    ran = [STATIONS.index(r["station"]) for r in db.execute("SELECT DISTINCT station FROM station_runs WHERE slug=?", (slug,))]
    if ran and idx < min(max(ran), LOOP_BACK_TO):  # only the 5 and 6 loop-back may move a dish backward
        raise FoundryError("refused: dish stage was moved backward past a station that already ran")
    gaps = ledger_gaps(db, slug, idx)
    if gaps:
        raise FoundryError("refused: prior station gate not passed: " + ", ".join(gaps))
    station = STATIONS[idx]
    results = GATES[idx](db, d, root)
    passed = all(ok for _, ok, _ in results)
    attempt = db.execute("SELECT COUNT(*) FROM station_runs WHERE slug=? AND station=?", (slug, station)).fetchone()[0] + 1
    now = time.time()
    run_id = db.execute("INSERT INTO station_runs(slug,station,attempt,outcome,ts) VALUES(?,?,?,?,?)",
                        (slug, station, attempt, "pass" if passed else "fail", now)).lastrowid
    db.executemany("INSERT INTO gate_results(run_id,slug,station,gate,passed,detail,ts) VALUES(?,?,?,?,?,?,?)",
                   [(run_id, slug, station, n, int(ok), det, now) for n, ok, det in results])
    tamper = any(n == "holdout_intact" and not ok for n, ok, _ in results)
    if passed:
        done = idx == len(STATIONS) - 1
        db.execute("UPDATE dishes SET stage_idx=?, status=? WHERE slug=?",
                   (idx if done else idx + 1, "plated" if done else "cooking", slug))
    elif tamper:
        db.execute("UPDATE dishes SET status='quarantined' WHERE slug=?", (slug,))
    elif idx in (4, 5):
        db.execute("UPDATE dishes SET stage_idx=?, status='back_pressure' WHERE slug=?", (LOOP_BACK_TO, slug))
    db.commit()
    return {"slug": slug, "station": station, "attempt": attempt, "passed": passed, "tamper": tamper,
            "gates": [{"gate": n, "passed": ok, "detail": det} for n, ok, det in results],
            "dish": dict(get_dish(db, slug))}


def status(root, slug=None):
    with contextlib.closing(connect(root)) as db:
        return _status(db, slug)


def _status(db, slug):
    rows = [get_dish(db, slug)] if slug else db.execute("SELECT * FROM dishes ORDER BY created").fetchall()
    out = []
    for d in rows:
        last = db.execute("SELECT station, attempt, outcome FROM station_runs WHERE slug=? ORDER BY id DESC LIMIT 1",
                          (d["slug"],)).fetchone()
        fails = [r["gate"] + ": " + r["detail"] for r in db.execute(
            "SELECT gate, detail FROM gate_results WHERE run_id=(SELECT MAX(id) FROM station_runs WHERE slug=?)"
            " AND passed=0", (d["slug"],))]
        out.append({"slug": d["slug"], "hook": d["hook"], "station": STATIONS[d["stage_idx"]], "status": d["status"],
                    "last_run": dict(last) if last else None, "last_failures": fails})
    return out
