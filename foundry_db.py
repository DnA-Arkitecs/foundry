"""Foundry ledger basics: constants, the SQLite schema, hashing and the sealed holdout."""
import hashlib
import sqlite3
import time
from pathlib import Path

STATIONS = ["1_menu_gate", "2_recipe_spec", "3_mise_en_place", "4_line_cooks",
            "5_hard_validators", "6_blind_holdout_taste", "7_plating"]
LOOP_BACK_TO = 3  # index of 4_line_cooks: a failure at station 5 or 6 sends the dish back here
MAX_LINES = 250   # liquid software: small modules an agent can reshape
SPEC_SECTIONS = ["User Journey", "Performance Floor", "Anti-Slop Rules", "Privacy Boundaries"]
RUNNER_NAMES = ["justfile", "Makefile", "run.sh", "run.cmd"]
GENERIC_HOOKS = ["chatbot", "gpt wrapper", "wrapper around"]
SECRET_PATTERNS = [
    # prefixes are split so this file never holds a real key prefix (repo scanners would flag it)
    ("anthropic key", r"sk-" r"ant-[A-Za-z0-9_-]{8,}"),
    ("aws key id", r"AK" r"IA[0-9A-Z]{16}"),
    ("google key", r"AI" r"za[0-9A-Za-z_-]{35}"),
    ("github token", r"gh[pousr]_[A-Za-z0-9]{36}"),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("windows user path", r"[A-Za-z]:\\Users\\[^\\\s\"']+"),
    ("posix user path", r"/(?:Users|home)/[A-Za-z0-9._-]+/"),
]
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS dishes (slug TEXT PRIMARY KEY, hook TEXT NOT NULL, workspace TEXT NOT NULL,
  holdout TEXT NOT NULL, test_cmd TEXT NOT NULL, stage_idx INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'cooking', created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS station_runs (id INTEGER PRIMARY KEY, slug TEXT NOT NULL, station TEXT NOT NULL,
  attempt INTEGER NOT NULL, outcome TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS gate_results (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, slug TEXT NOT NULL,
  station TEXT NOT NULL, gate TEXT NOT NULL, passed INTEGER NOT NULL, detail TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (id INTEGER PRIMARY KEY, slug TEXT NOT NULL, kind TEXT NOT NULL,
  path TEXT NOT NULL, sha256 TEXT NOT NULL, ts REAL NOT NULL);
"""

SPEC_TEMPLATE = "# SPEC: {slug}\n\nHook: {hook}\n\n" + "".join(
    "## {}\n\nTBD\n\n".format(s) for s in SPEC_SECTIONS)
STARTER_SCENARIO = {"name": "starter", "cmd": ["{python}", "-c", "print('replace me')"],
                    "expect_rc": 0, "expect_stdout_contains": "replace me"}


class FoundryError(Exception):
    """A refusal: the ADW will not do what was asked."""


def default_banned_words():
    """Words no shipped dish may contain. The first one is built from char codes on purpose."""
    return ["".join(map(chr, (102, 114, 101, 101))), "lorem ipsum", "as an ai language model",
            "todo: implement"]


def connect(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(root / "foundry.db"))
    db.row_factory = sqlite3.Row
    db.executescript(DB_SCHEMA)
    return db


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def holdout_files(holdout):
    return sorted(p for p in Path(holdout).glob("scenario_*.json") if p.is_file())


def seal_holdout(db, slug, holdout):
    """Record the SHA-256 of every holdout scenario. Only station 3 and init may call this."""
    db.execute("DELETE FROM artifacts WHERE slug=? AND kind='holdout'", (slug,))
    for p in holdout_files(holdout):
        db.execute("INSERT INTO artifacts(slug,kind,path,sha256,ts) VALUES(?,?,?,?,?)",
                   (slug, "holdout", p.name, sha256_file(p), time.time()))
    db.commit()


def get_dish(db, slug):
    row = db.execute("SELECT * FROM dishes WHERE slug=?", (slug,)).fetchone()
    if row is None:
        raise FoundryError("unknown dish: " + slug)
    return row

