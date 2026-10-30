"""The read commands ``elements list``, ``elements show`` and ``relations list``.

Elements and relationships live many-per-file, so they are read through ``dsl.load_zone`` --
the loader (with the staging overlay) that ``validate`` and ``compile`` use -- and filtered
linearly. Nothing is written, cached or indexed; output is sorted by id.
"""

from __future__ import annotations

import argparse
import difflib
from pathlib import Path
from typing import Any, Iterable

from . import decisions, dsl, oracle, records

MAX_SUGGESTIONS = 5
FUZZY_CUTOFF = 0.6


def suggest_ids(wanted: str, known: Iterable[str]) -> list[str]:
    """Ids close to ``wanted``, at most five: prefix matches, then substring matches.

    Only when neither finds anything does difflib (cutoff 0.6) fill in, so a clear prefix or
    substring hit is never buried under loosely similar ids.
    """
    if not wanted:
        return []
    pool = sorted(set(known))
    prefix = [k for k in pool if k.startswith(wanted)]
    substring = [k for k in pool if wanted in k and k not in prefix]
    if prefix or substring:
        return (prefix + substring)[:MAX_SUGGESTIONS]
    matcher = difflib.SequenceMatcher()
    matcher.set_seq2(wanted)
    scored: list[tuple[float, str]] = []
    for candidate in pool:
        matcher.set_seq1(candidate)
        ratio = matcher.ratio()
        if ratio >= FUZZY_CUTOFF:
            scored.append((-ratio, candidate))
    fuzzy = [candidate for _ratio, candidate in sorted(scored)]
    return fuzzy[:MAX_SUGGESTIONS]


def did_you_mean(wanted: str, known: Iterable[str]) -> str:
    """The suffix appended to an unknown-id error; empty when nothing is close."""
    suggestions = suggest_ids(wanted, known)
    return f". Did you mean: {', '.join(suggestions)}?" if suggestions else ""


# --------------------------------------------------------------------------- loading


class Snapshot:
    """One load of a zone, with the notes and skipped files that belong to it."""

    def __init__(self, root: Path, zone: str) -> None:
        self.root = root
        self.zone = zone
        self.model, documents, _config = dsl.load_zone(root, zone)
        self.skipped = sorted(
            ({"file": self.rel(d.path), "reason": " ".join(d.parse_error.split())} for d in documents if d.data is None),
            key=lambda item: item["file"],
        )
        self.staging_dir = dsl.zone_dir(root, "staging")

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def zone_of(self, concept: dsl.Concept) -> str:
        path = concept.source_path
        if self.zone == "staging" and path is not None and path.is_relative_to(self.staging_dir):
            return "staging"
        return "approved"

    def duplicate_notes(self, buckets: Iterable[dict[str, Any]]) -> list[str]:
        wanted = [bucket for bucket in buckets]
        return [
            f"duplicate id '{key}' in {self.rel(path)}; the first file ({self.rel(first)}) wins"
            for key, first, path in self.model.duplicate_ids
            if any(key in bucket for bucket in wanted)
        ]

    def element_row(self, element: dsl.Element) -> dict[str, Any]:
        return {
            "id": element.id,
            "type": element.type,
            "layer": oracle.layer_of(element.type),
            "name": element.name,
            "zone": self.zone_of(element),
        }

    def relation_row(self, relationship: dsl.Relationship) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": relationship.id,
            "type": relationship.type,
            "source": relationship.source,
            "target": relationship.target,
            "zone": self.zone_of(relationship),
        }
        if relationship.access_type:
            row["accessType"] = relationship.access_type
        if relationship.is_directed is not None:
            row["isDirected"] = relationship.is_directed
        if relationship.modifier:
            row["modifier"] = relationship.modifier
        return row


def _snapshot(args: argparse.Namespace) -> Snapshot | None:
    try:
        return Snapshot(args.root.resolve(), args.zone)
    except (OSError, *dsl.YAML_ERRORS) as exc:
        records.fail(args, f"cannot read the {args.zone} model: {' '.join(str(exc).split())}")
        return None


