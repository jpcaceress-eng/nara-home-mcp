"""Generic, privacy-preserving automation diagnostic capture helpers."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

from .anonymization import (
    CaptureAnonymizer,
    CaptureAuditError,
    audit_serialized_capture,
)

MAX_TRACES_SHOWN = 20
CAPTURE_SCHEMA_VERSION = 1


class RestCaptureClient(Protocol):
    async def get_state(self, entity_id: str) -> dict[str, Any]: ...

    async def list_states(self) -> list[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


class WebSocketCaptureClient(Protocol):
    async def connect(self) -> None: ...

    async def get_automation_config(self, entity_id: str) -> Any: ...

    async def list_traces(self, automation_id: str) -> Any: ...

    async def get_trace(self, automation_id: str, run_id: str) -> Any: ...

    async def aclose(self) -> None: ...


class CaptureOperationError(RuntimeError):
    """A sanitized error safe to display from a capture client."""

    def __init__(self, operation: str, error_type: str) -> None:
        self.operation = operation
        self.error_type = error_type
        super().__init__(f"Capture {operation} failed ({error_type})")


async def list_automations(
    rest_client: RestCaptureClient, *, output: TextIO = sys.stdout
) -> int:
    """Print a bounded one-line summary of every automation state from REST."""
    try:
        states = await rest_client.list_states()
        automations = sorted(
            (
                state
                for state in states
                if isinstance(state, dict)
                and isinstance(state.get("entity_id"), str)
                and state["entity_id"].startswith("automation.")
            ),
            key=lambda state: state["entity_id"],
        )
        print("INDEX\tENTITY_ID\tFRIENDLY_NAME\tSTATE\tLAST_TRIGGERED", file=output)
        for index, state in enumerate(automations, start=1):
            attributes = state.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {}
            print(
                "\t".join(
                    (
                        str(index),
                        _terminal_cell(state["entity_id"]),
                        _terminal_cell(attributes.get("friendly_name")),
                        _terminal_cell(state.get("state")),
                        _terminal_cell(attributes.get("last_triggered")),
                    )
                ),
                file=output,
            )
        return len(automations)
    except Exception as exc:
        raise CaptureOperationError("list_automations", type(exc).__name__) from None
    finally:
        await rest_client.aclose()


async def capture_automation_diagnostics(
    *,
    entity_id: str,
    rest_client: RestCaptureClient,
    websocket_client: WebSocketCaptureClient,
    output_dir: Path,
    run_id: str | None = None,
    latest: bool = False,
    overwrite: bool = False,
    anonymizer: CaptureAnonymizer | None = None,
    known_sensitive_values: Sequence[str] = (),
    output: TextIO = sys.stdout,
) -> list[Path]:
    """Capture and persist only anonymized automation diagnostic payloads."""
    sanitizer = anonymizer or CaptureAnonymizer()
    raw_internal_id: str | None = None
    selected_run_id: str | None = None
    completed_operations: list[str] = []

    try:
        _validate_capture_arguments(entity_id, run_id=run_id, latest=latest)
        state = await rest_client.get_state(entity_id)
        completed_operations.append("state")
        await websocket_client.connect()
        automation_config = await websocket_client.get_automation_config(entity_id)
        completed_operations.append("automation_config")
        raw_internal_id = _resolve_automation_id(state, automation_config)
        traces = await websocket_client.list_traces(raw_internal_id)
        completed_operations.append("trace_list")
        if not isinstance(traces, list):
            raise CaptureOperationError("trace_list", "unexpected_response")

        _print_trace_summary(traces, sanitizer, output=output)
        trace_get: Any | None = None
        if run_id is not None:
            selected_run_id = run_id
        elif latest:
            selected_run_id = _select_latest_run_id(traces)
            if selected_run_id is None:
                raise CaptureOperationError("trace_get", "no_trace_available")

        if selected_run_id is not None:
            trace_get = await websocket_client.get_trace(raw_internal_id, selected_run_id)
            completed_operations.append("trace_get")

        documents: dict[str, Any] = {
            "state.json": sanitizer.anonymize(state),
            "automation_config.json": sanitizer.anonymize(automation_config),
            "trace_list.json": sanitizer.anonymize(traces),
        }
        if selected_run_id is not None:
            documents["trace_get.json"] = sanitizer.anonymize(trace_get)
        documents["capture_metadata.json"] = {
            "capture_schema_version": CAPTURE_SCHEMA_VERSION,
            "captured_at_approximate": _rounded_capture_time(),
            "operations_completed": completed_operations,
            "traces_available": bool(traces),
            "trace_count": len(traces),
            "full_trace_captured": selected_run_id is not None,
            "anonymization_limits": sanitizer.limits_metadata(),
        }

        serialized = {
            filename: json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n"
            for filename, payload in documents.items()
        }
        forbidden_values = [raw_internal_id, selected_run_id or "", *known_sensitive_values]
        audit_serialized_capture(
            serialized,
            forbidden_values=[value for value in forbidden_values if value],
        )
        paths = _atomic_write_documents(output_dir, serialized, overwrite=overwrite)
        print(
            f"Capture completed safely: {len(paths)} anonymized files written.",
            file=output,
        )
        return paths
    except (CaptureOperationError, CaptureAuditError):
        raise
    except Exception as exc:
        raise CaptureOperationError("capture", type(exc).__name__) from None
    finally:
        await asyncio.gather(
            websocket_client.aclose(), rest_client.aclose(), return_exceptions=True
        )


def _validate_capture_arguments(
    entity_id: str, *, run_id: str | None, latest: bool
) -> None:
    if (
        not entity_id.startswith("automation.")
        or entity_id.count(".") != 1
        or not entity_id.split(".", 1)[1]
    ):
        raise CaptureOperationError("validation", "not_automation_entity")
    if run_id is not None and latest:
        raise CaptureOperationError("validation", "trace_selection_conflict")
    if run_id is not None and not run_id.strip():
        raise CaptureOperationError("validation", "empty_run_id")


def _resolve_automation_id(state: Any, automation_config: Any) -> str:
    candidates: list[Any] = []
    if isinstance(state, dict):
        attributes = state.get("attributes")
        if isinstance(attributes, dict):
            candidates.append(attributes.get("id"))
    if isinstance(automation_config, dict):
        config = automation_config.get("config")
        if isinstance(config, dict):
            candidates.append(config.get("id"))
        candidates.append(automation_config.get("id"))
    for candidate in candidates:
        if isinstance(candidate, (str, int)) and str(candidate).strip():
            return str(candidate).strip()
    raise CaptureOperationError("resolve_automation_id", "missing_internal_id")


def _print_trace_summary(
    traces: list[Any], sanitizer: CaptureAnonymizer, *, output: TextIO
) -> None:
    print(
        f"Trace summaries: {min(len(traces), MAX_TRACES_SHOWN)} shown"
        f" of {len(traces)} available.",
        file=output,
    )
    for position, trace in enumerate(traces[:MAX_TRACES_SHOWN], start=1):
        if not isinstance(trace, dict):
            print(f"{position}. invalid trace summary", file=output)
            continue
        run_id = trace.get("run_id")
        safe_run_id = (
            sanitizer.pseudonymize_identifier("run", run_id)
            if run_id is not None
            else "run_unknown"
        )
        state = _safe_summary_value(
            trace.get("state"), allowed={"running", "stopped", "debugged"}
        )
        started, finished = _trace_timestamps(trace)
        duration = _duration_seconds(started, finished)
        duration_text = "unknown" if duration is None else f"{duration:.1f}s"
        print(
            f"{position}. state={state} date={_approximate_timestamp(started)} "
            f"duration={duration_text} run={safe_run_id}",
            file=output,
        )


def _select_latest_run_id(traces: list[Any]) -> str | None:
    candidates: list[tuple[datetime, str]] = []
    fallback: str | None = None
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        run_id = trace.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        fallback = fallback or run_id
        started, _ = _trace_timestamps(trace)
        parsed = _parse_timestamp(started)
        if parsed is not None:
            candidates.append((parsed, run_id))
    return max(candidates, key=lambda item: item[0])[1] if candidates else fallback


def _trace_timestamps(trace: dict[str, Any]) -> tuple[Any, Any]:
    timestamp = trace.get("timestamp")
    if not isinstance(timestamp, dict):
        return None, None
    return timestamp.get("start"), timestamp.get("finish")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


def _approximate_timestamp(value: Any) -> str:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return "unknown"
    rounded = parsed.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return rounded.isoformat().replace("+00:00", "Z")


def _duration_seconds(start: Any, finish: Any) -> float | None:
    parsed_start = _parse_timestamp(start)
    parsed_finish = _parse_timestamp(finish)
    if parsed_start is None or parsed_finish is None:
        return None
    return max((parsed_finish - parsed_start).total_seconds(), 0.0)


def _safe_summary_value(value: Any, *, allowed: set[str]) -> str:
    normalized = str(value).lower()
    return normalized if normalized in allowed else "unknown"


def _terminal_cell(value: Any, *, max_length: int = 160) -> str:
    if value is None:
        return "-"
    normalized = " ".join(str(value).split())
    return normalized[:max_length] if normalized else "-"


def _rounded_capture_time() -> str:
    rounded = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return rounded.isoformat().replace("+00:00", "Z")


def _atomic_write_documents(
    output_dir: Path, documents: dict[str, str], *, overwrite: bool
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    destinations = [output_dir / filename for filename in documents]
    if any(path.exists() for path in destinations) and not overwrite:
        raise CaptureOperationError("write", "destination_exists")

    temporary_paths: list[tuple[Path, Path]] = []
    try:
        for filename, serialized in documents.items():
            destination = output_dir / filename
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{filename}.", suffix=".tmp", dir=output_dir
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise
            temporary_paths.append((temporary_path, destination))
        for temporary_path, destination in temporary_paths:
            os.replace(temporary_path, destination)
        return destinations
    finally:
        for temporary_path, _ in temporary_paths:
            temporary_path.unlink(missing_ok=True)
