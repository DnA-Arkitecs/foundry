#!/usr/bin/env python3
"""foundry.py: the Foundry ADW runner. Seven stations, one SQLite ledger, fail-closed gates.

  foundry.py init <dish-slug> --hook "<5-second magic or bottleneck>" --root <dir>
  foundry.py advance <dish-slug> --root <dir>
  foundry.py status [<dish-slug>] --root <dir>
  foundry.py --demo
"""
import argparse
import contextlib
import json
import sys
import tempfile
from pathlib import Path

import foundry_core as core

DEMO_HOOK = "A one-command calculator that adds numbers and never crashes on bad input"
DEMO_SPEC = """# SPEC: adder

## User Journey
Run `python adder.py 2 3` and see 5 in under one second, no setup.

## Performance Floor
Answers in under 200 ms on a cold start.

## Anti-Slop Rules
Plain output, one line, no decoration, no dead options, no filler text.

## Privacy Boundaries
Reads only its own arguments; writes nothing to disk and calls no network.
"""
FLAWED = "import sys\n\n\ndef add(a, b):\n    return a - b\n\n\nif __name__ == '__main__':\n    print(add(int(sys.argv[1]), int(sys.argv[2])))\n"
TEST = ("import unittest\nfrom adder import add\n\n\nclass T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n\nif __name__ == '__main__':\n    unittest.main()\n")
SCENARIO = {"name": "blind_two_plus_three", "cmd": ["{python}", "adder.py", "2", "3"],
            "expect_rc": 0, "expect_stdout_contains": "5"}


def show(res):
    mark = "PASS" if res["passed"] else "FAIL"
    print("[{}] {} attempt {}".format(mark, res["station"], res["attempt"]))
    for g in res["gates"]:
        print("    {} {}: {}".format("ok  " if g["passed"] else "FAIL", g["gate"], g["detail"]))
    print("    -> dish at {} ({})".format(core.STATIONS[res["dish"]["stage_idx"]], res["dish"]["status"]))


def run_demo():
    """Synthetic end-to-end run. Returns 0 only if every expected behavior happened."""
    problems = []

    def expect(label, cond):
        print("  [{}] {}".format("ok" if cond else "WRONG", label))
        if not cond:
            problems.append(label)

    with tempfile.TemporaryDirectory(prefix="foundry-demo-") as tmp:
        root = Path(tmp)
        d = core.init_dish(root, "adder", DEMO_HOOK)
        ws, ho = Path(d["workspace"]), Path(d["holdout"])
        (ho / "scenario_001.json").write_text(json.dumps(SCENARIO), encoding="utf-8")
        print("Dish 'adder': workspace and a separate holdout directory created.")
        r = core.advance(root, "adder"); show(r)
        r = core.advance(root, "adder"); show(r)
        expect("station 2 refuses an unfilled spec", not r["passed"])
        (ws / "SPEC.md").write_text(DEMO_SPEC, encoding="utf-8")
        for name in ("run.cmd",):
            (ws / name).write_text("python adder.py %*\n", encoding="utf-8")
        for _ in range(2):
            r = core.advance(root, "adder"); show(r)
        (ws / "adder.py").write_text(FLAWED, encoding="utf-8")
        (ws / "test_adder.py").write_text(TEST, encoding="utf-8")
        r = core.advance(root, "adder"); show(r)
        r = core.advance(root, "adder"); show(r)
        expect("station 5 catches the simulated flaw and loops back to station 4",
               not r["passed"] and r["dish"]["stage_idx"] == 3)
        (ws / "adder.py").write_text(FLAWED.replace("a - b", "a + b"), encoding="utf-8")
        for _ in range(2):
            r = core.advance(root, "adder"); show(r)
        expect("station 5 passes after the fix", core.STATIONS[r["dish"]["stage_idx"]] == "6_blind_holdout_taste")
        r = core.advance(root, "adder"); show(r)
        expect("station 6 refuses until the taste review is written", not r["passed"])
        (ho / "taste_review.json").write_text(json.dumps({"verdict": "pass", "notes": "demo"}), encoding="utf-8")
        for _ in range(3):  # a taste failure also loops back: stations 4, 5, then 6 again
            r = core.advance(root, "adder"); show(r)
        (ws / "README.md").write_text("# adder\n\nA one-command calculator. Run `python adder.py 2 3`. Tested, small, no dependencies.\n", encoding="utf-8")
        (ws / "LICENSE").write_text("MIT License\n", encoding="utf-8")
        r = core.advance(root, "adder"); show(r)
        expect("dish is plated at station 7", r["dish"]["status"] == "plated")
        core.init_dish(root, "sneaky", "A second dish used to prove holdout tampering is caught at once")
        with contextlib.closing(core.connect(root)) as db:
            sneaky = core.get_dish(db, "sneaky")
        (Path(sneaky["holdout"]) / "scenario_001.json").write_text(json.dumps(SCENARIO), encoding="utf-8")
        for p in (Path(sneaky["workspace"]),):
            (p / "SPEC.md").write_text(DEMO_SPEC, encoding="utf-8")
            (p / "run.cmd").write_text("echo hi\n", encoding="utf-8")
            (p / "a.py").write_text("x = 1\n", encoding="utf-8")
            (p / "test_a.py").write_text("import unittest\n", encoding="utf-8")
        for _ in range(4):
            r = core.advance(root, "sneaky")
        (Path(sneaky["holdout"]) / "scenario_001.json").write_text('{"cmd": ["true"]}', encoding="utf-8")
        r = core.advance(root, "sneaky"); show(r)
        expect("holdout edit is caught and the dish is quarantined", r["tamper"] and r["dish"]["status"] == "quarantined")
    print("DEMO " + ("OK" if not problems else "FAILED: " + "; ".join(problems)))
    return 0 if not problems else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="foundry.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="run a synthetic 7-station demo in a temp dir")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("init"); p.add_argument("slug"); p.add_argument("--hook", required=True)
    p.add_argument("--root", default="."); p.add_argument("--test-cmd", help="JSON list, default: unittest discover")
    p = sub.add_parser("advance"); p.add_argument("slug"); p.add_argument("--root", default=".")
    p = sub.add_parser("status"); p.add_argument("slug", nargs="?"); p.add_argument("--root", default=".")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.demo:
        return run_demo()
    try:
        if args.cmd == "init":
            d = core.init_dish(args.root, args.slug, args.hook, json.loads(args.test_cmd) if args.test_cmd else None)
            print("created {}\n  workspace: {}\n  holdout:   {}".format(d["slug"], d["workspace"], d["holdout"]))
        elif args.cmd == "advance":
            res = core.advance(args.root, args.slug); show(res)
            return 0 if res["passed"] else 2
        elif args.cmd == "status":
            rows = core.status(args.root, args.slug)
            if args.json:
                print(json.dumps(rows, indent=2))
            for r in rows if not args.json else []:
                print("{:<20} {:<24} {:<14} {}".format(r["slug"], r["station"], r["status"], "; ".join(r["last_failures"])))
        else:
            ap.print_help()
            return 1
    except core.FoundryError as exc:
        print("foundry: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
