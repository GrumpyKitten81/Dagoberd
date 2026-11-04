from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_issue_forms_require_sanitized_structured_reports():
    directory = ROOT / ".github" / "ISSUE_TEMPLATE"
    config = yaml.safe_load((directory / "config.yml").read_text(encoding="utf-8"))
    assert config["blank_issues_enabled"] is False
    for name in ("bug_report", "feature_request", "security_report"):
        form = yaml.safe_load((directory / f"{name}.yml").read_text(encoding="utf-8"))
        assert form["name"] and form["description"]
        assert "labels" not in form
        assert "public" in form["body"][0]["attributes"]["value"].lower()
        fields = [field for field in form["body"] if field["type"] == "textarea"]
        assert fields and all(field["validations"]["required"] for field in fields)
        privacy = next(field for field in form["body"] if field.get("id") == "privacy")
        assert all(option["required"] for option in privacy["attributes"]["options"])


def test_workflows_pin_actions_and_limit_credentials():
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert workflow["permissions"] == {"contents": "read"}
        for job in workflow["jobs"].values():
            assert 0 < job["timeout-minutes"] <= 30
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"[\w/-]+@[0-9a-f]{40}", step["uses"])
                    if step["uses"].startswith("actions/checkout@"):
                        assert step["with"]["persist-credentials"] is False
                if "gh release upload" in step.get("run", ""):
                    assert "${{" not in step["run"]
                    assert step["env"]["RELEASE_TAG"]
            if path.name != "release.yml":
                assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}
