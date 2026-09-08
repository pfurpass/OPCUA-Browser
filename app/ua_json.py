"""Konvertierung von OPC-UA-Typen in JSON-taugliche Python-Strukturen."""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import enum
import json
import uuid
from typing import Any

from asyncua import ua

MAX_DEPTH = 8


def status_info(code: ua.StatusCode | None) -> dict[str, Any]:
    """Statuscode in Name, Nummer und Schweregrad (good/uncertain/bad) zerlegen."""
    if code is None:
        return {"name": "Good", "code": "0x00000000", "severity": "good", "doc": ""}
    try:
        raw = int(code.value)
    except Exception:  # pragma: no cover - defensiv
        raw = 0
    severity = {0: "good", 1: "uncertain"}.get(raw >> 30, "bad")
    try:
        name = code.name
        doc = code.doc
    except Exception:
        name, doc = "Unknown", ""
    return {"name": name, "code": f"0x{raw:08X}", "severity": severity, "doc": doc}


def jsonable(value: Any, depth: int = 0) -> Any:
    """Beliebigen OPC-UA-Wert in etwas verwandeln, das json.dumps verdaut."""
    if value is None:
        return None
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, bool) or isinstance(value, str):
        return value
    if isinstance(value, int):
        # UInt64 jenseits von 2^53 verliert in JavaScript Präzision -> als Text
        return value if -(2**53) < value < 2**53 else str(value)
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (ua.NodeId, ua.ExpandedNodeId)):
        return value.to_string()
    if isinstance(value, ua.QualifiedName):
        return value.to_string()
    if isinstance(value, ua.LocalizedText):
        return value.Text
    if isinstance(value, ua.StatusCode):
        return status_info(value)["name"]
    if isinstance(value, ua.Variant):
        return jsonable(value.Value, depth + 1)
    if isinstance(value, ua.DataValue):
        return jsonable(value.Value, depth + 1)
    if depth >= MAX_DEPTH:
        return str(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name, None), depth + 1) for f in dataclasses.fields(value)}
    if hasattr(value, "__dict__"):
        return {k: jsonable(v, depth + 1) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def display_value(value: Any) -> str:
    """Kurze, gut lesbare Textdarstellung eines Werts für die Oberfläche."""
    plain = jsonable(value)
    if plain is None:
        return "null"
    if isinstance(plain, bool):
        return "true" if plain else "false"
    if isinstance(plain, (int, float, str)):
        return str(plain)
    try:
        return json.dumps(plain, ensure_ascii=False, default=str)
    except Exception:
        return str(plain)


def datavalue_payload(dv: ua.DataValue | None) -> dict[str, Any]:
    """DataValue in Wert, Status, Zeitstempel und Datentyp aufteilen."""
    if dv is None:
        return {"value": None, "text": "", "status": status_info(None), "type": None}
    variant = dv.Value
    vtype = None
    is_array = False
    if variant is not None:
        vtype = variant.VariantType.name if variant.VariantType is not None else None
        is_array = isinstance(variant.Value, (list, tuple))
    return {
        "value": jsonable(variant.Value if variant is not None else None),
        "text": display_value(variant.Value if variant is not None else None),
        "status": status_info(dv.StatusCode),
        "type": vtype,
        "isArray": is_array,
        "sourceTimestamp": dv.SourceTimestamp.isoformat() if dv.SourceTimestamp else None,
        "serverTimestamp": dv.ServerTimestamp.isoformat() if dv.ServerTimestamp else None,
    }


ACCESS_BITS = [
    (0x01, "CurrentRead"),
    (0x02, "CurrentWrite"),
    (0x04, "HistoryRead"),
    (0x08, "HistoryWrite"),
    (0x10, "SemanticChange"),
    (0x20, "StatusWrite"),
    (0x40, "TimestampWrite"),
]


def access_level_names(raw: Any) -> list[str]:
    """AccessLevel-Bitmaske in Klartext übersetzen."""
    try:
        if isinstance(raw, (set, frozenset, list, tuple)):
            bits = 0
            for item in raw:
                bits |= int(getattr(item, "value", item))
        else:
            bits = int(getattr(raw, "value", raw))
    except Exception:
        return []
    return [name for mask, name in ACCESS_BITS if bits & mask]
