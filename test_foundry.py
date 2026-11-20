"""Tests for Foundry. Run: python -m unittest -v   (or pytest). Stdlib only."""
import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import foundry
import foundry_core as core

HERE = Path(__file__).resolve().parent
BANNED = "".join(map(chr, (102, 114, 101, 101)))  # the word the validator must catch, never written literally
SPEC = ("## User Journey\nRun one command and see the answer at once.\n\n"
        "## Performance Floor\nCold start under two hundred milliseconds.\n\n"
        "## Anti-Slop Rules\nPlain output, no decoration, no dead options.\n\n"
        "## Privacy Boundaries\nReads only its arguments, no network, no disk writes.\n")
GOOD_PY = "def add(a, b):\n    return a + b\n\n\nif __name__ == '__main__':\n    import sys\n    print(add(int(sys.argv[1]), int(sys.argv[2])))\n"
GOOD_TEST = "import unittest\nfrom adder import add\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
SCENARIO = {"name": "blind", "cmd": ["{python}", "adder.py", "2", "3"], "expect_rc": 0, "expect_stdout_contains": "5"}
HOOK = "A one-command adder that never crashes on bad input"


class Kitchen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="foundry-test-")
        self.root = Path(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def dish(self, slug="adder", hook=HOOK):
        d = core.init_dish(self.root, slug, hook)
        return Path(d["workspace"]), Path(d["holdout"])

    def adv(self, slug="adder"):
        return core.advance(self.root, slug)

    def cook_to(self, station_index, slug="adder", py=GOOD_PY):
        """Fill every artifact and advance until the dish waits at station_index (0-based)."""
        ws, ho = self.dish(slug)
        (ho / "scenario_001.json").write_text(json.dumps(SCENARIO), encoding="utf-8")
        (ws / "SPEC.md").write_text(SPEC, encoding="utf-8")
        (ws / "run.cmd").write_text("python adder.py %*\n", encoding="utf-8")
        (ws / "adder.py").write_text(py, encoding="utf-8")
        (ws / "test_adder.py").write_text(GOOD_TEST, encoding="utf-8")
        (ws / "README.md").write_text("# adder\n\n" + "Adds two numbers from the command line. " * 3, encoding="utf-8")
        (ws / "LICENSE").write_text("MIT License\n", encoding="utf-8")
        (ho / "taste_review.json").write_text(json.dumps({"verdict": "pass"}), encoding="utf-8")
        for _ in range(station_index):
            self.assertTrue(self.adv(slug)["passed"])
        return ws, ho


class TestInit(Kitchen):
    def test_creates_workspace_and_holdout(self):
        ws, ho = self.dish()
        self.assertTrue(ws.is_dir() and ho.is_dir() and (ws / "SPEC.md").exists())

    def test_holdout_is_outside_workspace(self):
        ws, ho = self.dish()
        self.assertNotIn(ws.resolve(), ho.resolve().parents)
        self.assertNotIn(ho.resolve(), ws.resolve().parents)

    def test_ledger_has_four_tables(self):
        self.dish()
        with contextlib.closing(sqlite3.connect(str(self.root / "foundry.db"))) as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"dishes", "station_runs", "gate_results", "artifacts"} <= names)

    def test_init_records_holdout_sha256(self):
        ws, ho = self.dish()
        with contextlib.closing(sqlite3.connect(str(self.root / "foundry.db"))) as db:
            row = db.execute("SELECT path, sha256 FROM artifacts WHERE kind='holdout'").fetchone()
        self.assertEqual(row[0], "scenario_001.json")
        self.assertEqual(row[1], core.sha256_file(ho / "scenario_001.json"))
        self.assertEqual(len(row[1]), 64)

    def test_duplicate_slug_refused(self):
        self.dish()
        with self.assertRaises(core.FoundryError):
            self.dish()

    def test_bad_slug_refused(self):
        with self.assertRaises(core.FoundryError):
            core.init_dish(self.root, "Bad Slug!", HOOK)

    def test_unknown_dish_refused(self):
        core.connect(self.root).close()
        with self.assertRaises(core.FoundryError):
            self.adv("nope")


