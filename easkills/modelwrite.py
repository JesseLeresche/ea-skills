"""The write commands ``elements new|set|retire|remove`` and ``relations new|remove`` (plan 002).

Every command works in memory first. It builds the would-be staging overlay (the same
semantics as ``dsl.load_merged``, including file shadowing) with the candidate change applied,
runs the existing validator on it and on the overlay as it stands, and refuses when the change
introduces an error that was not already there. Only then does anything touch the disk, and
only under ``model/staging/`` -- ``promote`` stays the one way into ``approved/``.

New concepts get a file of their own (``model/staging/<id>.yaml``). ``set``, ``retire`` and
``remove`` edit one concept's block inside a many-per-file document surgically: comments,
wording and line endings stay, the edit is verified by re-parsing, and anything that is not
plainly simple YAML is refused (``records.EditRefused``) rather than guessed at.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import functools
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from . import dsl, genschema, modelquery, oracle, records
from . import validate as validate_mod

SECTIONS = {"element": "elements", "relationship": "relationships"}
CONCEPT_SECTIONS = ("elements", "relationships", "views", "stakeholders", "concerns")
ID_LIMIT = 80
NAME_LIMIT = 120
_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")
_BOT = re.compile(r"(?i)(\[bot\]|\bbot\b)")
_PROPERTY_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")
_PLAIN_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*[ \t]*:(?:[ \t]|$)")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_FIELD_ORDER = ("id", "type", "name", "documentation", "owner", "lastReviewed")
_SETTABLE = ("name", "documentation", "owner", "lastReviewed")
_ATTRIBUTE = {"name": "name", "documentation": "documentation", "owner": "owner", "lastReviewed": "last_reviewed"}


class Refusal(Exception):
    """The command refuses: exit 1, nothing written."""

    def __init__(self, message: str, findings: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.findings = findings


def _before_swap(path: Path) -> None:
    """Called after the new text is computed and just before the file is replaced (a test seam)."""


def git_user_name(root: Path) -> str | None:
    """``git config user.name`` as seen from ``root``; None when unset or git is unavailable."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "config", "user.name"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = done.stdout.strip()
    return name if done.returncode == 0 and name else None


# --------------------------------------------------------------------------- the overlay


def _document(path: Path, text: str) -> dsl.Document:
    try:
        data = yaml.safe_load(text)
    except dsl.YAML_ERRORS as exc:
        return dsl.Document(path=path, data=None, parse_error=str(exc))
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return dsl.Document(path=path, data=None, parse_error="top level of a model file must be a mapping")
    return dsl.Document(path=path, data=dsl._normalize_scalars(data))


class Workspace:
    """One read of both zones, from which the current and the candidate overlay are built."""

    def __init__(self, root: Path) -> None:
        if not (root / "model").is_dir() and not (root / dsl.CONFIG_FILENAME).is_file():
            raise Refusal(f"{root} is not a model repository: it has neither {dsl.CONFIG_FILENAME} nor a model/ directory")
        self.root = root
        self.staging_dir = dsl.zone_dir(root, "staging")
        self.approved_dir = dsl.zone_dir(root, "approved")
        self.config = dsl.load_config(root)
        self.approved_docs = dsl.load_documents(root, "approved")
        self.staging_docs = dsl.load_documents(root, "staging")
        self.approved_model = dsl.build_model(root, "approved", self.approved_docs, self.config)
        self.model, self.documents = self.overlay({})

    def overlay(self, changes: dict[Path, str | None]) -> tuple[dsl.Model, list[dsl.Document]]:
        """Staging on approved as ``dsl.load_merged`` builds it, with ``changes`` applied first.

        ``changes`` maps a staging path to its new text, or to None when the file goes away.
        """
        wanted = {path.resolve(): (path, text) for path, text in changes.items()}
        staging: list[dsl.Document] = []
        for doc in self.staging_docs:
            key = doc.path.resolve()
            if key not in wanted:
                staging.append(doc)
            elif wanted[key][1] is not None:
                staging.append(_document(doc.path, wanted[key][1]))
        present = {d.path.resolve() for d in self.staging_docs}
        for key, (path, text) in wanted.items():
            if key not in present and text is not None:
                staging.append(_document(path, text))
        staging.sort(key=lambda d: d.path)
        shadowed = dsl.shadowed_approved_files(self.root, [d.path for d in staging])
        approved = [d for d in self.approved_docs if d.path.resolve() not in shadowed]
        model = dsl.build_model(self.root, "staging", approved, self.config)
        overlay = dsl.build_model(self.root, "staging", staging, self.config)
        model.duplicate_ids.extend(overlay.duplicate_ids)
        model.elements.update(overlay.elements)
        model.relationships.update(overlay.relationships)
        model.views.update(overlay.views)
        model.stakeholders.update(overlay.stakeholders)
        model.concerns.update(overlay.concerns)
        return model, approved + staging

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def zone_of(self, path: Path | None) -> str:
        return "staging" if path is not None and path.is_relative_to(self.staging_dir) else "approved"


