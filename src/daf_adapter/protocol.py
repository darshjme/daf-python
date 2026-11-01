"""DAF adapter protocol v1. Strict validation precedes every side effect."""

import json
from pathlib import Path

MAX_REQUEST_BYTES = 1_048_576
FIELDS = {"schema_version", "id", "argv", "cwd", "stdin", "timeout_ms", "max_output_bytes"}


def validate_request(value):
    """Return a detached normalized request, rejecting ambiguous JSON types."""
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError("request must be an object with known protocol fields")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("request must contain finite valid UTF-8 JSON") from error
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds 1 MiB")
    request = json.loads(encoded)
    if type(request.get("schema_version")) is not int or request["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    ident = request.get("id")
    if not isinstance(ident, str) or not ident.strip() or len(ident.encode("utf-8")) > 128:
        raise ValueError("id must contain 1..128 UTF-8 bytes")
    argv = request.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 4096 or any(not isinstance(v, str) or "\0" in v for v in argv) or not argv[0]:
        raise ValueError("argv must be a nonempty string array without NUL; executable must be nonempty")
    for key in ("cwd", "stdin"):
        if request.get(key) is not None and not isinstance(request[key], str):
            raise ValueError(f"{key} must be a string or null")
    if request.get("cwd") is not None and (not request["cwd"] or "\0" in request["cwd"]):
        raise ValueError("cwd must be nonempty without NUL")
    if request.get("cwd") is not None and not Path(request["cwd"]).is_dir():
        raise ValueError("cwd must be an existing directory")
    for key, default, maximum in (("timeout_ms", 300_000, 3_600_000), ("max_output_bytes", 65_536, 1_048_576)):
        item = request.get(key, default)
        if type(item) is not int or not 1 <= item <= maximum:
            raise ValueError(f"{key} must be an integer in 1..{maximum}")
        request[key] = item
    request.setdefault("cwd", None)
    request.setdefault("stdin", None)
    if len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("normalized request exceeds 1 MiB")
    return request


def validate_result(value, ident):
    """Reject malformed callback success before it can release dependents."""
    required = {"schema_version", "id", "status", "exit_code", "stdout", "stderr", "stdout_truncated", "stderr_truncated"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("result fields do not match protocol 1")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["id"] != ident:
        raise ValueError("result identity or version mismatch")
    if value["status"] not in {"succeeded", "failed", "timed_out"}:
        raise ValueError("unknown result status")
    code = value["exit_code"]
    if code is not None and type(code) is not int:
        raise ValueError("invalid exit code")
    if (value["status"] == "succeeded") != (code == 0):
        raise ValueError("success requires exit code zero")
    for key in ("stdout", "stderr"):
        if not isinstance(value[key], str) or len(value[key].encode("utf-8")) > 3 * 1_048_576:
            raise ValueError("invalid or oversized output")
        if type(value[key + "_truncated"]) is not bool:
            raise ValueError("invalid truncation flag")
    return json.loads(json.dumps(value, allow_nan=False))


def capabilities():
    """Describe actual protocol features rather than model intelligence."""
    return {
        "schema_version": 1,
        "protocol": "daf.process.v1",
        "max_request_bytes": MAX_REQUEST_BYTES,
        "max_output_bytes": 1_048_576,
        "max_timeout_ms": 3_600_000,
        "shell": False,
        "retries": False,
        "termination_scope": "direct-child",
        "default_timeout_ms": 300_000,
        "default_max_output_bytes": 65_536,
        "output_encoding": "utf-8-lossy",
        "cwd_base": "caller",
        "environment": "inherited",
        "sandbox": False,
    }