class TestStationOrder(Kitchen):
    def test_seven_stations_in_the_documented_order(self):
        self.assertEqual(core.STATIONS, ["1_menu_gate", "2_recipe_spec", "3_mise_en_place", "4_line_cooks",
                                         "5_hard_validators", "6_blind_holdout_taste", "7_plating"])

    def test_dish_starts_at_station_one(self):
        self.dish()
        self.assertEqual(core.status(self.root, "adder")[0]["station"], "1_menu_gate")

    def test_one_advance_moves_exactly_one_station(self):
        self.cook_to(1)
        self.assertEqual(core.status(self.root, "adder")[0]["station"], "2_recipe_spec")

    def test_full_run_plates_the_dish(self):
        self.cook_to(7)
        self.assertEqual(core.status(self.root, "adder")[0]["status"], "plated")

    def test_plated_dish_cannot_advance(self):
        self.cook_to(7)
        with self.assertRaises(core.FoundryError):
            self.adv()

    def test_skipping_a_failed_prior_gate_is_refused(self):
        self.cook_to(2)
        with contextlib.closing(sqlite3.connect(str(self.root / "foundry.db"))) as db:
            db.execute("UPDATE station_runs SET outcome='fail' WHERE station='2_recipe_spec'")
            db.commit()
        with self.assertRaises(core.FoundryError) as ctx:
            self.adv()
        self.assertIn("2_recipe_spec", str(ctx.exception))

    def test_editing_stage_idx_to_skip_ahead_is_refused(self):
        self.dish()
        with contextlib.closing(sqlite3.connect(str(self.root / "foundry.db"))) as db:
            db.execute("UPDATE dishes SET stage_idx=5")
            db.commit()
        with self.assertRaises(core.FoundryError):
            self.adv()

    def test_every_run_is_logged_with_gate_rows(self):
        self.cook_to(3)
        with contextlib.closing(sqlite3.connect(str(self.root / "foundry.db"))) as db:
            runs = db.execute("SELECT COUNT(*) FROM station_runs").fetchone()[0]
            gates = db.execute("SELECT COUNT(*) FROM gate_results").fetchone()[0]
        self.assertEqual(runs, 3)
        self.assertGreater(gates, runs)


class TestFailClosed(Kitchen):
    def test_generic_chatbot_hook_dies_at_station_one(self):
        self.dish(hook="Yet another chatbot for everything you could ever want")
        r = self.adv()
        self.assertFalse(r["passed"])
        self.assertEqual(core.status(self.root, "adder")[0]["station"], "1_menu_gate")

    def test_too_short_hook_fails(self):
        self.dish(hook="cool app")
        self.assertFalse(self.adv()["passed"])

    def test_unfilled_spec_fails_station_two(self):
        self.dish()
        self.adv()
        self.assertFalse(self.adv()["passed"])

    def test_spec_missing_a_section_fails(self):
        ws, _ = self.cook_to(1)
        (ws / "SPEC.md").write_text(SPEC.replace("## Privacy Boundaries", "## Notes"), encoding="utf-8")
        r = self.adv()
        self.assertFalse(r["passed"])
        self.assertIn("spec_section_privacy_boundaries", [g["gate"] for g in r["gates"] if not g["passed"]])

    def test_no_runner_fails_station_three(self):
        ws, _ = self.cook_to(2)
        (ws / "run.cmd").unlink()
        self.assertFalse(self.adv()["passed"])

    def test_oversize_module_fails_station_four(self):
        ws, _ = self.cook_to(3)
        (ws / "big.py").write_text("x = 1\n" * (core.MAX_LINES + 5), encoding="utf-8")
        r = self.adv()
        self.assertIn("modules_stay_liquid", [g["gate"] for g in r["gates"] if not g["passed"]])

    def test_failing_tests_at_station_five_loop_back_to_four(self):
        self.cook_to(4, py=GOOD_PY.replace("a + b", "a - b"))
        r = self.adv()
        self.assertFalse(r["passed"])
        self.assertEqual(r["dish"]["stage_idx"], 3)
        self.assertEqual(r["dish"]["status"], "back_pressure")

    def test_fix_after_loop_back_passes_station_five(self):
        ws, _ = self.cook_to(4, py=GOOD_PY.replace("a + b", "a - b"))
        self.adv()
        (ws / "adder.py").write_text(GOOD_PY, encoding="utf-8")
        self.assertTrue(self.adv()["passed"])
        self.assertTrue(self.adv()["passed"])
        self.assertEqual(core.status(self.root, "adder")[0]["station"], "6_blind_holdout_taste")

    def test_missing_taste_review_fails_station_six_and_loops_back(self):
        _, ho = self.cook_to(5)
        (ho / "taste_review.json").unlink()
        r = self.adv()
        self.assertFalse(r["passed"])
        self.assertEqual(r["dish"]["stage_idx"], 3)

    def test_send_back_verdict_fails_station_six(self):
        _, ho = self.cook_to(5)
        (ho / "taste_review.json").write_text(json.dumps({"verdict": "send_back"}), encoding="utf-8")
        self.assertFalse(self.adv()["passed"])

    def test_blind_scenario_failure_fails_station_six(self):
        ws, _ = self.cook_to(5)
        (ws / "adder.py").write_text("print('wrong')\n", encoding="utf-8")
        r = self.adv()
        self.assertIn("blind_scenarios_pass", [g["gate"] for g in r["gates"] if not g["passed"]])

    def test_missing_license_fails_plating(self):
        ws, _ = self.cook_to(6)
        (ws / "LICENSE").unlink()
        self.assertFalse(self.adv()["passed"])


