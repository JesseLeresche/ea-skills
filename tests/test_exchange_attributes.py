"""Open Exchange relationship attributes must survive import -> compile.

The Exchange format carries three relationship attributes the DSL once had no field for:
``accessType`` (Access), ``isDirected`` (Association) and ``modifier`` (Influence). The
importer dropped them without a note and the compiler then wrote the defaults, so a
Read access came back as an unspecified one. These tests pin the round trip, the
authoring path and the wrong-type validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from easkills import aoef, dsl, importer, validate

NS = {"a": aoef.NS, "xsi": aoef.XSI}
XSI_TYPE = f"{{{aoef.XSI}}}type"

CRAFTED = """<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="http://www.opengroup.org/xsd/archimate/3.0/"
       xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
       xsi:schemaLocation="http://www.opengroup.org/xsd/archimate/3.0/ http://www.opengroup.org/xsd/archimate/3.1/archimate3_Diagram.xsd"
       identifier="id-model">
  <name>Attributes</name>
  <elements>
    <element identifier="e-app" xsi:type="ApplicationComponent"><name>Portal</name></element>
    <element identifier="e-data" xsi:type="DataObject"><name>Case</name></element>
    <element identifier="e-actor" xsi:type="BusinessActor"><name>Clinician</name></element>
    <element identifier="e-driver" xsi:type="Driver"><name>Waiting times</name></element>
    <element identifier="e-goal" xsi:type="Goal"><name>Faster triage</name></element>
  </elements>
  <relationships>
    <relationship identifier="r-access" source="e-app" target="e-data" xsi:type="Access" accessType="{access}"/>
    <relationship identifier="r-assoc" source="e-actor" target="e-app" xsi:type="Association" isDirected="{directed}"/>
    <relationship identifier="r-infl" source="e-driver" target="e-goal" xsi:type="Influence" modifier="{modifier}"/>
  </relationships>
</model>
"""


def _crafted(tmp_path: Path, access="Read", directed="true", modifier="++") -> Path:
    path = tmp_path / "crafted.xml"
    path.write_text(
        CRAFTED.format(access=access, directed=directed, modifier=modifier), encoding="utf-8"
    )
    return path


def _round_trip(tmp_path: Path, **values) -> tuple[importer.ImportReport, dict[str, etree._Element]]:
    """Import the crafted file, compile the staged result, return the report and the
    compiled relationship nodes by xsi:type."""
    source = _crafted(tmp_path, **values)
    root = tmp_path / "repo"
    report = importer.import_exchange(root, source, ids="identifiers")
    result = aoef.compile_model(root, zone="staging", out=tmp_path / "out.xml")
    assert result.schema_errors == [], "\n".join(result.schema_errors)
    tree = etree.fromstring(result.xml)
    nodes = {n.get(XSI_TYPE): n for n in tree.findall("a:relationships/a:relationship", NS)}
    return report, nodes


# ------------------------------------------------------------------ round trip


def test_access_type_survives_import_then_compile(tmp_path):
    _report, nodes = _round_trip(tmp_path)
    assert nodes["Access"].get("accessType") == "Read"


def test_directed_association_survives_import_then_compile(tmp_path):
    _report, nodes = _round_trip(tmp_path)
    assert nodes["Association"].get("isDirected") == "true"


def test_influence_modifier_survives_import_then_compile(tmp_path):
    _report, nodes = _round_trip(tmp_path)
    assert nodes["Influence"].get("modifier") == "++"


@pytest.mark.parametrize("access", ["Access", "Read", "Write", "ReadWrite"])
def test_every_access_type_survives(tmp_path, access):
    _report, nodes = _round_trip(tmp_path, access=access)
    assert nodes["Access"].get("accessType") == access


@pytest.mark.parametrize("modifier", ["+", "++", "-", "--", "0", "10", "strongly"])
def test_influence_modifiers_survive_including_ones_yaml_would_reinterpret(tmp_path, modifier):
    """`-` is a YAML sequence marker and `10` an int: the written DSL must quote them."""
    _report, nodes = _round_trip(tmp_path, modifier=modifier)
    assert nodes["Influence"].get("modifier") == modifier


def test_an_explicit_false_is_kept_not_collapsed_into_unset(tmp_path):
    _report, nodes = _round_trip(tmp_path, directed="false")
    assert nodes["Association"].get("isDirected") == "false"


def test_xsd_boolean_spellings_are_normalised(tmp_path):
    _report, nodes = _round_trip(tmp_path, directed="1")
    assert nodes["Association"].get("isDirected") == "true"


def test_the_imported_dsl_carries_the_three_fields(tmp_path):
    _round_trip(tmp_path)
    model, _docs, _config = dsl.load(tmp_path / "repo", "staging")
    by_type = {r.type: r for r in model.relationships.values()}
    assert by_type["Access"].access_type == "Read"
    assert by_type["Association"].is_directed is True
    assert by_type["Influence"].modifier == "++"


def test_the_imported_model_passes_the_gate_on_the_new_fields(tmp_path):
    _round_trip(tmp_path)
    report = validate.validate(tmp_path / "repo", zone="staging")
    codes = {f.code for f in report.findings}
    assert not codes & {"SCHEMA001", "REL004"}, "\n".join(f.render() for f in report.findings)


# ------------------------------------------------------------ never dropped silently


def test_an_invalid_value_is_reported_not_silently_dropped(tmp_path):
    report, nodes = _round_trip(tmp_path, access="Bogus", directed="maybe")
    assert "accessType" not in nodes["Access"].attrib
    assert "isDirected" not in nodes["Association"].attrib
    text = "\n".join(report.notes)
    assert "accessType" in text and "Bogus" in text
    assert "isDirected" in text and "maybe" in text


def test_an_absent_attribute_stays_absent(tmp_path):
    """Unspecified in the source means unspecified in the output: the compiler writes no
    value of its own, so the XSD default applies and the output stays valid."""
    source = tmp_path / "plain.xml"
    source.write_text(
        CRAFTED.replace(' accessType="{access}"', "")
        .replace(' isDirected="{directed}"', "")
        .replace(' modifier="{modifier}"', ""),
        encoding="utf-8",
    )
    importer.import_exchange(tmp_path / "repo", source, ids="identifiers")
    result = aoef.compile_model(tmp_path / "repo", zone="staging", out=tmp_path / "out.xml")
    assert result.schema_errors == []
    for node in etree.fromstring(result.xml).findall("a:relationships/a:relationship", NS):
        assert not {"accessType", "isDirected", "modifier"} & set(node.attrib)


# -------------------------------------------------------------------- authoring path

AUTHORED = """
elements:
  - {id: app, type: ApplicationComponent, name: Portal, assumed: true, rationale: test}
  - {id: data, type: DataObject, name: Case, assumed: true, rationale: test}
  - {id: driver, type: Driver, name: Waiting times, assumed: true, rationale: test}
  - {id: goal, type: Goal, name: Faster triage, assumed: true, rationale: test}