def _finding(f: validate_mod.Finding) -> dict[str, str]:
    message = f.message if f.concept else f"{f.message} ({f.file}:{f.locator})" if f.file else f.message
    return {"code": f.code, "severity": f.severity, "message": message, "concept": f.concept}


def _error_key(f: validate_mod.Finding) -> tuple[str, ...]:
    return (f.code, f.concept, f.message) if f.concept else (f.code, f.file, f.locator, f.message)


def _check(ws: Workspace, changes: dict[Path, str | None], concept_ids: set[str]) -> list[dict[str, str]]:
    """Findings the change adds. Raises Refusal on a new error or a new approximate quote (PROV004)."""
    before = validate_mod._run_checks(ws.model, ws.documents, ws.root, "staging", None)
    model, documents = ws.overlay(changes)
    after = validate_mod._run_checks(model, documents, ws.root, "staging", None)
    known_errors = {_error_key(f) for f in before.errors}
    known_warnings = {(f.code, f.concept, f.message) for f in before.warnings}
    new_errors = [f for f in after.errors if _error_key(f) not in known_errors]
    warnings = [f for f in after.warnings if (f.code, f.concept, f.message) not in known_warnings]
    approximate = [f for f in warnings if f.code == "PROV004" and f.concept in concept_ids]
    if new_errors or approximate:
        refusing = [*new_errors, *approximate]
        codes = ", ".join(sorted({f.code for f in refusing}))
        raise Refusal(f"refused: the change would introduce validation problems ({codes}); nothing was written",
                      [_finding(f) for f in refusing])
    return [_finding(f) for f in warnings]


# --------------------------------------------------------------------------- ids, evidence, creator


def slugify(name: str) -> str:
    text = re.sub(r"[\s_./]", "-", name.lower())
    text = re.sub(r"[^a-z0-9-]", "", text)
    return re.sub(r"-+", "-", text).strip("-")


def _check_id(candidate: str, how: str) -> str:
    if len(candidate) > ID_LIMIT:
        raise Refusal(f"the id '{candidate}' is longer than {ID_LIMIT} characters; {how}")
    if not candidate or not _SLUG.fullmatch(candidate):
        raise Refusal(f"'{candidate}' is not a valid id (lowercase letters, digits, '-', '.', '_'; "
                      f"starting and ending with a letter or digit); {how}")
    return candidate


def _claimed(ws: Workspace, new_id: str) -> None:
    model = ws.model
    for label, bucket in (("element", model.elements), ("relationship", model.relationships), ("view", model.views),
                          ("stakeholder", model.stakeholders), ("concern", model.concerns)):
        if new_id in bucket:
            zone = ws.zone_of(bucket[new_id].source_path)
            known = [*model.elements, *model.relationships, *model.views]
            similar = modelquery.suggest_ids(new_id, known)
            hint = f" Similar ids in use: {', '.join(similar)}." if similar else ""
            raise Refusal(f"the id '{new_id}' is already used by a {label} in the {zone} zone.{hint} Choose another with --id.")
    for directory, zone in ((ws.approved_dir, "approved"), (ws.staging_dir, "staging")):
        for suffix in (".yaml", ".yml"):
            if (directory / f"{new_id}{suffix}").exists():
                raise Refusal(f"model/{zone}/{new_id}{suffix} already exists; a file of that name would shadow "
                              f"or clash with it. Choose another id with --id.")


def _evidence(args: argparse.Namespace) -> tuple[list[dict[str, str]], bool, str]:
    sources, quotes, facts = args.source or [], args.quote or [], args.fact or []
    if args.assumed:
        if sources or facts:
            raise Refusal("--assumed cannot be combined with --source or --fact")
        if not (args.rationale or "").strip():
            raise Refusal("--assumed needs --rationale TEXT saying why the concept is not evidenced")
        return [], True, args.rationale.strip()
    if args.rationale:
        raise Refusal("--rationale only goes with --assumed")
    for source in sources:
        if source.startswith(("/", "\\")) or re.match(r"[A-Za-z]:[\\/]", source):
            raise Refusal(f"--source {source!r} is an absolute path; give it relative to the facts root "
                          "(factsRoot in ea.config.yaml, default the repository root)")
    provenance = [{"file": s, "quote": q.strip()} for s, q in zip(sources, quotes)]
    provenance += [{"fact": f} for f in facts]
    if not provenance:
        raise Refusal("evidence is required: give --source FILE --quote TEXT, or --fact ID, or --assumed --rationale TEXT")
    return provenance, False, ""