class TestHoldoutTamper(Kitchen):
    def tampered(self, mutate):
        ws, ho = self.cook_to(3)
        mutate(ho)
        return self.adv()

    def test_modified_holdout_is_caught_by_sha256(self):
        r = self.tampered(lambda ho: (ho / "scenario_001.json").write_text('{"cmd": ["x"]}', encoding="utf-8"))
        self.assertTrue(r["tamper"])
        self.assertIn("modified scenario_001.json", [g["detail"] for g in r["gates"] if g["gate"] == "holdout_intact"][0])

    def test_tamper_quarantines_the_dish_for_good(self):
        self.tampered(lambda ho: (ho / "scenario_001.json").write_text("{}", encoding="utf-8"))
        self.assertEqual(core.status(self.root, "adder")[0]["status"], "quarantined")
        with self.assertRaises(core.FoundryError):
            self.adv()

    def test_deleted_holdout_is_caught(self):
        r = self.tampered(lambda ho: (ho / "scenario_001.json").unlink())
        self.assertTrue(r["tamper"])

    def test_added_holdout_is_caught(self):
        r = self.tampered(lambda ho: (ho / "scenario_002.json").write_text("{}", encoding="utf-8"))
        self.assertTrue(r["tamper"])

    def test_restoring_the_bytes_does_not_hide_a_logged_quarantine(self):
        ws, ho = self.cook_to(3)
        original = (ho / "scenario_001.json").read_bytes()
        (ho / "scenario_001.json").write_bytes(b"{}")
        self.adv()
        (ho / "scenario_001.json").write_bytes(original)
        with self.assertRaises(core.FoundryError):
            self.adv()

    def test_taste_review_edits_do_not_count_as_tamper(self):
        _, ho = self.cook_to(5)
        (ho / "taste_review.json").write_text(json.dumps({"verdict": "pass", "notes": "edited"}), encoding="utf-8")
        self.assertFalse(self.adv()["tamper"])

    def test_builder_peeking_at_the_holdout_path_fails_station_five(self):
        ws, ho = self.cook_to(4)
        (ws / "notes.txt").write_text("answers live in " + str(ho), encoding="utf-8")
        r = self.adv()
        self.assertIn("builder_never_read_holdout", [g["gate"] for g in r["gates"] if not g["passed"]])