relationships:
  - {id: reads, type: Access, source: app, target: data, accessType: ReadWrite,
     assumed: true, rationale: test}
  - {id: links, type: Association, source: app, target: data, isDirected: true,
     assumed: true, rationale: test}
  - {id: pushes, type: Influence, source: driver, target: goal, modifier: "--",
     assumed: true, rationale: test}
"""


def _authored_repo(tmp_path: Path, text: str = AUTHORED) -> Path:
    (tmp_path / "model" / "staging").mkdir(parents=True)
    (tmp_path / "model" / "staging" / "m.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_the_dsl_can_author_the_attributes(tmp_path):
    root = _authored_repo(tmp_path)
    result = aoef.compile_model(root, zone="staging", out=tmp_path / "out.xml")
    assert result.schema_errors == [], "\n".join(result.schema_errors)
    nodes = {
        n.get(XSI_TYPE): n
        for n in etree.fromstring(result.xml).findall("a:relationships/a:relationship", NS)
    }
    assert nodes["Access"].get("accessType") == "ReadWrite"
    assert nodes["Association"].get("isDirected") == "true"
    assert nodes["Influence"].get("modifier") == "--"
    report = validate.validate(root, zone="staging")
    assert not {"SCHEMA001", "REL004"} & {f.code for f in report.findings}


def test_authored_integer_modifier_is_a_string_in_the_model(tmp_path):
    root = _authored_repo(tmp_path, AUTHORED.replace('modifier: "--"', "modifier: 7"))
    model, _docs, _config = dsl.load(root, "staging")
    assert model.relationships["pushes"].modifier == "7"
    assert "SCHEMA001" not in {f.code for f in validate.validate(root, zone="staging").findings}


# ------------------------------------------------------------- wrong-type validation


@pytest.mark.parametrize(
    "bad, field",
    [
        ("{id: x, type: Serving, source: app, target: data, accessType: Read}", "accessType"),
        ("{id: x, type: Serving, source: app, target: data, isDirected: true}", "isDirected"),
        ("{id: x, type: Serving, source: driver, target: goal, modifier: '++'}", "modifier"),
        ("{id: x, type: Access, source: app, target: data, isDirected: true}", "isDirected"),
    ],
)
def test_a_field_on_the_wrong_relationship_type_is_rel004(tmp_path, bad, field):
    text = AUTHORED + f"  - {bad}\n"
    report = validate.validate(_authored_repo(tmp_path, text), zone="staging")
    hits = [f for f in report.errors if f.code == "REL004" and f.concept == "x"]
    assert len(hits) == 1, "\n".join(f.render() for f in report.findings)
    assert field in hits[0].message and "only valid on" in hits[0].message


def test_an_invalid_access_type_value_is_a_schema_error(tmp_path):
    text = AUTHORED.replace("accessType: ReadWrite", "accessType: Append")
    report = validate.validate(_authored_repo(tmp_path, text), zone="staging")
    assert "SCHEMA001" in {f.code for f in report.errors}


def test_the_generated_schema_is_current_and_lists_the_xsd_enum():
    from easkills import genschema, oracle

    xsd = etree.parse(str(oracle.MODEL_XSD))
    xs = {"xs": "http://www.w3.org/2001/XMLSchema"}
    enum = xsd.xpath("//xs:simpleType[@name='AccessTypeEnum']//xs:enumeration/@value", namespaces=xs)
    assert list(genschema.ACCESS_TYPES) == [str(v) for v in enum], "the XSD is the authority"
    props = genschema.build_schema()["$defs"]["relationship"]["properties"]
    assert props["accessType"]["enum"] == ["Access", "Read", "Write", "ReadWrite"]
    assert props["isDirected"]["type"] == "boolean"
