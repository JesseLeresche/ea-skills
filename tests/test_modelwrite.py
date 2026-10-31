"""The write commands `elements new|set|retire|remove` and `relations new|remove` (plan 002).

Everything goes through `cli.main([...])` against a scratch repository in `tmp_path` and copies
of the example repository, so what is tested is what an operator types. `eval/example` and
`eval/golden` are never written: every test that needs the example copies it first.

Contract decisions the implementation must follow
-------------------------------------------------
Where docs/plans/002-elements-write-commands.md is silent, these are the readings the tests use.

* New module `easkills/modelwrite.py` with `add_leaves(elements_sub, relations_sub)`;
  `modelquery.register` calls it so the leaves join the existing `elements` and `relations`
  groups. Everything is tested through `cli.main([...])` (SystemExit is turned into its code).
* Commands (each takes --root, --dry-run, boolean --json):
    elements new --type T --name N [--id ID] [--documentation D] [--property k=v]...
                 [--owner O] [--last-reviewed YYYY-MM-DD] [--created-by NAME] + evidence
    elements set ID FIELD VALUE   (FIELD in name, documentation, owner, lastReviewed,
                 properties.KEY; refused: id, type, provenance, lists, properties.createdBy,
                 an empty name, a lastReviewed that is not YYYY-MM-DD)
    elements retire ID            (sets properties.lifecycle: retired; already retired = exit 0
                 no-op, nothing written)
    elements remove ID --confirm ID ; relations remove ID --confirm ID   (hard delete; refused
                 if the id is unknown, if --confirm differs, or (elements) if any relationship
                 still names the element as source or target)
    relations new --type T --from A --to B [--id ID] [--access-type X] [--directed true|false]
                 [--modifier M] [--created-by NAME] + evidence
  Evidence (new commands): `--source FILE --quote TEXT` (repeatable; the Nth --source pairs with
  the Nth --quote; a count mismatch is a usage error, exit 2), and/or `--fact ID` (repeatable), or
  `--assumed --rationale TEXT`. None of these, or --assumed without --rationale, or --assumed
  together with --source/--fact, is refused with exit 1 and nothing written.
* Exit codes: 0 done (also --dry-run that would succeed, and retire of an already retired
  element); 1 refused, nothing written (unknown id, validation, conflict, unsafe edit, missing
  evidence, bot name without --created-by); 2 argparse usage error.
* Writes go ONLY under model/staging/. The whole tree outside model/staging/ (especially
  model/approved/) is byte-identical before and after every command variant. `--dry-run` writes
  nothing (whole-tree hash identical) but reports the target file and findings. A refusal leaves
  no new file or directory and no temp files.
* File layout: a new element or relationship goes to its own file model/staging/<id>.yaml (YAML
  with top-level `elements:` or `relationships:` list). If model/approved/<id>.yaml or <id>.yml
  already exists the command is refused (it would shadow it). Ids: element default = slug of
  --name (an already valid slug passes through; otherwise lowercase, spaces/underscores/dots/
  slashes to '-', collapse repeats, strip '-', keep [a-z0-9-]); relationship default =
  `rel-<from>-<type lowercased>-<to>`. An id longer than 80 chars or not matching the schema slug
  pattern is refused (use --id). An id already present in either zone (element OR relationship
  id) is refused with suggestions and never gets a numbered suffix.
* Validation: the command builds the would-be staging overlay in memory (same semantics as
  dsl.load_merged incl. shadowing), runs the existing validator on it and on the overlay before
  the change; any finding of severity error whose (code, concept) was not present before refuses
  (exit 1, nothing written, the findings printed). Pre-existing errors never block. Warnings are
  printed and the write proceeds, EXCEPT PROV004 (approximate quote) on the new concept, which is
  refused.
* createdBy: `new` writes `properties.createdBy` from --created-by, else from the module function
  `modelwrite.git_user_name(root) -> str | None` (tests monkeypatch it; the real one runs
  `git -C root config user.name`). A name is a bot if it matches `(?i)(\\[bot\\]|\\bbot\\b)`; a
  bot name or None without --created-by is refused (exit 1, message mentions --created-by); an
  explicit --created-by that is bot-like is also refused. createdBy is only written if the
  --property list did not already set it (an explicit --property createdBy=... is refused as a
  conflicting source). `set` never changes it. `owner` and `lastReviewed` are NEVER stamped:
  absent unless the flags are given (staging then shows GOV001/GOV002 warnings, which are
  printed, not errors). For relationships createdBy goes in the relationship's `properties` if
  schema/model.schema.json allows `properties` on relationships; if it does not, it is omitted
  and the JSON/text output says so in `notes` (the tests read the schema and assert whichever
  is true).
* set/retire/remove on an element: locate it. (a) In staging only: edit that staging file in
  place. (b) In approved only: copy that whole approved file byte-for-byte into model/staging/ at
  the same relative path, then edit the one element's block (so staging replaces the approved
  file on promote); the output names the file. (c) In both with a staging file at the SAME
  relative path: edit the staging file. (d) In both with the staging copy at a DIFFERENT path:
  refuse (exit 1) with an explanation, nothing written. Edits are surgical: comments, wording,
  line endings (CRLF too) and every other element/line stay byte-identical; only the intended
  lines change. Anything not plainly simple YAML (flow-style element block, anchors, multi-line
  scalars on the edited key) is refused (exit 1) with nothing written, not guessed at. A
  `properties` mapping is created if absent; a new property key is added; an existing one
  replaced.
* remove details: staging-only element/relationship in a per-concept file: the file is deleted
  when it would be left empty (and the staging directory it created is not removed if it holds
  other files). Approved concept: stage a copy of the approved file without that block (file kept
  even if the list becomes empty: `elements: []`), approved/ untouched, and `promote --dry-run`
  then lists the removed id under dropped concepts. Removing an element named by a view's include
  is refused by the validator (REF002) with nothing written. Removing a relationship never needs
  relationships checked. After `remove`, `validate --zone staging` has no new errors.
* Conflict: modelwrite has a module-level hook `_before_swap(path)` (default no-op) called after
  the file was read and the new text computed and just before the atomic replace; the
  implementation re-reads the file and refuses (exit 1, message contains "changed on disk") if
  its bytes differ from what was read. Tests monkeypatch `_before_swap` to modify the file.
* JSON success document: {"ok": true, "action": "new|set|retire|remove", "kind":
  "element|relationship", "id", "file": "model/staging/..." (posix, relative to root), "written":
  bool (false on --dry-run or no-op), "findings": [{"code","severity","message","concept"}...]
  (warnings only on success), "notes": [str...]}. JSON failure: {"ok": false, "error": str,
  "findings": [...]} (findings only when validation refused). Text mode success: one line
  `created element ID in model/staging/ID.yaml` (verbs: created, changed, retired, removed;
  `would ...` on --dry-run), warnings as `warning: CODE message` lines on stderr; failures
  `error: ...` on stderr.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from easkills import cli, dsl, modelquery, modelwrite, oracle

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "eval" / "example"
GOLDEN = REPO_ROOT / "eval" / "golden"
SCHEMA = json.loads((REPO_ROOT / "schema" / "model.schema.json").read_text(encoding="utf-8"))
RELATIONSHIP_HAS_PROPERTIES = "properties" in SCHEMA["$defs"]["relationship"]["properties"]
REAL_GIT_USER_NAME = modelwrite.git_user_name  # the tests below patch the module attribute

SOURCE = "facts/sources/notes.md"
OTHER_SOURCE = "facts/sources/other.md"
Q_ERP = "The ERP system publishes the order API to the portal."
Q_PICK = "Warehouse picking runs on the floor terminal."
Q_BILL = "The billing service invoices every customer monthly."
Q_ARCHIVE = "Invoices are archived for seven years."
Q_APPROX = "The ERP system publishes the order API to the porta1."
FACT = "fact-picking-terminal"
EVIDENCE_QUOTE = ["--source", SOURCE, "--quote", Q_PICK]
EVIDENCE_ASSUMED = ["--assumed", "--rationale", "pending confirmation"]


# --------------------------------------------------------------------------- fixture repository

NOTES_MD = f"{Q_ERP}\n{Q_PICK}\n{Q_BILL}\nThe data warehouse stores nightly order extracts.\n"
OTHER_MD = f"{Q_ARCHIVE}\nAudit logs are kept for one year.\n"
FACTS_YAML = f"""facts:
  - id: {FACT}
    statement: Warehouse picking runs on the floor terminal.
    provenance:
      - file: {SOURCE}
        quote: {Q_PICK}
    topics: [application]
"""

APPLICATION_YAML = """# Application layer -- comments must survive every edit.
elements:
  # The ERP.
  - id: app-erp
    type: ApplicationComponent
    name: ERP Core   # inline comment
    documentation: Order master data
    owner: finance@example.test
    lastReviewed: 2026-06-30
    properties:
      lifecycle: active
      hosting: on-premise
    assumed: true
    rationale: fixture

  # The WMS (no properties, no documentation).
  - id: app-wms
    type: ApplicationComponent
    name: Warehouse System
    owner: logistics@example.test
    lastReviewed: 2026-06-30
    assumed: true
    rationale: fixture

  - id: app-lonely
    type: ApplicationComponent
    name: Lonely App
    documentation: Nothing points here
    owner: logistics@example.test
    lastReviewed: 2026-06-30
    properties:
      lifecycle: active
    assumed: true
    rationale: fixture

  - id: app-viewed
    type: ApplicationComponent
    name: Viewed App
    owner: logistics@example.test
    lastReviewed: 2026-06-30
    assumed: true
    rationale: fixture
"""

BUSINESS_YAML = """elements:
  - id: proc-fulfil
    type: BusinessProcess
    name: Fulfilment
    owner: ops@example.test
    lastReviewed: 2026-06-30
    assumed: true
    rationale: fixture
  - id: actor-cust
    type: BusinessActor
    name: Customer
    owner: ops@example.test
    lastReviewed: 2026-06-30
    assumed: true
    rationale: fixture
  - id: cap-orders
    type: Capability
    name: Order Management
    owner: ops@example.test
    lastReviewed: 2026-06-30
    assumed: true
    rationale: fixture
  - id: goal-speed
    type: Goal
    name: Ship faster
    owner: ops@example.test
    lastReviewed: 2026-06-30
    assumed: true
    rationale: fixture
  - id: req-fast
    type: Requirement
    name: Orders ship in a day
    owner: ops@example.test
    lastReviewed: 2026-06-30
    assumed: true
    rationale: fixture
"""

TECHNOLOGY_YAML = """elements:
  - id: node-srv
    type: Node
    name: App Server
    owner: infra@example.test
    lastReviewed: 2026-06-30
    assumed: true
    rationale: fixture
  - id: svc-api
    type: ApplicationService
    name: Order API
    owner: finance@example.test
    lastReviewed: 2026-06-30
    assumed: true
    rationale: fixture
  - id: data-order
    type: DataObject
    name: Order Record
    owner: finance@example.test
    lastReviewed: 2026-06-30
    assumed: true
    rationale: fixture
"""

SOLO_YAML = """elements:
  - id: solo-app
    type: ApplicationComponent
    name: Solo App
    owner: ops@example.test
    lastReviewed: 2026-06-30
    assumed: true
    rationale: fixture
"""

RELATIONS_YAML = """# Relationships.
relationships:
  - id: rel-erp-realizes-api
    type: Realization
    source: app-erp
    target: svc-api
    assumed: true
    rationale: fixture
  - id: rel-erp-realizes-cap
    type: Realization
    source: app-erp
    target: cap-orders
    assumed: true
    rationale: fixture
  - id: rel-api-serves-proc
    type: Serving
    source: svc-api
    target: proc-fulfil
    assumed: true
    rationale: fixture
  - id: rel-proc-accesses-data
    type: Access
    source: proc-fulfil
    target: data-order
    accessType: Read
    assumed: true
    rationale: fixture
  - id: rel-node-serves-erp
    type: Serving
    source: node-srv
    target: app-erp
    assumed: true
    rationale: fixture
  - id: rel-wms-serves-erp
    type: Serving
    source: app-wms
    target: app-erp
    assumed: true
    rationale: fixture
  - id: rel-cust-assigned-proc
    type: Assignment
    source: actor-cust
    target: proc-fulfil
    assumed: true
    rationale: fixture