def _creator(args: argparse.Namespace, root: Path) -> str:
    explicit = args.created_by
    name = explicit if explicit is not None else git_user_name(root)
    if explicit is not None and not explicit.strip():
        raise Refusal("--created-by must name a person")
    if explicit is not None and _BOT.search(explicit):
        raise Refusal("--created-by must name a person, not a bot")
    if name is None or not name.strip() or _BOT.search(name):
        why = "is a bot name" if name else "is not set"
        raise Refusal(f"cannot record who created this: the git user name {why}. "
                      "Pass --created-by NAME with the person who asked for it.")
    if explicit is not None and _BOT.search(explicit):
        raise Refusal("--created-by must name a person, not a bot")
    return name.strip()


def _new_properties(args: argparse.Namespace, creator: str | None) -> dict[str, str]:
    properties: dict[str, str] = {}
    for key, value in getattr(args, "property", None) or []:
        if key == "createdBy":
            raise Refusal("createdBy is set from --created-by or git config user.name, not with --property")
        properties[key] = value
    if creator is not None:
        properties["createdBy"] = creator
    return properties


class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


def _render(section: str, item: dict[str, Any]) -> str:
    return yaml.dump({section: [item]}, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=10**9,
                     default_flow_style=False)


def _with_evidence(item: dict[str, Any], args: argparse.Namespace) -> None:
    provenance, assumed, rationale = _evidence(args)
    if provenance:
        item["provenance"] = provenance
    if assumed:
        item["assumed"] = True
        item["rationale"] = rationale


# --------------------------------------------------------------------------- plans


@dataclass
class Plan:
    action: str
    kind: str
    cid: str
    path: Path
    changes: dict[Path, str | None] = field(default_factory=dict)
    # ("create", text) | ("replace", text, original bytes) | ("delete", original bytes)
    operation: tuple = ()
    notes: list[str] = field(default_factory=list)
    noop: str = ""
    refuse_approximate: set[str] = field(default_factory=set)


def _plan_new_element(ws: Workspace, args: argparse.Namespace) -> Plan:
    if args.type not in oracle.element_types():
        raise Refusal(f"'{args.type}' is not an ArchiMate element type"
                      f"{modelquery.did_you_mean(args.type, oracle.element_types())}")
    if not args.name.strip():
        raise Refusal("--name must not be blank")
    cid = _check_id(args.id if args.id is not None else slugify(args.name),
                    "pass --id to choose one" if args.id is None else "choose another")
    _claimed(ws, cid)
    item: dict[str, Any] = {"id": cid, "type": args.type, "name": args.name}
    if args.documentation is not None:
        item["documentation"] = args.documentation
    if args.owner is not None:
        item["owner"] = args.owner
    if args.last_reviewed is not None:
        item["lastReviewed"] = _datetime.date.fromisoformat(args.last_reviewed)
    _with_evidence(item, args)
    properties = _new_properties(args, _creator(args, ws.root))
    item["properties"] = properties
    return _plan_create(ws, "element", cid, item)


def _plan_new_relationship(ws: Workspace, args: argparse.Namespace) -> Plan:
    if args.type not in oracle.relationship_types():
        raise Refusal(f"'{args.type}' is not an ArchiMate relationship type"
                      f"{modelquery.did_you_mean(args.type, oracle.relationship_types())}")
    for flag, value in (("--from", args.source_id), ("--to", args.target_id)):
        if value not in ws.model.elements:
            raise Refusal(f"{flag} '{value}' is not an element of the model"
                          f"{modelquery.did_you_mean(value, ws.model.elements)}")
    default = f"rel-{args.source_id}-{args.type.lower()}-{args.target_id}"
    cid = _check_id(args.id if args.id is not None else default,
                    "pass --id to choose one" if args.id is None else "choose another")
    _claimed(ws, cid)
    item: dict[str, Any] = {"id": cid, "type": args.type, "source": args.source_id, "target": args.target_id}
    if args.access_type is not None:
        item["accessType"] = args.access_type
    if args.directed is not None:
        item["isDirected"] = args.directed
    if args.modifier is not None:
        item["modifier"] = args.modifier
    _with_evidence(item, args)
    creator = _creator(args, ws.root)
    notes: list[str] = []
    if "properties" in genschema.load_schema()["$defs"]["relationship"]["properties"]:
        item["properties"] = _new_properties(args, creator)
    else:
        notes.append("createdBy was not recorded: the schema does not allow properties on relationships")
    plan = _plan_create(ws, "relationship", cid, item)
    plan.notes.extend(notes)
    return plan


