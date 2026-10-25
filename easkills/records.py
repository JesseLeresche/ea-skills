"""A record-type-neutral core for file-per-record registers (list, show, new, set-status, supersede).

Everything specific to one kind of record -- its directory, schema, field names, status
vocabulary and transition table -- comes from a ``RecordType``; this module names none of
it. Adapters live in their own modules and register through ``add_parser``.

Edits to existing files are *surgical*: one or two lines change, comments, wording and
line endings stay, and anything that is not plainly simple YAML is refused (``EditRefused``)
rather than guessed at. Every edit is re-parsed and compared with the intended data before
it is allowed to reach disk.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple

import yaml
from jsonschema import Draft202012Validator

from . import dsl

_WIDE = 10**9
_DATE_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_NUMBERED = re.compile(r"^([0-9]{3,6})-(?![0-9]{2}(?:-|$))")


@dataclass(frozen=True)
class RecordType:
    name: str
    code_prefix: str
    directory: Path
    schema: Callable[[], dict]
    key_order: tuple[str, ...]
    row_fields: tuple[str, ...]
    search_fields: tuple[str, ...]
    text_fields: tuple[str, ...]
    status_field: str
    date_field: str
    ref_field: str
    ref_flag: str
    initial_statuses: tuple[str, ...]
    default_status: str
    transitions: Mapping[str, frozenset[str]]
    superseded_status: str
    live_status: str
    successor_field: str
    predecessors_field: str
    check_references: Callable[[Path, dict], list[str]] | None = None


@dataclass
class Record:
    id: str
    data: dict
    path: Path
    text: str
    number: int | None


@dataclass
class Loaded:
    records: dict[str, Record] = field(default_factory=dict)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    stems: set[str] = field(default_factory=set)


class Problem(NamedTuple):
    code: str
    locator: str
    message: str


class EditRefused(Exception):
    """The file is not simple enough to edit without guessing."""


class WouldOverwrite(Exception):
    """The target file already exists."""


# --------------------------------------------------------------------------- small helpers


def today() -> date:
    return date.today()


def sequence_number(record_id: str) -> int | None:
    """Leading number of ids shaped ``NNNN-slug``; date-prefixed ids are not numbered."""
    match = _NUMBERED.match(record_id)
    return int(match.group(1)) if match else None


def _real_date(value: Any) -> date | None:
    if not isinstance(value, str) or not _DATE_SHAPE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _date_arg(value: str) -> str:
    if _real_date(value) is None:
        raise argparse.ArgumentTypeError(f"{value!r} is not a real calendar date (YYYY-MM-DD)")
    return value


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _one_line(message: str) -> str:
    return " ".join(str(message).split())


def _as_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if isinstance(x, (str, int))]


# --------------------------------------------------------------------------- loading


def load(rtype: RecordType, root: Path) -> Loaded:
    loaded = Loaded()
    base = root / rtype.directory
    if not base.is_dir():
        return loaded
    files = sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in {".yaml", ".yml"})
    first_file: dict[str, Path] = {}
    for path in files:
        loaded.stems.add(path.stem)
        try:
            text = path.read_bytes().decode("utf-8")
            data = yaml.safe_load(text)
        except (OSError, UnicodeDecodeError) as exc:
            loaded.skipped.append((path, f"cannot read file: {_one_line(exc)}"))
            continue
        except dsl.YAML_ERRORS as exc:
            loaded.skipped.append((path, f"cannot parse YAML: {_one_line(exc)}"))
            continue
        if not isinstance(data, dict) or not data:
            loaded.skipped.append((path, "not a single non-empty YAML mapping"))
            continue
        raw_id = data.get("id")
        if raw_id is None or raw_id == "" or isinstance(raw_id, (dict, list)):
            loaded.skipped.append((path, "no id"))
            continue
        record_id = str(raw_id)
        if record_id in loaded.records:
            loaded.skipped.append((path, f"duplicate id '{record_id}' (first seen in {first_file[record_id].name})"))
            continue
        first_file[record_id] = path
        loaded.records[record_id] = Record(
            id=record_id,
            data=dsl._normalize_scalars(data),
            path=path,
            text=text,
            number=sequence_number(record_id),
        )
    return loaded


# --------------------------------------------------------------------------- rows


def _row(rtype: RecordType, record: Record) -> dict[str, Any]:
    row: dict[str, Any] = {"id": record.id, "number": record.number}
    for name in rtype.row_fields:
        row[name] = record.data.get(name)
    return row


def rows(
    rtype: RecordType,
    loaded: Loaded,
    statuses: tuple[str, ...] | list[str] = (),
    refs: tuple[str, ...] | list[str] = (),
    text: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    needle = text.lower() if text else None
    chosen: list[Record] = []
    for record in loaded.records.values():
        data = record.data
        if statuses and data.get(rtype.status_field) not in statuses:
            continue
        if refs and not set(refs) & set(_as_list(data.get(rtype.ref_field))):
            continue
        if needle is not None:
            parts = [record.id, str(data.get("title", "") or "")]
            parts += [str(data.get(name, "") or "") for name in rtype.search_fields]
            if needle not in "\n".join(parts).lower():
                continue
        if since is not None:
            when = data.get(rtype.date_field)
            if _real_date(when) is None or when < since:
                continue
        chosen.append(record)

    def order(record: Record) -> tuple:
        if record.number is not None:
            return (0, record.number, record.id)
        when = record.data.get(rtype.date_field)
        return (1, str(when) if when else "", record.id)

    return [_row(rtype, record) for record in sorted(chosen, key=order)]


# --------------------------------------------------------------------------- ids


def _slug(title: str, limit: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "record"
    if len(slug) <= limit:
        return slug
    kept = ""
    for word in slug.split("-"):
        candidate = f"{kept}-{word}" if kept else word
        if len(candidate) > limit:
            break
        kept = candidate
    return (kept or slug[:limit]).strip("-") or "record"


def allocate_id(rtype: RecordType, loaded: Loaded, title: str) -> str:
    numbers = [n for n in (sequence_number(i) for i in [*loaded.records, *loaded.stems]) if n is not None]
    number = max(numbers, default=0) + 1
    while True:
        prefix = f"{number:04d}-"
        candidate = prefix + _slug(str(title), 80 - len(prefix))
        if candidate not in loaded.records and candidate not in loaded.stems:
            return candidate
        number += 1


# --------------------------------------------------------------------------- rendering


def _dump_scalar(value: Any, force_double: bool = False) -> str:
    out = yaml.safe_dump(
        value, allow_unicode=True, width=_WIDE, default_style='"' if force_double else None
    ).rstrip("\n")
    if out.endswith("\n..."):
        out = out[: -len("\n...")]
    return out


def _dump_flow(value: Any) -> str:
    return yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=_WIDE).strip()


def _block_scalar(value: str, content_indent: int) -> list[str] | None:
    """Lines of a ``|`` block scalar for ``value``, or None when that would not be exact."""
    if "\t" in value or "\r" in value:
        return None
    stripped = value.rstrip("\n")
    if not stripped.strip():
        return None
    body = stripped.split("\n")
    first = next(line for line in body if line)
    if first.startswith(" "):
        return None
    if any(line != line.rstrip() for line in body):
        return None
    trailing = len(value) - len(stripped)
    pad = " " * content_indent
    lines = [pad + line if line else "" for line in body]
    lines += [""] * max(trailing - 1, 0)
    indicator = "|-" if trailing == 0 else "|" if trailing == 1 else "|+"
    return [indicator, *lines]


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _prune(value: Any) -> Any:
    if isinstance(value, dict):
        pruned = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if not _is_empty(v)}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def _emit(rtype: RecordType, key: str, value: Any, indent: int, prefix: str = "") -> list[str]:
    """Lines for ``key: value`` at column ``indent``; ``prefix`` ("- ") leads the first line."""
    lead = " " * indent + prefix
    if isinstance(value, str):
        if key == rtype.date_field and _real_date(value):
            return [f"{lead}{key}: {value}"]
        if "\n" in value:
            block = _block_scalar(value, indent + len(prefix) + 2)
            if block is not None:
                return [f"{lead}{key}: {block[0]}", *block[1:]]
            return [f"{lead}{key}: {_dump_scalar(value, force_double=True)}"]
        return [f"{lead}{key}: {_dump_scalar(value)}"]
    if isinstance(value, list):
        if all(isinstance(x, dict) for x in value):
            lines = [f"{lead}{key}:"]
            for item in value:
                for index, (name, inner) in enumerate(item.items()):
                    lines += _emit(rtype, name, inner, indent + 2, "- " if index == 0 else "  ")
            return lines
        return [f"{lead}{key}: {_dump_flow(value)}"]
    if isinstance(value, dict):
        return [f"{lead}{key}: {_dump_flow(value)}"]
    return [f"{lead}{key}: {_dump_scalar(value)}"]


def _ordered(rtype: RecordType, data: dict) -> dict:
    known = [k for k in rtype.key_order if k in data]
    return {k: data[k] for k in [*known, *sorted(k for k in data if k not in rtype.key_order)]}


def render_yaml(rtype: RecordType, data: dict) -> str:
    wanted = _prune(dsl._normalize_scalars(dict(data)))
    ordered = _ordered(rtype, wanted)
    lines: list[str] = []
    for key, value in ordered.items():
        lines += _emit(rtype, key, value, 0)
    text = "\n".join(lines) + "\n"
    try:
        exact = dsl._normalize_scalars(yaml.safe_load(text)) == wanted
    except dsl.YAML_ERRORS:
        exact = False
    if not exact:
        text = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=_WIDE)
    return text


# --------------------------------------------------------------------------- validation


def validate_candidate(rtype: RecordType, root: Path, data: dict, references: bool = True) -> list[Problem]:
    data = dsl._normalize_scalars(data)
    code = f"{rtype.code_prefix}001"
    problems: list[Problem] = []
    validator = Draft202012Validator(rtype.schema())
    for error in validator.iter_errors(data):
        locator = "/" + "/".join(str(p) for p in error.absolute_path)
        problems.append(Problem(code, locator, error.message))
    when = data.get(rtype.date_field)
    if isinstance(when, str) and _DATE_SHAPE.fullmatch(when) and _real_date(when) is None:
        problems.append(Problem(code, f"/{rtype.date_field}", f"'{when}' is not a real calendar date"))
    if references and rtype.check_references is not None:
        for message in rtype.check_references(root, data):
            problems.append(Problem(f"{rtype.code_prefix}005", f"/{rtype.ref_field}", message))
    return sorted(problems)


# --------------------------------------------------------------------------- surgical edits

_EOL = re.compile(r"(\r\n|\n)$")


def _lines(text: str) -> list[str]:
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _split_eol(line: str) -> tuple[str, str]:
    match = _EOL.search(line)
    return (line[: match.start()], match.group(1)) if match else (line, "")


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _key_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(key)}[ \t]*:(?=[ \t]|$)")


def _quoted_key_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf"""^["']{re.escape(key)}["'][ \t]*:""")


def _find_key(lines: list[str], key: str) -> int | None:
    plain, quoted = _key_pattern(key), _quoted_key_pattern(key)
    hits = []
    for index, line in enumerate(lines):
        body, _ = _split_eol(line)
        if quoted.match(body):
            raise EditRefused(f"key '{key}' is written in a quoted form")
        if plain.match(body):
            hits.append(index)
    if len(hits) > 1:
        raise EditRefused(f"key '{key}' appears more than once")
    return hits[0] if hits else None


def _check_simple(text: str) -> dict:
    """Parse ``text`` and refuse anything that is not a plain block-mapping document."""
    try:
        tokens = list(yaml.scan(text, Loader=yaml.SafeLoader))
        data = yaml.safe_load(text)
    except dsl.YAML_ERRORS as exc:
        raise EditRefused(f"cannot parse the file as a single YAML document: {_one_line(exc)}") from None
    if not isinstance(data, dict):
        raise EditRefused("the file is not a YAML mapping")
    kinds = [type(t).__name__ for t in tokens]
    if "FlowMappingStartToken" in kinds[:4] or "BlockMappingStartToken" not in kinds[:4]:
        raise EditRefused("the document is not written as a block mapping")
    if kinds.count("DocumentStartToken") > 1:
        raise EditRefused("the file holds several YAML documents")
    for banned, what in (("AnchorToken", "anchors"), ("AliasToken", "aliases"), ("TagToken", "tags")):
        if banned in kinds:
            raise EditRefused(f"the file uses YAML {what}")
    return dsl._normalize_scalars(data)


def _verify(new_text: str, expected: dict) -> str:
    try:
        parsed = dsl._normalize_scalars(yaml.safe_load(new_text))
    except dsl.YAML_ERRORS:
        parsed = None
    if parsed != dsl._normalize_scalars(expected):
        raise EditRefused("the edit would change more than the intended fields")
    return new_text


def insert_after(text: str, anchor_key: str, new_line: str) -> str:
    lines = _lines(text)
    index = _find_key(lines, anchor_key)
    if index is None:
        raise EditRefused(f"cannot place a new line: key '{anchor_key}' not found")
    if index + 1 < len(lines) and lines[index + 1][:1] in (" ", "\t"):
        raise EditRefused(f"key '{anchor_key}' spans several lines")
    nl = _newline(text)
    anchor_body, anchor_eol = _split_eol(lines[index])
    if anchor_eol:
        lines.insert(index + 1, new_line + nl)
    else:
        lines[index] = anchor_body + nl
        lines.append(new_line)
    return "".join(lines)


_QUOTED = {
    '"': re.compile(r'"((?:[^"\\]|\\.)*)"(\s*(?:#.*)?)'),
    "'": re.compile(r"'((?:[^']|'')*)'(\s*(?:#.*)?)"),
}
_BARE = re.compile(r"(\S(?:.*?\S)?)(\s+#.*|\s*)")


def set_scalar(text: str, key: str, value: str, after: str | None = None) -> str:
    old = _check_simple(text)
    expected = {**old, key: value}
    lines = _lines(text)
    index = _find_key(lines, key)
    if index is None:
        if after is None:
            raise EditRefused(f"key '{key}' is absent and no anchor was given")
        return _verify(insert_after(text, after, f"{key}: {_dump_scalar(value)}"), expected)
    body, eol = _split_eol(lines[index])
    head = _key_pattern(key).match(body)
    assert head is not None
    rest = body[head.end():]
    spacing = rest[: len(rest) - len(rest.lstrip(" \t"))]
    token = rest[len(spacing):]
    if token[:1] in _QUOTED:
        match = _QUOTED[token[0]].fullmatch(token)
        if match is None:
            raise EditRefused(f"the value of '{key}' is not a simple one-line quoted scalar")
        if token[0] == '"':
            replacement = json.dumps(value, ensure_ascii=False)
        else:
            replacement = "'" + value.replace("'", "''") + "'"
        tail = match.group(2)
    else:
        if token == "" or token[0] in "#&*!|>%@`[{":
            raise EditRefused(f"the value of '{key}' is not a simple one-line scalar")
        match = _BARE.fullmatch(token)
        if match is None:
            raise EditRefused(f"the value of '{key}' is not a simple one-line scalar")
        replacement, tail = _dump_scalar(value), match.group(2)
    lines[index] = f"{body[: head.end()]}{spacing or ' '}{replacement}{tail}{eol}"
    return _verify("".join(lines), expected)


def _flow_close(rest: str) -> int | None:
    """Index of the bracket that closes the flow list opening ``rest``, or None."""
    depth, quote, index = 0, "", 0
    while index < len(rest):
        char = rest[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 1
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
        elif char == "#" and rest[index - 1 : index] in (" ", "\t"):
            return None
        index += 1
    return None


def add_to_list(text: str, key: str, item: str, after: str | None = None) -> str:
    old = _check_simple(text)
    lines = _lines(text)
    index = _find_key(lines, key)
    if index is None:
        if after is None:
            raise EditRefused(f"key '{key}' is absent and no anchor was given")
        return _verify(
            insert_after(text, after, f"{key}: {_dump_flow([item])}"), {**old, key: [item]}
        )
    current = old.get(key)
    if not isinstance(current, list) or any(isinstance(x, (dict, list)) for x in current):
        raise EditRefused(f"'{key}' is not a plain list")
    if item in [str(x) for x in current]:
        return text
    expected = {**old, key: [*current, item]}
    body, eol = _split_eol(lines[index])
    head = _key_pattern(key).match(body)
    assert head is not None
    rest = body[head.end():]
    stripped = rest.lstrip(" \t")
    nl = _newline(text)
    if stripped.startswith("["):
        offset = len(rest) - len(stripped)
        close = _flow_close(stripped)
        if close is None:
            raise EditRefused(f"the list '{key}' is not closed on its own line")
        inner = stripped[1:close]
        flow_item = _dump_flow([item])[1:-1]
        bare = inner.rstrip()
        if not bare:
            inner = flow_item
        elif bare.endswith(","):
            inner = f"{bare} {flow_item}{inner[len(bare):]}"
        else:
            inner = f"{bare}, {flow_item}{inner[len(bare):]}"
        new_rest = rest[:offset] + "[" + inner + stripped[close:]
        lines[index] = body[: head.end()] + new_rest + eol
        return _verify("".join(lines), expected)
    if stripped and not stripped.startswith("#"):
        raise EditRefused(f"the list '{key}' is not written as a plain flow or block list")
    last, indent = None, ""
    for position in range(index + 1, len(lines)):
        line_body, _ = _split_eol(lines[position])
        match = re.match(r"^(\s*)-(?:\s|$)", line_body)
        if match is None:
            if line_body.strip() and line_body[:1] in (" ", "\t"):
                raise EditRefused(f"the block list '{key}' has an item that spans several lines")
            break
        last, indent = position, match.group(1)
    if last is None:
        raise EditRefused(f"the block list '{key}' has no items to extend")
    _, last_eol = _split_eol(lines[last])
    new_line = f"{indent}- {_dump_scalar(item)}"
    if last_eol:
        lines.insert(last + 1, new_line + nl)
    else:
        lines[last] = lines[last] + nl
        lines.insert(last + 1, new_line)
    return _verify("".join(lines), expected)


# --------------------------------------------------------------------------- file writes


def _temp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")


def write_new(path: Path, text: str) -> None:
    """Create ``path`` with exactly ``text``; never replace an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_path(path)
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(text.encode("utf-8"))
        try:
            os.link(temp, path)
        except FileExistsError:
            raise WouldOverwrite(f"{path.name} already exists") from None
        except OSError:
            try:
                with open(path, "xb") as handle:
                    handle.write(text.encode("utf-8"))
            except FileExistsError:
                raise WouldOverwrite(f"{path.name} already exists") from None
    finally:
        temp.unlink(missing_ok=True)


def replace_file(path: Path, text: str) -> None:
    """Swap the content of an existing file atomically, keeping its mode."""
    mode = path.stat().st_mode & 0o7777
    temp = _temp_path(path)
    try:
        with open(temp, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- output


def _print_json(document: dict) -> None:
    print(json.dumps(document, indent=2, ensure_ascii=False))


def _fail(args: argparse.Namespace, message: str) -> int:
    if args.as_json:
        _print_json({"ok": False, "error": message})
    else:
        print(f"error: {message}", file=sys.stderr)
    return 1


def _fail_problems(args: argparse.Namespace, problems: list[Problem], file: str | None = None) -> int:
    if args.as_json:
        errors = [{"code": p.code, "locator": p.locator, "message": p.message} for p in problems]
        if file is not None:
            for error in errors:
                error["file"] = file
        _print_json({"ok": False, "errors": errors})
    else:
        for problem in problems:
            lead = f"{file}: " if file else ""
            print(f"{lead}{problem.code} {problem.locator}: {problem.message}", file=sys.stderr)
    return 1


def _lookup(rtype: RecordType, loaded: Loaded, key: str) -> tuple[Record | None, str]:
    if key in loaded.records:
        return loaded.records[key], ""
    if key.isdigit():
        matches = sorted((r for r in loaded.records.values() if r.number == int(key)), key=lambda r: r.id)
        if len(matches) == 1:
            return matches[0], ""
        if matches:
            names = ", ".join(r.id for r in matches)
            return None, f"{key} is ambiguous: it matches {names}; use the full id"
    return None, f"no {rtype.name} record '{key}'"


# --------------------------------------------------------------------------- commands


def cmd_list(rtype: RecordType, args: argparse.Namespace) -> int:
    loaded = load(rtype, args.root)
    found = rows(rtype, loaded, tuple(args.status or ()), tuple(args.refs or ()), args.text, args.since)
    skipped = [(_rel(args.root, path), reason) for path, reason in loaded.skipped]
    if args.as_json:
        _print_json({
            "type": rtype.name,
            "count": len(found),
            "records": found,
            "skipped": [{"file": file, "reason": reason} for file, reason in skipped],
        })
        return 0
    print(f"{len(found)} {rtype.name} record(s) in {rtype.directory.as_posix()}")
    width = max((len(row["id"]) for row in found), default=0)
    for row in found:
        status = row.get(rtype.status_field) or "-"
        when = row.get(rtype.date_field) or "-"
        line = f"{row['id'].ljust(width)}  {status}  {when}  {row.get('title') or '-'}"
        successor = row.get(rtype.successor_field)
        print(f"{line} -> {successor}" if successor else line)
    for file, reason in skipped:
        print(f"skipped: {file}: {reason} (run validate-gov)")
    return 0


def _links(rtype: RecordType, loaded: Loaded, record: Record) -> tuple[list[str], str | None]:
    declared = _as_list(record.data.get(rtype.predecessors_field))
    inferred = sorted(
        r.id for r in loaded.records.values()
        if r.data.get(rtype.successor_field) == record.id and r.id not in declared
    )
    successor = record.data.get(rtype.successor_field)
    if not successor:
        later = sorted(
            r.id for r in loaded.records.values() if record.id in _as_list(r.data.get(rtype.predecessors_field))
        )
        successor = later[0] if later else None
    return [*declared, *inferred], (str(successor) if successor else None)


def cmd_show(rtype: RecordType, args: argparse.Namespace) -> int:
    loaded = load(rtype, args.root)
    record, problem = _lookup(rtype, loaded, args.ref)
    if record is None:
        return _fail(args, problem)
    predecessors, successor = _links(rtype, loaded, record)
    file = _rel(args.root, record.path)
    data = _ordered(rtype, record.data)
    if args.as_json:
        _print_json({
            "record": data,
            "file": file,
            rtype.predecessors_field: predecessors,
            rtype.successor_field: successor,
        })
        return 0
    print(f"file: {file}")
    print(render_yaml(rtype, data), end="")
    if predecessors:
        print(f"resolved {rtype.predecessors_field}: {', '.join(predecessors)}")
    if successor:
        print(f"resolved {rtype.successor_field}: {successor}")
    return 0


def _read_input(args: argparse.Namespace) -> tuple[dict, str]:
    if args.from_file is not None:
        try:
            raw = args.from_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return {}, f"cannot read {args.from_file}: {_one_line(exc)}"
    elif args.from_stdin:
        raw = sys.stdin.read()
    else:
        return {}, ""
    try:
        data = yaml.safe_load(raw)
    except dsl.YAML_ERRORS as exc:
        return {}, f"the input is not valid YAML: {_one_line(exc)}"
    if data is None:
        return {}, ""
    if not isinstance(data, dict):
        return {}, "the input must be a YAML mapping"
    return dsl._normalize_scalars(data), ""


def cmd_new(rtype: RecordType, args: argparse.Namespace, parser: argparse.ArgumentParser | None = None) -> int:
    data, error = _read_input(args)
    if error:
        return _fail(args, error)
    if "id" in data:
        return _fail(args, "id is not accepted in the input: the tool allocates ids")
    for name in (rtype.successor_field, rtype.predecessors_field):
        if name in data:
            return _fail(args, f"{name} cannot be set when creating a record: use the supersede command")
    if args.title is not None:
        data["title"] = args.title
    if not data.get("title"):
        message = "a title is required: pass --title or supply one in the --from-file/--from-stdin input"
        if parser is not None:
            parser.error(message)
        return _fail(args, message)
    for name in rtype.text_fields:
        value = getattr(args, f"text__{name}", None)
        if value is not None:
            data[name] = value
    if args.refs:
        data[rtype.ref_field] = list(dict.fromkeys(args.refs))
    if args.status is not None:
        data[rtype.status_field] = args.status
    data.setdefault(rtype.status_field, rtype.default_status)
    if args.date is not None:
        data[rtype.date_field] = args.date
    data.setdefault(rtype.date_field, today().isoformat())

    loaded = load(rtype, args.root)
    data["id"] = allocate_id(rtype, loaded, str(data["title"]))
    problems = validate_candidate(rtype, args.root, data)
    status = data.get(rtype.status_field)
    if status not in rtype.initial_statuses:
        hint = f"; '{status}' is not set at creation" if status == rtype.superseded_status else ""
        problems = [p for p in problems if p.locator != f"/{rtype.status_field}"]
        problems.append(Problem(
            f"{rtype.code_prefix}001",
            f"/{rtype.status_field}",
            f"a new record starts as one of: {', '.join(rtype.initial_statuses)}{hint}",
        ))
        problems.sort()
    if problems:
        return _fail_problems(args, problems)

    record = _ordered(rtype, _prune(data))
    path = args.root / rtype.directory / f"{data['id']}.yaml"
    try:
        write_new(path, render_yaml(rtype, record))
    except WouldOverwrite as exc:
        return _fail(args, str(exc))
    except OSError as exc:
        return _fail(args, f"cannot write {_rel(args.root, path)}: {_one_line(exc)}")
    file = _rel(args.root, path)
    if args.as_json:
        _print_json({"ok": True, "id": data["id"], "file": file, "record": record})
    else:
        print(f"created {data['id']} {file}")
    return 0


def _check_text(rtype: RecordType, root: Path, text: str) -> list[Problem]:
    try:
        parsed = dsl._normalize_scalars(yaml.safe_load(text))
    except dsl.YAML_ERRORS as exc:
        return [Problem(f"{rtype.code_prefix}001", "/", f"cannot parse the edited file: {_one_line(exc)}")]
    if not isinstance(parsed, dict):
        return [Problem(f"{rtype.code_prefix}001", "/", "the edited file is not a mapping")]
    return validate_candidate(rtype, root, parsed, references=False)


def known_statuses(rtype: RecordType) -> list[str]:
    names = [*rtype.initial_statuses, rtype.default_status, rtype.live_status, rtype.superseded_status]
    for source, targets in rtype.transitions.items():
        names += [source, *sorted(targets)]
    return list(dict.fromkeys(names))


def cmd_set_status(rtype: RecordType, args: argparse.Namespace) -> int:
    loaded = load(rtype, args.root)
    record, problem = _lookup(rtype, loaded, args.ref)
    if record is None:
        return _fail(args, problem)
    current = record.data.get(rtype.status_field)
    target = args.status
    if not isinstance(current, str):
        return _fail(args, f"{record.id} has no readable {rtype.status_field}")
    if target == rtype.superseded_status:
        return _fail(args, f"'{target}' is only reached through the supersede command (supersede <old> <new>)")
    if target == current:
        if args.as_json:
            _print_json({"ok": True, "id": record.id, "from": current, "to": target, "changed": False})
        else:
            print(f"no change: {record.id} is already {current}")
        return 0
    allowed = rtype.transitions.get(current) or frozenset()
    if target not in allowed:
        if allowed:
            moves = f"from {current} the allowed moves are: {', '.join(sorted(allowed))}"
        else:
            moves = f"none: {current} is terminal"
        return _fail(args, f"cannot move {record.id} from {current} to {target}; {moves}")
    try:
        new_text = set_scalar(record.text, rtype.status_field, target)
    except EditRefused as exc:
        return _fail(args, f"refusing to edit {_rel(args.root, record.path)}: {exc}")
    problems = _check_text(rtype, args.root, new_text)
    if problems:
        return _fail_problems(args, problems, _rel(args.root, record.path))
    try:
        replace_file(record.path, new_text)
    except OSError as exc:
        return _fail(args, f"cannot write {_rel(args.root, record.path)}: {_one_line(exc)}")
    if args.as_json:
        _print_json({"ok": True, "id": record.id, "from": current, "to": target, "changed": True})
    else:
        print(f"{record.id}: {current} -> {target}")
    return 0


def cmd_supersede(rtype: RecordType, args: argparse.Namespace) -> int:
    loaded = load(rtype, args.root)
    old, problem = _lookup(rtype, loaded, args.old)
    if old is None:
        return _fail(args, problem)
    new, problem = _lookup(rtype, loaded, args.new)
    if new is None:
        return _fail(args, problem)
    if old.id == new.id:
        return _fail(args, f"{old.id} cannot supersede itself")
    old_status = old.data.get(rtype.status_field)
    new_status = new.data.get(rtype.status_field)
    named = old.data.get(rtype.successor_field)
    if named and str(named) != new.id:
        return _fail(args, f"{old.id} is already superseded by {named}")
    if old_status == rtype.superseded_status:
        if str(named or "") != new.id:
            return _fail(args, f"{old.id} is {old_status} but does not name {new.id}")
    elif old_status != rtype.live_status:
        return _fail(args, f"{old.id} is {old_status}; only a {rtype.live_status} record can be superseded")
    if new_status != rtype.live_status:
        return _fail(args, f"{new.id} is {new_status}; the superseding record must be {rtype.live_status}")

    try:
        old_text, new_text = old.text, new.text
        if old_status != rtype.superseded_status:
            old_text = set_scalar(old_text, rtype.status_field, rtype.superseded_status)
        if str(named or "") != new.id:
            old_text = set_scalar(old_text, rtype.successor_field, new.id, after=rtype.status_field)
        new_text = add_to_list(new_text, rtype.predecessors_field, old.id, after=rtype.status_field)
    except EditRefused as exc:
        return _fail(args, f"refusing to edit: {exc}")

    edits = [(r, before, after) for r, before, after in ((old, old.text, old_text), (new, new.text, new_text))
             if before != after]
    if not edits:
        if args.as_json:
            _print_json({"ok": True, "old": old.id, "new": new.id, "changed": []})
        else:
            print(f"already linked: {old.id} is superseded by {new.id}")
        return 0
    for record, _before, after in edits:
        problems = _check_text(rtype, args.root, after)
        if problems:
            return _fail_problems(args, problems, _rel(args.root, record.path))

    written: list[tuple[Record, str]] = []
    try:
        for record, before, after in edits:
            replace_file(record.path, after)
            written.append((record, before))
    except OSError as exc:
        failures = []
        for record, before in reversed(written):
            try:
                replace_file(record.path, before)
            except OSError as undo:
                failures.append(f"could not restore {_rel(args.root, record.path)}: {_one_line(undo)}")
        extra = f" ({'; '.join(failures)})" if failures else " (the first file was restored)" if written else ""
        return _fail(args, f"write failed: {_one_line(exc)}{extra}")
    changed = [_rel(args.root, record.path) for record, _, _ in edits]
    if args.as_json:
        _print_json({"ok": True, "old": old.id, "new": new.id, "changed": changed})
    else:
        print(f"{old.id} superseded by {new.id}")
        for file in changed:
            print(f"  updated {file}")
    return 0


# --------------------------------------------------------------------------- argparse wiring


def _leaf(sub: Any, name: str, help_text: str, func: Callable[[argparse.Namespace], int]) -> argparse.ArgumentParser:
    leaf = sub.add_parser(name, help=help_text)
    leaf.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: cwd)")
    leaf.add_argument("--json", dest="as_json", action="store_true", help="print one JSON document to stdout")
    leaf.set_defaults(func=func)
    return leaf


def add_parser(subparsers: Any, rtype: RecordType, command_name: str) -> argparse.ArgumentParser:
    group = subparsers.add_parser(command_name, help=f"list, show, create and update {rtype.name} records")
    sub = group.add_subparsers(dest=f"{command_name}_command", required=True)

    p_list = _leaf(sub, "list", "one line per record, never the bodies", functools.partial(cmd_list, rtype))
    p_list.add_argument("--status", action="append", help="only this status (repeatable, any of)")
    p_list.add_argument(
        f"--{rtype.ref_flag}", dest="refs", action="append", metavar="ID", help="only records naming this id (repeatable, any of)"
    )
    p_list.add_argument("--text", help="case-insensitive substring over id, title and body text")
    p_list.add_argument("--since", type=_date_arg, metavar="YYYY-MM-DD", help="only records dated on or after this day")

    p_show = _leaf(sub, "show", "print one record in full", functools.partial(cmd_show, rtype))
    p_show.add_argument("ref", metavar="ID_OR_NUMBER")

    p_new = _leaf(sub, "new", "create a record with the next free id", None)  # func set below
    p_new.set_defaults(func=functools.partial(cmd_new, rtype, parser=p_new))
    p_new.add_argument("--title")
    p_new.add_argument("--status", help=f"one of {', '.join(rtype.initial_statuses)} (default {rtype.default_status})")
    p_new.add_argument("--date", type=_date_arg, metavar="YYYY-MM-DD", help="default: today")
    p_new.add_argument(
        f"--{rtype.ref_flag}", dest="refs", action="append", metavar="ID", help="repeatable"
    )
    for name in rtype.text_fields:
        p_new.add_argument(f"--{name}", dest=f"text__{name}", metavar="TEXT")
    source = p_new.add_mutually_exclusive_group()
    source.add_argument("--from-file", type=Path, metavar="FILE.yaml", help="fields from a YAML mapping")
    source.add_argument("--from-stdin", action="store_true", help="fields from a YAML mapping on stdin")

    p_status = _leaf(
        sub, "set-status", "move a record along its allowed transitions", functools.partial(cmd_set_status, rtype)
    )
    p_status.add_argument("ref", metavar="ID_OR_NUMBER")
    p_status.add_argument("status", choices=known_statuses(rtype))

    p_super = _leaf(sub, "supersede", "link two records: the old one is replaced by the new one", functools.partial(cmd_supersede, rtype))
    p_super.add_argument("old", metavar="OLD")
    p_super.add_argument("new", metavar="NEW")
    return group
