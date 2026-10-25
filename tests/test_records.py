"""The record-type-neutral core (`easkills.records`), proven on a fake type that is not a decision.

The core must work for any record type described by a `RecordType`; a made-up "note"
(directory `notes/`, prefix `NOT`, fields `state`, `when`, `follows`, `followedBy`, `tags`)
is the proof that nothing decision-shaped leaked into it.
"""

# API contract
# ------------
# Where SPEC.md leaves a name open, these are the names the tests use. The implementer follows them.
#
# * Module-level names in `easkills.records`:
#     RecordType, Record, Loaded, Problem, EditRefused, WouldOverwrite (both Exception subclasses),
#     sequence_number, load, rows, allocate_id, render_yaml, validate_candidate,
#     insert_after, set_scalar, add_to_list, write_new, replace_file, today,
#     cmd_list, cmd_show, cmd_new, cmd_set_status, cmd_supersede, add_parser.
# * `import os` happens at module level in records.py (tests patch `os.link` globally).
# * `Record` has: id, data, path (absolute Path), text (raw, newline-preserved str), number (int | None).
# * `Loaded` has: `records: dict[str, Record]` (id -> Record, first file wins), `skipped: list[tuple[Path, str]]`
#   (absolute path, reason) and `stems: set[str]` (file stems of every file, readable or not).
#   `Path` in `skipped` is absolute; commands print it relative to root with forward slashes.
# * `rows(rtype, loaded, statuses=(), refs=(), text=None, since=None) -> list[dict]` filters and sorts per the spec.
#   Each row is `{"id", "number", *row_fields}`; absent keys are None.
# * `allocate_id(rtype, loaded, title) -> str`.
# * `render_yaml(rtype, data) -> str`.
# * `Problem` is a NamedTuple `(code, locator, message)`; `validate_candidate(rtype, root, data) -> list[Problem]`.
#   Schema errors have code `<prefix>001` and a JSON-pointer locator ("/body"); a non-calendar date_field is
#   `<prefix>001` with locator "/<date_field>". `check_references` messages become Problems too (tests only
#   look at the message text for those).
# * `insert_after(text, anchor_key, new_line) -> text`: `new_line` has no newline; one is added in the file's style.
# * `set_scalar(text, key, value, after=None) -> text` and `add_to_list(text, key, item, after=None) -> text`:
#   `after` is the anchor key used when `key` is absent (the status key); absent key and no usable anchor
#   -> EditRefused. The two edit functions raise EditRefused for anything not simple.
# * `write_new(path, text)` writes exactly `text` (no newline translation); `replace_file(path, text)` likewise.
# * Commands are reached through `add_parser(subparsers, rtype, command_name)`:
#     - the group parser has `dest=f"{command_name}_command"` and `required=True`;
#     - every leaf parser has `--root` (Path, default cwd) and `--json` (store_true, dest `as_json`);
#     - every leaf parser does `set_defaults(func=...)`: `args.func(args) -> int`
#       (so the group needs no dispatch of its own; cli.py just calls `args.func(args)`).
#   Leaf commands: list, show, new, set-status, supersede.
# * `set-status`: the status argument is argparse `choices` = every status the type knows
#   (initial_statuses | transitions keys and values | superseded_status | live_status) -> unknown status exits 2.
# * `new` with neither `--title` nor a title in the input file/stdin is a usage error: exit 2 (via parser.error).
# * `new --status` is NOT argparse `choices`: a status outside initial_statuses is a validation refusal (exit 1).
# * `new --date` / `list --since` use an argparse `type=` rejecting non-calendar dates (exit 2); a bad `date` inside
#   --from-file/--from-stdin input is a validation refusal (exit 1, `<prefix>001` code).
# * `new --title` is therefore not `required=True` in argparse; argparse misuse otherwise exits 2.
# * `show --json` spells the resolved links with the type's own field names: `{record, file, <predecessors_field>:
#   [...], <successor_field>: id|None}` (decisions: `supersedes`, `supersededBy`; the fake note: `follows`, `followedBy`).
# * Text-mode errors go to stderr, JSON-mode errors to stdout (`{"ok": false, ...}`), per SPEC.md.

from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import re
import stat
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

from easkills import records
from easkills.records import RecordType

REPO_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- the fake type


def _note_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "title", "state", "when", "body"],
        "properties": {
            "id": {"type": "string", "pattern": "^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$", "maxLength": 80},
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "state": {"enum": ["draft", "live", "dropped", "retired", "replaced"]},
            "when": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
            "body": {"type": "string", "minLength": 8},
            "remark": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "follows": {"type": "array", "items": {"type": "string"}},
            "followedBy": {"type": "string"},
        },
    }


def _note_references(root: Path, data: dict) -> list[str]:
    return [f"tags names '{tag}' which is not allowed" for tag in data.get("tags", []) if tag == "forbidden"]


