"""Tests for the experiment-XML overlay rewrite (pure XML transform)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from xnatctl.services.transfer.xml_overlay import rewrite_experiment_xml

# -- Sample XNAT experiment XML for XML overlay tests --

_SAMPLE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<xnat:MRSession xmlns:xnat="http://nrg.wustl.edu/xnat"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://nrg.wustl.edu/xnat https://src.example.org/schemas/xnat/xnat.xsd"
    ID="XNAT_E001" label="EXP001" project="SRC"
    session_type="Guimond, Synthia^Development"
    modality="MR" UID="1.2.3.4.5">
  <!-- hidden_fields[internal_db_ref] -->
  <xnat:subject_ID>XNAT_S001</xnat:subject_ID>
  <xnat:prearchivePath>/data/prearchive/SRC/20260101/EXP001</xnat:prearchivePath>
  <xnat:date>2026-01-01</xnat:date>
  <xnat:time>10:00:00</xnat:time>
  <xnat:acquisition_site>Site A</xnat:acquisition_site>
  <xnat:scanner manufacturer="Siemens" model="Prisma"/>
  <xnat:sharing>
    <xnat:share label="shared_exp" project="OTHER"/>
  </xnat:sharing>
  <xnat:fields>
    <xnat:field name="custom_field">value</xnat:field>
  </xnat:fields>
  <xnat:resources>
    <xnat:resource label="QC" file_count="1"/>
  </xnat:resources>
  <xnat:scans>
    <xnat:scan ID="1" type="T1w" xnat:quality="usable">
      <xnat:image_session_ID>XNAT_E001</xnat:image_session_ID>
      <xnat:series_description>T1w MPRAGE</xnat:series_description>
      <xnat:quality>usable</xnat:quality>
      <xnat:parameters>
        <xnat:tr>2300</xnat:tr>
        <xnat:te>2.98</xnat:te>
      </xnat:parameters>
      <xnat:file label="DICOM" URI="/data/experiments/XNAT_E001/scans/1/resources/DICOM"/>
    </xnat:scan>
    <xnat:scan ID="2" type="fMRI">
      <xnat:image_session_ID>XNAT_E001</xnat:image_session_ID>
      <xnat:quality>usable</xnat:quality>
    </xnat:scan>
  </xnat:scans>
  <xnat:addParam name="extra_param">extra_value</xnat:addParam>
</xnat:MRSession>
"""


class TestRewriteExperimentXml:
    def test_strips_internal_elements(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        # Flatten all local tag names
        all_tags = {
            elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag for elem in root.iter()
        }

        assert "subject_ID" not in all_tags
        assert "prearchivePath" not in all_tags
        assert "image_session_ID" not in all_tags
        assert "sharing" not in all_tags
        assert "share" not in all_tags
        assert "fields" not in all_tags
        assert "file" not in all_tags

    def test_strips_session_level_resources(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        # Session-level resources should be removed
        # But scan-level elements should remain
        all_tags = {
            elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag for elem in root.iter()
        }
        assert "resources" not in all_tags

    def test_strips_schema_location(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        xsi_ns = "http://www.w3.org/2001/XMLSchema-instance"
        assert f"{{{xsi_ns}}}schemaLocation" not in root.attrib

    def test_strips_html_comments(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML)
        assert "hidden_fields" not in cleaned

    def test_preserves_session_type(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)
        assert root.attrib.get("session_type") == "Guimond, Synthia^Development"

    def test_preserves_scan_quality(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        xnat_ns = ""
        for elem in root.iter():
            if "xnat" in elem.tag and "}" in elem.tag:
                xnat_ns = elem.tag[1 : elem.tag.index("}")]
                break

        qualities = root.findall(f".//{{{xnat_ns}}}quality")
        assert len(qualities) == 2

    def test_preserves_scan_parameters(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        xnat_ns = ""
        for elem in root.iter():
            if "xnat" in elem.tag and "}" in elem.tag:
                xnat_ns = elem.tag[1 : elem.tag.index("}")]
                break

        tr_elems = root.findall(f".//{{{xnat_ns}}}tr")
        assert len(tr_elems) == 1
        assert tr_elems[0].text == "2300"

    def test_preserves_acquisition_site(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        xnat_ns = ""
        for elem in root.iter():
            if "xnat" in elem.tag and "}" in elem.tag:
                xnat_ns = elem.tag[1 : elem.tag.index("}")]
                break

        site = root.find(f"{{{xnat_ns}}}acquisition_site")
        assert site is not None
        assert site.text == "Site A"

    def test_preserves_add_param(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)

        xnat_ns = ""
        for elem in root.iter():
            if "xnat" in elem.tag and "}" in elem.tag:
                xnat_ns = elem.tag[1 : elem.tag.index("}")]
                break

        params = root.findall(f"{{{xnat_ns}}}addParam")
        assert len(params) == 1
        assert params[0].text == "extra_value"

    def test_rewrites_experiment_id(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML, "XNAT_E999")
        root = ET.fromstring(cleaned)
        assert root.attrib["ID"] == "XNAT_E999"

    def test_rewrites_project_attribute(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML, dest_project="DST")
        root = ET.fromstring(cleaned)
        assert root.attrib["project"] == "DST"

    def test_preserves_id_when_no_dest(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)
        assert root.attrib["ID"] == "XNAT_E001"

    def test_preserves_project_when_no_dest(self) -> None:
        cleaned = rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)
        assert root.attrib["project"] == "SRC"

    def test_strips_label_attribute(self) -> None:
        """Label is always stripped to avoid 400 on destination PUT."""
        cleaned = rewrite_experiment_xml(_SAMPLE_XML)
        root = ET.fromstring(cleaned)
        assert "label" not in root.attrib
