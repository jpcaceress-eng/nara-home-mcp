from __future__ import annotations

import pytest

from scripts.verify_release_contract import (
    project_version,
    remote_image,
    validate_read_only_contract,
    validate_versions,
    validate_workflows,
)


def test_versions_and_remote_derived_image_are_synchronized() -> None:
    validate_versions()
    assert remote_image() == "ghcr.io/jpcaceress-eng/nara-home-mcp"


def test_release_is_blocked_while_beta_uses_local_build() -> None:
    with pytest.raises(AssertionError, match="release publishing is disabled"):
        validate_versions(project_version())


def test_read_only_release_contract() -> None:
    validate_read_only_contract()


def test_workflows_are_pinned_and_separate_validation_from_publication() -> None:
    validate_workflows()