NOTE = RecordType(
    name="note",
    code_prefix="NOT",
    directory=Path("notes"),
    schema=_note_schema,
    key_order=("id", "title", "state", "when", "body", "remark", "tags", "follows", "followedBy"),
    row_fields=("title", "state", "when", "followedBy"),
    search_fields=("body", "remark"),
    text_fields=("body", "remark"),
    status_field="state",
    date_field="when",
    ref_field="tags",
    ref_flag="tag",
    initial_statuses=("draft", "live"),
    default_status="draft",
    transitions={"draft": frozenset({"live", "dropped"}), "live": frozenset({"retired"})},
    superseded_status="replaced",
    live_status="live",
    successor_field="followedBy",
    predecessors_field="follows",
    check_references=_note_references,
)

BODY = "A body that is long enough"


def note_text(
    note_id: str,
    *,
    state: str = "draft",
    when: str = "2026-01-02",
    title: str = "A title",
    body: str = BODY,
    extra: str = "",
    nl: str = "\n",
) -> str:
    text = f"id: {note_id}\ntitle: {title}\nstate: {state}\nwhen: {when}\nbody: {body}\n{extra}"
    return text.replace("\n", nl)


def put(root: Path, name: str, text: str) -> Path:
    path = root / "notes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="t")
    sub = top.add_subparsers(dest="command", required=True)
    records.add_parser(sub, NOTE, "notes")
    return top


def run(capsys, argv: list[str], root: Path | None = None) -> tuple[int, str, str]:
    full = ["notes", *argv] + (["--root", str(root)] if root is not None else [])
    try:
        args = parser().parse_args(full)
        code = args.func(args)
    except SystemExit as exit_:
        code = exit_.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def changed_lines(before: str, after: str) -> list[str]:
    return [
        line for line in difflib.ndiff(before.splitlines(), after.splitlines()) if line[:1] in "+-"
    ]


# --------------------------------------------------------------------------- neutrality


def test_core_source_names_no_decision_vocabulary():
    source = (REPO_ROOT / "easkills" / "records.py").read_text(encoding="utf-8")
    assert "decision" not in source.lower()
    for literal in ("DEC0", "supersededBy", "relatedElements", "governance-log"):
        assert literal not in source, f"records.py must stay record-type-neutral, found {literal!r}"


def test_core_does_not_import_govern():
    source = (REPO_ROOT / "easkills" / "records.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+(easkills\.)?govern\b", source, re.MULTILINE)
    assert not re.search(r"^\s*from\s+(easkills|\.)\s+import\s+.*\bgovern\b", source, re.MULTILINE)


# --------------------------------------------------------------------------- sequence_number


@pytest.mark.parametrize(
    ("record_id", "expected"),
    [
        ("0008-x", 8),
        ("0001-first-one", 1),
        ("0123-abc", 123),
        ("123-abc", 123),
        ("000123-abc", 123),
        ("12345-abc", 12345),
        ("1234567-abc", None),  # 7 digits: not a sequence number
        ("12-abc", None),  # 2 digits
        ("decision-x", None),
        ("2026-07-20-foo", None),  # date-prefixed
        ("2026-07-20", None),
        ("0008", None),  # no dash, no slug
        ("", None),
        ("x-0008-y", None),
    ],
)
def test_sequence_number(record_id, expected):
    assert records.sequence_number(record_id) == expected


# --------------------------------------------------------------------------- load


def test_load_reads_records_and_normalises_scalars(tmp_path):
    put(tmp_path, "0001-a.yaml", note_text("0001-a"))
    loaded = records.load(NOTE, tmp_path)
    record = loaded.records["0001-a"]
    assert record.id == "0001-a"
    assert record.number == 1
    assert record.data["when"] == "2026-01-02"  # PyYAML's date object is normalised to a string
    assert record.path == tmp_path / "notes" / "0001-a.yaml"
    assert loaded.skipped == []
    assert loaded.stems == {"0001-a"}


def test_load_walks_subdirectories_and_both_extensions_sorted(tmp_path):
    put(tmp_path, "b.yml", note_text("b-note"))
    put(tmp_path, "sub/a.yaml", note_text("a-note"))
    put(tmp_path, "readme.txt", "ignore me")
    loaded = records.load(NOTE, tmp_path)
    assert set(loaded.records) == {"a-note", "b-note"}
    assert loaded.stems == {"a", "b"}  # the .txt file is not a record file


def test_load_missing_directory_is_empty(tmp_path):
    loaded = records.load(NOTE, tmp_path)
    assert loaded.records == {} and loaded.skipped == [] and loaded.stems == set()


def test_load_keeps_raw_text_with_its_newlines(tmp_path):
    text = note_text("crlf-note", nl="\r\n")
    put(tmp_path, "crlf-note.yaml", text)
    assert records.load(NOTE, tmp_path).records["crlf-note"].text == text


