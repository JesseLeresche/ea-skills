"""The read commands `elements list`, `elements show` and `relations list` (plan 001).

Everything goes through `cli.main([...])` against scratch repositories in `tmp_path` and a
copy of the example repository, so what is tested is what an operator types.

Contract decisions the implementation must follow
-------------------------------------------------
Where docs/plans/001-elements-read-commands.md is silent, these are the readings the tests use.

* Module `easkills.modelquery` has `register(subparsers)` and `suggest_ids(wanted, known)`.
  `cli.main` wires it like `decisions`: leaf parsers do `set_defaults(func=...)` and the cli
  calls `args.func(args) -> int`. Groups: `elements` (list, show) and `relations` (list).
* Every leaf has `--root` (default cwd) and boolean `--json`. `elements show` also accepts
  `--zone approved|staging` (default approved), so the staging overlay can be shown too.
* Exit codes: 0 the query ran (also zero matches); 1 `show` of a missing id or a read failure;
  2 argparse usage error. Usage errors include: `--zone` outside approved|staging, `--limit`
  below 1 or not an integer, `--property` without `=`, `show` without an id.
* Filters: repeated flag = any-of, different flags AND. `--property k=v` splits on the FIRST `=`
  and matches the exact string value. `--layer` is case-insensitive against
  `oracle.layer_of(type)`. `--type`, `--from`, `--to` are exact (case-sensitive).
  `--text` is a case-insensitive substring over id, name and documentation only.
* Order: rows sorted by id (plain string order), across both zones when staging overlays.
* A filter value that is not known gives exit 0 and a note that names the value and lists
  suggestions: `--type` is checked against the oracle's element (list) or relationship
  (relations) types, `--layer` against the layers the oracle knows, `--from`/`--to` against
  the ids of the loaded model's elements. A known value that merely matches nothing gives no note.
* `--limit N` keeps the first N rows after sorting; `total` is the match count before it.
* JSON documents (one per command, to stdout):
    elements list:  {"ok": true, "zone", "count", "total", "elements": [row...], "notes": [str...],
                     "skipped": [{"file", "reason"}...]}
      row: {"id", "type", "layer", "name", "zone"}
    relations list: same envelope with "relationships": [row...]
      row: {"id", "type", "source", "target", "zone"} plus "accessType", "isDirected",
      "modifier" ONLY when set (isDirected False counts as set).
    elements show:  {"ok": true, "zone", "kind": "element"|"relationship",
                     "element": {full element incl. "id","type","layer","name","documentation",
                                 "properties","provenance","zone"}   (kind element)
                     "relationship": {full relationship, same idea}  (kind relationship)
                     "relationships": {"outgoing": [row...], "incoming": [row...]}  (kind element only)
                     "decisions": [{"id","title","status"}...], "views": [{"id","name"}...],
                     "notes": [...], "skipped": [...]}
      Rows in outgoing/incoming are relations-list rows. For a relationship, "views" is [].
    errors (exit 1): {"ok": false, "error": str, "suggestions": [ids]}.
  `skipped` entries use the file path relative to the root with forward slashes.
* Text mode: one line per row on stdout, the first whitespace-separated token is the id, no
  header and no summary line (zero matches print nothing). Notes go to stderr as `note: ...`,
  skipped files as `skipped: <file>: <reason>`. A failing `show` prints `error: ...` to stderr
  and lists suggestions in the message.
* `suggest_ids(wanted, known) -> list[str]`: prefix matches, then substring
  matches (both case-sensitive, on `known` ids), each sorted. Only when both are empty, difflib
  close matches at cutoff 0.6 ordered by ratio descending then id. No duplicates, at most 5, `wanted` itself is not special-cased, empty `wanted` gives [], and the
  result does not depend on the order or type (list, set) of `known`.
* The unknown-scope errors of `impact` and `context` keep their text and, only when suggestions
  exist, append ". Did you mean: a, b?" (same helper, same ids as the elements of the model).
* Duplicate ids in a zone: the loader's first file (path order) wins; a note names the id and
  contains the word "duplicate" (any case).
* The records for `show` come from `records.load(decisions.RECORD_TYPE, root)`; a decision names
  an element in `relatedElements`.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import time
from pathlib import Path

import pytest
import yaml

from easkills import cli, contextpack, decisions, impact, modelquery, oracle, records

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "eval" / "example"


# --------------------------------------------------------------------------- helpers


def el(element_id: str, element_type: str = "ApplicationComponent", name: str | None = None, **extra) -> dict:
    return {
        "id": element_id,
        "type": element_type,
        "name": name if name is not None else element_id.replace("-", " ").title(),
        "owner": "someone@example.test",
        "lastReviewed": "2026-07-01",
        "assumed": True,
        "rationale": "fixture",
        **extra,
    }


def rel(rel_id: str, kind: str, source: str, target: str, **extra) -> dict:
    return {"id": rel_id, "type": kind, "source": source, "target": target, "assumed": True, "rationale": "fixture", **extra}


def put_model(root: Path, name: str, zone: str = "approved", elements=(), relationships=(), views=()) -> Path:
    doc: dict = {}
    if elements:
        doc["elements"] = list(elements)
    if relationships:
        doc["relationships"] = list(relationships)
    if views:
        doc["views"] = list(views)
    path = root / "model" / zone / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    return path


def put_decision(root: Path, record_id: str, related: list[str], title: str = "A decision", status: str = "accepted") -> Path:
    path = root / "governance-log" / "decisions" / f"{record_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "id": record_id,
        "title": title,
        "status": status,
        "date": "2026-01-02",
        "decision": "Do it",
        "rationale": "Because",
        "relatedElements": related,
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline="\n")
    return path


def seed(root: Path) -> Path:
    """Six elements over four layers, five relationships, two views, two decisions."""
    put_model(
        root,
        "model.yaml",
        elements=[
            el("app-erp", name="ERP Core", documentation="Order master data", properties={"lifecycle": "active", "hosting": "on-premise"}),
            el(
                "app-wms",
                name="Warehouse System",
                documentation="Runs picking",
                properties={"lifecycle": "retiring", "hosting": "cloud"},
                provenance=[{"file": "facts/x.md", "quote": "zebra-quote about stock"}],
            ),
            el("svc-api", "ApplicationService", name="Order API", documentation="Published by the ERP"),
            el("cap-orders", "Capability", name="Order Management"),
            el("node-srv", "Node", name="App Server", properties={"hosting": "on-premise"}),
            el("proc-fulfil", "BusinessProcess", name="Fulfilment", documentation="Uses ORDER data"),
        ],
        relationships=[
            rel("r-erp-realizes-cap", "Realization", "app-erp", "cap-orders"),
            rel("r-erp-realizes-api", "Realization", "app-erp", "svc-api"),
            rel("r-api-serves-proc", "Serving", "svc-api", "proc-fulfil"),
            rel("r-proc-accesses-erp", "Access", "proc-fulfil", "app-erp", accessType="Read", isDirected=True),
            rel("r-srv-serves-erp", "Serving", "node-srv", "app-erp", modifier="+"),
        ],
        views=[
            {"id": "view-all", "name": "Everything", "viewpoint": "Layered", "include": ["app-erp", "app-wms", "svc-api"]},
            {"id": "view-biz", "name": "Business", "viewpoint": "Layered", "include": ["proc-fulfil", "app-erp"]},
        ],
    )
    put_decision(root, "dec-1", ["app-erp", "svc-api"], title="Keep the API")
    put_decision(root, "dec-2", ["app-wms"], title="Retire the WMS", status="proposed")
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return seed(tmp_path / "repo")


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


def ids(doc: dict, key: str = "elements") -> list[str]:
    return [row["id"] for row in doc[key]]


def elements(capsys, root: Path, *flags: str) -> dict:
    code, doc = jrun(capsys, "elements", "list", *flags, root=root)
    assert code == 0
    return doc


def relations(capsys, root: Path, *flags: str) -> dict:
    code, doc = jrun(capsys, "relations", "list", *flags, root=root)
    assert code == 0
    return doc


def digest(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


ALL_ELEMENTS = ["app-erp", "app-wms", "cap-orders", "node-srv", "proc-fulfil", "svc-api"]
ALL_RELATIONS = [
    "r-api-serves-proc",
    "r-erp-realizes-api",
    "r-erp-realizes-cap",
    "r-proc-accesses-erp",
    "r-srv-serves-erp",
]


# --------------------------------------------------------------------------- wiring


def test_register_adds_both_groups():
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    modelquery.register(sub)
    assert {"elements", "relations"} <= set(sub.choices)


@pytest.mark.parametrize(
    "argv",
    [["elements", "list"], ["elements", "show", "x"], ["relations", "list"]],
)
def test_every_leaf_takes_root_and_json(argv):
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    modelquery.register(sub)
    args = parser.parse_args([*argv, "--root", "somewhere", "--json"])
    assert args.root == Path("somewhere") and args.as_json is True
    assert callable(args.func)
    assert parser.parse_args(argv).as_json is False


@pytest.mark.parametrize("argv", [["elements"], ["relations"], ["elements", "bogus"], ["relations", "show", "x"]])
def test_incomplete_groups_are_usage_errors(capsys, argv):
    assert run(capsys, *argv)[0] == 2


# --------------------------------------------------------------------------- elements list


def test_list_all_elements_sorted_with_row_shape(capsys, repo):
    doc = elements(capsys, repo)
    assert doc["ok"] is True and doc["zone"] == "approved"
    assert ids(doc) == ALL_ELEMENTS
    assert doc["count"] == doc["total"] == 6
    assert doc["notes"] == [] and doc["skipped"] == []
    erp = doc["elements"][0]
    assert erp == {"id": "app-erp", "type": "ApplicationComponent", "layer": "Application", "name": "ERP Core", "zone": "approved"}


def test_json_output_is_a_single_document(capsys, repo):
    _code, out, err = run(capsys, "elements", "list", "--json", root=repo)
    assert err == ""
    assert json.loads(out)["count"] == 6  # json.loads fails on anything but one document


def test_text_output_is_one_line_per_row(capsys, repo):
    code, out, err = run(capsys, "elements", "list", root=repo)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert [line.split()[0] for line in lines] == ALL_ELEMENTS
    assert "ERP Core" in lines[0] and "ApplicationComponent" in lines[0]


def test_row_layer_comes_from_the_oracle(capsys, repo):
    doc = elements(capsys, repo)
    for row in doc["elements"]:
        assert row["layer"] == oracle.layer_of(row["type"])


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (["--type", "ApplicationComponent"], ["app-erp", "app-wms"]),
        (["--type", "Capability"], ["cap-orders"]),
        (["--type", "Capability", "--type", "Node"], ["cap-orders", "node-srv"]),
        (["--layer", "Application"], ["app-erp", "app-wms", "svc-api"]),
        (["--layer", "application"], ["app-erp", "app-wms", "svc-api"]),
        (["--layer", "TECHNOLOGY"], ["node-srv"]),
        (["--layer", "strategy", "--layer", "business"], ["cap-orders", "proc-fulfil"]),
        (["--property", "hosting=on-premise"], ["app-erp", "node-srv"]),
        (["--property", "hosting=On-Premise"], []),
        (["--property", "hosting=on"], []),
        (["--property", "hosting=on-premise", "--property", "hosting=cloud"], ["app-erp", "app-wms", "node-srv"]),
        (["--property", "hosting=on-premise", "--property", "lifecycle=active"], ["app-erp"]),
        (["--property", "nosuchkey=x"], []),
        (["--layer", "Application", "--type", "ApplicationService"], ["svc-api"]),
        (["--layer", "Technology", "--type", "ApplicationService"], []),
        (["--layer", "Application", "--property", "lifecycle=retiring"], ["app-wms"]),
    ],
)
def test_filters(capsys, repo, flags, expected):
    doc = elements(capsys, repo, *flags)
    assert ids(doc) == expected
    assert doc["count"] == doc["total"] == len(expected)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("erp", ["app-erp", "svc-api"]),  # id of app-erp, documentation of svc-api
        ("ERP", ["app-erp", "svc-api"]),
        ("warehouse", ["app-wms"]),  # name
        ("picking", ["app-wms"]),  # documentation
        ("order", ["app-erp", "cap-orders", "proc-fulfil", "svc-api"]),  # case-insensitive across fields
        ("zebra-quote", []),  # provenance quotes are not searched
        ("fulfil", ["proc-fulfil"]),
        ("no-such-text", []),
    ],
)
def test_text_filter(capsys, repo, query, expected):
    assert ids(elements(capsys, repo, "--text", query)) == expected


def test_property_value_may_contain_equals(capsys, tmp_path):
    put_model(tmp_path, "m.yaml", elements=[el("a", properties={"formula": "x=y"}), el("b", properties={"formula": "x"})])
    assert ids(elements(capsys, tmp_path, "--property", "formula=x=y")) == ["a"]


def test_text_combined_with_other_filters_is_an_and(capsys, repo):
    assert ids(elements(capsys, repo, "--text", "order", "--layer", "Application")) == ["app-erp", "svc-api"]


def test_limit_truncates_after_sorting_and_total_counts_the_matches(capsys, repo):
    doc = elements(capsys, repo, "--limit", "2")
    assert ids(doc) == ["app-erp", "app-wms"]
    assert doc["count"] == 2 and doc["total"] == 6


def test_limit_larger_than_matches_changes_nothing(capsys, repo):
    doc = elements(capsys, repo, "--limit", "100")
    assert doc["count"] == doc["total"] == 6


def test_limit_applies_after_filtering(capsys, repo):
    doc = elements(capsys, repo, "--layer", "Application", "--limit", "1")
    assert ids(doc) == ["app-erp"] and doc["total"] == 3


def test_limit_in_text_mode(capsys, repo):
    _code, out, _err = run(capsys, "elements", "list", "--limit", "3", root=repo)
    assert len(out.splitlines()) == 3


def test_zero_matches_is_exit_zero_and_empty(capsys, repo):
    code, out, _ = run(capsys, "elements", "list", "--text", "qqq", root=repo)
    assert (code, out) == (0, "")
    doc = elements(capsys, repo, "--text", "qqq")
    assert doc["count"] == doc["total"] == 0 and doc["elements"] == []


def test_empty_model_lists_nothing(capsys, tmp_path):
    doc = elements(capsys, tmp_path)
    assert doc["elements"] == [] and doc["total"] == 0 and doc["skipped"] == []


@pytest.mark.parametrize(
    "flags",
    [
        ["--zone", "bogus"],
        ["--limit", "0"],
        ["--limit", "-1"],
        ["--limit", "many"],
        ["--property", "novalue"],
        ["--bogus-flag"],
        ["--type"],
    ],
)
def test_usage_errors_exit_two(capsys, repo, flags):
    assert run(capsys, "elements", "list", *flags, root=repo)[0] == 2


@pytest.mark.parametrize(
    ("flags", "wanted", "suggestion"),
    [
        (["--type", "Nonsense"], "Nonsense", None),
        (["--type", "ApplicationComponnt"], "ApplicationComponnt", "ApplicationComponent"),
        (["--layer", "Nonsense"], "Nonsense", None),
        (["--layer", "Applicaton"], "Applicaton", "Application"),
    ],
)
def test_unknown_filter_value_is_a_note_not_an_error(capsys, repo, flags, wanted, suggestion):
    doc = elements(capsys, repo, *flags)
    assert doc["count"] == 0
    assert len(doc["notes"]) == 1 and wanted in doc["notes"][0]
    if suggestion:
        assert suggestion in doc["notes"][0]


def test_notes_go_to_stderr_in_text_mode(capsys, repo):
    code, out, err = run(capsys, "elements", "list", "--type", "Nonsense", root=repo)
    assert code == 0 and out == ""
    assert err.startswith("note: ") and "Nonsense" in err


def test_known_filter_values_that_match_nothing_give_no_note(capsys, repo):
    doc = elements(capsys, repo, "--type", "Goal", "--layer", "Motivation")
    assert doc["count"] == 0 and doc["notes"] == []


def test_a_known_and_an_unknown_type_still_match_the_known(capsys, repo):
    doc = elements(capsys, repo, "--type", "Capability", "--type", "Nonsense")
    assert ids(doc) == ["cap-orders"] and len(doc["notes"]) == 1


# --------------------------------------------------------------------------- determinism


def test_output_is_identical_across_runs(capsys, repo):
    first = run(capsys, "elements", "list", "--json", root=repo)
    second = run(capsys, "elements", "list", "--json", root=repo)
    assert first == second


def test_order_does_not_depend_on_file_creation_order(capsys, tmp_path):
    names = [f"e-{c}" for c in "dbfcae"]
    left, right = tmp_path / "left", tmp_path / "right"
    for root, order in ((left, names), (right, list(reversed(names)))):
        for index, name in enumerate(order):
            put_model(root, f"{index}-{name}.yaml", elements=[el(name)])
    a = elements(capsys, left)
    b = elements(capsys, right)
    assert ids(a) == ids(b) == sorted(names)


def test_order_is_by_id_within_one_file_regardless_of_declaration(capsys, tmp_path):
    put_model(tmp_path, "m.yaml", elements=[el("zz"), el("aa"), el("mm")])
    assert ids(elements(capsys, tmp_path)) == ["aa", "mm", "zz"]


def test_relations_order_is_by_id(capsys, tmp_path):
    put_model(
        tmp_path,
        "m.yaml",
        elements=[el("a"), el("b")],
        relationships=[rel("r3", "Serving", "a", "b"), rel("r1", "Serving", "a", "b"), rel("r2", "Serving", "b", "a")],
    )
    assert ids(relations(capsys, tmp_path), "relationships") == ["r1", "r2", "r3"]


# --------------------------------------------------------------------------- relations list


def test_relations_list_all_with_row_shape(capsys, repo):
    doc = relations(capsys, repo)
    assert doc["ok"] is True and doc["zone"] == "approved"
    assert ids(doc, "relationships") == ALL_RELATIONS
    assert doc["count"] == doc["total"] == 5 and doc["notes"] == [] and doc["skipped"] == []
    plain = doc["relationships"][0]
    assert plain == {
        "id": "r-api-serves-proc",
        "type": "Serving",
        "source": "svc-api",
        "target": "proc-fulfil",
        "zone": "approved",
    }


def test_relations_optional_fields_only_when_set(capsys, repo):
    by_id = {row["id"]: row for row in relations(capsys, repo)["relationships"]}
    access = by_id["r-proc-accesses-erp"]
    assert access["accessType"] == "Read" and access["isDirected"] is True and "modifier" not in access
    assert by_id["r-srv-serves-erp"]["modifier"] == "+"
    assert "accessType" not in by_id["r-srv-serves-erp"] and "isDirected" not in by_id["r-srv-serves-erp"]
    assert not {"accessType", "isDirected", "modifier"} & set(by_id["r-erp-realizes-cap"])


def test_relations_is_directed_false_counts_as_set(capsys, tmp_path):
    put_model(tmp_path, "m.yaml", elements=[el("a"), el("b")], relationships=[rel("r", "Association", "a", "b", isDirected=False)])
    row = relations(capsys, tmp_path)["relationships"][0]
    assert row["isDirected"] is False


def test_relations_text_output(capsys, repo):
    code, out, _ = run(capsys, "relations", "list", root=repo)
    assert code == 0
    lines = out.splitlines()
    assert [line.split()[0] for line in lines] == ALL_RELATIONS
    assert "app-erp" in lines[1] and "cap-orders" not in lines[1] and "svc-api" in lines[1]


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (["--type", "Serving"], ["r-api-serves-proc", "r-srv-serves-erp"]),
        (["--type", "Serving", "--type", "Access"], ["r-api-serves-proc", "r-proc-accesses-erp", "r-srv-serves-erp"]),
        (["--from", "app-erp"], ["r-erp-realizes-api", "r-erp-realizes-cap"]),
        (["--from", "app-erp", "--from", "node-srv"], ["r-erp-realizes-api", "r-erp-realizes-cap", "r-srv-serves-erp"]),
        (["--to", "app-erp"], ["r-proc-accesses-erp", "r-srv-serves-erp"]),
        (["--to", "svc-api", "--to", "cap-orders"], ["r-erp-realizes-api", "r-erp-realizes-cap"]),
        (["--from", "app-erp", "--to", "svc-api"], ["r-erp-realizes-api"]),
        (["--from", "app-erp", "--to", "node-srv"], []),
        (["--type", "Realization", "--from", "app-erp", "--to", "cap-orders"], ["r-erp-realizes-cap"]),
        (["--type", "Serving", "--from", "app-erp"], []),
        (["--from", "app-erp-core"], []),
    ],
)
def test_relations_filters(capsys, repo, flags, expected):
    doc = relations(capsys, repo, *flags)
    assert ids(doc, "relationships") == expected
    assert doc["total"] == len(expected)


def test_relations_limit_and_total(capsys, repo):
    doc = relations(capsys, repo, "--limit", "2")
    assert ids(doc, "relationships") == ALL_RELATIONS[:2]
    assert doc["count"] == 2 and doc["total"] == 5


@pytest.mark.parametrize("flags", [["--zone", "x"], ["--limit", "0"], ["--limit", "x"], ["--layer", "Business"], ["--text", "q"]])
def test_relations_usage_errors_exit_two(capsys, repo, flags):
    assert run(capsys, "relations", "list", *flags, root=repo)[0] == 2


@pytest.mark.parametrize(
    ("flags", "wanted"),
    [(["--type", "Nonsense"], "Nonsense"), (["--from", "nope-nope"], "nope-nope"), (["--to", "nope-nope"], "nope-nope")],
)
def test_relations_unknown_filter_value_is_a_note(capsys, repo, flags, wanted):
    doc = relations(capsys, repo, *flags)
    assert doc["count"] == 0 and len(doc["notes"]) == 1 and wanted in doc["notes"][0]


def test_relations_unknown_endpoint_suggests_close_ids(capsys, repo):
    doc = relations(capsys, repo, "--from", "app-er")
    assert "app-erp" in doc["notes"][0]
    assert "app-erp" in doc["notes"][0] and doc["count"] == 0  # a prefix is never auto-resolved


def test_relations_unknown_type_suggests_close_types(capsys, repo):
    doc = relations(capsys, repo, "--type", "Realisation")
    assert "Realization" in doc["notes"][0]


def test_relations_empty_model(capsys, tmp_path):
    doc = relations(capsys, tmp_path)
    assert doc["relationships"] == [] and doc["total"] == 0


# --------------------------------------------------------------------------- skipped files


def test_a_broken_file_is_listed_not_silent(capsys, repo):
    bad = repo / "model" / "approved" / "broken.yaml"
    bad.write_text("elements: [unclosed\n  - : :\n", encoding="utf-8")
    doc = elements(capsys, repo)
    assert ids(doc) == ALL_ELEMENTS  # the good file still loads
    assert [s["file"] for s in doc["skipped"]] == ["model/approved/broken.yaml"]
    assert doc["skipped"][0]["reason"]


def test_a_non_mapping_file_is_listed(capsys, repo):
    (repo / "model" / "approved" / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")
    doc = relations(capsys, repo)
    assert [s["file"] for s in doc["skipped"]] == ["model/approved/list.yaml"]


def test_skipped_in_text_mode_goes_to_stderr(capsys, repo):
    (repo / "model" / "approved" / "broken.yaml").write_text("a: [\n", encoding="utf-8")
    code, out, err = run(capsys, "elements", "list", root=repo)
    assert code == 0 and len(out.splitlines()) == 6
    assert "skipped: model/approved/broken.yaml" in err


def test_broken_file_is_exit_zero_for_show_too(capsys, repo):
    (repo / "model" / "approved" / "broken.yaml").write_text("a: [\n", encoding="utf-8")
    code, doc = jrun(capsys, "elements", "show", "app-erp", root=repo)
    assert code == 0 and [s["file"] for s in doc["skipped"]] == ["model/approved/broken.yaml"]


def test_skipped_lists_every_broken_file_sorted(capsys, repo):
    for name in ("z.yaml", "a.yaml"):
        (repo / "model" / "approved" / name).write_text("x: [\n", encoding="utf-8")
    doc = elements(capsys, repo)
    assert [s["file"] for s in doc["skipped"]] == ["model/approved/a.yaml", "model/approved/z.yaml"]


# --------------------------------------------------------------------------- zones


def stage(root: Path) -> None:
    """A staging delta: one new element, one replacing app-wms, one new relationship."""
    put_model(
        root,
        "delta.yaml",
        zone="staging",
        elements=[el("app-new", name="Brand New"), el("app-wms", name="Warehouse System v2", documentation="Replaced")],
        relationships=[rel("r-new-serves-erp", "Serving", "app-new", "app-erp")],
    )


def test_approved_is_the_default_zone_and_ignores_staging(capsys, repo):
    stage(repo)
    doc = elements(capsys, repo)
    assert ids(doc) == ALL_ELEMENTS and {r["zone"] for r in doc["elements"]} == {"approved"}
    assert len(relations(capsys, repo)["relationships"]) == 5


def test_explicit_approved_zone_matches_the_default(capsys, repo):
    stage(repo)
    assert elements(capsys, repo, "--zone", "approved") == elements(capsys, repo)


def test_staging_overlay_adds_new_and_replaces_existing(capsys, repo):
    stage(repo)
    doc = elements(capsys, repo, "--zone", "staging")
    assert doc["zone"] == "staging"
    assert ids(doc) == sorted([*ALL_ELEMENTS, "app-new"])
    zones = {row["id"]: row["zone"] for row in doc["elements"]}
    assert zones["app-new"] == "staging" and zones["app-wms"] == "staging"
    assert {zones[i] for i in ALL_ELEMENTS if i != "app-wms"} == {"approved"}
    names = {row["id"]: row["name"] for row in doc["elements"]}
    assert names["app-wms"] == "Warehouse System v2"


def test_staging_replacement_changes_what_text_filter_sees(capsys, repo):
    stage(repo)
    assert ids(elements(capsys, repo, "--zone", "staging", "--text", "replaced")) == ["app-wms"]
    assert ids(elements(capsys, repo, "--text", "replaced")) == []


def test_staging_relationships_overlay(capsys, repo):
    stage(repo)
    doc = relations(capsys, repo, "--zone", "staging")
    assert ids(doc, "relationships") == sorted([*ALL_RELATIONS, "r-new-serves-erp"])
    zones = {row["id"]: row["zone"] for row in doc["relationships"]}
    assert zones["r-new-serves-erp"] == "staging" and zones["r-api-serves-proc"] == "approved"


def test_staging_replaces_a_relationship_by_id(capsys, repo):
    put_model(repo, "d.yaml", zone="staging", relationships=[rel("r-srv-serves-erp", "Association", "node-srv", "app-erp")])
    doc = relations(capsys, repo, "--zone", "staging")
    row = next(r for r in doc["relationships"] if r["id"] == "r-srv-serves-erp")
    assert row["type"] == "Association" and row["zone"] == "staging" and "modifier" not in row


def test_staging_file_with_the_same_relative_path_shadows_the_approved_file(capsys, repo):
    # Promotion replaces model/approved/model.yaml whole, so its other elements disappear.
    put_model(repo, "model.yaml", zone="staging", elements=[el("only-one")])
    doc = elements(capsys, repo, "--zone", "staging")
    assert ids(doc) == ["only-one"]
    assert ids(elements(capsys, repo)) == ALL_ELEMENTS


def test_staging_filters_see_the_merged_model(capsys, repo):
    stage(repo)
    doc = elements(capsys, repo, "--zone", "staging", "--type", "ApplicationComponent")
    assert ids(doc) == ["app-erp", "app-new", "app-wms"]
    doc = relations(capsys, repo, "--zone", "staging", "--from", "app-new")
    assert ids(doc, "relationships") == ["r-new-serves-erp"] and doc["notes"] == []


def test_staging_endpoints_are_known_to_the_from_filter(capsys, repo):
    stage(repo)
    # app-new exists only in staging: unknown in approved (note), known in staging (no note)
    assert relations(capsys, repo, "--from", "app-new")["notes"]
    assert relations(capsys, repo, "--zone", "staging", "--from", "app-new")["notes"] == []


def test_staging_zone_without_a_staging_directory_is_the_approved_model(capsys, repo):
    doc = elements(capsys, repo, "--zone", "staging")
    assert ids(doc) == ALL_ELEMENTS and {r["zone"] for r in doc["elements"]} == {"approved"}


def test_staging_text_mode_lists_the_union(capsys, repo):
    stage(repo)
    _code, out, _ = run(capsys, "elements", "list", "--zone", "staging", root=repo)
    assert [line.split()[0] for line in out.splitlines()] == sorted([*ALL_ELEMENTS, "app-new"])


def test_broken_staging_file_is_skipped_in_staging_only(capsys, repo):
    (repo / "model" / "staging").mkdir()
    (repo / "model" / "staging" / "bad.yaml").write_text("a: [\n", encoding="utf-8")
    assert elements(capsys, repo)["skipped"] == []
    assert [s["file"] for s in elements(capsys, repo, "--zone", "staging")["skipped"]] == ["model/staging/bad.yaml"]


# --------------------------------------------------------------------------- duplicates


def test_duplicate_id_first_file_wins_and_a_note_says_so(capsys, tmp_path):
    put_model(tmp_path, "a.yaml", elements=[el("dup", name="First")])
    put_model(tmp_path, "b.yaml", elements=[el("dup", name="Second"), el("other")])
    doc = elements(capsys, tmp_path)
    assert ids(doc) == ["dup", "other"]
    assert doc["elements"][0]["name"] == "First"
    assert len(doc["notes"]) == 1 and "dup" in doc["notes"][0] and "duplicate" in doc["notes"][0].lower()


def test_duplicate_relationship_id_is_noted(capsys, tmp_path):
    put_model(tmp_path, "a.yaml", elements=[el("x"), el("y")], relationships=[rel("r", "Serving", "x", "y")])
    put_model(tmp_path, "b.yaml", relationships=[rel("r", "Association", "y", "x")])
    doc = relations(capsys, tmp_path)
    assert doc["relationships"][0]["type"] == "Serving"
    assert any("r" in n and "duplicate" in n.lower() for n in doc["notes"])


def test_duplicate_note_goes_to_stderr_in_text_mode(capsys, tmp_path):
    put_model(tmp_path, "a.yaml", elements=[el("dup")])
    put_model(tmp_path, "b.yaml", elements=[el("dup")])
    _code, out, err = run(capsys, "elements", "list", root=tmp_path)
    assert out.split()[0] == "dup" and len(out.splitlines()) == 1
    assert "note:" in err and "duplicate" in err.lower()


# --------------------------------------------------------------------------- elements show


def test_show_element_json_full_record(capsys, repo):
    code, doc = jrun(capsys, "elements", "show", "app-wms", root=repo)
    assert code == 0 and doc["ok"] is True and doc["kind"] == "element" and doc["zone"] == "approved"
    element = doc["element"]
    assert element["id"] == "app-wms" and element["type"] == "ApplicationComponent"
    assert element["layer"] == "Application" and element["name"] == "Warehouse System"
    assert element["documentation"] == "Runs picking"
    assert element["properties"] == {"lifecycle": "retiring", "hosting": "cloud"}
    assert element["zone"] == "approved"
    assert [p["quote"] for p in element["provenance"]] == ["zebra-quote about stock"]  # show prints provenance


def test_show_relationships_in_and_out(capsys, repo):
    _code, doc = jrun(capsys, "elements", "show", "app-erp", root=repo)
    out = doc["relationships"]["outgoing"]
    inc = doc["relationships"]["incoming"]
    assert [r["id"] for r in out] == ["r-erp-realizes-api", "r-erp-realizes-cap"]
    assert [r["id"] for r in inc] == ["r-proc-accesses-erp", "r-srv-serves-erp"]
    assert out[0] == {"id": "r-erp-realizes-api", "type": "Realization", "source": "app-erp", "target": "svc-api", "zone": "approved"}
    assert inc[0]["accessType"] == "Read"


def test_show_element_without_relationships(capsys, repo):
    _code, doc = jrun(capsys, "elements", "show", "app-wms", root=repo)
    assert doc["relationships"] == {"outgoing": [], "incoming": []}


def test_show_lists_the_decisions_that_name_the_element(capsys, repo):
    _code, doc = jrun(capsys, "elements", "show", "app-erp", root=repo)
    assert doc["decisions"] == [{"id": "dec-1", "title": "Keep the API", "status": "accepted"}]
    _code, doc = jrun(capsys, "elements", "show", "app-wms", root=repo)
    assert doc["decisions"] == [{"id": "dec-2", "title": "Retire the WMS", "status": "proposed"}]
    _code, doc = jrun(capsys, "elements", "show", "node-srv", root=repo)
    assert doc["decisions"] == []


def test_show_decisions_sorted_by_id(capsys, repo):
    put_decision(repo, "dec-0", ["app-erp"], title="Earlier")
    _code, doc = jrun(capsys, "elements", "show", "app-erp", root=repo)
    assert [d["id"] for d in doc["decisions"]] == ["dec-0", "dec-1"]


def test_show_decisions_use_the_records_loader(capsys, repo):
    loaded = records.load(decisions.RECORD_TYPE, repo)
    assert {"dec-1", "dec-2"} <= set(loaded.records)
    _code, doc = jrun(capsys, "elements", "show", "svc-api", root=repo)
    assert [d["id"] for d in doc["decisions"]] == ["dec-1"]


def test_show_lists_the_views_that_include_the_element(capsys, repo):
    _code, doc = jrun(capsys, "elements", "show", "app-erp", root=repo)
    assert doc["views"] == [{"id": "view-all", "name": "Everything"}, {"id": "view-biz", "name": "Business"}]
    _code, doc = jrun(capsys, "elements", "show", "app-wms", root=repo)
    assert doc["views"] == [{"id": "view-all", "name": "Everything"}]
    _code, doc = jrun(capsys, "elements", "show", "node-srv", root=repo)
    assert doc["views"] == []


def test_show_text_mode_mentions_the_essentials(capsys, repo):
    code, out, err = run(capsys, "elements", "show", "app-erp", root=repo)
    assert code == 0 and err == ""
    for needle in ("app-erp", "ERP Core", "r-erp-realizes-api", "r-srv-serves-erp", "dec-1", "view-all", "view-biz"):
        assert needle in out
    assert "dec-2" not in out


def test_show_accepts_a_relationship_id(capsys, repo):
    code, doc = jrun(capsys, "elements", "show", "r-proc-accesses-erp", root=repo)
    assert code == 0 and doc["kind"] == "relationship"
    relationship = doc["relationship"]
    assert relationship["source"] == "proc-fulfil" and relationship["target"] == "app-erp"
    assert relationship["type"] == "Access" and relationship["accessType"] == "Read"
    assert "element" not in doc and doc["views"] == []


def test_show_relationship_lists_the_decisions_naming_it(capsys, repo):
    put_decision(repo, "dec-3", ["r-srv-serves-erp"], title="Keep the server")
    _code, doc = jrun(capsys, "elements", "show", "r-srv-serves-erp", root=repo)
    assert [d["id"] for d in doc["decisions"]] == ["dec-3"]


def test_show_relationship_in_text_mode(capsys, repo):
    code, out, _ = run(capsys, "elements", "show", "r-proc-accesses-erp", root=repo)
    assert code == 0 and "proc-fulfil" in out and "app-erp" in out and "Access" in out


def test_show_missing_id_exits_one_with_suggestions(capsys, repo):
    code, out, err = run(capsys, "elements", "show", "app-er", root=repo)
    assert code == 1 and out == ""
    assert err.startswith("error: ") and "app-er" in err and "app-erp" in err


def test_show_missing_id_json(capsys, repo):
    code, doc = jrun(capsys, "elements", "show", "app-er", root=repo)
    assert code == 1 and doc["ok"] is False and "app-er" in doc["error"]
    assert doc["suggestions"] == ["app-erp"]


def test_show_never_auto_resolves_a_unique_prefix(capsys, repo):
    code, doc = jrun(capsys, "elements", "show", "svc", root=repo)
    assert code == 1 and "element" not in doc and doc["suggestions"] == ["svc-api"]


def test_show_missing_id_without_suggestions(capsys, repo):
    code, doc = jrun(capsys, "elements", "show", "zzzzqqqq", root=repo)
    assert code == 1 and doc["suggestions"] == []


def test_show_is_case_sensitive(capsys, repo):
    assert jrun(capsys, "elements", "show", "APP-ERP", root=repo)[0] == 1


def test_show_requires_an_id(capsys, repo):
    assert run(capsys, "elements", "show", root=repo)[0] == 2


def test_show_on_an_empty_repo_is_a_failure_not_a_crash(capsys, tmp_path):
    code, doc = jrun(capsys, "elements", "show", "anything", root=tmp_path)
    assert code == 1 and doc["ok"] is False and doc["suggestions"] == []


def test_show_with_staging_replacement(capsys, repo):
    stage(repo)
    code, doc = jrun(capsys, "elements", "show", "app-wms", "--zone", "staging", root=repo)
    assert code == 0 and doc["zone"] == "staging"
    assert doc["element"]["name"] == "Warehouse System v2" and doc["element"]["zone"] == "staging"
    _code, approved = jrun(capsys, "elements", "show", "app-wms", root=repo)
    assert approved["element"]["name"] == "Warehouse System"


def test_show_a_new_staging_element_needs_the_staging_zone(capsys, repo):
    stage(repo)
    assert jrun(capsys, "elements", "show", "app-new", root=repo)[0] == 1
    code, doc = jrun(capsys, "elements", "show", "app-new", "--zone", "staging", root=repo)
    assert code == 0 and doc["element"]["zone"] == "staging"


def test_show_staging_relationships_include_the_staging_ones(capsys, repo):
    stage(repo)
    _code, doc = jrun(capsys, "elements", "show", "app-erp", "--zone", "staging", root=repo)
    incoming = {r["id"]: r["zone"] for r in doc["relationships"]["incoming"]}
    assert incoming["r-new-serves-erp"] == "staging" and incoming["r-srv-serves-erp"] == "approved"


# --------------------------------------------------------------------------- suggest_ids


def test_suggest_prefix_matches_sorted():
    known = ["app-wms", "app-erp-old", "cap-orders", "app-erp-core"]
    assert modelquery.suggest_ids("app-erp", known) == ["app-erp-core", "app-erp-old"]


def test_suggest_prefix_before_substring():
    assert modelquery.suggest_ids("erp", ["x-erp", "erp-a", "other"]) == ["erp-a", "x-erp"]


def test_suggest_substring_matches_sorted():
    assert modelquery.suggest_ids("erp", ["svc-erp-b", "app-erp-a", "wms"]) == ["app-erp-a", "svc-erp-b"]


def test_suggest_falls_back_to_difflib():
    assert modelquery.suggest_ids("app-wsm", ["app-wms", "zzz"]) == ["app-wms"]


def test_suggest_tier_order_prefix_then_substring():
    assert modelquery.suggest_ids("foo", ["fo0", "a-foo", "foo-b"]) == ["foo-b", "a-foo"]


def test_suggest_fuzzy_only_when_prefix_and_substring_find_nothing():
    assert modelquery.suggest_ids("foo", ["fo0", "bar"]) == ["fo0"]


def test_suggest_nothing_close_gives_empty_list():
    assert modelquery.suggest_ids("qqqqqq", ["app-erp", "app-wms"]) == []


def test_suggest_empty_known():
    assert modelquery.suggest_ids("app", []) == []


def test_suggest_empty_wanted():
    assert modelquery.suggest_ids("", ["app", "wms"]) == []


def test_suggest_caps_at_five():
    known = [f"app-{n}" for n in "hgfedcba"]
    assert modelquery.suggest_ids("app", known) == [f"app-{n}" for n in "abcde"]


def test_suggest_no_duplicates_across_tiers():
    assert modelquery.suggest_ids("app-erp", ["app-erp"]) == ["app-erp"]


def test_suggest_fuzzy_orders_by_ratio_then_id():
    result = modelquery.suggest_ids("order-api", ["order-apj", "order-ap1", "order-xpi"])
    assert result[:2] == ["order-ap1", "order-apj"] and set(result) <= {"order-apj", "order-ap1", "order-xpi"}


def test_suggest_is_deterministic_for_any_known_order():
    known = [f"app-{n}-core" for n in "abcdefgh"] + ["core-app", "apps", "ap"]
    expected = modelquery.suggest_ids("app-c", known)
    for seed_ in range(10):
        shuffled = known[:]
        random.Random(seed_).shuffle(shuffled)
        assert modelquery.suggest_ids("app-c", shuffled) == expected


def test_suggest_accepts_a_set_and_a_generator():
    known = {"app-erp", "app-wms"}
    assert modelquery.suggest_ids("app-e", known) == ["app-erp"]
    assert modelquery.suggest_ids("app-e", (k for k in sorted(known))) == ["app-erp"]


def test_suggest_returns_a_list():
    assert isinstance(modelquery.suggest_ids("a", ["abc"]), list)


def test_suggest_is_case_sensitive_for_prefix_and_substring():
    assert modelquery.suggest_ids("APP", ["app-erp"]) == []


# --------------------------------------------------------------------------- impact / context hints


def test_impact_unknown_scope_keeps_its_text_and_appends_a_hint(capsys, example_root):
    code = cli.main(["impact", "--root", str(example_root), "--scope", "app-erp-cor"])
    out = capsys.readouterr().out
    assert code == 1 and "ERROR" in out
    assert "'app-erp-cor' is not an element in the approved model" in out
    assert "Did you mean: app-erp-core?" in out


def test_impact_error_message_has_the_exact_suffix(example_root):
    with pytest.raises(impact.ImpactError) as caught:
        impact.analyse(example_root, "app-erp-cor")
    assert str(caught.value) == "'app-erp-cor' is not an element in the approved model. Did you mean: app-erp-core?"


def test_impact_hint_lists_several_suggestions_comma_separated(example_root):
    with pytest.raises(impact.ImpactError) as caught:
        impact.analyse(example_root, "app-")
    message = str(caught.value)
    assert message.startswith("'app-' is not an element in the approved model. Did you mean: ")
    listed = message.split("Did you mean: ", 1)[1].rstrip("?").split(", ")
    assert listed == sorted(listed) and "app-erp-core" in listed and len(listed) == 4


def test_impact_without_suggestions_appends_nothing(example_root):
    with pytest.raises(impact.ImpactError) as caught:
        impact.analyse(example_root, "zzzzqqqq")
    assert str(caught.value) == "'zzzzqqqq' is not an element in the approved model"


def test_impact_unknown_scope_without_suggestions_via_cli(capsys, example_root):
    assert cli.main(["impact", "--root", str(example_root), "--scope", "zzzzqqqq"]) == 1
    out = capsys.readouterr().out
    assert "is not an element in the approved model" in out and "Did you mean" not in out


def test_impact_hint_uses_the_requested_zone(repo):
    stage(repo)
    with pytest.raises(impact.ImpactError) as caught:
        impact.analyse(repo, "app-ne", zone="staging")
    assert str(caught.value).endswith("Did you mean: app-new?")
    with pytest.raises(impact.ImpactError) as caught:
        impact.analyse(repo, "app-ne")
    assert "app-new" not in str(caught.value)


def test_context_unknown_scope_keeps_its_text_and_appends_a_hint(capsys, example_root):
    code = cli.main(["context", "--root", str(example_root), "--scope", "app-erp-cor"])
    out = capsys.readouterr().out
    assert code == 1 and "'app-erp-cor' is not an element in the approved model" in out
    assert "Did you mean: app-erp-core?" in out


def test_context_error_message_has_the_exact_suffix(example_root):
    with pytest.raises(contextpack.ContextError) as caught:
        contextpack.build(example_root, "app-erp-cor")
    assert str(caught.value) == "'app-erp-cor' is not an element in the approved model. Did you mean: app-erp-core?"


def test_context_without_suggestions_appends_nothing(example_root):
    with pytest.raises(contextpack.ContextError) as caught:
        contextpack.build(example_root, "zzzzqqqq")
    assert str(caught.value) == "'zzzzqqqq' is not an element in the approved model"


def test_hint_suggestions_match_suggest_ids(example_root):
    from easkills import dsl

    model, _docs, _config = dsl.load(example_root, "approved")
    expected = ", ".join(modelquery.suggest_ids("cap-order", model.elements))
    with pytest.raises(impact.ImpactError) as caught:
        impact.analyse(example_root, "cap-order")
    assert str(caught.value).endswith(f"Did you mean: {expected}?")


# --------------------------------------------------------------------------- read-only

COMMANDS = [
    ["elements", "list"],
    ["elements", "list", "--json"],
    ["elements", "list", "--zone", "staging"],
    ["elements", "list", "--zone", "staging", "--json"],
    ["elements", "list", "--layer", "Application", "--type", "ApplicationComponent", "--limit", "2"],
    ["elements", "list", "--property", "lifecycle=active", "--text", "erp"],
    ["elements", "list", "--type", "Nonsense"],
    ["elements", "show", "app-erp-core"],
    ["elements", "show", "app-erp-core", "--json"],
    ["elements", "show", "rel-erp-realizes-order-management"],
    ["elements", "show", "missing-id", "--json"],
    ["elements", "show", "app-order-portal", "--zone", "staging"],
    ["relations", "list"],
    ["relations", "list", "--json"],
    ["relations", "list", "--zone", "staging", "--type", "Serving", "--from", "node-erp-app-server"],
    ["relations", "list", "--to", "app-erp-core", "--limit", "1"],
    ["relations", "list", "--from", "nope"],
    ["elements", "list", "--bogus"],
]


@pytest.mark.parametrize("argv", COMMANDS, ids=[" ".join(c) for c in COMMANDS])
def test_commands_never_write_anything(capsys, tmp_path, argv):
    root = tmp_path / "copy"
    shutil.copytree(EXAMPLE, root)
    stage(root)  # staging content and a broken file, so every code path has something to read
    (root / "model" / "approved" / "zz-broken.yaml").write_text("a: [\n", encoding="utf-8")
    before = digest(root)
    run(capsys, *argv, root=root)
    assert digest(root) == before


def test_read_only_digest_helper_notices_changes(tmp_path):
    (tmp_path / "f").write_text("a")
    before = digest(tmp_path)
    (tmp_path / "g").write_text("b")
    assert digest(tmp_path) != before


# --------------------------------------------------------------------------- the example model


def test_example_elements_list_json(capsys, example_root):
    code, doc = jrun(capsys, "elements", "list", root=example_root)
    assert code == 0
    assert doc["count"] == doc["total"] == 20 and doc["notes"] == [] and doc["skipped"] == []
    assert ids(doc) == sorted(ids(doc))
    assert {"app-erp-core", "app-order-portal", "cap-order-management", "service-order-api"} <= set(ids(doc))
    erp = next(r for r in doc["elements"] if r["id"] == "app-erp-core")
    assert erp == {"id": "app-erp-core", "type": "ApplicationComponent", "layer": "Application", "name": "ERP Core", "zone": "approved"}


def test_example_layer_counts(capsys, example_root):
    expected = {"Application": 5, "Business": 4, "Implementation": 3, "Motivation": 3, "Strategy": 3, "Technology": 2}
    for layer, count in expected.items():
        assert elements(capsys, example_root, "--layer", layer)["total"] == count


def test_example_property_filter(capsys, example_root):
    doc = elements(capsys, example_root, "--property", "hosting=on-premise")
    assert "app-erp-core" in ids(doc)


def test_example_relations_list_json(capsys, example_root):
    doc = relations(capsys, example_root)
    assert doc["count"] == doc["total"] == 19 and doc["notes"] == [] and doc["skipped"] == []
    assert ids(doc, "relationships") == sorted(ids(doc, "relationships"))
    kinds = {}
    for row in doc["relationships"]:
        kinds[row["type"]] = kinds.get(row["type"], 0) + 1
    assert kinds == {
        "Realization": 7,
        "Serving": 5,
        "Aggregation": 2,
        "Access": 2,
        "Triggering": 1,
        "Association": 1,
        "Composition": 1,
    }


def test_example_relations_touching_the_erp(capsys, example_root):
    outgoing = ids(relations(capsys, example_root, "--from", "app-erp-core"), "relationships")
    incoming = ids(relations(capsys, example_root, "--to", "app-erp-core"), "relationships")
    assert outgoing == ["rel-erp-accesses-order-record", "rel-erp-realizes-order-api", "rel-erp-realizes-order-management"]
    assert incoming == ["rel-erp-server-serves-erp"]


def test_example_show_app_erp_core(capsys, example_root):
    code, doc = jrun(capsys, "elements", "show", "app-erp-core", root=example_root)
    assert code == 0 and doc["element"]["name"] == "ERP Core"
    assert doc["element"]["properties"]["timeDisposition"] == "Tolerate"
    assert len(doc["relationships"]["outgoing"]) == 3 and len(doc["relationships"]["incoming"]) == 1
    assert [d["id"] for d in doc["decisions"]] == ["decision-order-api-single-integration"]
    assert [v["id"] for v in doc["views"]] == ["layered-overview", "capability-realization", "customer-service-gap", "retention-obligations"]


def test_example_show_a_relationship(capsys, example_root):
    code, doc = jrun(capsys, "elements", "show", "rel-erp-realizes-order-management", root=example_root)
    assert code == 0 and doc["kind"] == "relationship"
    assert (doc["relationship"]["source"], doc["relationship"]["target"]) == ("app-erp-core", "cap-order-management")


def test_example_show_missing_suggests(capsys, example_root):
    code, doc = jrun(capsys, "elements", "show", "app-erp", root=example_root)
    assert code == 1 and "app-erp-core" in doc["suggestions"]


def test_example_text_mode_runs(capsys, example_root):
    code, out, err = run(capsys, "elements", "list", root=example_root)
    assert code == 0 and err == "" and len(out.splitlines()) == 20
    code, out, err = run(capsys, "relations", "list", root=example_root)
    assert code == 0 and err == "" and len(out.splitlines()) == 19


def test_example_golden_outputs_are_untouched():
    """The new commands must not add or change anything under eval/golden."""
    golden = REPO_ROOT / "eval" / "golden"
    assert golden.is_dir()
    assert not [p for p in golden.rglob("*") if "modelquery" in p.name]


# --------------------------------------------------------------------------- performance


def test_a_325_element_model_lists_quickly(capsys, tmp_path):
    types = ["ApplicationComponent", "ApplicationService", "BusinessProcess", "Node", "Capability"]
    all_ids = [f"e-{n:04d}" for n in range(325)]
    for chunk in range(5):
        put_model(
            tmp_path,
            f"part-{chunk}.yaml",
            elements=[
                el(all_ids[n], types[n % 5], properties={"lifecycle": "active" if n % 2 else "retiring"})
                for n in range(chunk * 65, (chunk + 1) * 65)
            ],
        )
    put_model(
        tmp_path,
        "relations.yaml",
        relationships=[rel(f"r-{n:04d}", "Serving", all_ids[n % 325], all_ids[(n * 7 + 1) % 325]) for n in range(400)],
    )
    start = time.perf_counter()
    code, doc = jrun(capsys, "elements", "list", root=tmp_path)
    elapsed = time.perf_counter() - start
    assert code == 0 and doc["total"] == 325 and elapsed < 5
    start = time.perf_counter()
    code, doc = jrun(capsys, "relations", "list", "--from", all_ids[0], root=tmp_path)
    assert code == 0 and doc["total"] == 2 and time.perf_counter() - start < 5
    assert doc["notes"] == [] and doc["skipped"] == []
    doc = elements(capsys, tmp_path, "--property", "lifecycle=active", "--limit", "10")
    assert doc["total"] == 162 and doc["count"] == 10


# --------------------------------------------------------------------------- review follow-ups
# Found in review of the first implementation; written red before the fix.


def test_show_reports_an_unreadable_decision_file_as_skipped(capsys, repo):
    broken = repo / "governance-log" / "decisions" / "dec-broken.yaml"
    broken.write_text("id: dec-broken\ntitle: [unclosed\n", encoding="utf-8")
    code, doc = jrun(capsys, "elements", "show", "app-erp", root=repo)
    assert code == 0
    assert [item["file"] for item in doc["skipped"]] == ["governance-log/decisions/dec-broken.yaml"]
    assert [d["id"] for d in doc["decisions"]] == ["dec-1"]
    code, _out, err = run(capsys, "elements", "show", "app-erp", root=repo)
    assert code == 0 and "skipped: governance-log/decisions/dec-broken.yaml" in err


def test_show_has_no_skipped_when_every_decision_reads(capsys, repo):
    _code, doc = jrun(capsys, "elements", "show", "app-erp", root=repo)
    assert doc["skipped"] == []


def test_show_notes_an_id_that_is_both_an_element_and_a_relationship(capsys, repo):
    put_model(repo, "clash.yaml", relationships=[rel("app-wms", "Flow", "app-erp", "app-wms")])
    code, doc = jrun(capsys, "elements", "show", "app-wms", root=repo)
    assert code == 0 and doc["kind"] == "element"
    assert any("app-wms" in note and "relationship" in note for note in doc["notes"])