def _plan_create(ws: Workspace, kind: str, cid: str, item: dict[str, Any]) -> Plan:
    path = ws.staging_dir / f"{cid}.yaml"
    text = _render(SECTIONS[kind], item)
    return Plan("new", kind, cid, path, {path: text}, ("create", text), refuse_approximate={cid})


# ---- locating a concept for set / retire / remove


@dataclass
class Located:
    path: Path  # the staging file to edit
    text: str
    original: bytes | None  # None when the staging file does not exist yet (a copy of the approved file)
    notes: list[str]


def _locate(ws: Workspace, kind: str, cid: str) -> tuple[Any, Located]:
    section = SECTIONS[kind]
    bucket = getattr(ws.model, section)
    noun = "an element" if kind == "element" else "a relationship"
    if cid not in bucket:
        other = "a relationship" if kind == "element" else "an element"
        if cid in (ws.model.relationships if kind == "element" else ws.model.elements):
            raise Refusal(f"'{cid}' is {other}, not {noun}")
        if cid in ws.model.views:
            raise Refusal(f"'{cid}' is a view, not {noun}")
        raise Refusal(f"'{cid}' is not {noun} in the model{modelquery.did_you_mean(cid, bucket)}")
    concept = bucket[cid]
    path = concept.source_path
    assert path is not None
    approved_twin = getattr(ws.approved_model, section).get(cid)
    if ws.zone_of(path) == "staging":
        if approved_twin is not None and approved_twin.source_path is not None:
            arel = approved_twin.source_path.relative_to(ws.approved_dir)
            srel = path.relative_to(ws.staging_dir)
            if arel != srel:
                raise Refusal(
                    f"'{cid}' is in the approved file model/approved/{arel.as_posix()} and also in the staging "
                    f"file {ws.rel(path)}, a different path. Promote or delete that staging file first.")
        data = path.read_bytes()
        return concept, Located(path, data.decode("utf-8"), data, [])
    relative = path.relative_to(ws.approved_dir)
    target = ws.staging_dir / relative
    data = path.read_bytes()
    note = (f"staged a copy of model/approved/{relative.as_posix()}; promote replaces the approved file "
            "with it, and approved/ is untouched until then")
    return concept, Located(target, data.decode("utf-8"), None, [note])


# ---- surgical block editing


def _comment_or_blank(line: str) -> bool:
    body, _ = records._split_eol(line)
    return not body.strip() or body.lstrip().startswith("#")


@dataclass
class Block:
    lines: list[str]
    key_index: int
    start: int
    end: int  # exclusive; trailing blanks and comments excluded
    list_end: int
    list_indent: int
    content_col: int
    items: list[Any]
    index: int
    count: int


def _find_block(text: str, section: str, cid: str) -> tuple[Block, dict]:
    data = records._check_simple(text)
    items = data.get(section)
    if not isinstance(items, list):
        raise records.EditRefused(f"'{section}' is not a plain list")
    matches = [i for i, x in enumerate(items) if isinstance(x, dict) and str(x.get("id", "")) == cid]
    if len(matches) != 1:
        raise records.EditRefused(f"'{cid}' does not appear exactly once under '{section}'")
    lines = records._lines(text)
    key_index = records._find_key(lines, section)
    if key_index is None:
        raise records.EditRefused(f"cannot find the top-level key '{section}'")
    head, _ = records._split_eol(lines[key_index])
    if not re.fullmatch(rf"{re.escape(section)}[ \t]*:[ \t]*(#.*)?", head):
        raise records.EditRefused(f"'{section}' is not written as a block list")
    starts: list[int] = []
    list_indent: int | None = None
    list_end = len(lines)
    for i in range(key_index + 1, len(lines)):
        body, _ = records._split_eol(lines[i])
        if _comment_or_blank(lines[i]):
            continue
        indent = len(body) - len(body.lstrip(" "))
        if body[indent:indent + 1] == "\t":
            raise records.EditRefused("the file indents with tabs")
        is_item = re.match(r"-(\s|$)", body[indent:]) is not None
        if list_indent is None:
            if not is_item:
                raise records.EditRefused(f"'{section}' is not written as a block list")
            list_indent = indent
        if indent < list_indent or (indent == list_indent and not is_item):
            list_end = i
            break
        if indent == list_indent:
            starts.append(i)
    if list_indent is None or len(starts) != len(items):
        raise records.EditRefused(f"cannot match the items of '{section}' to lines of the file")
    index = matches[0]
    start = starts[index]
    end = starts[index + 1] if index + 1 < len(starts) else list_end
    body, _ = records._split_eol(lines[start])
    dash = re.match(r"( *)-( +)(\S.*)$", body)
    if dash is None or not _PLAIN_KEY.match(dash.group(3)):
        raise records.EditRefused(f"the block of '{cid}' is not written as plain block YAML")
    content_col = len(dash.group(1)) + 1 + len(dash.group(2))
    # Trailing blanks and comments are not part of the block, but a '#' line indented deeper than
    # the block's own keys is the content of a block scalar, so it stays with the block.
    while end > start + 1 and (
        not lines[end - 1].strip()
        or (_comment_or_blank(lines[end - 1]) and len(lines[end - 1]) - len(lines[end - 1].lstrip(" ")) <= content_col)
    ):
        end -= 1
    return Block(lines, key_index, start, end, list_end, list_indent, content_col, items, index, len(starts)), data


