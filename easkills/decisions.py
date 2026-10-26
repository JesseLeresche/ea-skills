"""The ``decisions`` command group: architecture decision records on the neutral record core."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from . import dsl, genschema, records

DECISIONS_DIR = Path("governance-log") / "decisions"


def _check_references(root: Path, data: dict) -> list[str]:
    """relatedElements must name elements of the approved model (same wording as DEC005)."""
    raw = data.get("relatedElements")
    if not isinstance(raw, list) or not raw:
        return []
    model, _documents, _config = dsl.load(root, "approved")
    return [
        f"relatedElements names '{element}' which is not in the approved model"
        for element in raw
        if str(element) not in model.elements
    ]


RECORD_TYPE = records.RecordType(
    name="decision",
    code_prefix="DEC",
    directory=DECISIONS_DIR,
    schema=genschema.load_decision_schema,
    key_order=(
        "id", "title", "status", "date", "context", "decision", "rationale", "consequences",
        "options", "supersedes", "supersededBy", "relatedElements",
    ),
    row_fields=("title", "status", "date", "supersededBy"),
    search_fields=("context", "decision", "rationale", "consequences"),
    text_fields=("context", "decision", "rationale", "consequences"),
    status_field="status",
    date_field="date",
    ref_field="relatedElements",
    ref_flag="element",
    initial_statuses=("proposed", "accepted", "rejected"),
    default_status="proposed",
    transitions={
        "proposed": frozenset({"accepted", "rejected"}),
        "accepted": frozenset({"deprecated"}),
    },
    superseded_status="superseded",
    live_status="accepted",
    successor_field="supersededBy",
    predecessors_field="supersedes",
    check_references=_check_references,
)


def register(subparsers: Any) -> argparse.ArgumentParser:
    return records.add_parser(subparsers, RECORD_TYPE, "decisions")