def test_load_reports_unreadable_files_duplicates_and_idless_files(tmp_path):
    put(tmp_path, "good.yaml", note_text("same-id"))
    put(tmp_path, "zz-dup.yaml", note_text("same-id", title="The duplicate"))
    put(tmp_path, "bad.yaml", "id: [unclosed\n")
    put(tmp_path, "list.yaml", "- a\n- b\n")
    put(tmp_path, "empty.yaml", "")
    put(tmp_path, "noid.yaml", "title: no id here\n")
    loaded = records.load(NOTE, tmp_path)
    assert set(loaded.records) == {"same-id"}
    assert loaded.records["same-id"].data["title"] == "A title"  # the first file (sorted) wins
    skipped = {path.name: reason for path, reason in loaded.skipped}
    assert set(skipped) == {"zz-dup.yaml", "bad.yaml", "list.yaml", "empty.yaml", "noid.yaml"}
    assert all(reason for reason in skipped.values())
    assert "same-id" in skipped["zz-dup.yaml"]
    assert loaded.stems == {"good", "zz-dup", "bad", "list", "empty", "noid"}


# --------------------------------------------------------------------------- rows: filters and sort


def _rows_fixture(tmp_path) -> records.Loaded:
    put(tmp_path, "0010-ten.yaml", note_text("0010-ten", state="live", when="2026-03-01", title="Ten", extra="tags: [alpha]\n"))
    put(tmp_path, "0002-two.yaml", note_text("0002-two", state="draft", when="2026-05-01", title="Two", extra="tags: [beta, alpha]\n"))
    put(tmp_path, "2026-01-01-old.yaml", note_text("2026-01-01-old", state="live", when="2026-01-01", title="Old"))
    put(tmp_path, "zeta.yaml", note_text("zeta", state="live", when="2026-01-01", title="Zeta", body="Mentions a Needle inside"))
    put(tmp_path, "alpha.yaml", note_text("alpha", state="draft", when="2026-06-01", title="Alpha", extra="remark: hidden REMARK words\n"))
    put(tmp_path, "undated.yaml", "id: undated\ntitle: Undated\nstate: live\nbody: Some long enough body\n")
    return records.load(NOTE, tmp_path)


def ids(rows):
    return [r["id"] for r in rows]


def test_rows_sort_numbered_first_then_unnumbered_by_date_and_id(tmp_path):
    rows = records.rows(NOTE, _rows_fixture(tmp_path))
    # numbered by number; unnumbered by (date, id); the undated one has no date and sorts first among them.
    assert ids(rows) == ["0002-two", "0010-ten", "undated", "2026-01-01-old", "zeta", "alpha"]


def test_row_shape_and_missing_fields_are_null(tmp_path):
    rows = {r["id"]: r for r in records.rows(NOTE, _rows_fixture(tmp_path))}
    assert rows["0002-two"] == {
        "id": "0002-two", "number": 2, "title": "Two", "state": "draft", "when": "2026-05-01", "followedBy": None,
    }
    assert rows["zeta"]["number"] is None
    assert rows["undated"]["when"] is None


def test_rows_status_filter_is_or(tmp_path):
    loaded = _rows_fixture(tmp_path)
    assert set(ids(records.rows(NOTE, loaded, statuses=("draft",)))) == {"0002-two", "alpha"}
    assert set(ids(records.rows(NOTE, loaded, statuses=("draft", "dropped")))) == {"0002-two", "alpha"}
    assert len(records.rows(NOTE, loaded, statuses=("draft", "live"))) == 6


def test_rows_ref_filter_is_or_over_membership(tmp_path):
    loaded = _rows_fixture(tmp_path)
    assert set(ids(records.rows(NOTE, loaded, refs=("alpha",)))) == {"0010-ten", "0002-two"}
    assert set(ids(records.rows(NOTE, loaded, refs=("beta",)))) == {"0002-two"}
    assert set(ids(records.rows(NOTE, loaded, refs=("beta", "gamma")))) == {"0002-two"}
    assert ids(records.rows(NOTE, loaded, refs=("gamma",))) == []


def test_rows_text_filter_is_case_insensitive_over_id_title_and_search_fields(tmp_path):
    loaded = _rows_fixture(tmp_path)
    assert ids(records.rows(NOTE, loaded, text="needle")) == ["zeta"]  # body
    assert ids(records.rows(NOTE, loaded, text="hidden remark")) == ["alpha"]  # remark
    assert ids(records.rows(NOTE, loaded, text="OLD")) == ["2026-01-01-old"]  # id and title
    assert ids(records.rows(NOTE, loaded, text="no-such-words")) == []


def test_rows_since_is_inclusive_and_excludes_undated(tmp_path):
    loaded = _rows_fixture(tmp_path)
    assert set(ids(records.rows(NOTE, loaded, since="2026-05-01"))) == {"0002-two", "alpha"}
    assert "undated" not in ids(records.rows(NOTE, loaded, since="2000-01-01"))