def _emit_diagnostics(snap: Snapshot, notes: list[str]) -> None:
    import sys

    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    for item in snap.skipped:
        print(f"skipped: {item['file']}: {item['reason']}", file=sys.stderr)


# --------------------------------------------------------------------------- argparse types


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _property(value: str) -> tuple[str, str]:
    key, separator, wanted = value.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError(f"{value!r} is not key=value")
    return key, wanted


def _zone_args(leaf: argparse.ArgumentParser) -> None:
    leaf.add_argument("--zone", choices=("approved", "staging"), default="approved", help="zone to read (default: approved)")


# --------------------------------------------------------------------------- filtering


def _unknown_note(kind: str, value: str, known: Iterable[str]) -> str:
    suggestions = suggest_ids(value, known)
    hint = f" (did you mean: {', '.join(suggestions)}?)" if suggestions else ""
    return f"unknown {kind} '{value}'{hint}"


def _property_match(properties: dict[str, str], wanted: list[tuple[str, str]]) -> bool:
    by_key: dict[str, set[str]] = {}
    for key, value in wanted:
        by_key.setdefault(key, set()).add(value)
    return all(properties.get(key) in values for key, values in by_key.items())


def _finish(args: argparse.Namespace, snap: Snapshot, key: str, rows: list[dict], notes: list[str], printer) -> int:
    total = len(rows)
    if args.limit is not None:
        rows = rows[: args.limit]
    if args.as_json:
        records.print_json(
            {
                "ok": True,
                "zone": args.zone,
                key: rows,
                "count": len(rows),
                "total": total,
                "notes": notes,
                "skipped": snap.skipped,
            }
        )
    else:
        for row in rows:
            print(printer(row))
        _emit_diagnostics(snap, notes)
    return 0


def _element_line(row: dict[str, Any]) -> str:
    return f"{row['id']}  {row['type']}  {row['layer']}  {row['name']}  [{row['zone']}]"


def _relation_line(row: dict[str, Any]) -> str:
    extras = "".join(f"  {name}={row[name]}" for name in ("accessType", "isDirected", "modifier") if name in row)
    return f"{row['id']}  {row['type']}  {row['source']} -> {row['target']}  [{row['zone']}]{extras}"


def cmd_elements_list(args: argparse.Namespace) -> int:
    snap = _snapshot(args)
    if snap is None:
        return 1
    notes = snap.duplicate_notes([snap.model.elements])
    layers = {name.lower(): name for name in oracle.LAYER_ORDER}
    for value in args.type or []:
        if value not in oracle.element_types():
            notes.append(_unknown_note("type", value, oracle.element_types()))
    for value in args.layer or []:
        if value.lower() not in layers:
            notes.append(_unknown_note("layer", value, oracle.LAYER_ORDER))
    wanted_layers = {value.lower() for value in args.layer or []}
    needle = args.text.lower() if args.text else None
    rows = []
    for element in sorted(snap.model.elements.values(), key=lambda e: e.id):
        if args.type and element.type not in args.type:
            continue
        if wanted_layers and oracle.layer_of(element.type).lower() not in wanted_layers:
            continue
        if args.property and not _property_match(element.properties, args.property):
            continue
        if needle and not any(needle in field.lower() for field in (element.id, element.name, element.documentation)):
            continue
        rows.append(snap.element_row(element))
    return _finish(args, snap, "elements", rows, notes, _element_line)


def cmd_relations_list(args: argparse.Namespace) -> int:
    snap = _snapshot(args)
    if snap is None:
        return 1
    notes = snap.duplicate_notes([snap.model.relationships])
    for value in args.type or []:
        if value not in oracle.relationship_types():
            notes.append(_unknown_note("type", value, oracle.relationship_types()))
    for flag, values in (("from", args.sources), ("to", args.targets)):
        for value in values or []:
            if value not in snap.model.elements:
                notes.append(_unknown_note(f"--{flag} element", value, snap.model.elements))
    rows = []
    for relationship in sorted(snap.model.relationships.values(), key=lambda r: r.id):
        if args.type and relationship.type not in args.type:
            continue
        if args.sources and relationship.source not in args.sources:
            continue
        if args.targets and relationship.target not in args.targets:
            continue
        rows.append(snap.relation_row(relationship))
    return _finish(args, snap, "relationships", rows, notes, _relation_line)


