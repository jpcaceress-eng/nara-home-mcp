from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator


YAML_SUFFIXES = frozenset({".yaml", ".yml"})
MAX_YAML_BYTES = 512 * 1024
MAX_DASHBOARD_BYTES = 1024 * 1024
MAX_DISCOVERED_FILES = 10_000
MAX_TEXT_BYTES = 1024 * 1024
TEXT_SUFFIXES = frozenset(
    {
        ".css", ".html", ".j2", ".jinja", ".js", ".json", ".md", ".py",
        ".svg", ".toml", ".ts", ".txt", ".yaml", ".yml",
    }
)
EXCLUDED_DIRECTORIES = frozenset(
    {".git", ".backup", "backup", "backups", "deps", "node_modules", "tts"}
)
SENSITIVE_STORAGE_PREFIXES = (
    "auth", "auth_provider.", "application_credentials", "cloud", "core.uuid",
    "http", "mobile_app", "onboarding", "webhook",
)
SENSITIVE_FILENAMES = frozenset(
    {".env", ".env.local", "known_devices.yaml", "secrets.yaml"}
)


class ConfigAccessError(ValueError):
    """A public, stable configuration access failure."""


@dataclass(frozen=True, slots=True)
class ConfigFile:
    path: str
    size: int
    modified_at: str