def _dedent(lines: list[str], col: int) -> str:
    out = [lines[0][col:]]
    for line in lines[1:]:
        if not line.strip():
            out.append(line)
        elif line.startswith(" " * col):
            out.append(line[col:])
        else:
            raise records.EditRefused("a line inside the block is indented less than the block")
    return "".join(out)


def _indent(mini: str, col: int, first_prefix: str) -> list[str]:
    out = []
    for n, line in enumerate(records._lines(mini)):
        if n == 0:
            out.append(first_prefix + line)
        elif not line.strip():
            out.append(line)
        else:
            out.append(" " * col + line)
    return out


def _set_property(mini: str, key: str, value: str) -> str:
    old = records._check_simple(mini)
    lines = records._lines(mini)
    nl = records._newline(mini)
    at = records._find_key(lines, "properties")
    if at is None:
        anchor = next((k for k in reversed(_FIELD_ORDER) if k in old), None)
        if anchor is None:
            raise records.EditRefused("cannot place a new 'properties' mapping")
        line = f"properties:{nl}  {key}: {records._dump_scalar(value)}"
        return records._verify(records.insert_after(mini, anchor, line), {**old, "properties": {key: value}})
    head, _ = records._split_eol(lines[at])
    if not re.fullmatch(r"properties[ \t]*:[ \t]*(#.*)?", head):
        raise records.EditRefused("'properties' is not written as a block mapping")
    current = old.get("properties")
    if current is None:
        return records._verify(records.insert_after(mini, "properties", f"  {key}: {records._dump_scalar(value)}"),
                               {**old, "properties": {key: value}})
    if not isinstance(current, dict):
        raise records.EditRefused("'properties' is not a mapping")
    stop = at + 1
    while stop < len(lines) and (not lines[stop].strip() or lines[stop][:1] == " "):
        stop += 1
    while stop > at + 1 and not lines[stop - 1].strip():
        stop -= 1
    children = lines[at + 1:stop]
    first = next((line for line in children if line.strip()), None)
    if first is None:
        raise records.EditRefused("'properties' has no readable entries")
    col = len(first) - len(first.lstrip(" "))
    sub = _dedent_children(children, col)
    sub_old = records._check_simple(sub)
    if sub_old != dsl._normalize_scalars(current):
        raise records.EditRefused("cannot read the 'properties' block reliably")
    after = None if key in sub_old else list(sub_old)[-1]
    new_sub = records.set_scalar(sub, key, value, after=after)
    new_children = _indent_children(new_sub, col)
    result = "".join(lines[:at + 1] + new_children + lines[stop:])
    return records._verify(result, {**old, "properties": {**current, key: value}})


def _dedent_children(children: list[str], col: int) -> str:
    out = []
    for line in children:
        if not line.strip():
            out.append(line)
        elif line.startswith(" " * col):
            out.append(line[col:])
        else:
            raise records.EditRefused("a property line is indented less than the first one")
    return "".join(out)


def _indent_children(text: str, col: int) -> list[str]:
    return [line if not line.strip() else " " * col + line for line in records._lines(text)]


def _set_field(mini: str, name: str, value: str) -> str:
    old = records._check_simple(mini)
    if name.startswith("properties."):
        return _set_property(mini, name[len("properties."):], value)
    position = _FIELD_ORDER.index(name)
    anchor = next((k for k in reversed(_FIELD_ORDER[:position]) if k in old), None)
    return records.set_scalar(mini, name, value, after=anchor)


