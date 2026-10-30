from __future__ import annotations

import json
import re
from pathlib import Path

from jobforge import cli


def test_skill_export_is_generic_and_does_not_overwrite(tmp_path: Path, capsys):
    destination = tmp_path / "skills"
    assert cli.main(["skills", "export", "--destination", str(destination)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    skill = destination / "jobforge" / "SKILL.md"
    assert skill.is_file()
    assert (destination / "jobforge" / "references" / "workflow.md").is_file()
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\nname: jobforge\n")
    assert "interface-version: \"1\"" in text
    assert len(text.splitlines()) < 200
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (destination / "jobforge").rglob("*.md")
    )
    assert "/Users/" not in combined
    assert "tafseer" not in combined.lower()
    assert not re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", combined)
    assert "never submit" in combined.lower() or "never claim" in combined.lower()

    assert cli.main(["skills", "export", "--destination", str(destination)]) == 2
    assert cli.main(
        ["skills", "export", "--destination", str(destination), "--force"]
    ) == 0
