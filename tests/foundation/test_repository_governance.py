"""Offline policy contracts; these do not assert live GitHub enforcement."""

import json
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def test_main_requires_administrator_owned_pull_requests_without_bypass():
    policy = json.loads((ROOT / ".github/main-protection.json").read_text(encoding="utf-8"))
    assert policy["enforce_admins"] is True
    assert policy["required_pull_request_reviews"] == {
        "dismiss_stale_reviews": True,
        "require_code_owner_reviews": True,
        "required_approving_review_count": 1,
        "require_last_push_approval": True,
    }
    assert policy["allow_force_pushes"] is False
    assert policy["allow_deletions"] is False
    assert policy["required_conversation_resolution"] is True
    assert policy["required_status_checks"] is None
    owners = [line.split() for line in (ROOT / ".github/CODEOWNERS").read_text().splitlines()
              if line.strip() and not line.startswith("#")]
    assert owners == [["*", "@jpbraga", "@Maheidem", "@oktolabsai-developer"]]


def test_custom_license_and_addendum_remain_explicit():
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["project"]["license"] == {
        "text": "Elastic License 2.0 + SaaS/Branding Addendum"
    }
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Addendum: SaaS, Competing Service, Internal Use, and Attribution" in license_text
    assert "III. Attribution - REQUIRED preservation" in license_text
    assert "OSI-approved open-source license" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_local_browser_and_secret_artifacts_are_ignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".playwright-mcp/", ".claude/", ".env", ".env.*"} <= set(ignored)