def _edit_block(text: str, section: str, cid: str, edit: Callable[[str], str], expected_item: Callable[[dict], dict]) -> str:
    block, data = _find_block(text, section, cid)
    first = block.lines[block.start]
    prefix = first[:block.content_col]
    mini = _dedent(block.lines[block.start:block.end], block.content_col)
    new_block = _indent(edit(mini), block.content_col, prefix)
    new_text = "".join(block.lines[:block.start] + new_block + block.lines[block.end:])
    items = list(block.items)
    items[block.index] = expected_item(dict(items[block.index]))
    return records._verify(new_text, {**data, section: items})


def _remove_block(text: str, section: str, cid: str) -> tuple[str, dict]:
    block, data = _find_block(text, section, cid)
    lines = list(block.lines)
    start, end = block.start, block.end
    if start - 1 > block.key_index and not lines[start - 1].strip():
        start -= 1
    elif end < len(lines) and end < block.list_end and not lines[end].strip():
        end += 1
    del lines[start:end]
    items = [x for n, x in enumerate(block.items) if n != block.index]
    if block.count == 1:
        at = block.key_index
        body, eol = records._split_eol(lines[at])
        lines[at] = re.sub(rf"^({re.escape(section)}[ \t]*:)", r"\1 []", body) + eol
    remaining = {**data, section: items}
    return records._verify("".join(lines), remaining), remaining


def _plan_edit(ws: Workspace, args: argparse.Namespace, action: str, field_name: str, value: str) -> Plan:
    concept, located = _locate(ws, "element", args.id)
    if field_name.startswith("properties."):
        key = field_name[len("properties."):]
        current: Any = concept.properties.get(key)
    else:
        current = getattr(concept, _ATTRIBUTE[field_name])
    if current == value:
        noop = f"element {concept.id} is already retired" if action == "retire" else f"unchanged element {concept.id}"
        return Plan(action, "element", concept.id, located.path, noop=noop)

    def expected(item: dict) -> dict:
        if field_name.startswith("properties."):
            item["properties"] = {**(item.get("properties") or {}), key: value}
        else:
            item[field_name] = value
        return item

    new_text = _edit_block(located.text, "elements", concept.id, lambda mini: _set_field(mini, field_name, value), expected)
    return _plan_write(ws, action, "element", concept.id, located, new_text, {concept.id})


def _plan_write(ws: Workspace, action: str, kind: str, cid: str, located: Located, new_text: str,
                approximate: set[str]) -> Plan:
    operation = ("create", new_text) if located.original is None else ("replace", new_text, located.original)
    return Plan(action, kind, cid, located.path, {located.path: new_text}, operation, list(located.notes),
                refuse_approximate=approximate)


def _plan_remove(ws: Workspace, args: argparse.Namespace, kind: str) -> Plan:
    concept, located = _locate(ws, kind, args.id)
    cid = concept.id
    if args.confirm != cid:
        raise Refusal(f"--confirm must repeat the id exactly ('--confirm {cid}') to remove {kind} '{cid}'")
    if kind == "element":
        pointing = sorted(r.id for r in ws.model.relationships.values() if cid in (r.source, r.target))
        if pointing:
            raise Refusal(f"relationships still point at '{cid}': {', '.join(pointing)}. Remove them first with "
                          f"'relations remove ID --confirm ID'.")
    new_text, remaining = _remove_block(located.text, SECTIONS[kind], cid)
    relative = located.path.relative_to(ws.staging_dir)
    shadows_approved = (ws.approved_dir / relative).is_file()
    empty = all(not remaining.get(s) for s in CONCEPT_SECTIONS) and set(remaining) <= set(CONCEPT_SECTIONS)
    if located.original is not None and empty and not shadows_approved:
        return Plan("remove", kind, cid, located.path, {located.path: None}, ("delete", located.original))
    return _plan_write(ws, "remove", kind, cid, located, new_text, set())


# --------------------------------------------------------------------------- executing a plan


def _conflict(path: Path) -> Refusal:
    return Refusal(f"{path.name} changed on disk while the command ran; nothing was written. Re-run the command.")


def _apply(ws: Workspace, plan: Plan) -> None:
    kind, *rest = plan.operation
    if kind == "create":
        try:
            records.write_new(plan.path, rest[0])
        except records.WouldOverwrite:
            raise _conflict(plan.path) from None
        return
    original = rest[-1]
    _before_swap(plan.path)
    try:
        if plan.path.read_bytes() != original:
            raise _conflict(plan.path)
    except FileNotFoundError:
        raise _conflict(plan.path) from None
    if kind == "replace":
        records.replace_file(plan.path, rest[0])
        return
    plan.path.unlink()
    parent = plan.path.parent
    while parent != ws.staging_dir and parent.is_relative_to(ws.staging_dir) and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


