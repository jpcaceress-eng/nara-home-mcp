from __future__ import annotations

from scripts.verify_release_contract import (
    project_version,
    remote_image,
    validate_read_only_contract,
    validate_versions,
    validate_workflows,
)


def test_versions_and_remote_derived_image_are_synchronized() -> None:
    validate_versions(project_version())
    assert remote_image() == "ghcr.io/jpcaceress-eng/nara-home-mcp"


def test_read_only_release_contract() -> None:
    validate_read_only_contract()


def test_workflows_are_pinned_and_separate_validation_from_publication() -> None:
    validate_workflows()
