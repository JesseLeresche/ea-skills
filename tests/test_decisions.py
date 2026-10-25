"""The `decisions` command group: the first adapter on the record-type-neutral core, end to end.

Everything goes through `cli.main([...])` against scratch repositories in `tmp_path`, so
what is tested is what an operator types. The core's own behaviour (editing, rendering,
loading) is covered on a fake record type in `test_records.py`; the decision-specific
rules (DEC006-DEC008) are covered at the bottom.

API contract: see the comment block at the top of `tests/test_records.py`.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

from easkills import cli, decisions, govern, records

DIR = Path("governance-log") / "decisions"
DATE = "2026-07-30"
TODAY = date(2026, 7, 30)
DECISION = "Adopt the shared platform"
RATIONALE = "It is the simplest option that works"


def dec_text(
    record_id: str,
    *,
    status: str = "accepted",
    when: str = "2026-01-02",
    title: str = "A decision",
    extra: str = "",
    nl: str = "\n",
) -> str:
    text = (
        f"id: {record_id}\ntitle: {title}\nstatus: {status}\ndate: {when}\n"
        f"decision: {DECISION}\nrationale: {RATIONALE}\n{extra}"
    )
    return text.replace("\n", nl)


def put(root: Path, name: str, text: str) -> Path:
    path = root / DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def seed(root: Path, record_id: str, **kwargs) -> Path:
    return put(root, f"{record_id}.yaml", dec_text(record_id, **kwargs))


def read(root: Path, record_id: str) -> str:
    return (root / DIR / f"{record_id}.yaml").read_bytes().decode("utf-8")


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def decisions_main(capsys, argv: list[str], root: Path | None = None) -> tuple[int, str, str]:
    full = ["decisions", *argv] + (["--root", str(root)] if root is not None else [])
    try:
        code = cli.main(full)
    except SystemExit as exit_:
        code = exit_.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def new(capsys, root: Path, *argv: str, title: str = "Use the shared platform", date_: str = DATE):
    base = ["new", "--title", title, "--decision", DECISION, "--rationale", RATIONALE]
    if date_:
        base += ["--date", date_]
    return decisions_main(capsys, [*base, *argv], root)


def gov(capsys, root: Path, *extra: str) -> tuple[int, str]:
    code = cli.main(["validate-gov", "--root", str(root), "--strict", *extra])
    return code, capsys.readouterr().out


def changed_lines(before: str, after: str) -> list[str]:
    import difflib

    return [line for line in difflib.ndiff(before.splitlines(), after.splitlines()) if line[:1] in "+-"]


# --------------------------------------------------------------------------- wiring


def test_decisions_is_registered_in_the_cli():
    assert "decisions" in cli.HANDLERS
    assert decisions.RECORD_TYPE.name == "decision"
    assert decisions.RECORD_TYPE.code_prefix == "DEC"
    assert decisions.RECORD_TYPE.directory == DIR


def test_record_type_matches_the_spec():
    rt = decisions.RECORD_TYPE
    assert rt.key_order == (
        "id", "title", "status", "date", "context", "decision", "rationale", "consequences",
        "options", "supersedes", "supersededBy", "relatedElements",
    )
    assert rt.row_fields == ("title", "status", "date", "supersededBy")
    assert rt.search_fields == ("context", "decision", "rationale", "consequences")
    assert rt.text_fields == ("context", "decision", "rationale", "consequences")
    assert (rt.ref_field, rt.ref_flag) == ("relatedElements", "element")
    assert rt.initial_statuses == ("proposed", "accepted", "rejected") and rt.default_status == "proposed"
    assert rt.superseded_status == "superseded" and rt.live_status == "accepted"
    assert (rt.successor_field, rt.predecessors_field) == ("supersededBy", "supersedes")
    assert rt.transitions["proposed"] == {"accepted", "rejected"}
    assert rt.transitions["accepted"] == {"deprecated"}
    for terminal in ("rejected", "deprecated", "superseded"):
        assert not rt.transitions.get(terminal)


def _group() -> dict[str, argparse.ArgumentParser]:
    top = cli.build_parser()
    sub = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction))
    group = sub.choices["decisions"]
    return dict(next(a for a in group._actions if isinstance(a, argparse._SubParsersAction)).choices)


def test_every_subcommand_accepts_root_and_a_flagless_json():
    leaves = _group()
    assert set(leaves) == {"list", "show", "new", "set-status", "supersede"}
    for name, leaf in leaves.items():
        options = {s: a for a in leaf._actions for s in a.option_strings}
        assert "--root" in options, name
        assert "--json" in options and options["--json"].nargs == 0, name


def test_new_has_a_flag_per_text_field_and_the_element_flag():
    options = {s for a in _group()["new"]._actions for s in a.option_strings}
    assert {"--title", "--status", "--date", "--element", "--context", "--decision", "--rationale",
            "--consequences", "--from-file", "--from-stdin"} <= options


def test_argparse_misuse_exits_2(tmp_path, capsys):
    for argv in (
        [],  # no subcommand
        ["new"],  # no title anywhere
        ["new", "--title", "T", "--nope"],
        ["new", "--title", "T", "--date", "2026-02-30"],  # not a real calendar date
        ["list", "--since", "yesterday"],
        ["set-status", "0001-a", "bogus"],
        ["set-status", "0001-a"],
        ["supersede", "only-one"],
        ["new", "--title", "T", "--from-file", "x.yaml", "--from-stdin"],
    ):
        code, _, _ = decisions_main(capsys, argv, tmp_path if argv else None)
        assert code == 2, argv
    assert not (tmp_path / "governance-log").exists()


# --------------------------------------------------------------------------- list


def _list_fixture(root: Path) -> None:
    seed(root, "0010-ten", status="accepted", when="2026-03-01", title="Ten",
         extra="context: SECRETCONTEXT words\nrelatedElements: [app-a]\n")
    seed(root, "0002-two", status="proposed", when="2026-05-01", title="Two",
         extra="relatedElements: [app-b, app-a]\n")
    seed(root, "2026-01-01-old", status="superseded", when="2026-01-01", title="Old",
         extra="supersededBy: 0010-ten\n")
    seed(root, "legacy-z", status="accepted", when="2026-01-01", title="Legacy Z")
    seed(root, "legacy-a", status="rejected", when="2026-06-01", title="Legacy A",
         extra="consequences: mentions a NEEDLE here\n")


def test_list_text_sorts_numbered_then_unnumbered_and_never_prints_bodies(tmp_path, capsys):
    _list_fixture(tmp_path)
    code, out, err = decisions_main(capsys, ["list"], tmp_path)
    assert (code, err) == (0, "")
    lines = out.strip().splitlines()
    assert "5" in lines[0]  # header carries the count
    body = lines[1:]
    assert [line.split()[0] for line in body] == ["0002-two", "0010-ten", "2026-01-01-old", "legacy-z", "legacy-a"]
    old = next(line for line in body if line.startswith("2026-01-01-old"))
    assert re.search(r"2026-01-01-old\s+superseded\s+2026-01-01\s+Old -> 0010-ten$", old)
    for hidden in (DECISION, RATIONALE, "SECRETCONTEXT", "NEEDLE"):
        assert hidden not in out
    assert "rationale" not in out.lower()


def test_list_json_shape(tmp_path, capsys):
    _list_fixture(tmp_path)
    code, out, _ = decisions_main(capsys, ["list", "--json"], tmp_path)
    document = json.loads(out)
    assert code == 0
    assert document["type"] == "decision" and document["count"] == 5 and document["skipped"] == []
    assert [r["id"] for r in document["records"]] == ["0002-two", "0010-ten", "2026-01-01-old", "legacy-z", "legacy-a"]
    assert document["records"][0] == {
        "id": "0002-two", "number": 2, "title": "Two", "status": "proposed", "date": "2026-05-01",
        "supersededBy": None,
    }
    assert document["records"][2]["number"] is None and document["records"][2]["supersededBy"] == "0010-ten"
    assert "SECRETCONTEXT" not in out and RATIONALE not in out
    assert out.endswith("\n") and out == json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _list_ids(capsys, root, *argv) -> list[str]:
    code, out, _ = decisions_main(capsys, ["list", "--json", *argv], root)
    assert code == 0
    return [r["id"] for r in json.loads(out)["records"]]


def test_list_filters(tmp_path, capsys):
    _list_fixture(tmp_path)
    assert set(_list_ids(capsys, tmp_path, "--status", "accepted")) == {"0010-ten", "legacy-z"}
    assert set(_list_ids(capsys, tmp_path, "--status", "accepted", "--status", "proposed")) == {
        "0010-ten", "legacy-z", "0002-two"}
    assert set(_list_ids(capsys, tmp_path, "--element", "app-a")) == {"0010-ten", "0002-two"}
    assert set(_list_ids(capsys, tmp_path, "--element", "app-b", "--element", "app-zzz")) == {"0002-two"}
    assert _list_ids(capsys, tmp_path, "--text", "needle") == ["legacy-a"]  # matched inside, never printed
    assert _list_ids(capsys, tmp_path, "--text", "secretcontext") == ["0010-ten"]
    assert _list_ids(capsys, tmp_path, "--text", "legacy z") == []
    assert set(_list_ids(capsys, tmp_path, "--text", "legacy")) == {"legacy-z", "legacy-a"}  # id and title
    assert set(_list_ids(capsys, tmp_path, "--since", "2026-05-01")) == {"0002-two", "legacy-a"}


def test_list_filters_combine_and_across_flags(tmp_path, capsys):
    _list_fixture(tmp_path)
    assert _list_ids(capsys, tmp_path, "--status", "accepted", "--status", "proposed", "--element", "app-a",
                     "--since", "2026-04-01") == ["0002-two"]
    assert _list_ids(capsys, tmp_path, "--status", "rejected", "--element", "app-a") == []


def test_list_empty_result_and_empty_repo_exit_zero(tmp_path, capsys):
    code, out, _ = decisions_main(capsys, ["list"], tmp_path)
    assert code == 0 and "0" in out.splitlines()[0]
    _list_fixture(tmp_path)
    code, out, _ = decisions_main(capsys, ["list", "--text", "nothing-matches-this"], tmp_path)
    assert code == 0


def test_list_reports_skipped_files_in_text_and_json(tmp_path, capsys):
    seed(tmp_path, "0001-good")
    put(tmp_path, "0002-bad.yaml", "id: [unclosed\n")
    put(tmp_path, "0003-noid.yaml", "title: no id\n")
    put(tmp_path, "0004-dup.yaml", dec_text("0001-good", title="Duplicate"))
    code, out, _ = decisions_main(capsys, ["list"], tmp_path)
    assert code == 0
    for name in ("0002-bad", "0003-noid", "0004-dup"):
        assert re.search(
            rf"^skipped: governance-log/decisions/{name}\.yaml: .+ \(run validate-gov\)$", out, re.MULTILINE
        ), name
    code, out, _ = decisions_main(capsys, ["list", "--json"], tmp_path)
    document = json.loads(out)
    assert code == 0 and document["count"] == 1
    assert {s["file"] for s in document["skipped"]} == {
        f"governance-log/decisions/{n}.yaml" for n in ("0002-bad", "0003-noid", "0004-dup")
    }
    assert all(s["reason"] for s in document["skipped"])


# --------------------------------------------------------------------------- show


def test_show_by_id_and_by_number(tmp_path, capsys):
    seed(tmp_path, "0008-eight", title="Eight", extra="context: Some context\n")
    for key in ("0008-eight", "8", "0008"):
        code, out, err = decisions_main(capsys, ["show", key], tmp_path)
        assert (code, err) == (0, "")
        assert "0008-eight" in out and "governance-log/decisions/0008-eight.yaml" in out
        assert RATIONALE in out and "Some context" in out  # show prints the whole record


def test_show_exact_id_wins_over_number_lookup(tmp_path, capsys):
    seed(tmp_path, "0008-a")
    seed(tmp_path, "0008-b")
    code, out, _ = decisions_main(capsys, ["show", "0008-a", "--json"], tmp_path)
    assert code == 0 and json.loads(out)["record"]["id"] == "0008-a"


def test_show_ambiguous_number_lists_the_candidates(tmp_path, capsys):
    seed(tmp_path, "0008-a")
    seed(tmp_path, "0008-b")
    code, out, err = decisions_main(capsys, ["show", "8"], tmp_path)
    assert code == 1 and out == ""
    assert "0008-a" in err and "0008-b" in err


def test_show_not_found(tmp_path, capsys):
    seed(tmp_path, "0001-a")
    for key in ("0002-nope", "2", "nope"):
        code, out, err = decisions_main(capsys, ["show", key], tmp_path)
        assert code == 1 and out == "" and err.strip(), key
        code, out, err = decisions_main(capsys, ["show", key, "--json"], tmp_path)
        assert code == 1 and err == "" and json.loads(out)["ok"] is False


def test_show_json_shape(tmp_path, capsys):
    seed(tmp_path, "0001-a", extra="relatedElements: [app-a]\n")
    code, out, _ = decisions_main(capsys, ["show", "1", "--json"], tmp_path)
    document = json.loads(out)
    assert code == 0
    assert set(document) == {"record", "file", "supersedes", "supersededBy"}
    assert document["file"] == "governance-log/decisions/0001-a.yaml"
    assert document["record"]["relatedElements"] == ["app-a"]
    assert document["record"]["date"] == "2026-01-02"
    assert document["supersedes"] == [] and document["supersededBy"] is None


def test_show_resolves_a_legacy_one_way_link_in_both_directions(tmp_path, capsys):
    # The old record points forward, the new one never declared `supersedes`.
    seed(tmp_path, "0001-old", status="superseded", extra="supersededBy: 0002-new\n")
    seed(tmp_path, "0002-new")
    code, out, _ = decisions_main(capsys, ["show", "0002-new", "--json"], tmp_path)
    assert code == 0 and json.loads(out)["supersedes"] == ["0001-old"]
    code, out, _ = decisions_main(capsys, ["show", "0001-old", "--json"], tmp_path)
    assert json.loads(out)["supersededBy"] == "0002-new"

    # The new record points back, the old one has no supersededBy.
    seed(tmp_path, "0003-newer", extra="supersedes: [0002-new]\n")
    code, out, _ = decisions_main(capsys, ["show", "0002-new", "--json"], tmp_path)
    assert json.loads(out)["supersededBy"] == "0003-newer"
    code, out, _ = decisions_main(capsys, ["show", "0003-newer", "--json"], tmp_path)
    assert json.loads(out)["supersedes"] == ["0002-new"]


def test_show_supersedes_is_the_union_of_declared_and_inferred(tmp_path, capsys):
    seed(tmp_path, "0001-a", status="superseded", extra="supersededBy: 0003-c\n")
    seed(tmp_path, "0002-b", status="superseded")
    seed(tmp_path, "0003-c", extra="supersedes: [0002-b]\n")
    code, out, _ = decisions_main(capsys, ["show", "3", "--json"], tmp_path)
    assert sorted(json.loads(out)["supersedes"]) == ["0001-a", "0002-b"]


# --------------------------------------------------------------------------- new


def test_new_in_an_empty_repo(tmp_path, capsys):
    code, out, err = new(capsys, tmp_path, title="Use PostgreSQL")
    assert (code, err) == (0, "")
    assert out.strip() == "created 0001-use-postgresql governance-log/decisions/0001-use-postgresql.yaml"
    record = yaml.safe_load(read(tmp_path, "0001-use-postgresql"))
    assert record == {
        "id": "0001-use-postgresql", "title": "Use PostgreSQL", "status": "proposed",
        "date": date(2026, 7, 30), "decision": DECISION, "rationale": RATIONALE,
    }


def test_new_json_output(tmp_path, capsys):
    code, out, _ = new(capsys, tmp_path, "--json", "--status", "accepted", "--context", "Why we ask")
    document = json.loads(out)
    assert code == 0 and document["ok"] is True
    assert document["id"] == "0001-use-the-shared-platform"
    assert document["file"] == "governance-log/decisions/0001-use-the-shared-platform.yaml"
    assert document["record"]["status"] == "accepted" and document["record"]["context"] == "Why we ask"
    assert document["record"]["date"] == DATE


def test_new_never_reuses_gaps(tmp_path, capsys):
    seed(tmp_path, "0003-c")
    code, out, _ = new(capsys, tmp_path)
    assert code == 0 and "0004-use-the-shared-platform" in out
    assert (tmp_path / DIR / "0003-c.yaml").exists()


def test_new_ignores_legacy_and_date_prefixed_ids(tmp_path, capsys):
    seed(tmp_path, "decision-order-api")
    seed(tmp_path, "2026-07-20-dated")
    code, out, _ = new(capsys, tmp_path)
    assert code == 0 and "created 0001-" in out


def test_new_counts_the_stem_of_an_unparseable_file(tmp_path, capsys):
    put(tmp_path, "0006-broken.yaml", "id: [unclosed\n")
    code, out, _ = new(capsys, tmp_path)
    assert code == 0 and "created 0007-" in out


def test_new_slug_truncation_keeps_the_id_short_and_ends_on_an_alphanumeric(tmp_path, capsys):
    title = "Standardise on one internationalisation approach for every customer facing channel we operate today"
    code, out, _ = new(capsys, tmp_path, "--json", title=title)
    new_id = json.loads(out)["id"]
    assert code == 0 and len(new_id) <= 80 and new_id[-1].isalnum()
    assert re.fullmatch(r"0001-[a-z0-9]+(-[a-z0-9]+)*", new_id)


def test_new_slug_fallback(tmp_path, capsys):
    code, out, _ = new(capsys, tmp_path, "--json", title="???")
    assert code == 0 and json.loads(out)["id"] == "0001-record"


def test_new_date_override_and_default(tmp_path, capsys, monkeypatch):
    code, out, _ = new(capsys, tmp_path, "--json", date_="2024-02-29")
    assert code == 0 and json.loads(out)["record"]["date"] == "2024-02-29"
    monkeypatch.setattr(records, "today", lambda: date(2031, 5, 6))
    code, out, _ = new(capsys, tmp_path, "--json", date_="")
    assert code == 0 and json.loads(out)["record"]["date"] == "2031-05-06"


def test_new_from_file_and_flags_override_the_file(tmp_path, capsys):
    source = tmp_path / "input.yaml"
    source.write_text(
        f"title: Title from file\nstatus: accepted\ndecision: Decision from the file\nrationale: {RATIONALE}\n"
        "context: Context from file\nrelatedElements: []\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    code, out, _ = decisions_main(
        capsys, ["new", "--from-file", str(source), "--date", DATE, "--title", "Title from flag",
                 "--decision", "Decision from the flag", "--json"], repo)
    record = json.loads(out)["record"]
    assert code == 0
    assert record["title"] == "Title from flag" and record["decision"] == "Decision from the flag"
    assert record["status"] == "accepted" and record["context"] == "Context from file"


def test_new_from_file_may_supply_the_title(tmp_path, capsys):
    source = tmp_path / "input.yaml"
    source.write_text(f"title: Only in the file\ndecision: {DECISION}\nrationale: {RATIONALE}\n", encoding="utf-8")
    code, out, _ = decisions_main(capsys, ["new", "--from-file", str(source), "--date", DATE], tmp_path / "repo")
    assert code == 0 and "0001-only-in-the-file" in out


def test_new_from_stdin(tmp_path, capsys, monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(
        f"title: Piped in\ndecision: {DECISION}\nrationale: {RATIONALE}\nrelatedElements: []\n"))
    code, out, _ = decisions_main(capsys, ["new", "--from-stdin", "--date", DATE], tmp_path)
    assert code == 0 and "created 0001-piped-in " in out


def test_new_refuses_an_id_in_the_input(tmp_path, capsys):
    source = tmp_path / "input.yaml"
    source.write_text(f"id: 0099-mine\ntitle: T\ndecision: {DECISION}\nrationale: {RATIONALE}\n", encoding="utf-8")
    repo = tmp_path / "repo"
    code, out, err = decisions_main(capsys, ["new", "--from-file", str(source), "--date", DATE], repo)
    assert code == 1 and "allocates ids" in err
    assert not repo.exists()


@pytest.mark.parametrize("line", ["supersedes: [0001-a]", "supersededBy: 0001-a"])
def test_new_refuses_link_fields_in_the_input(tmp_path, capsys, line):
    source = tmp_path / "input.yaml"
    source.write_text(f"title: T\ndecision: {DECISION}\nrationale: {RATIONALE}\n{line}\n", encoding="utf-8")
    repo = tmp_path / "repo"
    code, out, err = decisions_main(capsys, ["new", "--from-file", str(source), "--date", DATE], repo)
    assert code == 1 and "supersede" in err
    assert not repo.exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--rationale", "short"],  # schema: rationale too short
        ["--status", "bogus"],
        ["--status", "superseded"],  # only reachable through `supersede`
        ["--status", "deprecated"],  # not an initial status
    ],
    ids=["short-rationale", "bad-status", "status-superseded", "status-deprecated"],
)
def test_new_invalid_input_writes_nothing_and_creates_no_directory(tmp_path, capsys, extra):
    repo = tmp_path / "repo"
    repo.mkdir()
    code, out, err = decisions_main(
        capsys,
        ["new", "--title", "T", "--decision", DECISION, "--rationale", RATIONALE, "--date", DATE, *extra],
        repo,
    )
    assert code == 1 and err.strip() and out == ""
    assert list(repo.iterdir()) == []


def test_new_invalid_values_in_an_input_file_write_nothing(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    for body in (
        f"title: T\ndecision: {DECISION}\nrationale: {RATIONALE}\ndate: '2026-02-30'\n",  # not a calendar date
        f"title: T\ndecision: {DECISION}\nrationale: {RATIONALE}\nsurprise: 1\n",  # unknown key
        f"title: T\ndecision: {DECISION}\n",  # required field missing
    ):
        source = tmp_path / "input.yaml"
        source.write_text(body, encoding="utf-8")
        # An explicit --date would override the bad date in the file, so pass it only when the file has none.
        date_flag = [] if "date:" in body else ["--date", DATE]
        code, out, err = decisions_main(capsys, ["new", "--from-file", str(source), *date_flag], repo)
        assert code == 1 and err.strip(), body
        assert re.search(r"^DEC001 /", err, re.MULTILINE), err
        assert list(repo.iterdir()) == []


def test_new_json_refusal_is_json_with_errors(tmp_path, capsys):
    repo = tmp_path / "repo"
    code, out, err = decisions_main(
        capsys, ["new", "--title", "T", "--decision", DECISION, "--rationale", "short", "--date", DATE, "--json"], repo)
    document = json.loads(out)
    assert code == 1 and err == "" and document["ok"] is False
    assert document["errors"][0]["code"] == "DEC001" and document["errors"][0]["locator"] == "/rationale"
    assert not repo.exists()


def test_new_with_an_unknown_element_and_no_model_is_refused(tmp_path, capsys):
    repo = tmp_path / "repo"
    code, out, err = new(capsys, repo, "--element", "app-ghost")
    assert code == 1 and "app-ghost" in err and "not in the approved model" in err
    assert not repo.exists()


def test_new_with_elements_checked_against_the_model(tmp_path, capsys, example_root):
    repo = tmp_path / "repo"
    shutil.copytree(example_root, repo)
    code, out, err = new(capsys, repo, "--element", "app-erp-core", "--element", "app-order-portal", "--json")
    assert code == 0, err
    assert json.loads(out)["record"]["relatedElements"] == ["app-erp-core", "app-order-portal"]
    before = tree(repo)
    code, out, err = new(capsys, repo, "--element", "app-erp-core", "--element", "app-ghost")
    assert code == 1 and "app-ghost" in err
    assert tree(repo) == before


def test_new_does_not_overwrite(tmp_path, capsys, monkeypatch):
    existing = seed(tmp_path, "0001-use-the-shared-platform")
    before = existing.read_bytes()
    monkeypatch.setattr(records, "allocate_id", lambda rtype, loaded, title: "0001-use-the-shared-platform")
    code, out, err = new(capsys, tmp_path)
    assert code == 1 and err.strip()
    assert existing.read_bytes() == before
    assert sorted(p.name for p in (tmp_path / DIR).iterdir()) == ["0001-use-the-shared-platform.yaml"]


def test_new_problems_in_other_records_do_not_block(tmp_path, capsys):
    put(tmp_path, "0001-invalid.yaml", "id: 0001-invalid\ntitle: Missing everything\n")
    put(tmp_path, "0002-unparseable.yaml", "id: [unclosed\n")
    code, out, _ = new(capsys, tmp_path)
    assert code == 0 and "created 0003-" in out


def test_new_is_byte_stable_across_repositories(tmp_path, capsys):
    outputs = []
    for name in ("one", "two"):
        repo = tmp_path / name
        code, _, _ = new(capsys, repo, "--status", "accepted", "--context", "Line one\nLine two\n",
                         "--consequences", "Something: here")
        assert code == 0
        outputs.append(tree(repo))
    assert outputs[0] == outputs[1] and len(outputs[0]) == 1


AWKWARD = [
    "colon: inside the sentence",
    "- leading dash and more",
    "# starts like a comment",
    "has # hash inside",
    "it's got 'single' and \"double\" quotes",
    "unicode: café – naïve – 日本語 – 🙂",
    "multi\nline text\nover three lines",
    "multi\nline with a colon: here\n- and a dash",
    "trailing newline\n",
    "two trailing newlines\n\n",
    "no trailing newline at all",
    "yes", "null", "12345678", "2026-07-30",
    "trailing space here ",
    "tab\tinside the text",
    "{curly} [square] & star * bang ! pipe | gt > pct % at @ tick `",
    "line with trailing spaces   \nsecond line",
]


@pytest.mark.parametrize("text", AWKWARD, ids=range(len(AWKWARD)))
def test_new_round_trips_awkward_text(tmp_path, capsys, text):
    code, out, err = decisions_main(
        capsys,
        ["new", "--title", "Awkward text", "--decision", text if len(text) >= 8 else text + " padding",
         "--rationale", text if len(text) >= 8 else text + " padding", "--context", text, "--consequences", text,
         "--date", DATE, "--json"],
        tmp_path)
    assert code == 0, err
    document = json.loads(out)
    on_disk = yaml.safe_load(read(tmp_path, document["id"]))
    for field in ("context", "consequences"):
        assert on_disk[field] == text, field
        assert document["record"][field] == text


def test_new_result_passes_strict_validation_in_a_decisions_only_repo(tmp_path, capsys):
    code, out, err = new(capsys, tmp_path, "--status", "accepted", "--context", "Why\nwe do this\n")
    assert code == 0, err
    code, out = gov(capsys, tmp_path)
    assert code == 0, out
    code, _, _ = new(capsys, tmp_path, "--status", "rejected", title="Another one")
    assert code == 0
    code, out = gov(capsys, tmp_path)
    assert code == 0, out


def test_new_result_passes_strict_validation_with_an_empty_config(tmp_path, capsys):
    (tmp_path / "ea.config.yaml").write_text("", encoding="utf-8")
    code, _, err = new(capsys, tmp_path, "--status", "accepted")
    assert code == 0, err
    code, out = gov(capsys, tmp_path)
    assert code == 0, out


# --------------------------------------------------------------------------- set-status


ALLOWED = [("proposed", "accepted"), ("proposed", "rejected"), ("accepted", "deprecated")]
DISALLOWED = [
    ("proposed", "deprecated"),
    ("accepted", "proposed"),
    ("accepted", "rejected"),
    ("rejected", "proposed"),
    ("rejected", "accepted"),
    ("rejected", "deprecated"),
    ("deprecated", "proposed"),
    ("deprecated", "accepted"),
    ("deprecated", "rejected"),
    ("superseded", "proposed"),
    ("superseded", "accepted"),
    ("superseded", "deprecated"),
]


@pytest.mark.parametrize(("current", "target"), ALLOWED)
def test_set_status_allowed_transitions(tmp_path, capsys, current, target):
    seed(tmp_path, "0001-a", status=current)
    code, out, err = decisions_main(capsys, ["set-status", "0001-a", target, "--json"], tmp_path)
    assert (code, err) == (0, "")
    assert json.loads(out) == {"ok": True, "id": "0001-a", "from": current, "to": target, "changed": True}
    assert yaml.safe_load(read(tmp_path, "0001-a"))["status"] == target


@pytest.mark.parametrize(("current", "target"), DISALLOWED)
def test_set_status_disallowed_transitions_change_nothing(tmp_path, capsys, current, target):
    extra = "supersededBy: 0002-b\n" if current == "superseded" else ""
    path = seed(tmp_path, "0001-a", status=current, extra=extra)
    before = path.read_bytes()
    code, out, err = decisions_main(capsys, ["set-status", "0001-a", target], tmp_path)
    assert code == 1 and out == "" and err.strip()
    if current in ("rejected", "deprecated", "superseded"):
        assert "terminal" in err
    assert path.read_bytes() == before


@pytest.mark.parametrize("current", ["proposed", "accepted", "rejected", "deprecated"])
def test_set_status_to_superseded_points_at_supersede(tmp_path, capsys, current):
    path = seed(tmp_path, "0001-a", status=current)
    before = path.read_bytes()
    code, out, err = decisions_main(capsys, ["set-status", "0001-a", "superseded"], tmp_path)
    assert code == 1 and "supersede" in err
    assert path.read_bytes() == before


def test_set_status_names_the_allowed_moves(tmp_path, capsys):
    seed(tmp_path, "0001-a", status="proposed")
    code, _, err = decisions_main(capsys, ["set-status", "0001-a", "deprecated"], tmp_path)
    assert code == 1 and "accepted" in err and "rejected" in err


def test_set_status_same_status_is_a_noop(tmp_path, capsys):
    path = seed(tmp_path, "0001-a", status="accepted")
    before = path.read_bytes()
    code, out, _ = decisions_main(capsys, ["set-status", "0001-a", "accepted", "--json"], tmp_path)
    assert code == 0 and json.loads(out)["changed"] is False
    assert path.read_bytes() == before


def test_set_status_by_number_and_unknown_record(tmp_path, capsys):
    seed(tmp_path, "0007-a", status="proposed")
    code, _, _ = decisions_main(capsys, ["set-status", "7", "accepted"], tmp_path)
    assert code == 0
    code, out, _ = decisions_main(capsys, ["set-status", "9", "accepted", "--json"], tmp_path)
    assert code == 1 and json.loads(out)["ok"] is False


def test_set_status_preserves_comments_and_wording(tmp_path, capsys):
    text = (
        "# Hand-written record, do not reformat\n"
        "id: 0001-a\n"
        "title: Keep   this wording  # why\n"
        "status: proposed   # still open\n"
        "date: 2026-01-02\n"
        "context: >\n"
        "  Folded context\n"
        "  over two lines.\n"
        "decision: Adopt the shared platform\n"
        "rationale: It is the simplest option that works\n"
        "relatedElements: [ app-a,  app-b ]   # untouched spacing\n"
    )
    path = put(tmp_path, "0001-a.yaml", text)
    code, _, _ = decisions_main(capsys, ["set-status", "0001-a", "accepted"], tmp_path)
    assert code == 0
    assert changed_lines(text, path.read_text(encoding="utf-8")) == [
        "- status: proposed   # still open", "+ status: accepted   # still open"]


def test_set_status_edit_refused_for_exotic_formatting(tmp_path, capsys):
    text = dec_text("0001-a", status="proposed").replace("status: proposed", 'status: "propo\\\n  sed"')
    path = put(tmp_path, "0001-a.yaml", text)
    assert yaml.safe_load(text)["status"] == "proposed"  # valid YAML that the editor must still refuse
    code, out, err = decisions_main(capsys, ["set-status", "0001-a", "accepted"], tmp_path)
    assert code == 1 and err.strip()
    assert path.read_text(encoding="utf-8") == text


# --------------------------------------------------------------------------- supersede


def supersede(capsys, root, old="0001-old", new_="0002-new", *extra):
    return decisions_main(capsys, ["supersede", old, new_, *extra], root)


def test_supersede_happy_path_writes_both_files(tmp_path, capsys):
    seed(tmp_path, "0001-old")
    seed(tmp_path, "0002-new")
    code, out, err = supersede(capsys, tmp_path, "0001-old", "0002-new", "--json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "ok": True, "old": "0001-old", "new": "0002-new",
        "changed": ["governance-log/decisions/0001-old.yaml", "governance-log/decisions/0002-new.yaml"],
    }
    old = yaml.safe_load(read(tmp_path, "0001-old"))
    new_ = yaml.safe_load(read(tmp_path, "0002-new"))
    assert old["status"] == "superseded" and old["supersededBy"] == "0002-new"
    assert new_["supersedes"] == ["0001-old"] and new_["status"] == "accepted"


def test_supersede_by_number_and_text_output(tmp_path, capsys):
    seed(tmp_path, "0001-old")
    seed(tmp_path, "0002-new")
    code, out, _ = supersede(capsys, tmp_path, "1", "2")
    assert code == 0 and "0001-old" in out and "0002-new" in out


def test_supersede_round_trip_is_clean_under_strict_validation(tmp_path, capsys):
    assert new(capsys, tmp_path, "--status", "accepted", title="First choice")[0] == 0
    assert new(capsys, tmp_path, "--status", "accepted", title="Second choice")[0] == 0
    assert gov(capsys, tmp_path)[0] == 0
    code, out, err = supersede(capsys, tmp_path, "1", "2")
    assert code == 0, err
    code, out = gov(capsys, tmp_path)
    assert code == 0, out
    code, out, _ = decisions_main(capsys, ["show", "2", "--json"], tmp_path)
    assert json.loads(out)["supersedes"] == ["0001-first-choice"]


def test_supersede_preserves_comments_and_wording_with_a_two_line_diff(tmp_path, capsys):
    old_text = (
        "# Hand-written, keep my layout\n"
        "id: 0001-old\n"
        "title: Old   choice  # keep\n"
        "status: accepted   # live\n"
        "date: 2026-01-02\n"
        "decision: >\n"
        "  Use the old thing\n"
        "  everywhere.\n"
        "rationale: It was what we had\n"
        "relatedElements: [ app-a,  app-b ]\n"
    )
    new_text = old_text.replace("0001-old", "0002-new").replace("Old   choice", "New choice")
    old_path = put(tmp_path, "0001-old.yaml", old_text)
    new_path = put(tmp_path, "0002-new.yaml", new_text)
    code, _, err = supersede(capsys, tmp_path)
    assert code == 0, err
    assert sorted(changed_lines(old_text, old_path.read_text(encoding="utf-8"))) == sorted([
        "- status: accepted   # live", "+ status: superseded   # live", "+ supersededBy: 0002-new"])
    assert changed_lines(new_text, new_path.read_text(encoding="utf-8")) == ["+ supersedes: [0001-old]"]


def test_supersede_appends_to_an_existing_supersedes_list(tmp_path, capsys):
    seed(tmp_path, "0001-old")
    seed(tmp_path, "0002-mid", status="superseded", extra="supersededBy: 0003-new\n")
    new_path = seed(tmp_path, "0003-new", extra="supersedes: [0002-mid]\n")
    code, _, err = supersede(capsys, tmp_path, "0001-old", "0003-new")
    assert code == 0, err
    assert new_path.read_text(encoding="utf-8").count("supersedes: [0002-mid, 0001-old]") == 1


def test_supersede_keeps_crlf_line_endings(tmp_path, capsys):
    seed(tmp_path, "0001-old", nl="\r\n")
    seed(tmp_path, "0002-new", nl="\r\n")
    code, _, err = supersede(capsys, tmp_path)
    assert code == 0, err
    for record_id in ("0001-old", "0002-new"):
        text = read(tmp_path, record_id)
        assert "\n" not in text.replace("\r\n", ""), f"{record_id} gained a bare LF"
        assert text.endswith("\r\n")
    assert "supersededBy: 0002-new\r\n" in read(tmp_path, "0001-old")
    assert "supersedes: [0001-old]\r\n" in read(tmp_path, "0002-new")


REFUSALS = {
    "old-not-accepted": dict(old={"status": "proposed"}, new={}),
    "old-rejected": dict(old={"status": "rejected"}, new={}),
    "new-not-accepted": dict(old={}, new={"status": "proposed"}),
    "new-deprecated": dict(old={}, new={"status": "deprecated"}),
    "same-id": dict(argv=("0001-old", "0001-old"), old={}, new={}),
    "already-superseded-by-another": dict(
        old={"status": "superseded", "extra": "supersededBy: 0003-other\n"}, new={}, other=True),
    "unknown-old": dict(argv=("0009-ghost", "0002-new"), old={}, new={}),
    "unknown-new": dict(argv=("0001-old", "0009-ghost"), old={}, new={}),
    "unknown-number": dict(argv=("9", "2"), old={}, new={}),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_supersede_refusals_write_nothing(tmp_path, capsys, case):
    spec = REFUSALS[case]
    seed(tmp_path, "0001-old", **spec["old"])
    seed(tmp_path, "0002-new", **spec["new"])
    if spec.get("other"):
        seed(tmp_path, "0003-other")
    before = tree(tmp_path)
    old, new_ = spec.get("argv", ("0001-old", "0002-new"))
    code, out, err = supersede(capsys, tmp_path, old, new_)
    assert code == 1 and out == "" and err.strip()
    assert tree(tmp_path) == before
    code, out, err = supersede(capsys, tmp_path, old, new_, "--json")
    document = json.loads(out)
    assert code == 1 and err == "" and document["ok"] is False
    assert tree(tmp_path) == before


def test_supersede_is_idempotent(tmp_path, capsys):
    seed(tmp_path, "0001-old")
    seed(tmp_path, "0002-new")
    assert supersede(capsys, tmp_path)[0] == 0
    snapshot = tree(tmp_path)
    code, out, err = supersede(capsys, tmp_path)
    assert code == 0 and "already linked" in out
    assert tree(tmp_path) == snapshot
    code, out, _ = supersede(capsys, tmp_path, "0001-old", "0002-new", "--json")
    assert code == 0 and json.loads(out)["changed"] == []
    assert tree(tmp_path) == snapshot


def test_supersede_repairs_a_legacy_one_way_link(tmp_path, capsys):
    old_path = seed(tmp_path, "0001-old", status="superseded", extra="supersededBy: 0002-new\n")
    seed(tmp_path, "0002-new")
    old_before = old_path.read_bytes()
    code, out, err = supersede(capsys, tmp_path, "0001-old", "0002-new", "--json")
    assert code == 0, err
    assert json.loads(out)["changed"] == ["governance-log/decisions/0002-new.yaml"]
    assert old_path.read_bytes() == old_before
    assert yaml.safe_load(read(tmp_path, "0002-new"))["supersedes"] == ["0001-old"]
    assert gov(capsys, tmp_path)[0] == 0


def test_supersede_rolls_back_the_first_file_when_the_second_write_fails(tmp_path, capsys, monkeypatch):
    old_path = seed(tmp_path, "0001-old")
    new_path = seed(tmp_path, "0002-new")
    old_before, new_before = old_path.read_bytes(), new_path.read_bytes()
    real = records.replace_file
    calls: list[Path] = []

    def flaky(path, text):
        calls.append(Path(path))
        if len(calls) == 2:
            raise OSError("disk full")
        return real(path, text)

    monkeypatch.setattr(records, "replace_file", flaky)
    code, out, err = supersede(capsys, tmp_path)
    assert code == 1 and err.strip()
    assert calls[0] == old_path and calls[1] == new_path  # old first, then new
    assert old_path.read_bytes() == old_before  # restored from its original text
    assert new_path.read_bytes() == new_before


@pytest.mark.parametrize("which", ["old", "new"])
@pytest.mark.parametrize("exotic", ["flow-mapping", "multiline-status"])
def test_supersede_refuses_exotic_formatting_and_writes_nothing(tmp_path, capsys, which, exotic):
    plain = {"old": dec_text("0001-old"), "new": dec_text("0002-new")}
    if exotic == "flow-mapping":
        record_id = f"000{1 if which == 'old' else 2}-{which}"
        plain[which] = (
            f"{{id: {record_id}, title: T, status: accepted, date: 2026-01-02, decision: {DECISION}, "
            f"rationale: {RATIONALE}}}\n"
        )
    else:
        plain[which] = plain[which].replace("status: accepted", 'status: "accep\\\n  ted"')
        assert yaml.safe_load(plain[which])["status"] == "accepted"
    put(tmp_path, "0001-old.yaml", plain["old"])
    put(tmp_path, "0002-new.yaml", plain["new"])
    before = tree(tmp_path)
    code, out, err = supersede(capsys, tmp_path)
    assert code == 1 and err.strip()
    assert tree(tmp_path) == before


def test_supersede_validates_both_files_before_writing_either(tmp_path, capsys, monkeypatch):
    # `new` already breaks the schema (rationale too short); the old file must stay untouched.
    old_path = seed(tmp_path, "0001-old")
    put(tmp_path, "0002-new.yaml", dec_text("0002-new").replace(RATIONALE, "short"))
    before = tree(tmp_path)
    writes: list[Path] = []
    monkeypatch.setattr(records, "replace_file", lambda path, text: writes.append(Path(path)))
    code, out, err = supersede(capsys, tmp_path)
    assert code == 1 and writes == [] and tree(tmp_path) == before
    assert old_path.read_bytes() == before["governance-log/decisions/0001-old.yaml"]


# --------------------------------------------------------------------------- --json on success and refusal


def test_every_json_subcommand_emits_one_parseable_document_on_success_and_refusal(tmp_path, capsys):
    seed(tmp_path, "0001-old", status="proposed")
    seed(tmp_path, "0002-new")
    cases = [
        (["list", "--json"], 0),
        (["list", "--json", "--status", "nonexistent"], 0),
        (["show", "1", "--json"], 0),
        (["show", "99", "--json"], 1),
        (["new", "--title", "T", "--decision", DECISION, "--rationale", RATIONALE, "--date", DATE, "--json"], 0),
        (["new", "--title", "T", "--decision", "short", "--rationale", RATIONALE, "--date", DATE, "--json"], 1),
        (["set-status", "1", "accepted", "--json"], 0),
        (["set-status", "1", "proposed", "--json"], 1),
        (["supersede", "1", "2", "--json"], 0),
        (["supersede", "1", "99", "--json"], 1),
    ]
    for argv, expected in cases:
        code, out, err = decisions_main(capsys, argv, tmp_path)
        assert code == expected, (argv, out, err)
        document = json.loads(out)  # exactly one document: json.loads rejects trailing text
        assert isinstance(document, dict)
        assert err == "", argv
        if expected == 1:
            assert document["ok"] is False and ("errors" in document or "error" in document)


# --------------------------------------------------------------------------- rules DEC006-DEC008


def gov_report(root: Path):
    return govern.validate_governance(root, today=TODAY)


def codes(report, code):
    return [f for f in report.findings if f.code == code]


def test_the_decision_loader_reads_supersedes(tmp_path):
    seed(tmp_path, "0001-a")
    seed(tmp_path, "0002-b", extra="supersedes: [0001-a, 0009-x]\n")
    loaded = govern.load(tmp_path)
    assert loaded.decisions["0002-b"].supersedes == ["0001-a", "0009-x"]
    assert loaded.decisions["0001-a"].supersedes == []


def test_the_schema_allows_supersedes_and_nothing_else_new(tmp_path):
    from easkills import genschema

    properties = genschema.build_decision_schema()["properties"]
    assert properties["supersedes"]["type"] == "array"
    assert properties["supersedes"]["items"] == properties["supersededBy"]


def test_dec006_supersedes_names_a_missing_record(tmp_path):
    seed(tmp_path, "0001-a", extra="supersedes: [0009-ghost]\n")
    found = codes(gov_report(tmp_path), "DEC006")
    assert len(found) == 1 and found[0].severity == govern.SEVERITY_ERROR
    assert "0009-ghost" in found[0].message and found[0].concept == "0001-a"


def test_dec006_is_quiet_when_the_target_exists(tmp_path):
    seed(tmp_path, "0001-a", status="superseded", extra="supersededBy: 0002-b\n")
    seed(tmp_path, "0002-b", extra="supersedes: [0001-a]\n")
    assert codes(gov_report(tmp_path), "DEC006") == []


def test_dec007_forward_link_without_the_back_link(tmp_path):
    seed(tmp_path, "0001-a", status="superseded", extra="supersededBy: 0002-b\n")
    seed(tmp_path, "0002-b")
    found = codes(gov_report(tmp_path), "DEC007")
    assert len(found) == 1 and found[0].severity == govern.SEVERITY_WARNING
    assert found[0].concept == "0001-a"


def test_dec007_back_link_without_the_forward_link(tmp_path):
    seed(tmp_path, "0001-a")
    seed(tmp_path, "0002-b", extra="supersedes: [0001-a]\n")
    found = codes(gov_report(tmp_path), "DEC007")
    assert len(found) == 1 and found[0].severity == govern.SEVERITY_WARNING
    assert found[0].concept == "0002-b"


def test_dec007_back_link_when_the_old_record_names_a_different_successor(tmp_path):
    seed(tmp_path, "0001-a", status="superseded", extra="supersededBy: 0003-c\n")
    seed(tmp_path, "0002-b", extra="supersedes: [0001-a]\n")
    seed(tmp_path, "0003-c", extra="supersedes: [0001-a]\n")
    found = codes(gov_report(tmp_path), "DEC007")
    assert [f.concept for f in found] == ["0002-b"]


def test_dec007_is_quiet_for_a_two_way_link_and_for_missing_targets(tmp_path):
    seed(tmp_path, "0001-a", status="superseded", extra="supersededBy: 0002-b\n")
    seed(tmp_path, "0002-b", extra="supersedes: [0001-a]\n")
    seed(tmp_path, "0003-c", extra="supersedes: [0009-ghost]\n")  # DEC006's business, not DEC007's
    assert codes(gov_report(tmp_path), "DEC007") == []


def test_dec008_two_records_share_a_sequence_number(tmp_path):
    seed(tmp_path, "0008-b")
    seed(tmp_path, "0008-a")
    seed(tmp_path, "0009-c")
    found = codes(gov_report(tmp_path), "DEC008")
    assert len(found) == 1 and found[0].severity == govern.SEVERITY_WARNING
    assert found[0].concept == "0008-b"  # reported on every record except the first by id
    assert "0008-a" in found[0].message


def test_dec008_reports_once_per_record_after_the_first(tmp_path):
    for name in ("0008-c", "0008-a", "0008-b"):
        seed(tmp_path, name)
    found = codes(gov_report(tmp_path), "DEC008")
    assert sorted(f.concept for f in found) == ["0008-b", "0008-c"]
    assert all("0008-a" in f.message for f in found)


def test_dec008_is_quiet_for_distinct_legacy_and_date_prefixed_ids(tmp_path):
    seed(tmp_path, "0001-a")
    seed(tmp_path, "0002-b")
    seed(tmp_path, "2026-07-20-first")
    seed(tmp_path, "2026-07-20-second")
    seed(tmp_path, "decision-legacy-one")
    seed(tmp_path, "decision-legacy-two")
    assert codes(gov_report(tmp_path), "DEC008") == []


def test_the_new_rules_leave_the_existing_decision_rules_alone(tmp_path):
    seed(tmp_path, "0001-a", status="superseded")  # DEC004: superseded with no supersededBy
    seed(tmp_path, "0002-b", extra="supersededBy: 0009-ghost\n")  # DEC003
    report = gov_report(tmp_path)
    assert len(codes(report, "DEC004")) == 1 and len(codes(report, "DEC003")) == 1
    assert codes(report, "DEC006") == [] and codes(report, "DEC008") == []


def test_example_repository_has_no_new_findings(example_root):
    report = govern.validate_governance(example_root, today=TODAY)
    assert not [f for f in report.findings if f.code in ("DEC006", "DEC007", "DEC008")]