def test_rows_since_excludes_unparseable_dates(tmp_path):
    put(tmp_path, "junk.yaml", note_text("junk", when="not-a-date"))
    put(tmp_path, "fine.yaml", note_text("fine", when="2026-02-02"))
    assert ids(records.rows(NOTE, records.load(NOTE, tmp_path), since="2000-01-01")) == ["fine"]


def test_rows_filters_and_across_flags_or_within(tmp_path):
    loaded = _rows_fixture(tmp_path)
    got = records.rows(NOTE, loaded, statuses=("live", "draft"), refs=("alpha",), since="2026-04-01")
    assert ids(got) == ["0002-two"]
    got = records.rows(NOTE, loaded, statuses=("live",), refs=("alpha", "beta"))
    assert ids(got) == ["0010-ten"]
    assert ids(records.rows(NOTE, loaded, statuses=("draft",), text="needle")) == []


# --------------------------------------------------------------------------- allocate_id


def _alloc(tmp_path, title="Use the thing"):
    return records.allocate_id(NOTE, records.load(NOTE, tmp_path), title)


def test_allocate_first_id(tmp_path):
    assert _alloc(tmp_path) == "0001-use-the-thing"


def test_allocate_never_reuses_gaps(tmp_path):
    put(tmp_path, "0003-c.yaml", note_text("0003-c"))
    put(tmp_path, "0001-a.yaml", note_text("0001-a"))
    assert _alloc(tmp_path) == "0004-use-the-thing"


def test_allocate_ignores_legacy_and_date_prefixed_ids(tmp_path):
    put(tmp_path, "legacy.yaml", note_text("legacy-record"))
    put(tmp_path, "dated.yaml", note_text("2026-07-20-dated"))
    assert _alloc(tmp_path) == "0001-use-the-thing"


def test_allocate_counts_the_stem_of_an_unparseable_file(tmp_path):
    put(tmp_path, "0007-broken.yaml", "id: [unclosed\n")
    assert _alloc(tmp_path) == "0008-use-the-thing"


def test_allocate_counts_numbered_id_inside_a_differently_named_file(tmp_path):
    put(tmp_path, "whatever.yaml", note_text("0012-inside"))
    assert _alloc(tmp_path) == "0013-use-the-thing"


def test_allocate_grows_past_four_digits(tmp_path):
    put(tmp_path, "9999-a.yaml", note_text("9999-a"))
    assert _alloc(tmp_path) == "10000-use-the-thing"


def test_allocate_slug_rules(tmp_path):
    assert _alloc(tmp_path, "  Hello,   World!! (v2) ") == "0001-hello-world-v2"
    assert _alloc(tmp_path, "Ünïcode & Ünderscores_here") .startswith("0001-")
    slug = _alloc(tmp_path, "Ünïcode & Ünderscores_here").split("-", 1)[1]
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug)


def test_allocate_slug_fallback_when_nothing_survives(tmp_path):
    assert _alloc(tmp_path, "!!! ???") == "0001-record"
    assert _alloc(tmp_path, "") == "0001-record"


def test_allocate_truncates_at_a_word_boundary_within_80_characters(tmp_path):
    title = " ".join(["internationalisation"] * 8)
    new_id = _alloc(tmp_path, title)
    assert len(new_id) <= 80
    assert new_id[-1].isalnum()
    assert re.fullmatch(r"0001-[a-z0-9-]+", new_id)
    assert new_id.split("-", 1)[1] in "-".join(["internationalisation"] * 8)
    assert not new_id.endswith("-")
    assert new_id == "0001-" + "-".join(["internationalisation"] * 3)  # 5 + 3*20 + 2 = 67; a fourth word would be 88


def test_allocate_truncation_is_sane_for_one_huge_word(tmp_path):
    new_id = _alloc(tmp_path, "x" * 200)
    assert len(new_id) <= 80 and new_id[-1].isalnum() and new_id.startswith("0001-")


def test_allocate_avoids_a_colliding_stem(tmp_path):
    # A stem with no number cannot collide with a numbered id, but the guard must still hold for odd inputs.
    put(tmp_path, "0001-use-the-thing.yaml", "id: [unclosed\n")
    assert _alloc(tmp_path) == "0002-use-the-thing"


# --------------------------------------------------------------------------- render_yaml

FULL = {
    "id": "0001-full",
    "title": "A full note",
    "state": "live",
    "when": "2026-07-30",
    "body": "First line\nSecond line\n",
    "remark": "single line remark",
    "tags": ["a", "b"],
    "follows": ["0000-older"],
    "followedBy": "0002-newer",
}


def load_text(text: str) -> dict:
    return records_normalise(yaml.safe_load(text))


def records_normalise(value):
    from easkills import dsl

    return dsl._normalize_scalars(value)


