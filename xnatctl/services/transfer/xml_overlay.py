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


def rewrite_experiment_xml(  # noqa: C901  # pre-existing; see pyproject
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
    # Strip HTML comments (hidden_fields, internal DB refs)
    xml_text = re.sub(r"<!--.*?-->", "", xml_text, flags=re.DOTALL)

    root = DefusedET.fromstring(xml_text)

    # Collect all namespace URIs used in the document (tags + attributes)
    ns_uris: set[str] = set()
    for elem in root.iter():
        tag = elem.tag
        if tag.startswith("{"):
            ns_uris.add(tag[1 : tag.index("}")])
        for attr_name in elem.attrib:
            if attr_name.startswith("{"):
                ns_uris.add(attr_name[1 : attr_name.index("}")])

    # Build namespace map: prefix -> URI
    ns_map: dict[str, str] = {}
    for uri in ns_uris:
        if "xnat" in uri:
            ns_map["xnat"] = uri
        elif "XMLSchema-instance" in uri:
            ns_map["xsi"] = uri

    xnat_ns = ns_map.get("xnat", "")
    xsi_ns = ns_map.get("xsi", "")

    # Elements to remove (direct children or nested within scans)
    remove_local_names = {
        "file",
        "subject_ID",
        "prearchivePath",
        "image_session_ID",
        "sharing",
        "fields",
    }

    # Remove session-level resources (but not scan-level resources)
    # Session-level resources are direct children of root
    if xnat_ns:
        for tag_name in ("resources",):
            for child in root.findall(f"{{{xnat_ns}}}{tag_name}"):
                root.remove(child)

    # Recursively remove targeted elements
    _remove_elements_recursive(root, remove_local_names, xnat_ns)

    # Remove xsi:schemaLocation attribute
    if xsi_ns:
        schema_attr = f"{{{xsi_ns}}}schemaLocation"
        if schema_attr in root.attrib:
            del root.attrib[schema_attr]

    # Rewrite root ID and project attributes
    if dest_experiment_id is not None and "ID" in root.attrib:
        root.attrib["ID"] = dest_experiment_id
    if dest_project is not None and "project" in root.attrib:
        root.attrib["project"] = dest_project

    # Strip label to avoid 400 "Label must be modified through separate URI"
    # TODO: rewrite label instead of stripping when label transformation is supported
    if "label" in root.attrib:
        del root.attrib["label"]

    # Register namespaces to avoid ns0/ns1 prefixes in output
    for prefix, uri in ns_map.items():
        ET.register_namespace(prefix, uri)

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _remove_elements_recursive(
    parent: ET.Element,
    local_names: set[str],
    xnat_ns: str,
) -> None:
    """Remove elements matching local names from parent and descendants.

    Args:
        parent: Parent XML element.
        local_names: Set of local tag names to remove.
        xnat_ns: XNAT namespace URI.
    """
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