"""

VIEWS_YAML = """views:
  - id: view-all
    name: Everything
    viewpoint: Layered
    include: [app-erp, app-wms, app-viewed, svc-api]
"""

SEED_FILES = {
    "facts/sources/notes.md": NOTES_MD,
    "facts/sources/other.md": OTHER_MD,
    "facts/register/notes.yaml": FACTS_YAML,
    "model/approved/application.yaml": APPLICATION_YAML,
    "model/approved/business.yaml": BUSINESS_YAML,
    "model/approved/layers/technology.yaml": TECHNOLOGY_YAML,
    "model/approved/solo.yaml": SOLO_YAML,
    "model/approved/relations.yaml": RELATIONS_YAML,
    "model/approved/views.yaml": VIEWS_YAML,
}


def write(root: Path, rel_path: str, text: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def read(root: Path, rel_path: str) -> str:
    return (root / rel_path).read_bytes().decode("utf-8")


def seed(root: Path) -> Path:
    for rel_path, text in SEED_FILES.items():
        write(root, rel_path, text)
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return seed(tmp_path / "repo")


@pytest.fixture
def example_root(tmp_path: Path) -> Path:
    root = tmp_path / "example-copy"
    shutil.copytree(EXAMPLE, root)
    return root


@pytest.fixture(autouse=True)
def git_name(monkeypatch):
    """The name `git_user_name` reports; tests change `holder['name']`."""
    holder = {"name": "Sam Person"}
    monkeypatch.setattr(modelwrite, "git_user_name", lambda root: holder["name"])
    return holder


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


@pytest.fixture(scope="module", autouse=True)
def reference_trees_untouched():
    before = (_tree_hash(EXAMPLE), _tree_hash(GOLDEN))
    yield
    assert (_tree_hash(EXAMPLE), _tree_hash(GOLDEN)) == before


# --------------------------------------------------------------------------- helpers


def run(capsys, *argv: str, root: Path | None = None) -> tuple[int, str, str]:
    full = list(argv) + (["--root", str(root)] if root is not None else [])
    try:
        code = cli.main(full)
    except SystemExit as exit_:
        code = exit_.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def jrun(capsys, *argv: str, root: Path) -> tuple[int, dict]:
    code, out, _err = run(capsys, *argv, "--json", root=root)
    return code, json.loads(out)


def digest(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def outside_staging(root: Path) -> dict[str, str]:
    return {k: v for k, v in digest(root).items() if not k.startswith("model/staging/")}


def listing(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def snap(root: Path) -> tuple[dict[str, str], set[str]]:
    return digest(root), listing(root)


def staged_files(root: Path) -> list[str]:
    base = root / "model" / "staging"
    return sorted(p.relative_to(root).as_posix() for p in base.rglob("*") if p.is_file()) if base.is_dir() else []


def no_temp_files(root: Path) -> bool:
    return not [p for p in root.rglob("*") if p.name.endswith(".tmp") or ".tmp" in p.suffixes]


def concept(root: Path, concept_id: str, zone: str = "staging"):
    model, _docs, _config = dsl.load_zone(root, zone)
    return model.elements.get(concept_id) or model.relationships.get(concept_id)


def yaml_of(root: Path, rel_path: str):
    return yaml.safe_load(read(root, rel_path))


def line_diff(before: str, after: str) -> tuple[list[str], list[str]]:
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    removed: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            removed.extend(a[i1:i2])
            added.extend(b[j1:j2])
    return removed, added


def is_subsequence(small: list[str], big: list[str]) -> bool:
    it = iter(big)
    return all(line in it for line in small)


def codes(doc: dict) -> list[str]:
    return [f["code"] for f in doc.get("findings", [])]


def own_warnings(doc: dict, concept_id: str) -> list[str]:
    """Warning codes about one concept; SMELL001 (an isolated element) is ignored: a fresh element is isolated."""
    return sorted(f["code"] for f in doc["findings"] if f["concept"] == concept_id and f["code"] != "SMELL001")


def enew(capsys, root, name="Billing Service", etype="ApplicationComponent", extra=(), ev=EVIDENCE_ASSUMED):
    return jrun(capsys, "elements", "new", "--type", etype, f"--name={name}", *extra, *ev, root=root)


def rnew(capsys, root, rtype="Serving", src="app-wms", tgt="svc-api", extra=(), ev=EVIDENCE_ASSUMED):
    return jrun(capsys, "relations", "new", "--type", rtype, "--from", src, "--to", tgt, *extra, *ev, root=root)


def stage_element(capsys, root, name="Staged App", extra=()):
    code, doc = enew(capsys, root, name, extra=extra)
    assert code == 0, doc
    return doc["id"]


def expected_rel001(rtype: str, source: tuple[str, str], target: tuple[str, str]) -> str:
    """The validator's REL001 text (see validate.check_relationship_matrix)."""
    (sid, stype), (tid, ttype) = source, target
    allowed = oracle.allowed_relationships(stype, ttype)
    reverse = oracle.allowed_relationships(ttype, stype)
    extra = ""
    if rtype in reverse:
        extra = " -- it is permitted in the opposite direction, so the endpoints are probably swapped"
    return (
        f"{rtype} from {stype} '{sid}' to {ttype} '{tid}' is not permitted by the ArchiMate "
        f"{oracle.matrix_version()} relationship matrix. Permitted here: {', '.join(sorted(allowed))}{extra}"
    )


def refused(capsys, root: Path, *argv: str, code: int = 1) -> dict:
    """Run with --json, expect a refusal and an untouched tree (files, directories, temp files)."""
    before = snap(root)
    got, doc = jrun(capsys, *argv, root=root)
    assert got == code, doc
    assert doc["ok"] is False and isinstance(doc["error"], str) and doc["error"]
    assert snap(root) == before
    return doc


def apply_edit(capsys, root, *argv):
    code, doc = jrun(capsys, *argv, root=root)
    assert code == 0, doc
    return doc


# --------------------------------------------------------------------------- the fixture itself


def test_seed_repository_validates_clean(capsys, repo):
    assert run(capsys, "validate", root=repo)[0] == 0
    assert run(capsys, "validate", "--zone", "staging", root=repo)[0] == 0


def test_approximate_quote_really_is_approximate(repo):
    from easkills import validate as validate_mod

    write(repo, "model/staging/probe.yaml", yaml.safe_dump(
        {"elements": [{"id": "probe", "type": "Node", "name": "Probe", "provenance": [{"file": SOURCE, "quote": Q_APPROX}]}]}
    ))
    found = {f.code for f in validate_mod.validate(repo, "staging").findings}
    assert "PROV004" in found and "PROV003" not in found


def test_add_leaves_exists_and_register_wires_it():
    assert callable(modelwrite.add_leaves)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    modelquery.register(sub)
    assert {"elements", "relations"} <= set(sub.choices)


def test_default_before_swap_hook_is_a_no_op(tmp_path):
    assert modelwrite._before_swap(tmp_path / "x.yaml") is None


# --------------------------------------------------------------------------- usage errors (exit 2)

NEW_EL = ["elements", "new", "--type", "Node", "--name", "N"]
NEW_REL = ["relations", "new", "--type", "Serving", "--from", "app-wms", "--to", "svc-api"]
USAGE_CASES = {
    "new-no-type": ["elements", "new", "--name", "N", *EVIDENCE_ASSUMED],
    "new-no-name": ["elements", "new", "--type", "Node", *EVIDENCE_ASSUMED],
    "new-two-sources-one-quote": [*NEW_EL, "--source", SOURCE, "--source", OTHER_SOURCE, "--quote", Q_PICK],
    "new-quote-without-source": [*NEW_EL, "--quote", Q_PICK],
    "new-source-without-quote": [*NEW_EL, "--source", SOURCE],
    "new-one-source-two-quotes": [*NEW_EL, "--source", SOURCE, "--quote", Q_PICK, "--quote", Q_ERP],
    "set-no-value": ["elements", "set", "app-erp", "name"],
    "retire-no-id": ["elements", "retire"],
    "rel-new-no-from": ["relations", "new", "--type", "Serving", "--to", "svc-api", *EVIDENCE_ASSUMED],
    "rel-new-no-to": ["relations", "new", "--type", "Serving", "--from", "app-wms", *EVIDENCE_ASSUMED],
    "rel-new-source-quote-mismatch": [*NEW_REL, "--source", SOURCE, "--quote", Q_PICK, "--quote", Q_ERP],
    "rel-remove-no-id": ["relations", "remove"],
}


@pytest.mark.parametrize("argv", USAGE_CASES.values(), ids=USAGE_CASES.keys())
def test_usage_errors_exit_2_and_write_nothing(capsys, repo, argv):
    before = snap(repo)
    code, _out, err = run(capsys, *argv, root=repo)
    assert code == 2 and err and "invalid choice" not in err  # the leaf exists; it is its own arguments that are wrong
    assert snap(repo) == before


@pytest.mark.parametrize("argv", [
    ["elements", "new", "--type", "Node", "--name", "N", "--last-reviewed", "30/06/2026", *EVIDENCE_ASSUMED],
    ["relations", "new", "--type", "Association", "--from", "app-wms", "--to", "app-lonely", "--directed", "maybe", *EVIDENCE_ASSUMED],
    ["relations", "new", "--type", "Access", "--from", "app-wms", "--to", "data-order", "--access-type", "Bogus", *EVIDENCE_ASSUMED],
], ids=["bad-date", "bad-directed", "bad-access-type"])
def test_bad_option_values_are_refused_or_usage_errors(capsys, repo, argv):
    before = snap(repo)
    code, _out, err = run(capsys, *argv, root=repo)
    assert code in (1, 2) and "invalid choice" not in err
    assert snap(repo) == before


# --------------------------------------------------------------------------- elements new: happy paths

TYPE_CASES = ["BusinessActor", "BusinessProcess", "ApplicationComponent", "DataObject", "Node", "Goal", "Requirement"]


@pytest.mark.parametrize("etype", TYPE_CASES)
def test_new_each_element_type(capsys, repo, etype):
    code, doc = enew(capsys, repo, "Fresh Thing", etype)
    assert code == 0 and doc["ok"] is True
    assert (doc["action"], doc["kind"], doc["id"]) == ("new", "element", "fresh-thing")
    assert doc["file"] == "model/staging/fresh-thing.yaml" and doc["written"] is True
    written = yaml_of(repo, "model/staging/fresh-thing.yaml")
    assert list(written) == ["elements"] and len(written["elements"]) == 1
    item = written["elements"][0]
    assert (item["id"], item["type"], item["name"]) == ("fresh-thing", etype, "Fresh Thing")
    assert item["assumed"] is True and item["rationale"] == "pending confirmation"
    assert item["properties"]["createdBy"] == "Sam Person"
    assert staged_files(repo) == ["model/staging/fresh-thing.yaml"] and no_temp_files(repo)


def test_new_accepts_every_element_type_the_validator_accepts(capsys, repo):
    for etype in sorted(oracle.element_types() - {"Plateau"}):
        code, doc = enew(capsys, repo, f"Probe {etype}", etype)
        assert code == 0, (etype, doc)


def test_new_plateau_needs_its_date(capsys, repo):
    doc = refused(capsys, repo, "elements", "new", "--type", "Plateau", "--name", "Later", *EVIDENCE_ASSUMED)
    assert "PLAT001" in codes(doc)
    code, doc = enew(capsys, repo, "Later", "Plateau", extra=["--property", "plateauDate=2028-01-01"])
    assert code == 0 and concept(repo, "later").properties["plateauDate"] == "2028-01-01"


def test_new_writes_optional_fields(capsys, repo):
    code, doc = enew(capsys, repo, "Billing Service", extra=[
        "--documentation", "Sends invoices", "--property", "lifecycle=active", "--property", "hosting=cloud",
        "--owner", "billing@example.test", "--last-reviewed", "2026-07-15",
    ])
    assert code == 0
    item = yaml_of(repo, "model/staging/billing-service.yaml")["elements"][0]
    assert item["documentation"] == "Sends invoices"
    assert item["owner"] == "billing@example.test" and str(item["lastReviewed"]) == "2026-07-15"
    assert item["properties"] == {"lifecycle": "active", "hosting": "cloud", "createdBy": "Sam Person"}
    assert own_warnings(doc, "billing-service") == []