def test_render_orders_keys_and_roundtrips():
    text = records.render_yaml(NOTE, dict(reversed(list(FULL.items()))))
    assert [line.split(":")[0] for line in text.splitlines() if re.match(r"^\w", line)] == list(FULL)
    assert load_text(text) == FULL


def test_render_unknown_keys_come_last_sorted():
    text = records.render_yaml(NOTE, {**FULL, "zzz": "1", "aaa": "2"})
    tops = [line.split(":")[0] for line in text.splitlines() if re.match(r"^\w", line)]
    assert tops[-2:] == ["aaa", "zzz"]
    assert tops[:-2] == list(FULL)


def test_render_dates_unquoted_and_id_lists_flow():
    text = records.render_yaml(NOTE, FULL)
    assert "\nwhen: 2026-07-30\n" in text
    assert "\ntags: [a, b]\n" in text
    assert "\nfollows: [0000-older]\n" in text


def test_render_omits_empty_optional_values():
    text = records.render_yaml(NOTE, {**FULL, "remark": "", "tags": [], "follows": [], "followedBy": None})
    for key in ("remark", "tags", "follows", "followedBy"):
        assert f"{key}:" not in text
    assert text.endswith("\n")


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        ("line one\nline two", "body: |-"),
        ("line one\nline two\n", "body: |\n"),
        ("line one\nline two\n\n", "body: |+"),
    ],
)
def test_render_multiline_uses_block_scalars_per_trailing_newlines(body, marker):
    text = records.render_yaml(NOTE, {**FULL, "body": body})
    assert marker in text
    assert load_text(text)["body"] == body


def test_render_options_as_a_block_list_of_mappings():
    options = [{"option": "One", "pros": "fast"}, {"option": "Two", "cons": "slow"}]
    text = records.render_yaml(NOTE, {**FULL, "options": options})
    assert "\noptions:\n  - option: One\n" in text
    assert load_text(text)["options"] == options


AWKWARD = [
    "colon: inside",
    "- leading dash",
    "# starts like a comment",
    "has # hash inside",
    "it's got 'single' and \"double\" quotes",
    "unicode: café – naïve – 日本語 – 🙂",
    "yes", "no", "null", "123", "1.5", "2026-07-30", "~", "true",
    "trailing space ",
    " leading space",
    "tab\tinside",
    "carriage\rreturn",
    "multi\nline: with colon\n- and dash",
    "multi\nline with trailing spaces   \nnext",
    " starts with space\nsecond line",
    "multi\n\ttabbed\nlines",
    "ends with newline\n",
    "ends with two newlines\n\n",
    "\nstarts with newline",
    "{curly} [square] & anchor * star ! bang | pipe > gt % pct @ at ` tick",
    "a" * 300,
]


@pytest.mark.parametrize("value", AWKWARD, ids=range(len(AWKWARD)))
def test_render_awkward_strings_roundtrip(value):
    data = {**FULL, "body": value, "remark": value, "title": value.replace("\n", " ") or "t"}
    data["title"] = data["title"].strip() or "t"
    text = records.render_yaml(NOTE, data)
    assert load_text(text) == data


def test_render_is_byte_stable():
    assert records.render_yaml(NOTE, FULL) == records.render_yaml(NOTE, dict(FULL))
    assert records.render_yaml(NOTE, FULL) == records.render_yaml(NOTE, dict(reversed(list(FULL.items()))))


# --------------------------------------------------------------------------- validate_candidate


def good() -> dict:
    return {"id": "0001-a", "title": "T", "state": "draft", "when": "2026-07-30", "body": BODY}


def test_validate_candidate_accepts_a_good_record(tmp_path):
    assert records.validate_candidate(NOTE, tmp_path, good()) == []


def test_validate_candidate_schema_error_has_code_and_locator(tmp_path):
    problems = records.validate_candidate(NOTE, tmp_path, {**good(), "body": "short"})
    assert [(p.code, p.locator) for p in problems] == [("NOT001", "/body")]
    assert problems[0].message


def test_validate_candidate_missing_required_and_unknown_key(tmp_path):
    data = good()
    del data["body"]
    data["surprise"] = 1
    problems = records.validate_candidate(NOTE, tmp_path, data)
    assert len(problems) == 2 and all(p.code == "NOT001" for p in problems)
    assert problems == sorted(problems)  # deterministic order


def test_validate_candidate_rejects_a_non_calendar_date(tmp_path):
    problems = records.validate_candidate(NOTE, tmp_path, {**good(), "when": "2026-02-30"})
    assert [(p.code, p.locator) for p in problems] == [("NOT001", "/when")]


def test_validate_candidate_runs_the_reference_hook(tmp_path):
    problems = records.validate_candidate(NOTE, tmp_path, {**good(), "tags": ["ok", "forbidden"]})
    assert len(problems) == 1
    assert "forbidden" in problems[0].message


def test_validate_candidate_without_hook(tmp_path):
    import dataclasses

    bare = dataclasses.replace(NOTE, check_references=None)
    assert records.validate_candidate(bare, tmp_path, {**good(), "tags": ["forbidden"]}) == []


