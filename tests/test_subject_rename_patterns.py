from __future__ import annotations

import json
from pathlib import Path
import tempfile

from xnatctl.cli.subject import _apply_template, _load_patterns_file


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