@pytest.mark.parametrize("flags,present,missing", [
    ([], [], ["owner", "lastReviewed"]),
    (["--owner", "o@example.test"], ["owner"], ["lastReviewed"]),
    (["--last-reviewed", "2026-07-15"], ["lastReviewed"], ["owner"]),
], ids=["neither", "owner-only", "date-only"])
def test_owner_and_last_reviewed_are_never_stamped(capsys, repo, flags, present, missing):
    code, doc = enew(capsys, repo, extra=flags)
    assert code == 0
    item = yaml_of(repo, "model/staging/billing-service.yaml")["elements"][0]
    assert all(key in item for key in present) and not any(key in item for key in missing)
    expected = {"owner": "GOV001", "lastReviewed": "GOV002"}
    assert own_warnings(doc, "billing-service") == sorted(expected[key] for key in missing)
    assert all(f["severity"] == "warning" for f in doc["findings"])


def test_new_text_mode_prints_one_line_and_warnings_on_stderr(capsys, repo):
    code, out, err = run(capsys, "elements", "new", "--type", "Node", "--name", "Billing Service", *EVIDENCE_ASSUMED, root=repo)
    assert code == 0
    assert out.strip().splitlines() == ["created element billing-service in model/staging/billing-service.yaml"]
    lines = err.splitlines()
    assert any(re.match(r"warning: GOV001 \S", line) for line in lines)
    assert any(re.match(r"warning: GOV002 \S", line) for line in lines)


def test_new_json_document_shape(capsys, repo):
    code, out, _err = run(capsys, "elements", "new", "--type", "Node", "--name", "Edge", *EVIDENCE_ASSUMED, "--json", root=repo)
    doc = json.loads(out)
    assert code == 0
    assert {"ok", "action", "kind", "id", "file", "written", "findings", "notes"} <= set(doc)
    assert isinstance(doc["notes"], list) and all(isinstance(n, str) for n in doc["notes"])
    assert all({"code", "severity", "message", "concept"} <= set(f) for f in doc["findings"])
    assert "\\" not in doc["file"] and not Path(doc["file"]).is_absolute()


def test_new_result_loads_as_a_staging_concept_and_validates(capsys, repo):
    enew(capsys, repo, extra=["--owner", "o@example.test", "--last-reviewed", "2026-07-15"])
    item = concept(repo, "billing-service")
    assert item.type == "ApplicationComponent" and item.source_path == repo / "model" / "staging" / "billing-service.yaml"
    assert run(capsys, "validate", "--zone", "staging", root=repo)[0] == 0


def test_new_creates_the_staging_directory_and_keeps_other_staging_files(capsys, repo):
    write(repo, "model/staging/other.yaml", "elements: []\n")
    before = digest(repo)
    code, _doc = enew(capsys, repo)
    assert code == 0
    assert digest(repo)["model/staging/other.yaml"] == before["model/staging/other.yaml"]
    assert outside_staging(repo) == {k: v for k, v in before.items() if not k.startswith("model/staging/")}


def test_new_line_endings_are_lf_and_file_ends_with_newline(capsys, repo):
    enew(capsys, repo)
    raw = (repo / "model" / "staging" / "billing-service.yaml").read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")


# --------------------------------------------------------------------------- ids


@pytest.mark.parametrize("name,expected", [
    ("ERP Core", "erp-core"),
    ("erp-core", "erp-core"),
    ("Order_Management", "order-management"),
    ("A.B/C d", "a-b-c-d"),
    ("  Spaced   Out  ", "spaced-out"),
    ("--Dashes--", "dashes"),
    ("a--b", "a-b"),
    ("Upper CASE 9", "upper-case-9"),
    ("x_y.z", "x-y-z"),
    ("Café Ünït", "caf-nt"),
    ("Billing (EU) Service!", "billing-eu-service"),
])
def test_default_element_id_is_the_slug_of_the_name(capsys, repo, name, expected):
    code, doc = enew(capsys, repo, name)
    assert code == 0 and doc["id"] == expected
    assert doc["file"] == f"model/staging/{expected}.yaml"
    assert concept(repo, expected).name == name


def test_explicit_id_wins_over_the_name(capsys, repo):
    code, doc = enew(capsys, repo, "Billing Service", extra=["--id", "my-billing"])
    assert code == 0 and doc["file"] == "model/staging/my-billing.yaml"
    assert concept(repo, "my-billing").name == "Billing Service"


def test_explicit_id_may_use_dots_and_underscores(capsys, repo):
    code, doc = enew(capsys, repo, extra=["--id", "a_b.c-1"])
    assert code == 0 and (repo / "model" / "staging" / "a_b.c-1.yaml").is_file()


def test_an_id_of_exactly_80_characters_is_accepted(capsys, repo):
    code, _doc = enew(capsys, repo, extra=["--id", "a" * 80])
    assert code == 0


def test_a_blank_name_is_refused(capsys, repo):
    refused(capsys, repo, "elements", "new", "--type", "Node", "--name", "   ", *EVIDENCE_ASSUMED)


@pytest.mark.parametrize("bad", ["Bad_ID", "UPPER", "-lead", "trail-", "has space", "a/b", "x" * 81, ".dot"])
def test_invalid_explicit_ids_are_refused(capsys, repo, bad):
    refused(capsys, repo, "elements", "new", "--type", "Node", "--name", "N", f"--id={bad}", *EVIDENCE_ASSUMED)


@pytest.mark.parametrize("name", ["!!!", "??", "x" * 90])
def test_a_name_that_cannot_give_a_valid_id_is_refused_with_a_hint(capsys, repo, name):
    doc = refused(capsys, repo, "elements", "new", "--type", "Node", "--name", name, *EVIDENCE_ASSUMED)
    assert "--id" in doc["error"]


@pytest.mark.parametrize("taken", ["app-erp", "svc-api", "rel-erp-realizes-api", "rel-wms-serves-erp"])
def test_id_taken_in_approved_is_refused_without_a_suffix(capsys, repo, taken):
    doc = refused(capsys, repo, "elements", "new", "--type", "Node", "--name", "N", "--id", taken, *EVIDENCE_ASSUMED)
    assert taken in doc["error"]
    assert not [f for f in staged_files(repo) if f.startswith(f"model/staging/{taken}")]


def test_id_taken_by_a_staged_element_is_refused_without_a_suffix(capsys, repo):
    stage_element(capsys, repo, "Billing Service")
    doc = refused(capsys, repo, "elements", "new", "--type", "Node", "--name", "Billing Service", *EVIDENCE_ASSUMED)
    assert "billing-service" in doc["error"]
    assert staged_files(repo) == ["model/staging/billing-service.yaml"]


def test_id_taken_by_a_staged_relationship_is_refused(capsys, repo):
    code, doc = rnew(capsys, repo)
    assert code == 0
    refused(capsys, repo, "elements", "new", "--type", "Node", "--name", "N", "--id", doc["id"], *EVIDENCE_ASSUMED)


def test_relationship_id_collision_with_an_element_is_refused(capsys, repo):
    refused(capsys, repo, "relations", "new", "--type", "Serving", "--from", "app-wms", "--to", "svc-api",
            "--id", "app-erp", *EVIDENCE_ASSUMED)


@pytest.mark.parametrize("suffix", [".yaml", ".yml"])
def test_an_approved_file_named_after_the_id_blocks_the_new_file(capsys, repo, suffix):
    write(repo, f"model/approved/billing-service{suffix}", "elements: []\n")
    refused(capsys, repo, "elements", "new", "--type", "Node", "--name", "Billing Service", *EVIDENCE_ASSUMED)


def test_an_approved_file_named_after_a_relationship_id_blocks_it_too(capsys, repo):
    write(repo, "model/approved/rel-app-wms-serving-svc-api.yaml", "elements: []\n")
    refused(capsys, repo, "relations", "new", "--type", "Serving", "--from", "app-wms", "--to", "svc-api", *EVIDENCE_ASSUMED)


# --------------------------------------------------------------------------- evidence


@pytest.mark.parametrize("kind", ["element", "relationship"])
def test_evidence_by_source_and_quote(capsys, repo, kind):
    code, doc = (enew(capsys, repo, ev=EVIDENCE_QUOTE) if kind == "element" else rnew(capsys, repo, ev=EVIDENCE_QUOTE))
    assert code == 0
    item = concept(repo, doc["id"])
    assert [(p.file, p.quote, p.fact) for p in item.provenance] == [(SOURCE, Q_PICK, "")]
    assert item.assumed is False


@pytest.mark.parametrize("kind", ["element", "relationship"])
def test_evidence_by_fact(capsys, repo, kind):
    ev = ["--fact", FACT]
    code, doc = (enew(capsys, repo, ev=ev) if kind == "element" else rnew(capsys, repo, ev=ev))
    assert code == 0
    assert [(p.file, p.quote, p.fact) for p in concept(repo, doc["id"]).provenance] == [("", "", FACT)]


def test_source_and_quote_pairs_are_matched_by_position(capsys, repo):
    ev = ["--source", SOURCE, "--quote", Q_ERP, "--source", OTHER_SOURCE, "--quote", Q_ARCHIVE]
    code, doc = enew(capsys, repo, ev=ev)
    assert code == 0
    assert [(p.file, p.quote) for p in concept(repo, doc["id"]).provenance] == [(SOURCE, Q_ERP), (OTHER_SOURCE, Q_ARCHIVE)]


def test_swapped_pairs_are_a_fabricated_citation(capsys, repo):
    ev = ["--source", SOURCE, "--quote", Q_ARCHIVE, "--source", OTHER_SOURCE, "--quote", Q_ERP]
    doc = refused(capsys, repo, "elements", "new", "--type", "Node", "--name", "N", *ev)
    assert "PROV003" in codes(doc)


def test_source_and_fact_can_be_combined(capsys, repo):
    code, doc = enew(capsys, repo, ev=[*EVIDENCE_QUOTE, "--fact", FACT])
    assert code == 0
    got = {(p.file, p.quote, p.fact) for p in concept(repo, doc["id"]).provenance}
    assert got == {(SOURCE, Q_PICK, ""), ("", "", FACT)}


def test_a_quote_is_matched_ignoring_case_and_whitespace(capsys, repo):
    code, _doc = enew(capsys, repo, ev=["--source", SOURCE, "--quote", "  WAREHOUSE   picking runs\n on the floor terminal. "])
    assert code == 0


NO_EVIDENCE = {
    "none": [],
    "assumed-without-rationale": ["--assumed"],
    "assumed-with-source": [*EVIDENCE_ASSUMED, *EVIDENCE_QUOTE],
}


@pytest.mark.parametrize("kind", ["element", "relationship"])
@pytest.mark.parametrize("ev", NO_EVIDENCE.values(), ids=NO_EVIDENCE.keys())
def test_missing_or_conflicting_evidence_is_refused(capsys, repo, kind, ev):
    base = NEW_EL if kind == "element" else NEW_REL
    refused(capsys, repo, *base, *ev)


BAD_EVIDENCE = {
    "fabricated-quote": (["--source", SOURCE, "--quote", "The ERP system is hosted on Mars."], "PROV003"),
    "missing-source": (["--source", "facts/sources/nope.md", "--quote", Q_PICK], "PROV002"),
    "source-outside-repo": (["--source", "../outside.md", "--quote", "outside the repository"], "PROV008"),
    "approximate-quote": (["--source", SOURCE, "--quote", Q_APPROX], "PROV004"),
    "unknown-fact": (["--fact", "fact-does-not-exist"], "PROV007"),
}


BAD_EVIDENCE_CASES = [(k, key) for key in BAD_EVIDENCE for k in ("element", "relationship")
                      if k == "element" or key in ("fabricated-quote", "unknown-fact", "approximate-quote")]


@pytest.mark.parametrize("kind,key", BAD_EVIDENCE_CASES, ids=[f"{k}-{key}" for k, key in BAD_EVIDENCE_CASES])
def test_bad_evidence_is_refused_with_the_validators_code(capsys, repo, kind, key):
    ev, code = BAD_EVIDENCE[key]
    write(repo.parent, "outside.md", "outside the repository\n")
    base = NEW_EL if kind == "element" else NEW_REL
    doc = refused(capsys, repo, *base, *ev)
    assert code in codes(doc)
    assert all(f["severity"] in ("error", "warning") for f in doc["findings"])


def test_a_quote_too_short_for_the_schema_is_refused(capsys, repo):
    doc = refused(capsys, repo, *NEW_EL, "--source", SOURCE, "--quote", "ERP")
    assert doc["findings"]