# --------------------------------------------------------------------------- surgical edits

SAMPLE = (
    "# a leading comment\n"
    "id: x-1\n"
    "title: Keep this wording   # trailing comment\n"
    "state: draft   # why draft\n"
    "when: 2026-01-02\n"
    "body: >\n"
    "  Folded text\n"
    "  continues here.\n"
)


def test_set_scalar_replaces_bare_value_and_keeps_the_trailing_comment():
    out = records.set_scalar(SAMPLE, "state", "live")
    assert changed_lines(SAMPLE, out) == ["- state: draft   # why draft", "+ state: live   # why draft"]


@pytest.mark.parametrize("quote", ["'", '"'])
def test_set_scalar_keeps_the_quote_style(quote):
    text = f"id: x\nstate: {quote}draft{quote}  # c\nwhen: 2026-01-02\n"
    out = records.set_scalar(text, "state", "live")
    assert out == f"id: x\nstate: {quote}live{quote}  # c\nwhen: 2026-01-02\n"


def test_set_scalar_only_touches_top_level_keys():
    text = "id: x\noptions:\n  - option: a\n    state: nested\nstate: draft\n"
    out = records.set_scalar(text, "state", "live")
    assert out == "id: x\noptions:\n  - option: a\n    state: nested\nstate: live\n"


def test_set_scalar_absent_key_is_inserted_after_the_anchor():
    out = records.set_scalar(SAMPLE, "followedBy", "y-2", after="state")
    assert changed_lines(SAMPLE, out) == ["+ followedBy: y-2"]
    lines = out.splitlines()
    assert lines[lines.index("followedBy: y-2") - 1].startswith("state: draft")


def test_set_scalar_absent_key_without_usable_anchor_is_refused():
    with pytest.raises(records.EditRefused):
        records.set_scalar(SAMPLE, "followedBy", "y-2")
    with pytest.raises(records.EditRefused):
        records.set_scalar(SAMPLE, "followedBy", "y-2", after="nope")


def test_insert_after_adds_one_line_after_the_anchor():
    out = records.insert_after("a: 1\nb: 2\n", "a", "c: 3")
    assert out == "a: 1\nc: 3\nb: 2\n"


def test_edits_use_the_files_newline_style():
    crlf = SAMPLE.replace("\n", "\r\n")
    out = records.set_scalar(crlf, "followedBy", "y-2", after="state")
    assert "followedBy: y-2\r\n" in out
    assert out.replace("\r\n", "") .count("\n") == 0  # no bare LF introduced
    out = records.add_to_list(crlf, "follows", "z", after="state")
    assert "follows: [z]\r\n" in out and "\n" not in out.replace("\r\n", "")


def test_edits_on_the_last_line_without_a_trailing_newline():
    text = "id: x\nstate: draft"
    assert records.set_scalar(text, "state", "live") == "id: x\nstate: live"
    out = records.add_to_list(text, "follows", "z", after="state")
    assert yaml.safe_load(out) == {"id": "x", "state": "draft", "follows": ["z"]}


def test_add_to_list_appends_to_a_flow_list():
    text = "id: x\nstate: draft\nfollows: [a, b]\nwhen: 2026-01-02\n"
    assert records.add_to_list(text, "follows", "c") == "id: x\nstate: draft\nfollows: [a, b, c]\nwhen: 2026-01-02\n"


def test_add_to_list_empty_flow_list():
    assert records.add_to_list("state: draft\nfollows: []\n", "follows", "c") == "state: draft\nfollows: [c]\n"


def test_add_to_list_appends_to_a_block_list_with_the_existing_indent():
    text = "id: x\nfollows:\n    - a\n    - b   # keep\nwhen: 2026-01-02\n"
    out = records.add_to_list(text, "follows", "c")
    assert out == "id: x\nfollows:\n    - a\n    - b   # keep\n    - c\nwhen: 2026-01-02\n"
    flush = "follows:\n- a\nwhen: 2026-01-02\n"
    assert records.add_to_list(flush, "follows", "c") == "follows:\n- a\n- c\nwhen: 2026-01-02\n"


def test_add_to_list_absent_key_inserted_after_the_anchor():
    out = records.add_to_list(SAMPLE, "follows", "z", after="state")
    assert changed_lines(SAMPLE, out) == ["+ follows: [z]"]


def test_add_to_list_already_present_is_unchanged():
    text = "state: draft\nfollows: [a, b]\n"
    assert records.add_to_list(text, "follows", "b") == text
    block = "state: draft\nfollows:\n  - a\n  - b\n"
    assert records.add_to_list(block, "follows", "a") == block