class TestValidators(Kitchen):
    def failed_gates(self, filename, text):
        ws, _ = self.cook_to(4)
        (ws / filename).write_text(text, encoding="utf-8")
        return [g["gate"] for g in self.adv()["gates"] if not g["passed"]]

    def test_banned_word_in_code_is_caught(self):
        self.assertIn("no_banned_words", self.failed_gates("notes.md", "this is " + BANNED + " of charge"))

    def test_banned_word_inside_longer_words_passes(self):
        text = "a " + BANNED + "lance plan, " + BANNED + "dom of choice, a care" + BANNED + " day"
        self.assertNotIn("no_banned_words", self.failed_gates("notes.md", text))

    def test_custom_banned_word_list_is_used(self):
        ws, _ = self.cook_to(4)
        (self.root / "banned_words.txt").write_text("zebra\n", encoding="utf-8")
        (ws / "notes.md").write_text("a zebra appears", encoding="utf-8")
        self.assertIn("no_banned_words", [g["gate"] for g in self.adv()["gates"] if not g["passed"]])

    def test_anthropic_key_shape_is_caught(self):
        self.assertIn("no_secret_patterns", self.failed_gates("cfg.txt", "key = sk-" + "ant-api03-abcdefgh12345678"))

    def test_private_key_header_is_caught(self):
        self.assertIn("no_secret_patterns", self.failed_gates("k.txt", "-----BEGIN RSA PRIVATE KEY-----"))

    def test_local_user_path_is_caught(self):
        self.assertIn("no_secret_patterns", self.failed_gates("p.txt", "C:\\Users\\someone\\project"))

    def test_clean_workspace_passes_every_validator(self):
        ws, _ = self.cook_to(4)
        r = self.adv()
        self.assertTrue(r["passed"], r["gates"])

    def test_scan_helpers_report_the_offending_file(self):
        ws, _ = self.dish("scan")
        (ws / "a.txt").write_text("clean", encoding="utf-8")
        (ws / "b.txt").write_text("sk-" + "ant-api03-abcdefgh12345678", encoding="utf-8")
        self.assertEqual(core.scan_secrets(ws), ["b.txt: anthropic key"])
        self.assertEqual(core.scan_banned(ws, core.default_banned_words()), [])