def test_refusal_findings_are_printed_in_text_mode(capsys, repo):
    code, out, err = run(capsys, *NEW_EL, "--source", SOURCE, "--quote", "Not in the file at all", root=repo)
    assert code == 1 and out == ""
    assert "error:" in err and "PROV003" in err


def test_preexisting_errors_never_block_and_are_not_reported(capsys, repo):
    write(repo, "model/approved/broken.yaml", yaml.safe_dump({"elements": [
        {"id": "no-evidence", "type": "Node", "name": "No Evidence", "owner": "o@example.test", "lastReviewed": "2026-06-30"}
    ]}))
    write(repo, "model/approved/garbage.yaml", "a: [\n")
    assert run(capsys, "validate", root=repo)[0] == 1
    code, doc = enew(capsys, repo)
    assert code == 0
    assert all(f["severity"] == "warning" for f in doc["findings"])
    assert "PROV001" not in codes(doc)


def test_warnings_are_printed_and_do_not_block(capsys, repo):
    code, doc = rnew(capsys, repo, "Serving", "app-wms", "app-erp", extra=["--id", "rel-second-wms-erp"])
    assert code == 0 and "REL003" in codes(doc)
    assert all(f["severity"] == "warning" for f in doc["findings"])
    assert concept(repo, "rel-second-wms-erp") is not None


# --------------------------------------------------------------------------- createdBy


def test_created_by_comes_from_the_flag(capsys, repo, git_name):
    git_name["name"] = "Someone Else"
    code, _doc = enew(capsys, repo, extra=["--created-by", "Jesse Leresche"])
    assert code == 0 and concept(repo, "billing-service").properties["createdBy"] == "Jesse Leresche"


def test_created_by_defaults_to_the_git_user_name(capsys, repo, git_name):
    git_name["name"] = "Pat Architect"
    code, _doc = enew(capsys, repo)
    assert code == 0 and concept(repo, "billing-service").properties["createdBy"] == "Pat Architect"


@pytest.mark.parametrize("name", ["Jesse Leresche", "Abbott", "Robotson"])
def test_names_that_are_not_bots_are_accepted(capsys, repo, git_name, name):
    git_name["name"] = name
    code, _doc = enew(capsys, repo)
    assert code == 0 and concept(repo, "billing-service").properties["createdBy"] == name


@pytest.mark.parametrize("name", ["claude[bot]", "home-agent bot", "BOT"])
def test_bot_git_name_without_flag_is_refused_and_flag_fixes_it(capsys, repo, git_name, name):
    git_name["name"] = name
    doc = refused(capsys, repo, *NEW_EL, *EVIDENCE_ASSUMED)
    assert "--created-by" in doc["error"]
    code, _doc = enew(capsys, repo, extra=["--created-by", "Jesse Leresche"])
    assert code == 0


def test_no_git_name_without_flag_is_refused(capsys, repo, git_name):
    git_name["name"] = None
    doc = refused(capsys, repo, *NEW_EL, *EVIDENCE_ASSUMED)
    assert "--created-by" in doc["error"]
    assert enew(capsys, repo, extra=["--created-by", "Jesse Leresche"])[0] == 0


@pytest.mark.parametrize("explicit", ["deploy[bot]", "Deploy Bot", "bot"])
def test_explicit_bot_like_created_by_is_refused(capsys, repo, explicit):
    doc = refused(capsys, repo, *NEW_EL, "--created-by", explicit, *EVIDENCE_ASSUMED)
    assert "--created-by" in doc["error"]


@pytest.mark.parametrize("extra", [[], ["--created-by", "Jesse Leresche"]], ids=["git-name", "flag"])
def test_property_created_by_conflicts_with_the_other_sources(capsys, repo, extra):
    refused(capsys, repo, *NEW_EL, "--property", "createdBy=Somebody", *extra, *EVIDENCE_ASSUMED)


def test_set_refuses_to_change_created_by(capsys, repo):
    stage_element(capsys, repo)
    before = read(repo, "model/staging/staged-app.yaml")
    refused(capsys, repo, "elements", "set", "staged-app", "properties.createdBy", "Somebody Else")
    assert read(repo, "model/staging/staged-app.yaml") == before


def test_set_of_another_field_leaves_created_by_alone(capsys, repo):
    stage_element(capsys, repo)
    apply_edit(capsys, repo, "elements", "set", "staged-app", "name", "Renamed App")
    assert concept(repo, "staged-app").properties["createdBy"] == "Sam Person"


def test_relationship_created_by_follows_the_schema(capsys, repo):
    code, doc = rnew(capsys, repo)
    assert code == 0
    properties = concept(repo, doc["id"]).properties
    if RELATIONSHIP_HAS_PROPERTIES:
        assert properties.get("createdBy") == "Sam Person"
    else:
        assert "createdBy" not in properties
        assert any("createdBy" in note for note in doc["notes"])


def test_relationship_with_a_bot_name_needs_the_flag_too(capsys, repo, git_name):
    git_name["name"] = "claude[bot]"
    doc = refused(capsys, repo, *NEW_REL, *EVIDENCE_ASSUMED)
    assert "--created-by" in doc["error"]
    code, doc = rnew(capsys, repo, extra=["--created-by", "Jesse Leresche"])
    assert code == 0