@pytest.mark.parametrize(
    "text",
    [
        "{id: x, state: draft}\n",  # flow-mapping document
        "id: x\nstate: draft\nstate: live\n",  # duplicated key
        'id: x\nstate: "dra\\\n  ft"\n',  # multi-line quoted scalar
        "id: x\nstate: &anchor draft\nother: *anchor\n",  # anchors
        "id: x\nstate: !!str draft\n",  # tags
        "id: x\nstate: draft\n---\nid: y\nstate: live\n",  # several documents
    ],
    ids=["flow-mapping", "duplicate-key", "multiline-quoted", "anchor", "tag", "multi-document"],
)
def test_set_scalar_refuses_anything_not_simple(text):
    with pytest.raises(records.EditRefused) as excinfo:
        records.set_scalar(text, "state", "live")
    assert str(excinfo.value)  # a reason, never silence


def test_add_to_list_refuses_anything_not_simple():
    with pytest.raises(records.EditRefused):
        records.add_to_list("{id: x, state: draft}\n", "follows", "z", after="state")
    with pytest.raises(records.EditRefused):
        records.add_to_list("state: draft\nfollows: [a]\nfollows: [b]\n", "follows", "z")
    with pytest.raises(records.EditRefused):
        records.add_to_list("state: draft\nfollows: &x [a]\n", "follows", "z")
    with pytest.raises(records.EditRefused):
        records.add_to_list("state: draft\nfollows: not-a-list\n", "follows", "z")
    with pytest.raises(records.EditRefused):
        records.add_to_list("state: draft\nfollows: [a,\n  b]\n", "follows", "z")  # multi-line flow list


# --------------------------------------------------------------------------- file writes


def test_write_new_creates_parents_and_writes_exact_bytes(tmp_path):
    target = tmp_path / "deep" / "er" / "n.yaml"
    records.write_new(target, "a: 1\r\nb: 2\n")
    assert target.read_bytes() == b"a: 1\r\nb: 2\n"
    assert [p.name for p in target.parent.iterdir()] == ["n.yaml"]  # no temp file left behind


def test_write_new_never_clobbers(tmp_path):
    target = tmp_path / "n.yaml"
    target.write_text("original", encoding="utf-8")
    with pytest.raises(records.WouldOverwrite):
        records.write_new(target, "new")
    assert target.read_text(encoding="utf-8") == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["n.yaml"]


