"""Prepare new native Prosys projects offline; never start or contact a server.

The input is a saved desktop Project, not a NodeSet to import. Both modes retain
the vendor Project and all unchanged ZIP members. Live Prosys loading, observed
permissions, missing-node responses and fault recovery remain separate evidence.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sys
from typing import Any
import xml.etree.ElementTree as ET
import zipfile


UA_URI = "http://opcfoundation.org/UA/"
BUSINESS_URI = "http://www.prosysopc.com/OPCUA/SimulationNodes/"
CONFIG_URI = "http://www.prosysopc.com/OPCUA/SimulationNodes/SimulationConfiguration/"
SERVER_URI = "http://www.prosysopc.com/OPCUA/SimulationServer/"
XML_NS = "http://opcfoundation.org/UA/2011/03/UANodeSet.xsd"
VALUE_NS = "http://opcfoundation.org/UA/2008/02/Types.xsd"
INSTANCE_MEMBER = "simconf/instances/Instances_idx_3.NodeSet2.xml"
SIMULATION_MEMBER = "simconf/simulations/Simulations_idx_4.NodeSet2.xml"
DEFAULT_IDENTIFIERS = {
    "T": "Local.iControl.Experiment1.Trends.Tr.Value",
    "T_j": "Local.iControl.Experiment1.Trends.Tj.Value",
    "c": "Local.iControl.Experiment1.Trends.load.Value",
    "count_middle": "Local.iControl.Experiment1.Trends.count.Value",
    "T_j_set": "iCache.Temperature1",
}
PROPERTIES = {"HasReadHistoryAccess", "Initial Value", "IsInherited", "IsReadOnly"}
MODES = ("readonly_setpoint", "missing_count")


class FixtureError(ValueError):
    """The saved baseline or requested new artifact is not unambiguous."""


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tag(name: str) -> str:
    return f"{{{XML_NS}}}{name}"


@dataclass
class _NodeSet:
    name: str
    raw: bytes

    def __post_init__(self) -> None:
        try:
            self.root = ET.fromstring(self.raw)
        except ET.ParseError as exc:
            raise FixtureError(f"Invalid NodeSet XML: {self.name}") from exc
        if self.root.tag != _tag("UANodeSet"):
            raise FixtureError(f"Expected a standard UANodeSet: {self.name}")
        self.uris = [UA_URI] + [node.text for node in self.root.findall(f"{_tag('NamespaceUris')}/{_tag('Uri')}")]
        if any(not isinstance(uri, str) or not uri for uri in self.uris):
            raise FixtureError("NodeSet namespace URIs must be nonempty.")
        self.nodes = {}
        for node in self.root:
            if node.get("NodeId") is None:
                continue
            identity = self.identity(node.get("NodeId"))
            if identity in self.nodes:
                raise FixtureError(f"Duplicate NodeId in {self.name}: {node.get('NodeId')}")
            self.nodes[identity] = node

    def identity(self, value: str) -> tuple[str, str, str]:
        match = re.fullmatch(r"(?:ns=(\d+);)?([isgb])=(.+)", str(value))
        if match is None:
            raise FixtureError(f"Unsupported or aliased NodeId in {self.name}: {value}")
        index = int(match[1] or 0)
        if index >= len(self.uris):
            raise FixtureError(f"NodeId namespace is absent in {self.name}: {value}")
        identifier = str(int(match[3])) if match[2] == "i" else match[3]
        return self.uris[index], match[2], identifier

    def refs(self, node: ET.Element):
        for ref in node.findall(f"{_tag('References')}/{_tag('Reference')}"):
            yield ref, self.identity((ref.text or "").strip()), self.identity(ref.get("ReferenceType"))

    def require(self, uri: str, kind: str, identifier: str) -> ET.Element:
        try:
            return self.nodes[(uri, kind, identifier)]
        except KeyError as exc:
            raise FixtureError(f"Required node is absent in {self.name}: {identifier}") from exc


def _value(node: ET.Element, kind: str) -> str:
    value = node.find(f"{_tag('Value')}/{{{VALUE_NS}}}{kind}")
    if value is None or value.text is None:
        raise FixtureError(f"Expected {kind} value on {node.get('NodeId')}.")
    return value.text.strip()


def _baseline(instances: _NodeSet, simulations: _NodeSet):
    result = {}
    for field, identifier in DEFAULT_IDENTIFIERS.items():
        variable = instances.require(BUSINESS_URI, "s", identifier)
        if variable.tag != _tag("UAVariable") or instances.identity(variable.get("DataType")) != (UA_URI, "i", "11"):
            raise FixtureError(f"{field} must be a Double variable.")
        try:
            access = int(variable.get("AccessLevel", "1"))
            user_access = int(variable.get("UserAccessLevel", "1"))
            initial = float(_value(variable, "Double"))
        except (TypeError, ValueError) as exc:
            raise FixtureError(f"Invalid baseline value/permission for {field}.") from exc
        if not math.isfinite(initial) or any(level & 3 != 3 or not 0 <= level <= 255 for level in (access, user_access)):
            raise FixtureError(f"{field} baseline must be finite, readable and writable.")
        candidates = [node for node in simulations.nodes.values() if node.tag == _tag("UAObject") and any(
            ref.get("IsForward", "true") == "false" and target == (BUSINESS_URI, "s", identifier)
            and reference_type == (SERVER_URI, "i", "4002") for ref, target, reference_type in simulations.refs(node))]
        if len(candidates) != 1:
            raise FixtureError(f"{field} needs exactly one vendor ValueSimulation binding.")
        simulation = candidates[0]
        if not any(ref.get("IsForward", "true") == "true" and target == (SERVER_URI, "i", "1003")
                   and reference_type == (UA_URI, "i", "40") for ref, target, reference_type in simulations.refs(simulation)):
            raise FixtureError(f"{field} must use the vendor Constant simulation type.")
        parent = simulations.identity(simulation.get("NodeId"))
        children = [node for node in simulations.nodes.values() if any(
            ref.get("IsForward", "true") == "false" and target == parent
            and reference_type in {(UA_URI, "i", str(number)) for number in (35, 46, 47, 49)}
            for ref, target, reference_type in simulations.refs(node))]
        properties = {}
        for node in children:
            name = node.get("BrowseName", "").split(":", 1)[-1]
            if name in properties:
                raise FixtureError(f"Duplicate vendor property for {field}: {name}")
            properties[name] = node
        if set(properties) != PROPERTIES or any(node.tag != _tag("UAVariable") for node in children):
            raise FixtureError(f"Unexpected vendor simulation children for {field}; refusing to guess.")
        for name, node in properties.items():
            datatype = "11" if name == "Initial Value" else "1"
            if simulations.identity(node.get("DataType")) != (UA_URI, "i", datatype):
                raise FixtureError(f"Unexpected vendor property datatype for {field}: {name}")
            if not any(ref.get("IsForward", "true") == "false" and target == parent
                       and reference_type == (UA_URI, "i", "46")
                       for ref, target, reference_type in simulations.refs(node)):
                raise FixtureError(f"Vendor properties require HasProperty for {field}: {name}")
        if _value(properties["IsInherited"], "Boolean") != "false" or _value(properties["IsReadOnly"], "Boolean") != "false":
            raise FixtureError(f"{field} must have explicit writable baseline simulation settings.")
        if float(_value(properties["Initial Value"], "Double")) != initial:
            raise FixtureError(f"{field} instance and vendor initial values disagree.")
        result[field] = variable, simulation, properties
    return result


def _block(raw: bytes, node: ET.Element) -> tuple[int, int, bytes]:
    tag = node.tag.rsplit("}", 1)[-1].encode()
    identifier = re.escape(node.get("NodeId").encode())
    pattern = rb'<(?P<tag>(?:[A-Za-z_][\w.-]*:)?' + tag + rb')\b[^>]*\bNodeId="' + identifier + rb'"[^>]*>.*?</(?P=tag)>'
    matches = list(re.finditer(pattern, raw, re.S))
    if len(matches) != 1:
        raise FixtureError(f"Expected one exact saved XML block for {node.get('NodeId')}.")
    match = matches[0]
    return match.start(), match.end(), match.group()


def _change(raw: bytes, node: ET.Element, replacement: bytes) -> bytes:
    start, end, _ = _block(raw, node)
    return raw[:start] + replacement + raw[end:]


def _attribute(block: bytes, name: str, old: str, new: str) -> bytes:
    header, rest = block.split(b">", 1)
    pattern = rb'(?<![\w:])' + name.encode() + rb'="' + re.escape(old.encode()) + rb'"'
    header, count = re.subn(pattern, name.encode() + b'="' + new.encode() + b'"', header)
    if count != 1:
        raise FixtureError(f"Expected one explicit saved {name} attribute.")
    return header + b">" + rest


def generate_fixture(source_project: Path, output_project: Path, mode: str, *, workspace_root: Path | None = None) -> dict[str, Any]:
    if mode not in MODES:
        raise FixtureError("Unknown native fixture mode.")
    source = Path(source_project).expanduser().resolve(strict=True)
    root = Path(workspace_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    runtime = (root / ".runtime").resolve()
    output = Path(output_project).expanduser().resolve()
    if not runtime.is_relative_to(root) or not output.is_relative_to(runtime) or output.suffix != ".uasim":
        raise FixtureError("Output must be a new .uasim file inside MPCrystal_final/.runtime.")
    evidence_path, diff_path = output.with_suffix(".evidence.json"), output.with_suffix(".diff.txt")
    if any(path.exists() for path in (output, evidence_path, diff_path)):
        raise FixtureError("Native fixture or sidecar already exists; nothing may be overwritten.")
    if source == output or source.suffix != ".uasim":
        raise FixtureError("Input must be a distinct saved .uasim project.")
    original = source.read_bytes()
    try:
        with zipfile.ZipFile(io.BytesIO(original)) as archive:
            entries = archive.infolist()
            if len(entries) > 256 or sum(entry.file_size for entry in entries) > 64 * 1024 * 1024:
                raise FixtureError("Saved project exceeds the bounded fixture size.")
            if len({entry.filename for entry in entries}) != len(entries):
                raise FixtureError("Saved project contains duplicate ZIP member names.")
            members = {entry.filename: archive.read(entry) for entry in entries}
            archive_comment = archive.comment
    except zipfile.BadZipFile as exc:
        raise FixtureError("Input is not a saved Prosys Project ZIP.") from exc
    if not {INSTANCE_MEMBER, SIMULATION_MEMBER} <= members.keys():
        raise FixtureError("Saved project lacks the expected vendor instance/simulation members.")
    instances = _NodeSet(INSTANCE_MEMBER, members[INSTANCE_MEMBER])
    simulations = _NodeSet(SIMULATION_MEMBER, members[SIMULATION_MEMBER])
    baseline = _baseline(instances, simulations)
    changed = dict(members)
    changes = []
    if mode == "readonly_setpoint":
        variable, simulation, properties = baseline["T_j_set"]
        block = _block(changed[INSTANCE_MEMBER], variable)[2]
        for attribute in ("AccessLevel", "UserAccessLevel"):
            before = variable.get(attribute)
            after = str(int(before) & ~2)
            block = _attribute(block, attribute, before, after)
            changes.append({"member": INSTANCE_MEMBER, "node_id": variable.get("NodeId"),
                "operation": f"set {attribute}", "before": before, "after": after})
        changed[INSTANCE_MEMBER] = _change(changed[INSTANCE_MEMBER], variable, block)
        property_node = properties["IsReadOnly"]
        block = _block(changed[SIMULATION_MEMBER], property_node)[2]
        block, count = re.subn(rb'(<(?:[A-Za-z_][\w.-]*:)?Boolean>\s*)false(\s*</(?:[A-Za-z_][\w.-]*:)?Boolean>)',
                               rb'\g<1>true\g<2>', block)
        if count != 1:
            raise FixtureError("Expected one false vendor IsReadOnly Boolean.")
        changed[SIMULATION_MEMBER] = _change(changed[SIMULATION_MEMBER], property_node, block)
        changes.append({"member": SIMULATION_MEMBER, "node_id": property_node.get("NodeId"),
            "operation": "set IsReadOnly", "before": False, "after": True})
        deleted = set()
    else:
        variable, simulation, properties = baseline["count_middle"]
        nodes = [(instances, variable)] + [(simulations, node) for node in [simulation, *properties.values()]]
        deleted = {document.identity(node.get("NodeId")) for document, node in nodes}
        for document, node in nodes:
            changed[document.name] = _change(changed[document.name], node, b"")
            changes.append({"member": document.name, "node_id": node.get("NodeId"),
                "operation": "remove node", "before": node.get("BrowseName"), "after": None})
        marker = DEFAULT_IDENTIFIERS["count_middle"].encode()
        if any(marker in raw for raw in changed.values()):
            raise FixtureError("Missing-node fixture retains the count identifier in a project member.")
        removed_local_ids = [node.get("NodeId").encode() for _, node in nodes]
        for name, raw in changed.items():
            if not name.endswith(".NodeSet2.xml"):
                # An opaque resource provides no namespace table with which to
                # disambiguate a saved local identifier. Refuse such residue
                # rather than assume it cannot recreate or reference a node.
                if any(identifier in raw for identifier in removed_local_ids):
                    raise FixtureError("An opaque project member may reference a removed node; refusing to guess.")
                continue
            document = _NodeSet(name, raw)
            if deleted & document.nodes.keys():
                raise FixtureError("A removed definition remains in another NodeSet.")
            for node in document.nodes.values():
                if any(target in deleted for _, target, _ in document.refs(node)):
                    raise FixtureError("A vendor reference still targets a removed node; refusing to guess.")

    # Reparse the modified XML and require all other business definitions and
    # all unaffected ZIP payloads to remain byte-identical.
    revised_instances = _NodeSet(INSTANCE_MEMBER, changed[INSTANCE_MEMBER])
    revised_simulations = _NodeSet(SIMULATION_MEMBER, changed[SIMULATION_MEMBER])
    untouched_fields = set(DEFAULT_IDENTIFIERS) - ({"T_j_set"} if mode == "readonly_setpoint" else {"count_middle"})
    for field in untouched_fields:
        variable, simulation, properties = baseline[field]
        for before_document, after_document, node in [(instances, revised_instances, variable)] + [
                (simulations, revised_simulations, item) for item in [simulation, *properties.values()]]:
            identifier = before_document.identity(node.get("NodeId"))
            revised = after_document.nodes.get(identifier)
            if revised is None or _block(before_document.raw, node)[2] != _block(after_document.raw, revised)[2]:
                raise FixtureError(f"Unrelated business definition changed: {field}.")
    if source.read_bytes() != original:
        raise FixtureError("Source project changed during preparation; no output was published.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.comment = archive_comment
        for entry in entries:
            archive.writestr(entry, changed[entry.filename])
    artifact = buffer.getvalue()
    member_evidence = [{"name": name, "before_sha256": _sha(raw), "after_sha256": _sha(changed[name]),
        "changed": raw != changed[name], "before_bytes": len(raw), "after_bytes": len(changed[name])}
        for name, raw in members.items()]
    evidence = {"schema_version": 1, "mode": mode, "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {"path": str(source), "sha256": _sha(original), "bytes": len(original)},
        "output": {"path": str(output), "sha256": _sha(artifact), "bytes": len(artifact)},
        "members": member_evidence, "changes": changes, "source_unchanged": True,
        "static_checks": {"five_node_baseline_verified": True, "namespace_aware_references": True,
            "unchanged_members_byte_identical": True, "unrelated_business_nodes_byte_identical": True,
            "removed_definition_count": len(deleted), "removed_nodes_have_no_remaining_references": True},
        "live_validation": "NOT_RUN", "evidence_path": str(evidence_path), "diff_path": str(diff_path)}
    diff = "".join("".join(difflib.unified_diff(members[name].decode().splitlines(keepends=True),
        changed[name].decode().splitlines(keepends=True), fromfile=f"baseline/{name}", tofile=f"{mode}/{name}"))
        for name in (INSTANCE_MEMBER, SIMULATION_MEMBER))
    output.parent.mkdir(parents=True, exist_ok=True)
    for path, content in ((output, artifact), (diff_path, diff.encode()),
                          (evidence_path, (json.dumps(evidence, indent=2, allow_nan=False) + "\n").encode())):
        with path.open("xb") as stream:
            stream.write(content)
    return evidence


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    args = parser.parse_args(argv)
    try:
        evidence = generate_fixture(args.source, args.output, args.mode)
    except (FixtureError, OSError) as exc:
        print(f"Offline fixture preparation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"output": evidence["output"]["path"], "evidence": evidence["evidence_path"],
        "diff": evidence["diff_path"], "live_validation": evidence["live_validation"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