def _git(cwd: Path, *args: str, env=None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def test_real_git_user_name_reads_the_repository_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _git(tmp_path, "init", "-q")
    assert REAL_GIT_USER_NAME(tmp_path) is None
    _git(tmp_path, "config", "user.name", "Real Person")
    assert REAL_GIT_USER_NAME(tmp_path) == "Real Person"


# --------------------------------------------------------------------------- relations new

LEGAL = [
    ("Serving", "app-wms", "svc-api", []),
    ("Realization", "app-wms", "svc-api", []),
    ("Association", "app-lonely", "app-wms", ["--directed", "false"]),
    ("Flow", "app-wms", "app-lonely", []),
    ("Triggering", "app-lonely", "app-wms", []),
    ("Access", "app-wms", "data-order", ["--access-type", "ReadWrite"]),
    ("Influence", "req-fast", "goal-speed", ["--modifier", "++"]),
]


@pytest.mark.parametrize("rtype,src,tgt,extra", LEGAL, ids=[f"{r[0]}-{r[1]}" for r in LEGAL])
def test_legal_relationships_are_written_to_their_own_file(capsys, repo, rtype, src, tgt, extra):
    code, doc = rnew(capsys, repo, rtype, src, tgt, extra=extra)
    new_id = f"rel-{src}-{rtype.lower()}-{tgt}"
    assert code == 0 and doc["id"] == new_id and doc["kind"] == "relationship"
    assert doc["file"] == f"model/staging/{new_id}.yaml" and doc["written"] is True
    written = yaml_of(repo, doc["file"])
    assert list(written) == ["relationships"] and len(written["relationships"]) == 1
    item = concept(repo, new_id)
    assert (item.type, item.source, item.target) == (rtype, src, tgt)
    assert run(capsys, "validate", "--zone", "staging", root=repo)[0] == 0


def test_relationship_attributes_are_written(capsys, repo):
    rnew(capsys, repo, "Access", "app-wms", "data-order", extra=["--access-type", "Write"])
    rnew(capsys, repo, "Association", "app-lonely", "app-wms", extra=["--directed", "true"])
    rnew(capsys, repo, "Influence", "req-fast", "goal-speed", extra=["--modifier", "+"])
    assert concept(repo, "rel-app-wms-access-data-order").access_type == "Write"
    assert concept(repo, "rel-app-lonely-association-app-wms").is_directed is True
    assert concept(repo, "rel-req-fast-influence-goal-speed").modifier == "+"


def test_directed_false_is_kept_as_false(capsys, repo):
    rnew(capsys, repo, "Association", "app-lonely", "app-wms", extra=["--directed", "false"])
    assert concept(repo, "rel-app-lonely-association-app-wms").is_directed is False


def test_explicit_relationship_id(capsys, repo):
    code, doc = rnew(capsys, repo, extra=["--id", "my-rel"])
    assert code == 0 and doc["file"] == "model/staging/my-rel.yaml"


def test_relationship_text_output_line(capsys, repo):
    code, out, _err = run(capsys, *NEW_REL, *EVIDENCE_ASSUMED, root=repo)
    assert code == 0
    assert out.strip() == "created relationship rel-app-wms-serving-svc-api in model/staging/rel-app-wms-serving-svc-api.yaml"


@pytest.mark.parametrize("rtype,source,target", [
    ("Realization", ("svc-api", "ApplicationService"), ("app-erp", "ApplicationComponent")),
    ("Composition", ("cap-orders", "Capability"), ("data-order", "DataObject")),
    ("Aggregation", ("node-srv", "Node"), ("app-erp", "ApplicationComponent")),
], ids=["swapped-direction", "no-alternative-hint", "aggregation"])
def test_illegal_relationship_is_refused_with_the_validators_text(capsys, repo, rtype, source, target):
    doc = refused(capsys, repo, "relations", "new", "--type", rtype, "--from", source[0], "--to", target[0], *EVIDENCE_ASSUMED)
    wanted = expected_rel001(rtype, source, target)
    assert [f["message"] for f in doc["findings"] if f["code"] == "REL001"] == [wanted]
    finding = next(f for f in doc["findings"] if f["code"] == "REL001")
    assert finding["severity"] == "error" and finding["concept"] == f"rel-{source[0]}-{rtype.lower()}-{target[0]}"


def test_swapped_direction_hint_only_when_the_reverse_is_legal(capsys, repo):
    swapped = refused(capsys, repo, "relations", "new", "--type", "Realization", "--from", "svc-api", "--to", "app-erp", *EVIDENCE_ASSUMED)
    plain = refused(capsys, repo, "relations", "new", "--type", "Composition", "--from", "cap-orders", "--to", "data-order", *EVIDENCE_ASSUMED)
    swapped_text = next(f["message"] for f in swapped["findings"] if f["code"] == "REL001")
    plain_text = next(f["message"] for f in plain["findings"] if f["code"] == "REL001")
    assert "opposite direction" in swapped_text and "opposite direction" not in plain_text
    assert "Permitted here: Association" in plain_text


def test_illegal_relationship_text_mode_prints_the_finding(capsys, repo):
    code, out, err = run(capsys, "relations", "new", "--type", "Realization", "--from", "svc-api", "--to", "app-erp", *EVIDENCE_ASSUMED, root=repo)
    assert code == 1 and out == "" and "REL001" in err and "Permitted here" in err


@pytest.mark.parametrize("flag,value", [("--from", "app-ghost"), ("--to", "svc-ghost")])
def test_unknown_endpoint_is_refused(capsys, repo, flag, value):
    ends = {"--from": "app-wms", "--to": "svc-api", flag: value}
    doc = refused(capsys, repo, "relations", "new", "--type", "Serving", "--from", ends["--from"], "--to", ends["--to"], *EVIDENCE_ASSUMED)
    assert value in json.dumps(doc)


def test_an_endpoint_that_exists_only_in_staging_is_accepted(capsys, repo):
    element_id = stage_element(capsys, repo, "Staged Service", extra=["--owner", "o@example.test", "--last-reviewed", "2026-07-15"])
    code, doc = rnew(capsys, repo, "Serving", element_id, "app-erp")
    assert code == 0 and concept(repo, doc["id"]).source == element_id
    assert run(capsys, "validate", "--zone", "staging", root=repo)[0] == 0


@pytest.mark.parametrize("rtype,src,tgt,extra,attr", [
    ("Serving", "app-wms", "svc-api", ["--access-type", "Read"], "accessType"),
    ("Serving", "app-wms", "svc-api", ["--directed", "true"], "isDirected"),
    ("Serving", "app-wms", "svc-api", ["--modifier", "+"], "modifier"),
    ("Access", "app-wms", "data-order", ["--modifier", "+"], "modifier"),
    ("Association", "app-lonely", "app-wms", ["--access-type", "Read"], "accessType"),
], ids=["access-on-serving", "directed-on-serving", "modifier-on-serving", "modifier-on-access", "access-on-association"])
def test_attribute_on_the_wrong_relationship_type_is_refused(capsys, repo, rtype, src, tgt, extra, attr):
    doc = refused(capsys, repo, "relations", "new", "--type", rtype, "--from", src, "--to", tgt, *extra, *EVIDENCE_ASSUMED)
    assert "REL004" in codes(doc) and attr in json.dumps(doc["findings"])


def test_a_second_identical_relationship_with_the_default_id_is_refused(capsys, repo):
    assert rnew(capsys, repo)[0] == 0
    refused(capsys, repo, *NEW_REL, *EVIDENCE_ASSUMED)


def test_duplicate_of_an_approved_relationship_warns_but_writes(capsys, repo):
    code, doc = rnew(capsys, repo, "Serving", "app-wms", "app-erp")
    assert code == 0
    assert [c for c in codes(doc) if c != "SMELL001"] == ["REL003"]
    assert next(f for f in doc["findings"] if f["code"] == "REL003")["severity"] == "warning"


def test_unknown_relationship_type_is_refused(capsys, repo):
    refused(capsys, repo, "relations", "new", "--type", "Befriends", "--from", "app-wms", "--to", "svc-api", *EVIDENCE_ASSUMED)


def test_unknown_element_type_is_refused(capsys, repo):
    refused(capsys, repo, "elements", "new", "--type", "Spaceship", "--name", "N", *EVIDENCE_ASSUMED)


# --------------------------------------------------------------------------- dry run

DRY_RUNS = {
    "elements-new": (["elements", "new", "--type", "Node", "--name", "Edge", *EVIDENCE_ASSUMED], "model/staging/edge.yaml"),
    "relations-new": ([*NEW_REL, *EVIDENCE_ASSUMED], "model/staging/rel-app-wms-serving-svc-api.yaml"),
    "set-approved": (["elements", "set", "app-erp", "name", "Renamed"], "model/staging/application.yaml"),
    "retire-approved": (["elements", "retire", "app-wms"], "model/staging/application.yaml"),
    "remove-approved": (["elements", "remove", "app-lonely", "--confirm", "app-lonely"], "model/staging/application.yaml"),
    "relations-remove-approved": (["relations", "remove", "rel-node-serves-erp", "--confirm", "rel-node-serves-erp"], "model/staging/relations.yaml"),
}


@pytest.mark.parametrize("argv,target", DRY_RUNS.values(), ids=DRY_RUNS.keys())
def test_dry_run_writes_nothing_and_names_the_file(capsys, repo, argv, target):
    before = snap(repo)
    code, doc = jrun(capsys, *argv, "--dry-run", root=repo)
    assert code == 0 and doc["ok"] is True and doc["written"] is False and doc["file"] == target
    assert isinstance(doc["findings"], list)
    assert snap(repo) == before


@pytest.mark.parametrize("argv,target", list(DRY_RUNS.values())[:3], ids=list(DRY_RUNS)[:3])
def test_dry_run_text_says_would(capsys, repo, argv, target):
    before = snap(repo)
    code, out, _err = run(capsys, *argv, "--dry-run", root=repo)
    assert code == 0 and out.startswith("would ") and target in out
    assert snap(repo) == before


def test_dry_run_on_a_staged_element_changes_nothing(capsys, repo):
    stage_element(capsys, repo)
    before = snap(repo)
    code, doc = jrun(capsys, "elements", "set", "staged-app", "name", "Other", "--dry-run", root=repo)
    assert code == 0 and doc["written"] is False and doc["file"] == "model/staging/staged-app.yaml"
    assert snap(repo) == before


def test_dry_run_reports_warnings(capsys, repo):
    code, doc = jrun(capsys, *NEW_EL, *EVIDENCE_ASSUMED, "--dry-run", root=repo)
    assert code == 0 and {"GOV001", "GOV002"} <= set(own_warnings(doc, "n"))


def test_dry_run_still_refuses_what_a_real_run_would_refuse(capsys, repo):
    before = snap(repo)
    code, doc = jrun(capsys, "relations", "new", "--type", "Realization", "--from", "svc-api", "--to", "app-erp",
                     *EVIDENCE_ASSUMED, "--dry-run", root=repo)
    assert code == 1 and doc["ok"] is False and "REL001" in codes(doc)
    assert snap(repo) == before


def test_dry_run_of_a_noop_retire_exits_0(capsys, repo):
    stage_element(capsys, repo, extra=["--property", "lifecycle=retired"])
    before = snap(repo)
    code, doc = jrun(capsys, "elements", "retire", "staged-app", "--dry-run", root=repo)
    assert code == 0 and doc["written"] is False
    assert snap(repo) == before


# --------------------------------------------------------------------------- approved/ is never written

SWEEP = {
    "new": [*NEW_EL, *EVIDENCE_ASSUMED],
    "new-bad-quote": [*NEW_EL, "--source", SOURCE, "--quote", "not in the file"],
    "new-id-taken": [*NEW_EL, "--id", "app-erp", *EVIDENCE_ASSUMED],
    "rel-new": [*NEW_REL, *EVIDENCE_ASSUMED],
    "rel-new-illegal": ["relations", "new", "--type", "Realization", "--from", "svc-api", "--to", "app-erp", *EVIDENCE_ASSUMED],
    "set-name": ["elements", "set", "app-erp", "name", "Renamed"],
    "set-property": ["elements", "set", "app-wms", "properties.tier", "gold"],
    "set-refused": ["elements", "set", "app-erp", "id", "other"],
    "retire": ["elements", "retire", "app-erp"],
    "retire-unknown": ["elements", "retire", "ghost"],
    "remove": ["elements", "remove", "app-lonely", "--confirm", "app-lonely"],
    "remove-referenced": ["elements", "remove", "app-erp", "--confirm", "app-erp"],
    "remove-solo": ["elements", "remove", "solo-app", "--confirm", "solo-app"],
    "rel-remove": ["relations", "remove", "rel-wms-serves-erp", "--confirm", "rel-wms-serves-erp"],
    "rel-remove-wrong-confirm": ["relations", "remove", "rel-wms-serves-erp", "--confirm", "nope"],
    "new-dry": [*NEW_EL, *EVIDENCE_ASSUMED, "--dry-run"],
    "set-dry": ["elements", "set", "app-erp", "name", "Renamed", "--dry-run"],
}


@pytest.mark.parametrize("argv", SWEEP.values(), ids=SWEEP.keys())
def test_nothing_outside_staging_ever_changes(capsys, repo, argv):
    before = outside_staging(repo)
    _code, out, _err = run(capsys, *argv, "--json", root=repo)
    assert out.lstrip().startswith("{")  # the command ran and reported, whatever it decided
    assert outside_staging(repo) == before
    assert no_temp_files(repo)


# --------------------------------------------------------------------------- set: fields

SET_FIELDS = [
    ("name", "ERP Core Plus", "name"),
    ("documentation", "Order and invoice master data", "documentation"),
    ("owner", "erp-team@example.test", "owner"),
    ("lastReviewed", "2026-07-15", "lastReviewed"),
]


@pytest.mark.parametrize("field,value,key", SET_FIELDS, ids=[f[0] for f in SET_FIELDS])
def test_set_on_an_approved_element_stages_a_copy_and_changes_one_line(capsys, repo, field, value, key):
    approved_before = read(repo, "model/approved/application.yaml")
    doc = apply_edit(capsys, repo, "elements", "set", "app-erp", field, value)
    assert doc["action"] == "set" and doc["kind"] == "element" and doc["written"] is True
    assert doc["file"] == "model/staging/application.yaml"
    removed, added = line_diff(approved_before, read(repo, "model/staging/application.yaml"))
    assert len(removed) == 1 and removed[0].startswith(f"    {key}:")
    assert len(added) == 1 and added[0].startswith(f"    {key}: ")
    if field == "name":
        assert added[0].rstrip().endswith("# inline comment")
    attribute = {"lastReviewed": "last_reviewed"}.get(field, field)
    assert str(getattr(concept(repo, "app-erp"), attribute)) == value
    assert read(repo, "model/approved/application.yaml") == approved_before
    assert staged_files(repo) == ["model/staging/application.yaml"]


def test_set_text_output(capsys, repo):
    code, out, _err = run(capsys, "elements", "set", "app-erp", "name", "Renamed", root=repo)
    assert code == 0 and out.strip() == "changed element app-erp in model/staging/application.yaml"


def test_set_result_reads_back_and_validates(capsys, repo):
    apply_edit(capsys, repo, "elements", "set", "app-erp", "lastReviewed", "2026-07-15")
    assert concept(repo, "app-erp").last_reviewed == "2026-07-15"
    assert concept(repo, "app-erp", "approved").last_reviewed == "2026-06-30"
    assert run(capsys, "validate", "--zone", "staging", root=repo)[0] == 0


def test_set_only_touches_the_named_element(capsys, repo):
    apply_edit(capsys, repo, "elements", "set", "app-wms", "name", "Warehouse Plus")
    before = yaml_of(repo, "model/approved/application.yaml")["elements"]
    after = yaml_of(repo, "model/staging/application.yaml")["elements"]
    assert [e for e in after if e["id"] != "app-wms"] == [e for e in before if e["id"] != "app-wms"]
    assert next(e for e in after if e["id"] == "app-wms")["name"] == "Warehouse Plus"


def test_set_property_replaces_an_existing_key(capsys, repo):
    before = read(repo, "model/approved/application.yaml")
    apply_edit(capsys, repo, "elements", "set", "app-erp", "properties.lifecycle", "retiring")
    removed, added = line_diff(before, read(repo, "model/staging/application.yaml"))
    assert removed == ["      lifecycle: active\n"] and added == ["      lifecycle: retiring\n"]


def test_set_property_adds_a_new_key(capsys, repo):
    before = read(repo, "model/approved/application.yaml")
    apply_edit(capsys, repo, "elements", "set", "app-erp", "properties.costCentre", "CC-42")
    removed, added = line_diff(before, read(repo, "model/staging/application.yaml"))
    assert removed == [] and len(added) == 1 and "costCentre" in added[0] and added[0].startswith("      ")
    assert concept(repo, "app-erp").properties == {"lifecycle": "active", "hosting": "on-premise", "costCentre": "CC-42"}


def test_set_property_creates_the_properties_mapping(capsys, repo):
    before = read(repo, "model/approved/application.yaml")
    apply_edit(capsys, repo, "elements", "set", "app-wms", "properties.tier", "gold")
    removed, added = line_diff(before, read(repo, "model/staging/application.yaml"))
    assert removed == [] and len(added) == 2
    assert concept(repo, "app-wms").properties == {"tier": "gold"}


@pytest.mark.parametrize("value", ["a: b", "x # y", "it's fine", '"dq"', "123"])
def test_awkward_values_round_trip_exactly(capsys, repo, value):
    apply_edit(capsys, repo, "elements", "set", "app-erp", "properties.note", value)
    assert concept(repo, "app-erp").properties["note"] == value
    apply_edit(capsys, repo, "elements", "set", "app-erp", "documentation", value)
    assert concept(repo, "app-erp").documentation == value


def test_set_name_with_unicode(capsys, repo):
    apply_edit(capsys, repo, "elements", "set", "app-erp", "name", "Café Ünï: ERP")
    assert concept(repo, "app-erp").name == "Café Ünï: ERP"


SET_REFUSALS = {
    "id": ("id", "other-id"),
    "type": ("type", "Node"),
    "provenance": ("provenance", "x"),
    "standards": ("standards", "x"),
    "created-by": ("properties.createdBy", "Somebody"),
    "unknown-field": ("colour", "red"),
    "empty-property-key": ("properties.", "x"),
    "bare-properties": ("properties", "x"),
    "empty-name": ("name", ""),
    "date-format": ("lastReviewed", "30/06/2026"),
    "date-impossible": ("lastReviewed", "2026-13-45"),
    "empty-owner": ("owner", ""),
    "name-too-long": ("name", "x" * 121),
}


@pytest.mark.parametrize("field,value", SET_REFUSALS.values(), ids=SET_REFUSALS.keys())
def test_set_refusals_write_nothing(capsys, repo, field, value):
    refused(capsys, repo, "elements", "set", "app-erp", field, value)


@pytest.mark.parametrize("target", ["ghost", "rel-erp-realizes-api", "view-all"])
def test_set_on_something_that_is_not_an_element_is_refused(capsys, repo, target):
    refused(capsys, repo, "elements", "set", target, "name", "x")


def test_set_unknown_id_suggests_close_ids(capsys, repo):
    doc = refused(capsys, repo, "elements", "set", "app-er", "name", "x")
    assert "app-erp" in doc["error"]


def test_preexisting_errors_do_not_block_a_set(capsys, repo):
    write(repo, "model/approved/broken.yaml", yaml.safe_dump({"elements": [
        {"id": "no-evidence", "type": "Node", "name": "No Evidence", "owner": "o@example.test", "lastReviewed": "2026-06-30"}
    ]}))
    apply_edit(capsys, repo, "elements", "set", "app-erp", "name", "Renamed")


def test_set_text_mode_failure(capsys, repo):
    code, out, err = run(capsys, "elements", "set", "app-erp", "id", "other", root=repo)
    assert code == 1 and out == "" and err.startswith("error:")


# --------------------------------------------------------------------------- locating the element


def test_a_staging_only_element_is_edited_in_place(capsys, repo):
    stage_element(capsys, repo)
    before = read(repo, "model/staging/staged-app.yaml")
    doc = apply_edit(capsys, repo, "elements", "set", "staged-app", "documentation", "Now documented")
    assert doc["file"] == "model/staging/staged-app.yaml" and staged_files(repo) == ["model/staging/staged-app.yaml"]
    assert concept(repo, "staged-app").documentation == "Now documented"
    removed, added = line_diff(before, read(repo, "model/staging/staged-app.yaml"))
    assert removed == [] and len(added) == 1


def test_a_second_set_edits_the_staged_copy_not_the_approved_file(capsys, repo):
    apply_edit(capsys, repo, "elements", "set", "app-erp", "name", "First")
    first = read(repo, "model/staging/application.yaml")
    apply_edit(capsys, repo, "elements", "set", "app-erp", "documentation", "Second")
    removed, added = line_diff(first, read(repo, "model/staging/application.yaml"))
    assert len(removed) == 1 and len(added) == 1
    assert staged_files(repo) == ["model/staging/application.yaml"]
    assert "First" not in read(repo, "model/approved/application.yaml")


def test_nested_approved_paths_are_mirrored_in_staging(capsys, repo):
    doc = apply_edit(capsys, repo, "elements", "set", "node-srv", "name", "App Server 2")
    assert doc["file"] == "model/staging/layers/technology.yaml"
    removed, added = line_diff(read(repo, "model/approved/layers/technology.yaml"), read(repo, "model/staging/layers/technology.yaml"))
    assert len(removed) == 1 and len(added) == 1


def _staging_copy_elsewhere(repo: Path, element_id: str = "app-erp") -> None:
    approved = yaml_of(repo, "model/approved/application.yaml")["elements"]
    write(repo, "model/staging/elsewhere.yaml", yaml.safe_dump({"elements": [e for e in approved if e["id"] == element_id]}))


DIFFERENT_PATH = {
    "set": ["elements", "set", "app-erp", "name", "Renamed"],
    "retire": ["elements", "retire", "app-erp"],
    "remove": ["elements", "remove", "app-lonely", "--confirm", "app-lonely"],
}


@pytest.mark.parametrize("argv", DIFFERENT_PATH.values(), ids=DIFFERENT_PATH.keys())
def test_a_staging_copy_at_a_different_path_is_refused(capsys, repo, argv):
    _staging_copy_elsewhere(repo, argv[2])
    doc = refused(capsys, repo, *argv)
    assert "elsewhere.yaml" in doc["error"]


# --------------------------------------------------------------------------- surgical edits


def test_comments_and_every_other_line_survive_a_set(capsys, repo):
    before = read(repo, "model/approved/application.yaml")
    apply_edit(capsys, repo, "elements", "set", "app-wms", "name", "Warehouse Plus")
    after = read(repo, "model/staging/application.yaml")
    for comment in ("# Application layer -- comments must survive every edit.", "# The ERP.", "# The WMS (no properties, no documentation).", "# inline comment"):
        assert comment in after
    removed, added = line_diff(before, after)
    assert len(removed) == 1 and len(added) == 1


def test_set_keeps_crlf_line_endings(capsys, repo):
    crlf = APPLICATION_YAML.replace("\n", "\r\n")
    write(repo, "model/approved/application.yaml", crlf)
    apply_edit(capsys, repo, "elements", "set", "app-erp", "name", "ERP Plus")
    raw = (repo / "model" / "staging" / "application.yaml").read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")
    removed, added = line_diff(crlf, raw.decode())
    assert len(removed) == 1 and len(added) == 1 and added[0].endswith("\r\n")


def test_set_property_in_a_crlf_file_adds_crlf_lines(capsys, repo):
    write(repo, "model/approved/application.yaml", APPLICATION_YAML.replace("\n", "\r\n"))
    apply_edit(capsys, repo, "elements", "set", "app-wms", "properties.tier", "gold")
    raw = (repo / "model" / "staging" / "application.yaml").read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_retire_keeps_crlf_line_endings(capsys, repo):
    write(repo, "model/approved/application.yaml", APPLICATION_YAML.replace("\n", "\r\n"))
    apply_edit(capsys, repo, "elements", "retire", "app-erp")
    raw = (repo / "model" / "staging" / "application.yaml").read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_remove_keeps_crlf_line_endings(capsys, repo):
    write(repo, "model/approved/application.yaml", APPLICATION_YAML.replace("\n", "\r\n"))
    apply_edit(capsys, repo, "elements", "remove", "app-lonely", "--confirm", "app-lonely")
    raw = (repo / "model" / "staging" / "application.yaml").read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n") and b"app-lonely" not in raw


def test_a_file_without_a_final_newline_is_edited_without_adding_one(capsys, repo):
    text = APPLICATION_YAML.rstrip("\n")
    write(repo, "model/approved/application.yaml", text)
    apply_edit(capsys, repo, "elements", "set", "app-erp", "name", "ERP Plus")
    raw = read(repo, "model/staging/application.yaml")
    assert not raw.endswith("\n")
    removed, added = line_diff(text, raw)
    assert len(removed) == 1 and len(added) == 1


def test_staging_copy_is_byte_identical_except_the_edit(capsys, repo):
    before = read(repo, "model/approved/application.yaml")
    apply_edit(capsys, repo, "elements", "set", "app-lonely", "name", "Lonelier App")
    after = read(repo, "model/staging/application.yaml")
    assert after == before.replace("name: Lonely App", "name: Lonelier App")


UNSAFE = {
    "flow-element-block": ("elements:\n  - {id: app-x, type: Node, name: X, owner: o@example.test, lastReviewed: 2026-06-30, assumed: true, rationale: r}\n",
                           ["elements", "set", "app-x", "name", "Y"]),
    "anchor": ("base: &b\n  owner: o@example.test\nelements:\n  - id: app-x\n    type: Node\n    name: X\n    <<: *b\n    lastReviewed: 2026-06-30\n    assumed: true\n    rationale: r\n",
               ["elements", "set", "app-x", "name", "Y"]),
    "multiline-folded-on-edited-key": ("elements:\n  - id: app-x\n    type: Node\n    name: X\n    documentation: >\n      one\n      two\n    owner: o@example.test\n    lastReviewed: 2026-06-30\n    assumed: true\n    rationale: r\n",
                                       ["elements", "set", "app-x", "documentation", "Y"]),
    "multiline-literal-on-edited-key": ("elements:\n  - id: app-x\n    type: Node\n    name: X\n    documentation: |\n      one\n      two\n    owner: o@example.test\n    lastReviewed: 2026-06-30\n    assumed: true\n    rationale: r\n",
                                        ["elements", "set", "app-x", "documentation", "Y"]),
    "multiline-plain-on-edited-key": ("elements:\n  - id: app-x\n    type: Node\n    name: X\n      continued\n    owner: o@example.test\n    lastReviewed: 2026-06-30\n    assumed: true\n    rationale: r\n",
                                      ["elements", "set", "app-x", "name", "Y"]),
    "flow-properties": ("elements:\n  - id: app-x\n    type: Node\n    name: X\n    owner: o@example.test\n    lastReviewed: 2026-06-30\n    properties: {lifecycle: active}\n    assumed: true\n    rationale: r\n",
                        ["elements", "set", "app-x", "properties.lifecycle", "retiring"]),
    "flow-properties-retire": ("elements:\n  - id: app-x\n    type: Node\n    name: X\n    owner: o@example.test\n    lastReviewed: 2026-06-30\n    properties: {lifecycle: active}\n    assumed: true\n    rationale: r\n",
                               ["elements", "retire", "app-x"]),
    "flow-element-block-remove": ("elements:\n  - {id: app-x, type: Node, name: X, owner: o@example.test, lastReviewed: 2026-06-30, assumed: true, rationale: r}\n",
                                  ["elements", "remove", "app-x", "--confirm", "app-x"]),
    "quoted-key": ("elements:\n  - id: app-x\n    type: Node\n    \"name\": X\n    owner: o@example.test\n    lastReviewed: 2026-06-30\n    assumed: true\n    rationale: r\n",
                   ["elements", "set", "app-x", "name", "Y"]),
    "whole-file-flow": ("{elements: [{id: app-x, type: Node, name: X, owner: o@example.test, lastReviewed: 2026-06-30, assumed: true, rationale: r}]}\n",
                        ["elements", "set", "app-x", "name", "Y"]),
}


@pytest.mark.parametrize("text,argv", UNSAFE.values(), ids=UNSAFE.keys())
def test_anything_not_plainly_simple_yaml_is_refused(capsys, repo, text, argv):
    write(repo, "model/approved/unsafe.yaml", text)
    refused(capsys, repo, *argv)


def test_simple_edit_next_to_a_multiline_value_on_another_key_is_fine(capsys, repo):
    write(repo, "model/approved/multi.yaml",
          "elements:\n  - id: app-x\n    type: Node\n    name: X\n    documentation: >\n      one\n      two\n    owner: o@example.test\n"
          "    lastReviewed: 2026-06-30\n    assumed: true\n    rationale: r\n")
    apply_edit(capsys, repo, "elements", "set", "app-x", "name", "Y")
    assert "      one\n      two\n" in read(repo, "model/staging/multi.yaml")


def test_set_that_makes_the_model_invalid_is_refused(capsys, repo):
    stage_element(capsys, repo, extra=["--owner", "o@example.test", "--last-reviewed", "2026-07-15"])
    refused(capsys, repo, "elements", "set", "staged-app", "owner", "")


# --------------------------------------------------------------------------- retire


def test_retire_replaces_an_existing_lifecycle(capsys, repo):
    before = read(repo, "model/approved/application.yaml")
    doc = apply_edit(capsys, repo, "elements", "retire", "app-erp")
    assert doc["action"] == "retire" and doc["file"] == "model/staging/application.yaml" and doc["written"] is True
    removed, added = line_diff(before, read(repo, "model/staging/application.yaml"))
    assert removed == ["      lifecycle: active\n"] and added == ["      lifecycle: retired\n"]
    assert concept(repo, "app-erp").properties["hosting"] == "on-premise"


def test_retire_creates_the_properties_mapping(capsys, repo):
    apply_edit(capsys, repo, "elements", "retire", "app-wms")
    assert concept(repo, "app-wms").properties == {"lifecycle": "retired"}


def test_retire_adds_lifecycle_to_existing_properties(capsys, repo):
    stage_element(capsys, repo, extra=["--property", "hosting=cloud"])
    apply_edit(capsys, repo, "elements", "retire", "staged-app")
    assert concept(repo, "staged-app").properties == {"hosting": "cloud", "createdBy": "Sam Person", "lifecycle": "retired"}


def test_retire_text_output(capsys, repo):
    code, out, _err = run(capsys, "elements", "retire", "app-erp", root=repo)
    assert code == 0 and out.strip() == "retired element app-erp in model/staging/application.yaml"


def test_retire_keeps_the_element_and_its_relationships(capsys, repo):
    apply_edit(capsys, repo, "elements", "retire", "app-erp")
    model, _docs, _config = dsl.load_zone(repo, "staging")
    assert "app-erp" in model.elements and "rel-erp-realizes-api" in model.relationships
    assert run(capsys, "validate", "--zone", "staging", root=repo)[0] == 0


def test_retiring_an_already_retired_approved_element_is_a_noop(capsys, repo):
    write(repo, "model/approved/application.yaml", APPLICATION_YAML.replace("lifecycle: active\n      hosting", "lifecycle: retired\n      hosting"))
    before = snap(repo)
    code, doc = jrun(capsys, "elements", "retire", "app-erp", root=repo)
    assert code == 0 and doc["ok"] is True and doc["written"] is False
    assert snap(repo) == before


def test_retiring_twice_is_a_noop_the_second_time(capsys, repo):
    apply_edit(capsys, repo, "elements", "retire", "app-erp")
    before = snap(repo)
    code, doc = jrun(capsys, "elements", "retire", "app-erp", root=repo)
    assert code == 0 and doc["written"] is False and snap(repo) == before


@pytest.mark.parametrize("target", ["ghost", "rel-erp-realizes-api", "view-all"])
def test_retire_refuses_what_is_not_an_element(capsys, repo, target):
    refused(capsys, repo, "elements", "retire", target)


# --------------------------------------------------------------------------- remove: elements


def test_remove_a_staging_only_element_deletes_its_file(capsys, repo):
    stage_element(capsys, repo)
    write(repo, "model/staging/keep.yaml", "elements: []\n")
    doc = apply_edit(capsys, repo, "elements", "remove", "staged-app", "--confirm", "staged-app")
    assert doc["action"] == "remove" and doc["file"] == "model/staging/staged-app.yaml" and doc["written"] is True
    assert staged_files(repo) == ["model/staging/keep.yaml"] and no_temp_files(repo)
    assert concept(repo, "staged-app") is None


def test_remove_text_output(capsys, repo):
    code, out, _err = run(capsys, "elements", "remove", "app-lonely", "--confirm", "app-lonely", root=repo)
    assert code == 0 and out.strip() == "removed element app-lonely in model/staging/application.yaml"


def test_remove_one_of_two_elements_in_a_staging_file_keeps_the_file(capsys, repo):
    write(repo, "model/staging/pair.yaml", "# two\nelements:\n  # first\n  - id: one\n    type: Node\n    name: One\n    assumed: true\n    rationale: r\n"
                                           "  # second\n  - id: two\n    type: Node\n    name: Two\n    assumed: true\n    rationale: r\n")
    before = read(repo, "model/staging/pair.yaml")
    apply_edit(capsys, repo, "elements", "remove", "one", "--confirm", "one")
    after = read(repo, "model/staging/pair.yaml")
    assert [e["id"] for e in yaml.safe_load(after)["elements"]] == ["two"]
    assert is_subsequence(after.splitlines(keepends=True), before.splitlines(keepends=True))
    assert "# two" in after and "id: two" in after


def test_remove_of_an_approved_element_stages_a_copy_without_it(capsys, repo):
    approved_before = read(repo, "model/approved/application.yaml")
    doc = apply_edit(capsys, repo, "elements", "remove", "app-lonely", "--confirm", "app-lonely")
    assert doc["file"] == "model/staging/application.yaml"
    after = read(repo, "model/staging/application.yaml")
    assert [e["id"] for e in yaml.safe_load(after)["elements"]] == ["app-erp", "app-wms", "app-viewed"]
    assert is_subsequence(after.splitlines(keepends=True), approved_before.splitlines(keepends=True))
    assert read(repo, "model/approved/application.yaml") == approved_before
    assert "# The ERP." in after and "# inline comment" in after


def test_promote_dry_run_lists_the_removed_approved_element_as_dropped(capsys, repo):
    apply_edit(capsys, repo, "elements", "remove", "app-lonely", "--confirm", "app-lonely")
    before = digest(repo)
    code, out, _err = run(capsys, "promote", "--dry-run", root=repo)
    assert code == 0 and "concept(s): app-lonely" in out
    assert digest(repo) == before


def test_removing_the_last_approved_element_keeps_an_empty_list(capsys, repo):
    apply_edit(capsys, repo, "elements", "remove", "solo-app", "--confirm", "solo-app")
    assert yaml_of(repo, "model/staging/solo.yaml") == {"elements": []}
    assert "solo-app" in run(capsys, "promote", "--dry-run", root=repo)[1]
    assert read(repo, "model/approved/solo.yaml") == SOLO_YAML


def test_remove_on_a_staged_copy_of_an_approved_file_keeps_the_copy(capsys, repo):
    apply_edit(capsys, repo, "elements", "set", "solo-app", "name", "Solo Renamed")
    apply_edit(capsys, repo, "elements", "remove", "solo-app", "--confirm", "solo-app")
    assert yaml_of(repo, "model/staging/solo.yaml") == {"elements": []}
    assert concept(repo, "solo-app") is None


def test_remove_a_nested_approved_element_mirrors_the_path(capsys, repo):
    apply_edit(capsys, repo, "relations", "remove", "rel-node-serves-erp", "--confirm", "rel-node-serves-erp")
    doc = apply_edit(capsys, repo, "elements", "remove", "node-srv", "--confirm", "node-srv")
    assert doc["file"] == "model/staging/layers/technology.yaml"
    out = run(capsys, "promote", "--dry-run", root=repo)[1]
    assert "node-srv" in out and "rel-node-serves-erp" in out


def test_remove_after_validate_has_no_new_errors(capsys, repo):
    apply_edit(capsys, repo, "elements", "remove", "app-lonely", "--confirm", "app-lonely")
    assert run(capsys, "validate", "--zone", "staging", root=repo)[0] == 0


REMOVE_REFUSALS = {
    "unknown-id": ["elements", "remove", "ghost", "--confirm", "ghost"],
    "wrong-confirm": ["elements", "remove", "app-lonely", "--confirm", "app-erp"],
    "typo-confirm": ["elements", "remove", "app-lonely", "--confirm", "app-lonel"],
    "case-confirm": ["elements", "remove", "app-lonely", "--confirm", "App-Lonely"],
    "empty-confirm": ["elements", "remove", "app-lonely", "--confirm", ""],
    "source-of-a-relationship": ["elements", "remove", "node-srv", "--confirm", "node-srv"],
    "target-of-a-relationship": ["elements", "remove", "cap-orders", "--confirm", "cap-orders"],
    "both-ends": ["elements", "remove", "app-erp", "--confirm", "app-erp"],
    "relationship-id": ["elements", "remove", "rel-wms-serves-erp", "--confirm", "rel-wms-serves-erp"],
    "view-id": ["elements", "remove", "view-all", "--confirm", "view-all"],
}


@pytest.mark.parametrize("argv", REMOVE_REFUSALS.values(), ids=REMOVE_REFUSALS.keys())
def test_remove_refusals_write_nothing(capsys, repo, argv):
    refused(capsys, repo, *argv)


def test_remove_without_confirm_is_refused_or_a_usage_error(capsys, repo):
    before = snap(repo)
    code, _out, err = run(capsys, "elements", "remove", "app-lonely", root=repo)
    assert code in (1, 2) and "invalid choice" not in err and snap(repo) == before


def test_remove_refused_for_a_relationship_names_it(capsys, repo):
    doc = refused(capsys, repo, "elements", "remove", "node-srv", "--confirm", "node-srv")
    assert "rel-node-serves-erp" in doc["error"]


def test_remove_wrong_confirm_mentions_the_flag(capsys, repo):
    doc = refused(capsys, repo, "elements", "remove", "app-lonely", "--confirm", "other")
    assert "--confirm" in doc["error"]


def test_remove_of_an_element_in_a_view_is_refused_by_the_validator(capsys, repo):
    doc = refused(capsys, repo, "elements", "remove", "app-viewed", "--confirm", "app-viewed")
    assert "REF002" in codes(doc)


def test_a_staged_relationship_blocks_removing_its_element(capsys, repo):
    element_id = stage_element(capsys, repo)
    code, rel_doc = rnew(capsys, repo, "Serving", element_id, "app-erp")
    assert code == 0
    refused(capsys, repo, "elements", "remove", element_id, "--confirm", element_id)
    apply_edit(capsys, repo, "relations", "remove", rel_doc["id"], "--confirm", rel_doc["id"])
    apply_edit(capsys, repo, "elements", "remove", element_id, "--confirm", element_id)
    assert staged_files(repo) == [] and no_temp_files(repo)


def test_a_staged_removal_of_a_relationship_unblocks_removing_its_element(capsys, repo):
    refused(capsys, repo, "elements", "remove", "node-srv", "--confirm", "node-srv")
    apply_edit(capsys, repo, "relations", "remove", "rel-node-serves-erp", "--confirm", "rel-node-serves-erp")
    apply_edit(capsys, repo, "elements", "remove", "node-srv", "--confirm", "node-srv")
    assert concept(repo, "node-srv") is None and concept(repo, "rel-node-serves-erp") is None
    assert run(capsys, "validate", "--zone", "staging", root=repo)[0] == 0


# --------------------------------------------------------------------------- remove: relationships


def test_remove_a_staging_only_relationship_deletes_its_file(capsys, repo):
    code, rel_doc = rnew(capsys, repo)
    doc = apply_edit(capsys, repo, "relations", "remove", rel_doc["id"], "--confirm", rel_doc["id"])
    assert (doc["action"], doc["kind"]) == ("remove", "relationship") and doc["file"] == rel_doc["file"]
    assert staged_files(repo) == [] and concept(repo, rel_doc["id"]) is None


def test_remove_an_approved_relationship_stages_a_copy_without_it(capsys, repo):
    approved_before = read(repo, "model/approved/relations.yaml")
    doc = apply_edit(capsys, repo, "relations", "remove", "rel-wms-serves-erp", "--confirm", "rel-wms-serves-erp")
    assert doc["file"] == "model/staging/relations.yaml"
    after = read(repo, "model/staging/relations.yaml")
    ids = [r["id"] for r in yaml.safe_load(after)["relationships"]]
    assert "rel-wms-serves-erp" not in ids and len(ids) == 6
    assert is_subsequence(after.splitlines(keepends=True), approved_before.splitlines(keepends=True))
    assert read(repo, "model/approved/relations.yaml") == approved_before
    assert "rel-wms-serves-erp" in run(capsys, "promote", "--dry-run", root=repo)[1]


def test_relations_remove_text_output(capsys, repo):
    code, out, _err = run(capsys, "relations", "remove", "rel-wms-serves-erp", "--confirm", "rel-wms-serves-erp", root=repo)
    assert code == 0 and out.strip() == "removed relationship rel-wms-serves-erp in model/staging/relations.yaml"


RELATION_REMOVE_REFUSALS = {
    "unknown": ["relations", "remove", "rel-ghost", "--confirm", "rel-ghost"],
    "wrong-confirm": ["relations", "remove", "rel-wms-serves-erp", "--confirm", "rel-node-serves-erp"],
    "element-id": ["relations", "remove", "app-erp", "--confirm", "app-erp"],
}


@pytest.mark.parametrize("argv", RELATION_REMOVE_REFUSALS.values(), ids=RELATION_REMOVE_REFUSALS.keys())
def test_relations_remove_refusals_write_nothing(capsys, repo, argv):
    refused(capsys, repo, *argv)


def test_removing_every_relationship_of_a_file_keeps_an_empty_staged_copy(capsys, repo):
    for rel_id in [r["id"] for r in yaml_of(repo, "model/approved/relations.yaml")["relationships"]]:
        apply_edit(capsys, repo, "relations", "remove", rel_id, "--confirm", rel_id)
    assert yaml_of(repo, "model/staging/relations.yaml") == {"relationships": []}


# --------------------------------------------------------------------------- failure documents


@pytest.mark.parametrize("argv", [
    ["elements", "set", "ghost", "name", "x"],
    ["elements", "retire", "ghost"],
    ["elements", "remove", "ghost", "--confirm", "ghost"],
    ["relations", "remove", "ghost", "--confirm", "ghost"],
], ids=["set", "retire", "remove", "rel-remove"])
def test_unknown_id_failures_in_both_modes(capsys, repo, argv):
    doc = refused(capsys, repo, *argv)
    assert "ghost" in doc["error"] and "findings" not in doc
    before = snap(repo)
    code, out, err = run(capsys, *argv, root=repo)
    assert code == 1 and out == "" and err.startswith("error:") and "ghost" in err
    assert snap(repo) == before


def test_validation_refusal_json_carries_the_findings(capsys, repo):
    doc = refused(capsys, repo, *NEW_EL, "--source", SOURCE, "--quote", "Not in the file at all")
    errors = [f for f in doc["findings"] if f["severity"] == "error"]
    assert [f["code"] for f in errors] == ["PROV003"]
    finding = errors[0]
    assert finding["severity"] == "error" and finding["concept"] == "n" and finding["message"]


def test_success_findings_never_contain_errors(capsys, repo):
    code, doc = enew(capsys, repo)
    assert code == 0 and all(f["severity"] != "error" for f in doc["findings"])


# --------------------------------------------------------------------------- concurrent change

CONFLICTS = {
    "set": ["elements", "set", "staged-app", "name", "Mine"],
    "retire": ["elements", "retire", "staged-app"],
    "remove": ["elements", "remove", "staged-app", "--confirm", "staged-app"],
}


@pytest.mark.parametrize("argv", CONFLICTS.values(), ids=CONFLICTS.keys())
def test_a_file_changed_before_the_swap_is_refused(capsys, repo, monkeypatch, argv):
    stage_element(capsys, repo)
    path = repo / "model" / "staging" / "staged-app.yaml"
    seen = []

    def other_writer(target: Path) -> None:
        seen.append(target)
        target.write_bytes(target.read_bytes() + b"# written by someone else\n")

    monkeypatch.setattr(modelwrite, "_before_swap", other_writer)
    before = listing(repo)
    code, doc = jrun(capsys, *argv, root=repo)
    assert code == 1 and doc["ok"] is False and "changed on disk" in doc["error"]
    assert [Path(p).resolve() for p in seen] == [path.resolve()]
    assert path.read_bytes().endswith(b"# written by someone else\n") and b"Mine" not in path.read_bytes()
    assert listing(repo) == before and no_temp_files(repo)


def test_a_same_bytes_rewrite_is_not_a_conflict(capsys, repo, monkeypatch):
    stage_element(capsys, repo)
    path = repo / "model" / "staging" / "staged-app.yaml"
    monkeypatch.setattr(modelwrite, "_before_swap", lambda target: target.write_bytes(target.read_bytes()))
    code, _doc = jrun(capsys, "elements", "set", "staged-app", "name", "Mine", root=repo)
    assert code == 0 and b"name: Mine" in path.read_bytes()


def test_conflict_on_a_staged_copy_of_an_approved_file(capsys, repo, monkeypatch):
    apply_edit(capsys, repo, "elements", "set", "app-erp", "name", "First")
    path = repo / "model" / "staging" / "application.yaml"
    monkeypatch.setattr(modelwrite, "_before_swap", lambda target: target.write_bytes(target.read_bytes() + b"# other\n"))
    code, doc = jrun(capsys, "elements", "set", "app-erp", "name", "Second", root=repo)
    assert code == 1 and "changed on disk" in doc["error"]
    assert path.read_bytes().endswith(b"# other\n") and b"Second" not in path.read_bytes()


def test_the_hook_is_not_called_for_a_refusal(capsys, repo, monkeypatch):
    calls = []
    monkeypatch.setattr(modelwrite, "_before_swap", lambda target: calls.append(target))
    refused(capsys, repo, "elements", "set", "ghost", "name", "x")
    assert calls == []


# --------------------------------------------------------------------------- integration: example copy


def test_round_trip_new_element_and_relation_then_promote(capsys, example_root):
    ev = ["--source", "facts/sources/systems-inventory-2026-06.md", "--quote", "Customer-facing ordering web application"]
    code, doc = jrun(capsys, "elements", "new", "--type", "ApplicationComponent", "--name", "Loyalty Portal", *ev,
                     "--owner", "ecommerce@aurorafoods.example", "--last-reviewed", "2026-07-15", "--created-by", "Jesse Leresche", root=example_root)
    assert code == 0 and doc["id"] == "loyalty-portal" and own_warnings(doc, "loyalty-portal") == []
    code, rel_doc = jrun(capsys, "relations", "new", "--type", "Serving", "--from", "loyalty-portal", "--to", "actor-b2b-customer", *ev, root=example_root)
    assert code == 0
    assert run(capsys, "validate", root=example_root)[0] == 0
    assert run(capsys, "validate", "--zone", "staging", root=example_root)[0] == 0
    code, out, _err = run(capsys, "promote", root=example_root)
    assert code == 0
    assert (example_root / "model" / "approved" / "loyalty-portal.yaml").is_file()
    assert (example_root / "model" / "approved" / Path(rel_doc["file"]).name).is_file()
    assert staged_files(example_root) == []
    assert run(capsys, "validate", root=example_root)[0] == 0


def test_round_trip_set_on_an_approved_element_then_promote(capsys, example_root):
    path = example_root / "model" / "approved" / "application.yaml"
    before = path.read_text(encoding="utf-8")
    doc = apply_edit(capsys, example_root, "elements", "set", "app-erp-core", "documentation", "Order master data, invoicing and finance.")
    assert doc["file"] == "model/staging/application.yaml"
    assert path.read_text(encoding="utf-8") == before
    code, out, _err = run(capsys, "promote", "--dry-run", root=example_root)
    assert code == 0 and "model/approved/application.yaml" in out and "replaces the approved file" in out and "removes" not in out
    code, out, _err = run(capsys, "promote", root=example_root)
    assert code == 0
    after = path.read_text(encoding="utf-8")
    removed, added = line_diff(before, after)
    assert len(removed) == 1 and len(added) == 1 and "Order master data, invoicing and finance." in added[0]
    assert re.findall(r"^\s*#.*$", before, flags=re.M) == re.findall(r"^\s*#.*$", after, flags=re.M)
    assert run(capsys, "validate", root=example_root)[0] == 0


def test_round_trip_remove_an_approved_element_then_promote(capsys, example_root):
    model, _docs, _config = dsl.load_zone(example_root, "approved")
    victim = "req-po-retention"
    named_by_view = [v.id for v in model.views.values() if victim in v.include]
    assert not [r for r in model.relationships.values() if victim in (r.source, r.target)]
    if named_by_view:  # a human takes it out of the views first (setup only; the command would refuse REF002)
        views_path = example_root / "model" / "approved" / "views.yaml"
        views_path.write_text(re.sub(rf"^\s*- {victim}\n", "", views_path.read_text(encoding="utf-8"), flags=re.M), encoding="utf-8")
    apply_edit(capsys, example_root, "elements", "remove", victim, "--confirm", victim)
    code, out, _err = run(capsys, "promote", "--dry-run", root=example_root)
    assert code == 0 and f"concept(s): {victim}" in out
    assert run(capsys, "promote", root=example_root)[0] == 0
    assert concept(example_root, victim, "approved") is None
    assert run(capsys, "validate", root=example_root)[0] == 0


def test_round_trip_remove_with_relationships_first(capsys, example_root):
    victim = "gap-wms-hosting"
    model, _docs, _config = dsl.load_zone(example_root, "approved")
    rel_ids = sorted(r.id for r in model.relationships.values() if victim in (r.source, r.target))
    assert rel_ids
    refused(capsys, example_root, "elements", "remove", victim, "--confirm", victim)
    for rel_id in rel_ids:
        apply_edit(capsys, example_root, "relations", "remove", rel_id, "--confirm", rel_id)
    apply_edit(capsys, example_root, "elements", "remove", victim, "--confirm", victim)
    out = run(capsys, "promote", "--dry-run", root=example_root)[1]
    assert victim in out and all(rel_id in out for rel_id in rel_ids)
    assert run(capsys, "promote", root=example_root)[0] == 0
    assert run(capsys, "validate", root=example_root)[0] == 0


def test_commands_on_the_example_copy_leave_the_originals_alone(capsys, example_root):
    before = (_tree_hash(EXAMPLE), _tree_hash(GOLDEN))
    run(capsys, "elements", "retire", "app-wms", root=example_root)
    run(capsys, "elements", "set", "app-wms", "name", "X", root=example_root)
    assert (_tree_hash(EXAMPLE), _tree_hash(GOLDEN)) == before


# --------------------------------------------------------------------------- performance


def test_new_in_a_325_element_model_is_fast(capsys, tmp_path):
    types = ["ApplicationComponent", "ApplicationService", "BusinessProcess", "Node", "Capability"]
    ids = [f"e-{n:04d}" for n in range(325)]
    for chunk in range(5):
        items = [
            {"id": ids[n], "type": types[n % 5], "name": f"Element {n}", "owner": "o@example.test", "lastReviewed": "2026-06-30",
             "assumed": True, "rationale": "synthetic"}
            for n in range(chunk * 65, (chunk + 1) * 65)
        ]
        write(tmp_path, f"model/approved/part-{chunk}.yaml", yaml.safe_dump({"elements": items}, sort_keys=False))
    rels = [
        {"id": f"r-{n:04d}", "type": "Association", "source": ids[n % 325], "target": ids[(n * 7 + 1) % 325], "assumed": True, "rationale": "synthetic"}
        for n in range(400)
        if ids[n % 325] != ids[(n * 7 + 1) % 325]
    ]
    write(tmp_path, "model/approved/relations.yaml", yaml.safe_dump({"relationships": rels}, sort_keys=False))
    start = time.perf_counter()
    code, doc = enew(capsys, tmp_path, "Brand New", "Node")
    elapsed = time.perf_counter() - start
    assert code == 0 and elapsed < 5, (code, doc, elapsed)


# --------------------------------------------------------------------------- review follow-ups
# Found in an independent review of the first implementation; written red before the fixes.

NL = chr(10)


def test_a_trailing_newline_is_not_a_valid_id(capsys, repo):
    refused(capsys, repo, "elements", "new", "--type", "Node", "--name", "Nl", f"--id=nl-id{NL}", *EVIDENCE_ASSUMED)


@pytest.mark.parametrize("extra", [
    ["--property", f"k{NL}=v"],
    ["--property", "good=v", "--property", f"bad{NL}key=v"],
])
def test_a_trailing_newline_is_not_a_valid_property_key(capsys, repo, extra):
    before = snap(repo)
    code, _out, _err = run(capsys, "elements", "new", "--type", "Node", "--name", "Nl", *extra, *EVIDENCE_ASSUMED, root=repo)
    assert code == 2 and snap(repo) == before


def test_a_second_error_with_the_same_code_on_a_concept_is_still_new(capsys, repo):
    write(repo, "model/staging/v.yaml", (
        "elements:\n"
        "  - id: keep\n    type: Node\n    name: Keeper\n    assumed: true\n    rationale: r\n"
        "views:\n"
        "  - id: v-bad\n    name: Bad view\n    viewpoint: Layered\n    include: [keep, ghost]\n"
    ))
    doc = refused(capsys, repo, "elements", "remove", "keep", "--confirm", "keep")
    assert "REF002" in codes(doc)


def test_a_root_that_is_not_a_model_repository_is_refused_and_not_created(capsys, tmp_path):
    missing = tmp_path / "nowhere"
    code, doc = jrun(capsys, "elements", "new", "--type", "Node", "--name", "Zed", *EVIDENCE_ASSUMED, root=missing)
    assert code == 1 and doc["ok"] is False and not missing.exists()
    empty = tmp_path / "empty"
    empty.mkdir()
    code, doc = jrun(capsys, "elements", "new", "--type", "Node", "--name", "Zed", *EVIDENCE_ASSUMED, root=empty)
    assert code == 1 and doc["ok"] is False and list(empty.iterdir()) == []


def test_an_absolute_source_path_is_refused(capsys, repo):
    doc = refused(capsys, repo, "elements", "new", "--type", "Node", "--name", "Abs",
                  "--source", str(repo / SOURCE), "--quote", Q_PICK)
    assert "relative" in doc["error"]


def test_remove_keeps_the_comment_above_the_block_and_leaves_no_residue(capsys, repo):
    write(repo, "model/staging/m.yaml", (
        "elements:\n"
        "  # Group heading\n"
        "  - id: first\n    type: Node\n    name: First\n    assumed: true\n    rationale: r\n"
        "    documentation: |\n      Text\n      # Heading inside doc\n"
        "  - id: second\n    type: Node\n    name: Second\n    assumed: true\n    rationale: r\n"
    ))
    code, _doc = jrun(capsys, "elements", "remove", "first", "--confirm", "first", root=repo)
    assert code == 0
    text = read(repo, "model/staging/m.yaml")
    assert "# Group heading" in text and "Heading inside doc" not in text and "id: second" in text
    assert yaml_of(repo, "model/staging/m.yaml")["elements"][0]["id"] == "second"


def test_an_explicit_bot_like_created_by_gets_its_own_message(capsys, repo):
    doc = refused(capsys, repo, "elements", "new", "--type", "Node", "--name", "Bot Made", "--created-by", "home-agent bot",
                  *EVIDENCE_ASSUMED)
    assert "--created-by must name a person" in doc["error"]
