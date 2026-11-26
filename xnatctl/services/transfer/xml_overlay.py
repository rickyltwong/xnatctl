"""Pure XML transforms for the experiment-XML metadata overlay.

Separated from :class:`~xnatctl.services.transfer.executor.TransferExecutor`
so the rewrite logic can be unit-tested directly against input/expected XML
strings, without going through HTTP. ``TransferExecutor.apply_xml_overlay``
fetches the source XML and PUTs the result; the transform itself lives here.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import defusedxml.ElementTree as DefusedET


def rewrite_experiment_xml(
    xml_text: str,
    dest_experiment_id: str | None = None,
    dest_project: str | None = None,
) -> str:
    """Strip internal references from experiment XML for overlay.

    Removes file/catalog elements, subject_ID, prearchivePath,
    image_session_ID, sharing, fields, session-level resources,
    schemaLocation, and label. Rewrites experiment ID and project
    if provided.

    The label attribute is always stripped because XNAT rejects PUT
    requests that include a label differing from the destination
    experiment's current label (400: "Label must be modified through
    separate URI."). Since xnatctl currently only supports same-label
    transfers, stripping it avoids the mismatch entirely.

    .. todo:: Support regex-based label transformation. When implemented,
       accept a ``dest_label`` parameter and rewrite instead of strip.

    Args:
        xml_text: Raw source experiment XML.
        dest_experiment_id: Destination experiment accession ID.
        dest_project: Destination project ID.

    Returns:
        Cleaned XML string suitable for PUT overlay.
    """
    xml_text = re.sub(r"<!--.*?-->", "", xml_text, flags=re.DOTALL)

    root = DefusedET.fromstring(xml_text)

    ns_map = _collect_namespace_map(root)
    xnat_ns = ns_map.get("xnat", "")
    xsi_ns = ns_map.get("xsi", "")

    _strip_internal_elements(root, xnat_ns)
    _strip_schema_location(root, xsi_ns)
    _rewrite_identity_attributes(root, dest_experiment_id, dest_project)

    # Register namespaces to avoid ns0/ns1 prefixes in output
    for prefix, uri in ns_map.items():
        ET.register_namespace(prefix, uri)

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _collect_namespace_map(root: ET.Element) -> dict[str, str]:
    ns_uris: set[str] = set()
    for elem in root.iter():
        tag = elem.tag
        if tag.startswith("{"):
            ns_uris.add(tag[1 : tag.index("}")])
        for attr_name in elem.attrib:
            if attr_name.startswith("{"):
                ns_uris.add(attr_name[1 : attr_name.index("}")])

    ns_map: dict[str, str] = {}
    for uri in ns_uris:
        if "xnat" in uri:
            ns_map["xnat"] = uri
        elif "XMLSchema-instance" in uri:
            ns_map["xsi"] = uri
    return ns_map


def _strip_internal_elements(root: ET.Element, xnat_ns: str) -> None:
    remove_local_names = {
        "file",
        "subject_ID",
        "prearchivePath",
        "image_session_ID",
        "sharing",
        "fields",
    }

    if xnat_ns:
        for tag_name in ("resources",):
            for child in root.findall(f"{{{xnat_ns}}}{tag_name}"):
                root.remove(child)

    _remove_elements_recursive(root, remove_local_names, xnat_ns)


def _strip_schema_location(root: ET.Element, xsi_ns: str) -> None:
    if xsi_ns:
        schema_attr = f"{{{xsi_ns}}}schemaLocation"
        if schema_attr in root.attrib:
            del root.attrib[schema_attr]


def _rewrite_identity_attributes(
    root: ET.Element,
    dest_experiment_id: str | None,
    dest_project: str | None,
) -> None:
    if dest_experiment_id is not None and "ID" in root.attrib:
        root.attrib["ID"] = dest_experiment_id
    if dest_project is not None and "project" in root.attrib:
        root.attrib["project"] = dest_project

    if "label" in root.attrib:
        del root.attrib["label"]


def _remove_elements_recursive(
    parent: ET.Element,
    local_names: set[str],
    xnat_ns: str,
) -> None:
    to_remove: list[ET.Element] = []
    for child in parent:
        tag = child.tag
        local = tag.rsplit("}", 1)[-1] if "}" in tag else tag
        if local in local_names:
            to_remove.append(child)
        else:
            _remove_elements_recursive(child, local_names, xnat_ns)
    for child in to_remove:
        parent.remove(child)