# --------------------------------------------------------------------------- show


def _concept_document(snap: Snapshot, concept: dsl.Concept) -> dict[str, Any]:
    provenance = []
    for item in concept.provenance:
        entry = {key: value for key, value in (("file", item.file), ("quote", item.quote), ("fact", item.fact)) if value}
        provenance.append(entry)
    document: dict[str, Any] = {
        "id": concept.id,
        "type": concept.type,
        "name": concept.name,
        "documentation": concept.documentation,
        "owner": concept.owner,
        "lastReviewed": concept.last_reviewed,
        "assumed": concept.assumed,
        "rationale": concept.rationale,
        "properties": dict(concept.properties),
        "provenance": provenance,
        "zone": snap.zone_of(concept),
    }
    if isinstance(concept, dsl.Element):
        document["layer"] = oracle.layer_of(concept.type)
        if concept.applies_to:
            document["appliesTo"] = list(concept.applies_to)
        if concept.standards:
            document["standards"] = list(concept.standards)
    elif isinstance(concept, dsl.Relationship):
        document["source"] = concept.source
        document["target"] = concept.target
        row = snap.relation_row(concept)
        for name in ("accessType", "isDirected", "modifier"):
            if name in row:
                document[name] = row[name]
    return document


def _decisions_naming(root: Path, concept_id: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Decisions that name the id, and the decision files that could not be read (never a silent miss)."""
    loaded = records.load(decisions.RECORD_TYPE, root)
    skipped = sorted(
        ({"file": path.relative_to(root).as_posix(), "reason": " ".join(str(reason).split())} for path, reason in loaded.skipped),
        key=lambda item: item["file"],
    )
    found = []
    for record_id in sorted(loaded.records):
        data = loaded.records[record_id].data
        related = data.get("relatedElements")
        if isinstance(related, list) and concept_id in [str(x) for x in related]:
            found.append({"id": record_id, "title": data.get("title"), "status": data.get("status")})
    return found, skipped


def _show_text(document: dict[str, Any]) -> list[str]:
    concept = document.get("element") or document["relationship"]
    head = f"{concept['id']}  ({document['kind']}, {concept['type']}"
    if "layer" in concept:
        head += f", {concept['layer']}"
    lines = [f"{head}, {concept['zone']})", f"name: {concept['name']}"]
    if document["kind"] == "relationship":
        lines.append(f"from: {concept['source']}")
        lines.append(f"to: {concept['target']}")
        for name in ("accessType", "isDirected", "modifier"):
            if name in concept:
                lines.append(f"{name}: {concept[name]}")
    for name in ("documentation", "owner", "lastReviewed", "rationale"):
        if concept.get(name):
            lines.append(f"{name}: {concept[name]}")
    lines.append(f"assumed: {str(concept['assumed']).lower()}")
    for name in ("appliesTo", "standards"):
        if concept.get(name):
            lines.append(f"{name}: {', '.join(concept[name])}")
    if concept["properties"]:
        lines.append("properties:")
        lines.extend(f"  {key}: {value}" for key, value in concept["properties"].items())
    if concept["provenance"]:
        lines.append("provenance:")
        for item in concept["provenance"]:
            lines.append("  - " + ": ".join(str(item[key]) for key in ("file", "fact", "quote") if key in item))
    if "relationships" in document:
        lines.append("outgoing:")
        lines.extend(f"  {_relation_line(row)}" for row in document["relationships"]["outgoing"])
        lines.append("incoming:")
        lines.extend(f"  {_relation_line(row)}" for row in document["relationships"]["incoming"])
    lines.append("decisions:")
    lines.extend(f"  {d['id']}  {d['status'] or '-'}  {d['title'] or ''}".rstrip() for d in document["decisions"])
    lines.append("views:")
    lines.extend(f"  {v['id']}  {v['name']}" for v in document["views"])
    return lines


def cmd_elements_show(args: argparse.Namespace) -> int:
    snap = _snapshot(args)
    if snap is None:
        return 1
    model = snap.model
    wanted = args.id
    if wanted not in model.elements and wanted not in model.relationships:
        known = [*model.elements, *model.relationships]
        suggestions = suggest_ids(wanted, known)
        message = f"'{wanted}' is not an element or relationship in the {args.zone} model{did_you_mean(wanted, known)}"
        if args.as_json:
            records.print_json({"ok": False, "error": message, "suggestions": suggestions})
            return 1
        return records.fail(args, message)
    document: dict[str, Any] = {"ok": True, "zone": args.zone}
    if wanted in model.elements:
        element = model.elements[wanted]
        document["kind"] = "element"
        document["element"] = _concept_document(snap, element)
        document["relationships"] = {
            "outgoing": [snap.relation_row(r) for r in sorted(model.relationships.values(), key=lambda r: r.id) if r.source == wanted],
            "incoming": [snap.relation_row(r) for r in sorted(model.relationships.values(), key=lambda r: r.id) if r.target == wanted],
        }
        views = [{"id": v.id, "name": v.name} for v in model.views.values() if wanted in v.include]
    else:
        document["kind"] = "relationship"
        document["relationship"] = _concept_document(snap, model.relationships[wanted])
        views = []
    document["decisions"], decision_skipped = _decisions_naming(snap.root, wanted)
    document["views"] = views
    notes = snap.duplicate_notes([model.elements, model.relationships])
    if wanted in model.elements and wanted in model.relationships:
        notes.append(f"'{wanted}' is also a relationship id; showing the element")
    snap.skipped = sorted([*snap.skipped, *decision_skipped], key=lambda item: item["file"])
    if args.as_json:
        document["notes"] = notes
        document["skipped"] = snap.skipped
        records.print_json(document)
    else:
        print("\n".join(_show_text(document)))
        _emit_diagnostics(snap, notes)
    return 0


# --------------------------------------------------------------------------- argparse wiring


def register(subparsers: Any) -> None:
    elements = subparsers.add_parser("elements", help="list and show the elements of the model")
    esub = elements.add_subparsers(dest="elements_command", required=True)
    p_list = records.leaf(esub, "list", "one line per element, sorted by id", cmd_elements_list)
    _zone_args(p_list)
    p_list.add_argument("--layer", action="append", help="ArchiMate layer, case-insensitive (repeatable, any of)")
    p_list.add_argument("--type", action="append", help="element type (repeatable, any of)")
    p_list.add_argument("--property", action="append", type=_property, metavar="KEY=VALUE", help="exact property value (repeatable)")
    p_list.add_argument("--text", help="case-insensitive substring over id, name and documentation")
    p_list.add_argument("--limit", type=_limit, metavar="N", help="keep the first N rows")
    p_show = records.leaf(esub, "show", "one element or relationship in full, with its relationships, decisions and views", cmd_elements_show)
    _zone_args(p_show)
    p_show.add_argument("id", metavar="ID")

    relations = subparsers.add_parser("relations", help="list the relationships of the model")
    rsub = relations.add_subparsers(dest="relations_command", required=True)
    r_list = records.leaf(rsub, "list", "one line per relationship, sorted by id", cmd_relations_list)
    _zone_args(r_list)
    r_list.add_argument("--type", action="append", help="relationship type (repeatable, any of)")
    r_list.add_argument("--from", dest="sources", action="append", metavar="ID", help="source element (repeatable, any of)")
    r_list.add_argument("--to", dest="targets", action="append", metavar="ID", help="target element (repeatable, any of)")
    r_list.add_argument("--limit", type=_limit, metavar="N", help="keep the first N rows")
