#!/usr/bin/env python3
"""Validate Nara Home's release, workflow, and read-only contracts."""

from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "nara_home"
CI = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
EXPECTED_IMAGE = "ghcr.io/jpcaceress-eng/nara-home-mcp"
BUILDER_SHA = "4de35182ce1e329181bffcbcc84d33db5e2c7e10"
CHECKOUT_SHA = "de0fac2e4500dabe0009e67214ff5f5447ce83dd"
SETUP_PYTHON_SHA = "a309ff8b426b58ec0e2a45f0f869d46889d02405"


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AssertionError(f"{path} must contain a YAML mapping")
    return data


def project_version() -> str:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(pyproject["project"]["version"])


def remote_image() -> str:
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?", remote)
    if not match:
        raise AssertionError("origin must be a GitHub HTTPS remote")
    return f"ghcr.io/{match.group(1).lower()}/{match.group(2).lower()}"


def validate_versions(requested: str | None = None) -> None:
    version = project_version()
    config = load_yaml(APP / "config.yaml")
    dockerfile = (APP / "Dockerfile").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    wheel = APP / f"nara_home_mcp-{version}-py3-none-any.whl"
    assert str(config["version"]) == version
    assert f'ARG BUILD_VERSION="{version}"' in dockerfile
    assert wheel.is_file(), f"missing versioned wheel: {wheel.name}"
    assert f"## {version} " in changelog
    if requested is not None:
        assert requested == version, f"release {requested} does not match project {version}"
        assert config.get("image") == EXPECTED_IMAGE, (
            "release publishing is disabled until config.yaml references the "
            "published image"
        )


def validate_read_only_contract() -> None:
    config = load_yaml(APP / "config.yaml")
    assert config["stage"] == "experimental"
    assert "image" not in config
    assert config["arch"] == ["amd64", "aarch64"]
    assert config["ports"] == {"8000/tcp": None}
    assert config["homeassistant_api"] is True
    for key in ("host_network", "hassio_api", "docker_api", "full_access"):
        assert config[key] is False
    assert config["map"] == [
        {"type": "homeassistant_config", "read_only": True, "path": "/homeassistant_config"}
    ]
    profile = (APP / "apparmor.txt").read_text(encoding="utf-8")
    assert "deny /homeassistant_config/** wklx," in profile
    assert "complain" not in profile
    dockerfile = (APP / "Dockerfile").read_text(encoding="utf-8")
    assert "drop_privileges()" in (APP / "run.sh").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in dockerfile


def validate_workflows() -> None:
    ci_text = CI.read_text(encoding="utf-8")
    release_text = RELEASE.read_text(encoding="utf-8")
    load_yaml(CI)
    load_yaml(RELEASE)

    assert "packages: write" not in ci_text
    assert "id-token: write" not in ci_text
    assert "push: \"false\"" in ci_text
    assert "load: \"true\"" in ci_text
    assert "container-registry-password: unused" in ci_text
    assert "pull_request:" in ci_text and "push:" in ci_text

    assert "release:" in release_text and "types: [published]" in release_text
    assert "workflow_dispatch:" in release_text
    assert "confirm_publish" in release_text
    assert "branches:" not in release_text
    assert "push: \"true\"" in release_text
    assert "secrets.GITHUB_TOKEN" in release_text
    assert "id-token: write" in release_text
    assert "packages: write" in release_text
    assert "REGISTRY_PREFIX: ghcr.io/jpcaceress-eng" in release_text

    for text in (ci_text, release_text):
        assert f"actions/checkout@{CHECKOUT_SHA}" in text
        assert "ssh" not in text.lower()
        for forbidden in ("cloudflare", "proxmox", "lxc 105", "192.168.", "10.0."):
            assert forbidden not in text.lower()
        for match in re.findall(r"uses:\s+[^\s]+@([^\s#]+)", text):
            assert re.fullmatch(r"[0-9a-f]{40}", match), f"mutable action ref: {match}"
    assert f"actions/setup-python@{SETUP_PYTHON_SHA}" in ci_text
    assert f"home-assistant/builder/actions/build-image@{BUILDER_SHA}" in ci_text
    for action in (
        "prepare-multi-arch-matrix",
        "build-image",
        "publish-multi-arch-manifest",
    ):
        assert f"home-assistant/builder/actions/{action}@{BUILDER_SHA}" in release_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ci", action="store_true")
    parser.add_argument("--release-version")
    args = parser.parse_args()
    if not args.ci and args.release_version is None:
        parser.error("choose --ci or --release-version")
    validate_versions(args.release_version)
    validate_read_only_contract()
    validate_workflows()


if __name__ == "__main__":
    main()