def test_write_new_falls_back_when_hard_links_are_unsupported(tmp_path, monkeypatch):
    def no_link(*args, **kwargs):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_link)
    target = tmp_path / "n.yaml"
    records.write_new(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    with pytest.raises(records.WouldOverwrite):
        records.write_new(target, "again\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert [p.name for p in tmp_path.iterdir()] == ["n.yaml"]


def test_replace_file_swaps_content_and_preserves_the_mode(tmp_path):
    target = tmp_path / "n.yaml"
    target.write_bytes(b"old\r\n")
    target.chmod(0o640)
    records.replace_file(target, "new\r\n")
    assert target.read_bytes() == b"new\r\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["n.yaml"]


def test_today_is_a_date_evaluated_at_call_time():
    assert records.today() == date.today()


# --------------------------------------------------------------------------- the commands, on the fake type


def test_every_leaf_command_has_root_and_a_flagless_json(tmp_path):
    top = parser()
    group = next(a for a in top._actions if isinstance(a, argparse._SubParsersAction)).choices["notes"]
    leaves = next(a for a in group._actions if isinstance(a, argparse._SubParsersAction)).choices
    assert set(leaves) == {"list", "show", "new", "set-status", "supersede"}
    for name, leaf in leaves.items():
        options = {s: a for a in leaf._actions for s in a.option_strings}
        assert "--root" in options, name
        assert "--json" in options, name
        assert options["--json"].nargs == 0, f"--json takes no argument on {name}"


def test_group_without_a_subcommand_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        parser().parse_args(["notes"])
    assert excinfo.value.code == 2


def test_new_without_a_title_anywhere_is_a_usage_error(tmp_path, capsys):
    code, _, _ = run(capsys, ["new"], tmp_path)
    assert code == 2
    assert not (tmp_path / "notes").exists()


def test_unknown_flag_and_bad_dates_are_usage_errors(tmp_path, capsys):
    for argv in (
        ["new", "--title", "T", "--nope"],
        ["new", "--title", "T", "--date", "2026-02-30"],
        ["new", "--title", "T", "--date", "30/02/2026"],
        ["list", "--since", "2026-13-01"],
        ["new", "--title", "T", "--from-file", "a.yaml", "--from-stdin"],
    ):
        code, _, _ = run(capsys, argv, tmp_path)
        assert code == 2, argv
    assert not (tmp_path / "notes").exists()


def test_cli_new_then_list_and_show_roundtrip(tmp_path, capsys):
    code, out, err = run(
        capsys, ["new", "--title", "First note", "--body", "Some body text", "--tag", "alpha", "--date", "2026-07-30"], tmp_path
    )
    assert (code, err) == (0, "")
    assert out.strip() == "created 0001-first-note notes/0001-first-note.yaml"
    path = tmp_path / "notes" / "0001-first-note.yaml"
    assert load_text(path.read_text(encoding="utf-8")) == {
        "id": "0001-first-note", "title": "First note", "state": "draft", "when": "2026-07-30",
        "body": "Some body text", "tags": ["alpha"],
    }
    code, out, _ = run(capsys, ["list", "--json"], tmp_path)
    document = json.loads(out)
    assert code == 0
    assert document["type"] == "note" and document["count"] == 1 and document["skipped"] == []
    assert document["records"][0]["id"] == "0001-first-note"
    assert "Some body text" not in out
    code, out, _ = run(capsys, ["show", "1", "--json"], tmp_path)
    shown = json.loads(out)
    assert shown["record"]["body"] == "Some body text"
    assert shown["file"] == "notes/0001-first-note.yaml"
    assert shown["follows"] == [] and shown["followedBy"] is None


def test_cli_new_runs_the_reference_hook_and_writes_nothing(tmp_path, capsys):
    code, out, err = run(capsys, ["new", "--title", "T", "--body", BODY, "--tag", "forbidden", "--date", "2026-07-30"], tmp_path)
    assert code == 1
    assert "forbidden" in err and out == ""
    assert not (tmp_path / "notes").exists()


def test_cli_new_validation_failure_in_json_mode_is_json_on_stdout(tmp_path, capsys):
    code, out, err = run(capsys, ["new", "--title", "T", "--body", "short", "--date", "2026-07-30", "--json"], tmp_path)
    assert code == 1 and err == ""
    document = json.loads(out)
    assert document["ok"] is False
    assert document["errors"][0].keys() >= {"code", "locator", "message"}
    assert document["errors"][0]["code"] == "NOT001"
    assert not (tmp_path / "notes").exists()


def test_cli_new_error_text_is_prefixed_with_code_and_locator(tmp_path, capsys):
    code, _, err = run(capsys, ["new", "--title", "T", "--body", "short", "--date", "2026-07-30"], tmp_path)
    assert code == 1
    assert re.search(r"^NOT001 /body: ", err, re.MULTILINE)


def test_cli_set_status_and_supersede_use_the_types_vocabulary(tmp_path, capsys):
    put(tmp_path, "0001-old.yaml", note_text("0001-old", state="live"))
    put(tmp_path, "0002-new.yaml", note_text("0002-new", state="live"))
    code, out, _ = run(capsys, ["supersede", "0001-old", "0002-new", "--json"], tmp_path)
    assert code == 0, out
    document = json.loads(out)
    assert document["ok"] is True and document["old"] == "0001-old" and document["new"] == "0002-new"
    assert document["changed"] == ["notes/0001-old.yaml", "notes/0002-new.yaml"]
    old = yaml.safe_load((tmp_path / "notes" / "0001-old.yaml").read_text(encoding="utf-8"))
    new = yaml.safe_load((tmp_path / "notes" / "0002-new.yaml").read_text(encoding="utf-8"))
    assert old["state"] == "replaced" and old["followedBy"] == "0002-new"
    assert new["follows"] == ["0001-old"]
    code, out, _ = run(capsys, ["supersede", "0001-old", "0002-new", "--json"], tmp_path)
    assert code == 0 and json.loads(out)["changed"] == []
    code, out, err = run(capsys, ["set-status", "0001-old", "replaced"], tmp_path)
    assert code == 1 and "supersede" in err


def test_cli_set_status_follows_the_transition_table(tmp_path, capsys):
    put(tmp_path, "0001-a.yaml", note_text("0001-a", state="draft"))
    code, out, _ = run(capsys, ["set-status", "1", "live", "--json"], tmp_path)
    assert code == 0
    assert json.loads(out) == {"ok": True, "id": "0001-a", "from": "draft", "to": "live", "changed": True}
    code, out, _ = run(capsys, ["set-status", "1", "draft", "--json"], tmp_path)
    assert code == 1 and json.loads(out)["ok"] is False
    code, out, err = run(capsys, ["set-status", "1", "bogus"], tmp_path)
    assert code == 2
    code, out, _ = run(capsys, ["set-status", "1", "live", "--json"], tmp_path)
    assert code == 0 and json.loads(out)["changed"] is False


def test_cli_new_reads_stdin(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"title: From stdin\nbody: {BODY}\n"))
    code, out, _ = run(capsys, ["new", "--from-stdin", "--date", "2026-07-30"], tmp_path)
    assert code == 0 and out.startswith("created 0001-from-stdin ")


def test_cli_default_date_is_taken_from_the_patched_today(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(records, "today", lambda: date(2031, 5, 6))
    code, out, _ = run(capsys, ["new", "--title", "Dated", "--body", BODY, "--json"], tmp_path)
    assert code == 0
    assert json.loads(out)["record"]["when"] == "2031-05-06"