class HomeAssistantConfigProvider:
    """Traverse one fixed root without following symlinks or escaping it."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        require_cifs: bool = True,
        enabled: bool = True,
    ) -> None:
        self._root = root
        self._require_cifs = require_cifs
        self._enabled = enabled

    @property
    def available(self) -> bool:
        if not self._enabled or self._root is None:
            return False
        if not self._root.is_dir() or self._root.is_symlink():
            return False
        configuration = self._root / "configuration.yaml"
        storage = self._root / ".storage"
        if (
            not configuration.is_file()
            or configuration.is_symlink()
            or not storage.is_dir()
            or storage.is_symlink()
        ):
            return False
        return not self._require_cifs or self._is_read_only_cifs_mount()

    def yaml_files(self) -> tuple[ConfigFile, ...]:
        self._require_root()
        files = []
        for path in self._walk(self._root):
            if path.suffix.lower() not in YAML_SUFFIXES:
                continue
            files.append(self._metadata(path))
            if len(files) > MAX_DISCOVERED_FILES:
                raise ConfigAccessError("Configuration file count exceeds the discovery limit")
        return tuple(sorted(files, key=lambda item: item.path))

    def text_files(self) -> tuple[ConfigFile, ...]:
        self._require_root()
        files = []
        for path in self._walk(self._root, include_storage=True):
            relative = path.relative_to(self._root).as_posix()
            if not self._is_allowed_text(relative):
                continue
            metadata = self._metadata(path)
            if metadata.size > MAX_TEXT_BYTES:
                continue
            files.append(metadata)
            if len(files) > MAX_DISCOVERED_FILES:
                raise ConfigAccessError("Configuration file count exceeds the discovery limit")
        return tuple(sorted(files, key=lambda item: item.path))

    def storage_dashboards(self) -> tuple[ConfigFile, ...]:
        self._require_root()
        storage = self._root / ".storage"
        if not storage.is_dir() or storage.is_symlink():
            return ()
        files = []
        for entry in os.scandir(storage):
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            if entry.name == "lovelace" or entry.name.startswith("lovelace."):
                files.append(self._metadata(Path(entry.path)))
        return tuple(sorted(files, key=lambda item: item.path))

    def dashboard_registry(self) -> ConfigFile | None:
        self._require_root()
        path = self._root / ".storage" / "lovelace_dashboards"
        if path.is_symlink() or not path.is_file():
            return None
        return self._metadata(path)

    def read_dashboard_registry(self) -> str:
        path = self._safe_file(
            ".storage/lovelace_dashboards", frozenset(), MAX_DASHBOARD_BYTES
        )
        return self._read_text(path)

    def read_yaml(self, relative: str) -> str:
        path = self._safe_file(relative, YAML_SUFFIXES, MAX_YAML_BYTES)
        if path.name.casefold() == "secrets.yaml":
            raise ConfigAccessError("Secret files cannot be read")
        return self._read_text(path)

    def read_dashboard_storage(self, relative: str) -> str:
        path = self._safe_file(relative, frozenset(), MAX_DASHBOARD_BYTES)
        if path.parent != self._root / ".storage" or not (
            path.name == "lovelace" or path.name.startswith("lovelace.")
        ):
            raise ConfigAccessError("Unknown dashboard")
        return self._read_text(path)

    def read_text_file(self, relative: str) -> tuple[ConfigFile, str]:
        if not self._is_allowed_text(relative):
            raise ConfigAccessError("Configuration file is unavailable")
        path = self._safe_file(relative, frozenset(), MAX_TEXT_BYTES)
        return self._metadata(path), self._read_text(path)

    def _safe_file(self, relative: str, suffixes: frozenset[str], max_bytes: int) -> Path:
        self._require_root()
        pure = PurePosixPath(relative)
        if not relative or pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
            raise ConfigAccessError("Invalid configuration path")
        path = self._root.joinpath(*pure.parts)
        try:
            info = path.lstat()
        except OSError:
            raise ConfigAccessError("Configuration file is unavailable") from None
        if path.is_symlink() or not path.is_file() or (suffixes and path.suffix.lower() not in suffixes):
            raise ConfigAccessError("Configuration file is unavailable")
        current = self._root
        for part in pure.parts[:-1]:
            current /= part
            if current.is_symlink() or not current.is_dir():
                raise ConfigAccessError("Configuration file is unavailable")
        if info.st_size > max_bytes:
            raise ConfigAccessError("Configuration file exceeds the read limit")
        return path

    def _walk(self, directory: Path, *, include_storage: bool = False) -> Iterator[Path]:
        for entry in os.scandir(directory):
            if entry.is_symlink():
                continue
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                if path.name in EXCLUDED_DIRECTORIES or (
                    path.name == ".storage" and not include_storage
                ):
                    continue
                yield from self._walk(path, include_storage=include_storage)
            elif entry.is_file(follow_symlinks=False):
                yield path

    def _metadata(self, path: Path) -> ConfigFile:
        info = path.stat(follow_symlinks=False)
        return ConfigFile(
            path=path.relative_to(self._root).as_posix(),
            size=info.st_size,
            modified_at=datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
        )

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ConfigAccessError("Configuration file is not readable text") from None

    def _require_root(self) -> None:
        if not self.available:
            raise ConfigAccessError("Home Assistant configuration root is unavailable")

    @staticmethod
    def _is_allowed_text(relative: str) -> bool:
        pure = PurePosixPath(relative)
        if (
            not relative
            or pure.is_absolute()
            or ".." in pure.parts
            or "." in pure.parts
            or any(part in EXCLUDED_DIRECTORIES for part in pure.parts)
            or pure.name.casefold() in SENSITIVE_FILENAMES
        ):
            return False
        lowered = relative.casefold()
        if lowered.startswith(".storage/"):
            storage_name = lowered.removeprefix(".storage/")
            if storage_name.startswith(SENSITIVE_STORAGE_PREFIXES):
                return False
            return "/" not in storage_name
        return pure.suffix.casefold() in TEXT_SUFFIXES

    def _is_read_only_cifs_mount(self) -> bool:
        """Require the configured root to be a read-only CIFS mount."""
        try:
            mount_lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        target = self._root.as_posix()
        for line in mount_lines:
            before, separator, after = line.partition(" - ")
            if not separator:
                continue
            fields = before.split()
            filesystem = after.split()
            if len(fields) < 6 or len(filesystem) < 1:
                continue
            mount_point = fields[4].replace("\\040", " ")
            mount_options = set(fields[5].split(","))
            if mount_point == target and filesystem[0] == "cifs" and "ro" in mount_options:
                return True
        return False
