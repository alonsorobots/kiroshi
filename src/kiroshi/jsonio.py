"""Fast JSON I/O with orjson, transparent fallback to stdlib json.

orjson (when installed via the `fast` extra) is roughly 2x faster to parse and
~16x faster to serialize than stdlib json. All three functions exist regardless.
"""
from __future__ import annotations

from typing import Any

try:
    import orjson

    def loads(data: str | bytes) -> Any:
        return orjson.loads(data)

    def dumps(obj: Any) -> str:
        """Serialize to a JSON string (text-mode friendly)."""
        return orjson.dumps(obj).decode("utf-8")

    def dumps_bytes(obj: Any) -> bytes:
        """Serialize to JSON bytes (fastest, binary-mode friendly)."""
        return orjson.dumps(obj)

    HAS_ORJSON = True

except ImportError:  # pragma: no cover - exercised only without orjson
    import json as _json

    def loads(data: str | bytes) -> Any:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        return _json.loads(data)

    def dumps(obj: Any) -> str:
        return _json.dumps(obj, separators=(",", ":"))

    def dumps_bytes(obj: Any) -> bytes:
        return dumps(obj).encode("utf-8")

    HAS_ORJSON = False