class TestWordBounds(unittest.TestCase):
    """The banned-word scan matches whole words only, in UTF-8 and UTF-16, any case, beside any punctuation."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="foundry-words-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def hits(self, data, name="f.txt"):
        p = self.tmp / name
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return core.scan_banned(self.tmp, core.default_banned_words())

    def test_standalone_word_is_caught(self):
        self.assertEqual(self.hits("it is " + BANNED + " today"), ["f.txt: '{}'".format(BANNED)])

    def test_case_and_punctuation_do_not_hide_it(self):
        for text in (BANNED.upper() + "!", "(" + BANNED.title() + ")", BANNED + "-tier", '"' + BANNED + '"', BANNED):
            self.assertTrue(self.hits(text), text)

    def test_longer_english_words_never_trip(self):
        for text in (BANNED + "dom", "care" + BANNED, BANNED + "lance", "hands-" + BANNED + "ly",
                     "duty" + BANNED, BANNED + "way"):
            self.assertEqual(self.hits(text), [], text)

    def test_utf16_with_byte_order_mark_is_caught(self):
        self.assertTrue(self.hits(("it is " + BANNED).encode("utf-16")))

    def test_utf16_le_without_byte_order_mark_is_caught(self):
        self.assertTrue(self.hits(("it is " + BANNED + " now").encode("utf-16-le")))

    def test_utf16_be_without_byte_order_mark_is_caught(self):
        self.assertTrue(self.hits(("it is " + BANNED + " now").encode("utf-16-be")))

    def test_utf16_longer_word_still_passes(self):
        self.assertEqual(self.hits(("a care" + BANNED + " day").encode("utf-16")), [])

    def test_multi_word_phrases_stay_bounded(self):
        self.assertTrue(self.hits("Lorem Ipsum dolor"))
        self.assertTrue(self.hits("# TODO: implement this"))
        self.assertEqual(self.hits("xlorem ipsumx"), [])

    def test_word_pattern_handles_punctuation_edges(self):
        rx = core.word_pattern("todo: implement")
        self.assertTrue(rx.search("TODO: IMPLEMENT later"))
        self.assertIsNone(rx.search("mytodo: implementation"))


GOOD_MJS = "export function add(a, b) {\n  return a + b;\n}\n"
GOOD_MJS_TEST = ("import test from 'node:test';\nimport assert from 'node:assert';\nimport { add } from './adder.mjs';\n\n"
                 "test('adds', () => assert.strictEqual(add(2, 3), 5));\n")
CLI_MJS = "import { add } from './adder.mjs';\nconsole.log(add(Number(process.argv[2]), Number(process.argv[3])));\n"
JS_SCENARIO = {"name": "blind-js", "cmd": ["node", "cli.mjs", "2", "3"], "expect_rc": 0, "expect_stdout_contains": "5"}


class TestJsDishes(Kitchen):
    """Station 4 judges JavaScript and TypeScript dishes the way it judges Python ones."""

    def js_dish(self, files, slug="jsadder"):
        d = core.init_dish(self.root, slug, HOOK, ["node", "--test"])
        ws, ho = Path(d["workspace"]), Path(d["holdout"])
        (ho / "scenario_001.json").write_text(json.dumps(JS_SCENARIO), encoding="utf-8")
        (ho / "taste_review.json").write_text(json.dumps({"verdict": "pass"}), encoding="utf-8")
        (ws / "SPEC.md").write_text(SPEC, encoding="utf-8")
        (ws / "run.cmd").write_text("node cli.mjs %*\n", encoding="utf-8")
        (ws / "README.md").write_text("# jsadder\n\n" + "Adds two numbers from the command line. " * 3, encoding="utf-8")
        (ws / "LICENSE").write_text("MIT License\n", encoding="utf-8")
        for name, text in files.items():
            (ws / name).parent.mkdir(parents=True, exist_ok=True)
            (ws / name).write_text(text, encoding="utf-8")
        for _ in range(3):
            self.assertTrue(self.adv(slug)["passed"])
        return ws

    def station_four(self, files):
        self.js_dish(files)
        r = self.adv("jsadder")
        self.assertEqual(r["station"], "4_line_cooks")
        return r, {g["gate"]: g for g in r["gates"]}

    def test_mjs_dish_passes_station_four(self):
        r, g = self.station_four({"adder.mjs": GOOD_MJS, "cli.mjs": CLI_MJS, "adder.test.mjs": GOOD_MJS_TEST})
        self.assertTrue(r["passed"], r["gates"])
        self.assertEqual(g["has_source"]["detail"], "3 source files")

    def test_ts_and_js_sources_count(self):
        files = {"src/add.ts": "export const add = (a: number, b: number) => a + b;\n",
                 "src/add.spec.ts": "import { add } from './add';\n",
                 "lib/util.js": "module.exports = {};\n"}
        r, g = self.station_four(files)
        self.assertTrue(g["has_source"]["passed"] and g["has_tests"]["passed"], r["gates"])

    def test_js_dish_with_only_tests_has_no_source(self):
        r, g = self.station_four({"adder.test.mjs": GOOD_MJS_TEST})
        self.assertFalse(g["has_source"]["passed"])

    def test_js_dish_without_tests_fails(self):
        r, g = self.station_four({"adder.mjs": GOOD_MJS})
        self.assertFalse(g["has_tests"]["passed"])

    def test_long_js_module_breaks_the_line_limit(self):
        files = {"adder.mjs": "export const x = 1;\n" * (core.MAX_LINES + 1), "adder.test.mjs": GOOD_MJS_TEST}
        r, g = self.station_four(files)
        self.assertFalse(g["modules_stay_liquid"]["passed"])
        self.assertIn("adder.mjs", g["modules_stay_liquid"]["detail"])

    def test_vendored_and_minified_code_is_not_judged_as_source(self):
        big = "var v = 0;\n" * (core.MAX_LINES + 50)
        files = {"adder.mjs": GOOD_MJS, "cli.mjs": CLI_MJS, "adder.test.mjs": GOOD_MJS_TEST,
                 "node_modules/dep/index.js": big, "dist/bundle.js": big, "vendor.min.js": big}
        r, g = self.station_four(files)
        self.assertTrue(r["passed"], r["gates"])

    def test_mjs_dish_plates_end_to_end_with_node_test(self):
        self.js_dish({"adder.mjs": GOOD_MJS, "cli.mjs": CLI_MJS, "adder.test.mjs": GOOD_MJS_TEST})
        for _ in range(4):
            r = self.adv("jsadder")
            self.assertTrue(r["passed"], (r["station"], r["gates"]))
        self.assertEqual(r["dish"]["status"], "plated")

    def test_test_file_conventions(self):
        for name, expect in (("test_a.py", True), ("a.test.ts", True), ("a.spec.mjs", True), ("test_a.js", True),
                             ("a.py", False), ("latest.js", False), ("contest.ts", False)):
            self.assertEqual(core.is_test_file(Path(name)), expect, name)


class TestHardening(Kitchen):
    """Regressions from the independent review."""

    def failed(self, r):
        return [g["gate"] for g in r["gates"] if not g["passed"]]

    def test_plating_reruns_the_blind_scenarios(self):
        ws, _ = self.cook_to(6)
        (ws / "adder.py").write_text("print('wrong')\n", encoding="utf-8")
        (ws / "test_adder.py").write_text("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        pass\n", encoding="utf-8")
        self.assertIn("blind_scenarios_pass", self.failed(self.adv()))

    def test_malformed_scenario_before_sealing_fails_station_three(self):
        ws, ho = self.dish()
        (ho / "scenario_001.json").write_text("{}", encoding="utf-8")
        (ws / "SPEC.md").write_text(SPEC, encoding="utf-8")
        (ws / "run.cmd").write_text("x\n", encoding="utf-8")
        self.adv(); self.adv()
        self.assertIn("scenarios_valid", self.failed(self.adv()))

    def test_starter_placeholder_scenario_cannot_be_sealed(self):
        ws, ho = self.dish()
        (ws / "SPEC.md").write_text(SPEC, encoding="utf-8")
        (ws / "run.cmd").write_text("x\n", encoding="utf-8")
        self.adv(); self.adv()
        self.assertIn("scenarios_not_placeholder", self.failed(self.adv()))

    def test_scenario_broken_after_sealing_quarantines_without_crashing(self):
        _, ho = self.cook_to(5)
        (ho / "scenario_001.json").write_text("{}", encoding="utf-8")
        r = self.adv()
        self.assertTrue(r["tamper"])
        self.assertEqual(core.status(self.root, "adder")[0]["status"], "quarantined")

    def test_tampered_scenario_command_is_never_executed(self):
        _, ho = self.cook_to(5)
        marker = self.root / "ran.txt"
        evil = {"cmd": ["{python}", "-c", "open(r'%s','w').write('x')" % marker], "expect_rc": 0}
        (ho / "scenario_001.json").write_text(json.dumps(evil), encoding="utf-8")
        self.adv()
        self.assertFalse(marker.exists())

    def test_invalid_taste_review_does_not_crash(self):
        _, ho = self.cook_to(5)
        (ho / "taste_review.json").write_text("[]", encoding="utf-8")
        self.assertIn("taste_review_pass", self.failed(self.adv()))

    def test_stage_cannot_be_moved_backward_to_reseal_the_holdout(self):
        self.cook_to(4)
        with contextlib.closing(sqlite3.connect(str(self.root / "foundry.db"))) as db:
            db.execute("UPDATE dishes SET stage_idx=2")
            db.commit()
        with self.assertRaises(core.FoundryError):
            self.adv()

    def test_holdout_inside_workspace_fails_station_three(self):
        ws, ho = self.cook_to(2)
        inside = ws / "holdout"
        shutil.copytree(ho, inside)
        with contextlib.closing(sqlite3.connect(str(self.root / "foundry.db"))) as db:
            db.execute("UPDATE dishes SET holdout=?", (str(inside),))
            db.commit()
        self.assertIn("holdout_outside_sandbox", self.failed(self.adv()))

    def test_banned_word_hidden_in_node_modules_is_caught(self):
        ws, _ = self.cook_to(4)
        (ws / "node_modules" / "x").mkdir(parents=True)
        (ws / "node_modules" / "x" / "a.js").write_text("// " + BANNED, encoding="utf-8")
        self.assertIn("no_banned_words", self.failed(self.adv()))

    def test_utf16_secret_is_caught(self):
        ws, _ = self.cook_to(4)
        (ws / "k.txt").write_bytes(("key = sk-" + "ant-api03-abcdefgh12345678").encode("utf-16"))
        self.assertIn("no_secret_patterns", self.failed(self.adv()))

    def test_holdout_path_with_forward_slashes_and_other_case_is_caught(self):
        ws, ho = self.cook_to(4)
        (ws / "n.txt").write_text(str(ho).replace("\\", "/").upper(), encoding="utf-8")
        self.assertIn("builder_never_read_holdout", self.failed(self.adv()))

    def test_zero_tests_run_fails_even_with_exit_zero(self):
        cmd = [sys.executable, "-c", "import sys; sys.stderr.write('Ran 0 tests'); sys.exit(0)"]
        d = core.init_dish(self.root, "empty", HOOK, cmd)
        ws, ho = Path(d["workspace"]), Path(d["holdout"])
        (ho / "scenario_001.json").write_text(json.dumps(SCENARIO), encoding="utf-8")
        (ws / "SPEC.md").write_text(SPEC, encoding="utf-8")
        (ws / "run.cmd").write_text("x\n", encoding="utf-8")
        (ws / "adder.py").write_text(GOOD_PY, encoding="utf-8")
        (ws / "test_adder.py").write_text(GOOD_TEST, encoding="utf-8")
        for _ in range(5):
            r = self.adv("empty")
        self.assertIn("tests_pass", self.failed(r))

    def test_builder_commands_do_not_inherit_secrets_from_the_environment(self):
        import os
        os.environ["FOUNDRY_TEST_SECRET"] = "hunter2"
        self.addCleanup(os.environ.pop, "FOUNDRY_TEST_SECRET", None)
        rc, out, _ = core.run_cmd(["{python}", "-c", "import os; print(os.environ.get('FOUNDRY_TEST_SECRET'))"], self.root)
        self.assertEqual(out.strip(), "None")


class TestCli(Kitchen):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "foundry.py"), *args], capture_output=True, text=True, timeout=120)

    def test_demo_exits_zero(self):
        r = self.run_cli("--demo")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DEMO OK", r.stdout)

    def test_demo_shows_back_pressure_and_plating(self):
        out = self.run_cli("--demo").stdout
        self.assertIn("loops back to station 4", out)
        self.assertIn("dish is plated at station 7", out)

    def test_init_advance_status_round_trip(self):
        r = self.run_cli("init", "cli-dish", "--hook", HOOK, "--root", self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.run_cli("advance", "cli-dish", "--root", self.tmp).returncode, 0)
        s = self.run_cli("status", "cli-dish", "--root", self.tmp, "--json")
        self.assertEqual(json.loads(s.stdout)[0]["station"], "2_recipe_spec")

    def test_failed_gate_exit_code_is_two(self):
        self.run_cli("init", "cli-dish", "--hook", HOOK, "--root", self.tmp)
        self.run_cli("advance", "cli-dish", "--root", self.tmp)
        self.assertEqual(self.run_cli("advance", "cli-dish", "--root", self.tmp).returncode, 2)

    def test_unknown_dish_exits_one_with_message(self):
        r = self.run_cli("advance", "ghost", "--root", self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown dish", r.stderr)

    def test_status_lists_every_dish(self):
        self.dish("one")
        self.dish("two")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            foundry.main(["status", "--root", self.tmp])
        self.assertIn("one", buf.getvalue())
        self.assertIn("two", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
