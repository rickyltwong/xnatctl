from __future__ import annotations

import json
import tempfile
from pathlib import Path

from xnatctl.cli.subject import _apply_template, _load_patterns_file, _projects_in_patterns_file


def test_patterns_file_load_and_apply_template() -> None:
    # Create a temporary patterns file with fake project IDs (no production identifiers).
    patterns = {
        "patterns": [
            {
                "project": "TESTPROJ",
                "match": r"^(ABC\d{3})$",
                "to": "{project}_00{1}",
                "description": "ABCNNN -> TESTPROJ_00ABCNNN",
            }
        ]
    }

    with tempfile.TemporaryDirectory() as tmp:
        patterns_path = Path(tmp) / "patterns.json"
        patterns_path.write_text(json.dumps(patterns), encoding="utf-8")

        rules = _load_patterns_file(path=str(patterns_path), project="TESTPROJ")
        assert len(rules) == 1

        regex, to_template, desc = rules[0]
        assert desc

        m = regex.fullmatch("ABC123")
        assert m is not None

        target = _apply_template(
            template=to_template,
            project="TESTPROJ",
            groups=m.groups(),
        )
        assert target == "TESTPROJ_00ABC123"


def test_projects_in_patterns_file_extracts_unique_projects() -> None:
    patterns = {
        "patterns": [
            {"project": "TESTPROJ", "match": r"^A$", "to": "{project}_{1}"},
            {"project": "TESTPROJ", "match": r"^B$", "to": "{project}_{1}"},
            {"project": "OTHERPROJ", "match": r"^C$", "to": "{project}_{1}"},
        ]
    }

    with tempfile.TemporaryDirectory() as tmp:
        patterns_path = Path(tmp) / "patterns.json"
        patterns_path.write_text(json.dumps(patterns), encoding="utf-8")

        projects = _projects_in_patterns_file(str(patterns_path))
        assert projects == {"TESTPROJ", "OTHERPROJ"}