_VERBS = {"new": ("created", "create"), "set": ("changed", "change"), "retire": ("retired", "retire"),
          "remove": ("removed", "remove")}


def _emit(args: argparse.Namespace, ws: Workspace, plan: Plan, written: bool, findings: list[dict[str, str]]) -> int:
    file = ws.rel(plan.path)
    if args.as_json:
        records.print_json({"ok": True, "action": plan.action, "kind": plan.kind, "id": plan.cid, "file": file,
                            "written": written, "findings": findings, "notes": plan.notes})
        return 0
    if plan.noop:
        print(plan.noop)
    else:
        past, would = _VERBS[plan.action]
        print(f"{past if written else 'would ' + would} {plan.kind} {plan.cid} in {file}")
    for finding in findings:
        print(f"warning: {finding['code']} {finding['message']}", file=sys.stderr)
    for note in plan.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0


def _refuse(args: argparse.Namespace, message: str, findings: list[dict[str, str]] | None = None) -> int:
    if args.as_json:
        document: dict[str, Any] = {"ok": False, "error": message}
        if findings is not None:
            document["findings"] = findings
        records.print_json(document)
        return 1
    print(f"error: {message}", file=sys.stderr)
    for finding in findings or []:
        print(f"{finding['severity']}: {finding['code']} {finding['message']}", file=sys.stderr)
    return 1


def _run(args: argparse.Namespace, build: Callable[[Workspace], Plan]) -> int:
    root = args.root.resolve()
    try:
        ws = Workspace(root)
        plan = build(ws)
        if plan.noop:
            return _emit(args, ws, plan, False, [])
        findings = _check(ws, plan.changes, plan.refuse_approximate)
        if args.dry_run:
            return _emit(args, ws, plan, False, findings)
        _apply(ws, plan)
        return _emit(args, ws, plan, True, findings)
    except Refusal as refusal:
        return _refuse(args, refusal.message, refusal.findings)
    except records.EditRefused as exc:
        return _refuse(args, f"refused, the edit is not plainly safe: {exc}")
    except (OSError, UnicodeDecodeError, *dsl.YAML_ERRORS) as exc:
        return _refuse(args, f"cannot complete: {' '.join(str(exc).split())}")


# --------------------------------------------------------------------------- commands


