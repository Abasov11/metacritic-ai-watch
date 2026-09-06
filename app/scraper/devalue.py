"""Decoder for the `devalue` payload Nuxt embeds in `<script id="__NUXT_DATA__">`.

`devalue.stringify` flattens a value graph into a single JSON array: every node lives
at its own index and every reference to a node is that index. Decoding is therefore
"walk from index 0 and replace integers by the values they point at", plus a handful
of sentinels for values JSON cannot express.

Reference: https://github.com/Rich-Harris/devalue
"""

from __future__ import annotations

import json
import re
from typing import Any

# Sentinels used instead of an index (see devalue's `parse`).
HOLE = -1
UNDEFINED = -2
NAN = -3
POSITIVE_INFINITY = -4
NEGATIVE_INFINITY = -5
NEGATIVE_ZERO = -6

_NUXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

#: Types devalue encodes as ``[type_name, payload]``; we unwrap them to the payload.
_TRANSPARENT_TYPES = frozenset(
    {
        "Date",  # ISO string — keeping the string is more useful than a datetime here
        "BigInt",
        "RegExp",
        "URL",
        "Object",  # boxed primitive
        # Nuxt's own revivers, all of which wrap a single value:
        "Ref",
        "ShallowRef",
        "EmptyRef",
        "Reactive",
        "ShallowReactive",
        "NuxtError",
    }
)


class DevalueError(ValueError):
    """Raised when a payload cannot be decoded."""


def parse(flat: list[Any]) -> Any:
    """Rebuild the original value graph from devalue's flat array.

    Cycles are supported: containers are registered in the memo before their children
    are hydrated, so a self-reference resolves to the (still filling) container.
    """
    if not isinstance(flat, list) or not flat:
        raise DevalueError("devalue payload must be a non-empty array")

    hydrated: dict[int, Any] = {}

    def hydrate(index: Any) -> Any:
        if not isinstance(index, int):
            # devalue only ever stores indices/sentinels in reference position.
            raise DevalueError(f"expected an index, got {index!r}")
        if index == UNDEFINED or index == HOLE:
            return None
        if index == NAN:
            return float("nan")
        if index == POSITIVE_INFINITY:
            return float("inf")
        if index == NEGATIVE_INFINITY:
            return float("-inf")
        if index == NEGATIVE_ZERO:
            return -0.0
        if index < 0 or index >= len(flat):
            raise DevalueError(f"index {index} out of range (len={len(flat)})")
        if index in hydrated:
            return hydrated[index]

        value = flat[index]

        if isinstance(value, dict):
            obj: dict[str, Any] = {}
            hydrated[index] = obj
            for key, ref in value.items():
                obj[key] = hydrate(ref)
            return obj

        if isinstance(value, list):
            if value and isinstance(value[0], str):
                return _hydrate_tagged(index, value, hydrate, hydrated)
            arr: list[Any] = []
            hydrated[index] = arr
            arr.extend(hydrate(ref) for ref in value)
            return arr

        # Plain scalar: string, number, bool or null.
        hydrated[index] = value
        return value

    return hydrate(0)


def _hydrate_tagged(
    index: int, value: list[Any], hydrate, hydrated: dict[int, Any]
) -> Any:
    """Handle ``[type_name, ...]`` nodes."""
    type_name = value[0]

    if type_name == "Set":
        # A Set becomes a list: nothing downstream needs set semantics.
        items: list[Any] = []
        # Register before recursing so a member may reference the set itself.
        hydrated[index] = items
        items.extend(hydrate(ref) for ref in value[1:])
        return items

    if type_name == "Map":
        mapping: dict[Any, Any] = {}
        hydrated[index] = mapping
        for i in range(1, len(value) - 1, 2):
            mapping[hydrate(value[i])] = hydrate(value[i + 1])
        return mapping

    if type_name == "null":
        # Object with a null prototype: ["null", key_idx, val_idx, ...]
        obj: dict[str, Any] = {}
        hydrated[index] = obj
        for i in range(1, len(value) - 1, 2):
            obj[hydrate(value[i])] = hydrate(value[i + 1])
        return obj

    if type_name in _TRANSPARENT_TYPES and len(value) == 2:
        out = hydrate(value[1])
        hydrated[index] = out
        return out

    # Unknown tag — the first string is data, not a type name, so treat as an array.
    arr: list[Any] = []
    hydrated[index] = arr
    arr.extend(hydrate(ref) if isinstance(ref, int) else ref for ref in value)
    return arr


def extract_nuxt_payload(html: str) -> Any:
    """Pull `__NUXT_DATA__` out of an SSR page and decode it."""
    match = _NUXT_DATA_RE.search(html)
    if not match:
        raise DevalueError("no __NUXT_DATA__ script found in page")
    try:
        flat = json.loads(match.group(1))
    except json.JSONDecodeError as exc:  # pragma: no cover - malformed page
        raise DevalueError(f"__NUXT_DATA__ is not valid JSON: {exc}") from exc
    return parse(flat)