def _evidence_counts(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if len(args.source or []) != len(args.quote or []):
        parser.error("each --source needs a matching --quote (the Nth --source pairs with the Nth --quote)")


def cmd_elements_new(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    _evidence_counts(args, parser)
    return _run(args, lambda ws: _plan_new_element(ws, args))


def cmd_relations_new(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    _evidence_counts(args, parser)
    return _run(args, lambda ws: _plan_new_relationship(ws, args))


_NAME_PROBLEMS = {
    "id": "id is not changeable: it is the stable key everything refers to",
    "type": "changing a type changes which relationships are legal; use retire plus new",
    "provenance": "provenance cannot be set with this command",
    "properties.createdBy": "createdBy records who created the concept and is never changed",
}


def _check_field(name: str, value: str) -> None:
    if name in _NAME_PROBLEMS:
        raise Refusal(f"cannot set '{name}': {_NAME_PROBLEMS[name]}")
    if name.startswith("properties."):
        key = name[len("properties."):]
        if not _PROPERTY_KEY.fullmatch(key):
            raise Refusal(f"'{name}' is not a property name (properties.KEY with letters, digits, '_', '.', '-')")
        return
    if name not in _SETTABLE:
        raise Refusal(f"cannot set '{name}'. Fields: {', '.join(_SETTABLE)}, properties.KEY")
    if name == "name" and (not value.strip() or len(value) > NAME_LIMIT):
        raise Refusal(f"name must be 1 to {NAME_LIMIT} characters")
    if name == "owner" and not value.strip():
        raise Refusal("owner must not be empty")
    if name == "lastReviewed":
        try:
            if not _DATE.fullmatch(value):
                raise ValueError
            _datetime.date.fromisoformat(value)
        except ValueError:
            raise Refusal(f"lastReviewed must be a real date written YYYY-MM-DD, not '{value}'") from None


def cmd_elements_set(args: argparse.Namespace) -> int:
    def build(ws: Workspace) -> Plan:
        _check_field(args.field, args.value)
        return _plan_edit(ws, args, "set", args.field, args.value)

    return _run(args, build)


def cmd_elements_retire(args: argparse.Namespace) -> int:
    return _run(args, lambda ws: _plan_edit(ws, args, "retire", "properties.lifecycle", "retired"))


def cmd_elements_remove(args: argparse.Namespace) -> int:
    return _run(args, lambda ws: _plan_remove(ws, args, "element"))


def cmd_relations_remove(args: argparse.Namespace) -> int:
    return _run(args, lambda ws: _plan_remove(ws, args, "relationship"))


# --------------------------------------------------------------------------- argparse wiring


def _property(value: str) -> tuple[str, str]:
    key, separator, wanted = value.partition("=")
    if not separator or not _PROPERTY_KEY.fullmatch(key):
        raise argparse.ArgumentTypeError(f"{value!r} is not key=value")
    return key, wanted


def _date(value: str) -> str:
    return records._date_arg(value)


def _boolean(value: str) -> bool:
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise argparse.ArgumentTypeError(f"{value!r} is not true or false")


def _common(leaf: argparse.ArgumentParser) -> None:
    leaf.add_argument("--dry-run", action="store_true", help="name the target file and print the findings, write nothing")


def _evidence_args(leaf: argparse.ArgumentParser) -> None:
    leaf.add_argument("--source", action="append", metavar="FILE", help="source file of a citation (repeatable; pairs with --quote)")
    leaf.add_argument("--quote", action="append", metavar="TEXT", help="verbatim quote from the Nth --source")
    leaf.add_argument("--fact", action="append", metavar="ID", help="fact in the register that evidences this (repeatable)")
    leaf.add_argument("--assumed", action="store_true", help="not evidenced by a source; needs --rationale")
    leaf.add_argument("--rationale", metavar="TEXT", help="why the concept is assumed")
    leaf.add_argument("--created-by", metavar="NAME", help="who created it (default: git config user.name; a bot name is refused)")


def _bound(sub: Any, name: str, help_text: str, func: Callable[..., int]) -> argparse.ArgumentParser:
    leaf = records.leaf(sub, name, help_text, func)
    leaf.set_defaults(func=functools.partial(func, parser=leaf))
    _common(leaf)
    return leaf


def add_leaves(elements_sub: Any, relations_sub: Any) -> None:
    """Join the write commands to the ``elements`` and ``relations`` groups."""
    p_new = _bound(elements_sub, "new", "stage a new element in its own file", cmd_elements_new)
    p_new.add_argument("--type", required=True, help="ArchiMate element type")
    p_new.add_argument("--name", required=True)
    p_new.add_argument("--id", help="default: the slug of the name")
    p_new.add_argument("--documentation")
    p_new.add_argument("--property", action="append", type=_property, metavar="KEY=VALUE", help="repeatable")
    p_new.add_argument("--owner")
    p_new.add_argument("--last-reviewed", type=_date, metavar="YYYY-MM-DD")
    _evidence_args(p_new)

    p_set = records.leaf(elements_sub, "set", "change one field of an element in staging", cmd_elements_set)
    _common(p_set)
    p_set.add_argument("id", metavar="ID")
    p_set.add_argument("field", metavar="FIELD", help="name, documentation, owner, lastReviewed or properties.KEY")
    p_set.add_argument("value", metavar="VALUE")

    p_retire = records.leaf(elements_sub, "retire", "set properties.lifecycle to retired", cmd_elements_retire)
    _common(p_retire)
    p_retire.add_argument("id", metavar="ID")

    p_remove = records.leaf(elements_sub, "remove", "stage the removal of an element (hard delete)", cmd_elements_remove)
    _common(p_remove)
    p_remove.add_argument("id", metavar="ID")
    p_remove.add_argument("--confirm", required=True, metavar="ID", help="repeat the id to confirm")

    r_new = _bound(relations_sub, "new", "stage a new relationship in its own file", cmd_relations_new)
    r_new.add_argument("--type", required=True, help="ArchiMate relationship type")
    r_new.add_argument("--from", dest="source_id", required=True, metavar="ID")
    r_new.add_argument("--to", dest="target_id", required=True, metavar="ID")
    r_new.add_argument("--id", help="default: rel-<from>-<type>-<to>")
    r_new.add_argument("--access-type", help="Access relationships only")
    r_new.add_argument("--directed", type=_boolean, metavar="true|false", help="Association relationships only")
    r_new.add_argument("--modifier", help="Influence relationships only")
    _evidence_args(r_new)

    r_remove = records.leaf(relations_sub, "remove", "stage the removal of a relationship (hard delete)", cmd_relations_remove)
    _common(r_remove)
    r_remove.add_argument("id", metavar="ID")
    r_remove.add_argument("--confirm", required=True, metavar="ID", help="repeat the id to confirm")
