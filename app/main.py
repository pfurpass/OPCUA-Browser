"""OPC UA Browser – Webdienst zum Erkunden des Adressraums einer SPS."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from asyncua import Client, ua
from asyncua.common.node import Node
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .sessions import ConnectError, Session, manager
from .ua_json import access_level_names, datavalue_payload, display_value, jsonable, status_info

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("opcua-browser")

STATIC_DIR = Path(__file__).parent / "static"
ROOT_NODE = "i=84"
READ_ONLY = os.environ.get("OPCUA_READ_ONLY", "false").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.start()
    log.info("OPC UA Browser bereit (Schreibzugriff: %s)", "aus" if READ_ONLY else "an")
    yield
    await manager.stop()


app = FastAPI(title="OPC UA Browser", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------- Modelle
class ConnectRequest(BaseModel):
    url: str
    username: str | None = None
    password: str | None = None
    policy: str = "None"
    mode: str = "SignAndEncrypt"
    timeout: float = Field(default=8.0, ge=1, le=60)


class EndpointRequest(BaseModel):
    url: str
    timeout: float = Field(default=8.0, ge=1, le=60)


class WriteRequest(BaseModel):
    nodeId: str
    value: str


# ----------------------------------------------------------------- Hilfsmittel
def get_session(session_id: str) -> Session:
    try:
        return manager.get(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Die Verbindung besteht nicht mehr. Bitte neu verbinden.")


def to_node(session: Session, node_id: str) -> Node:
    try:
        return session.client.get_node(ua.NodeId.from_string(node_id))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Ungültige NodeId '{node_id}': {exc}")


def ua_error(exc: Exception, session: Session | None = None) -> HTTPException:
    text = str(exc).lower()
    if isinstance(exc, (ConnectionError, BrokenPipeError)) or "disconnect" in text or "not connected" in text:
        if session is not None:
            asyncio.create_task(manager.disconnect(session.id))
        return HTTPException(
            status_code=409,
            detail="Die Verbindung zum Server ist abgerissen. Bitte neu verbinden.",
        )
    if isinstance(exc, ua.UaStatusCodeError):
        info = status_info(ua.StatusCode(exc.code)) if hasattr(exc, "code") else {"name": str(exc)}
        return HTTPException(status_code=502, detail=f"Der Server meldet {info['name']}.")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return HTTPException(status_code=504, detail="Der Server hat nicht rechtzeitig geantwortet.")
    if isinstance(exc, (ConnectionError, OSError)):
        return HTTPException(status_code=502, detail=f"Verbindung unterbrochen: {exc}")
    return HTTPException(status_code=502, detail=str(exc) or exc.__class__.__name__)


NODE_CLASS_ORDER = {
    "Object": 0,
    "View": 1,
    "Variable": 2,
    "Method": 3,
    "ObjectType": 4,
    "VariableType": 5,
    "DataType": 6,
    "ReferenceType": 7,
}


def node_class_name_of(value: Any) -> str:
    """NodeClass kommt je nach Server als Enum oder als nackte Zahl zurück."""
    if value is None:
        return "Unspecified"
    if hasattr(value, "name"):
        return value.name
    try:
        return ua.NodeClass(int(value)).name
    except Exception:  # noqa: BLE001
        return str(value)


def ref_to_entry(ref: ua.ReferenceDescription) -> dict[str, Any]:
    node_class = node_class_name_of(ref.NodeClass)
    return {
        "nodeId": ref.NodeId.NodeId.to_string() if isinstance(ref.NodeId, ua.ExpandedNodeId) else ref.NodeId.to_string(),
        "browseName": ref.BrowseName.to_string() if ref.BrowseName else "",
        "displayName": (ref.DisplayName.Text if ref.DisplayName and ref.DisplayName.Text else None)
        or (ref.BrowseName.Name if ref.BrowseName else "?"),
        "nodeClass": node_class,
        "typeDefinition": ref.TypeDefinition.to_string() if ref.TypeDefinition else None,
    }


async def browse_many(client: Client, node_ids: list[ua.NodeId]) -> list[list[ua.ReferenceDescription]]:
    """Mehrere Knoten in einem Aufruf nach hierarchischen Kindern durchsuchen."""
    if not node_ids:
        return []
    descriptions = []
    for nid in node_ids:
        desc = ua.BrowseDescription()
        desc.NodeId = nid
        desc.BrowseDirection = ua.BrowseDirection.Forward
        desc.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences)
        desc.IncludeSubtypes = True
        desc.NodeClassMask = ua.NodeClass.Unspecified
        desc.ResultMask = ua.BrowseResultMask.All
        descriptions.append(desc)
    params = ua.BrowseParameters()
    params.View = ua.ViewDescription()
    params.RequestedMaxReferencesPerNode = 0
    params.NodesToBrowse = descriptions
    results = await client.uaclient.browse(params)
    return [list(res.References or []) for res in results]


# ------------------------------------------------------------------ Verbindung
@app.post("/api/connect")
async def api_connect(req: ConnectRequest):
    try:
        session = await manager.connect(
            url=req.url.strip(),
            username=req.username or None,
            password=req.password or None,
            policy=req.policy,
            mode=req.mode,
            timeout=req.timeout,
        )
    except ConnectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    payload = session.summary()
    payload["readOnly"] = READ_ONLY
    payload["rootNodeId"] = ROOT_NODE
    return payload


@app.post("/api/endpoints")
async def api_endpoints(req: EndpointRequest):
    try:
        return {"endpoints": await manager.endpoints(req.url.strip(), req.timeout)}
    except ConnectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/sessions")
async def api_sessions():
    return {"sessions": [s.summary() for s in manager.all()], "readOnly": READ_ONLY}


@app.delete("/api/sessions/{session_id}")
async def api_disconnect(session_id: str):
    await manager.disconnect(session_id)
    return {"ok": True}


@app.get("/api/sessions/{session_id}/health")
async def api_health(session_id: str):
    session = get_session(session_id)
    try:
        await session.client.check_connection()
        return {"connected": True}
    except Exception as exc:  # noqa: BLE001
        return {"connected": False, "detail": str(exc)}


# ---------------------------------------------------------------------- Browse
@app.get("/api/sessions/{session_id}/browse")
async def api_browse(session_id: str, nodeId: str = ROOT_NODE, values: bool = True):
    session = get_session(session_id)
    node = to_node(session, nodeId)
    try:
        refs = await node.get_children_descriptions()
        entries = [ref_to_entry(r) for r in refs]
        entries.sort(key=lambda e: (NODE_CLASS_ORDER.get(e["nodeClass"], 9), e["displayName"].lower()))

        child_ids = [ua.NodeId.from_string(e["nodeId"]) for e in entries]
        if child_ids:
            try:
                grandchildren = await browse_many(session.client, child_ids)
                for entry, refs_of_child in zip(entries, grandchildren):
                    entry["hasChildren"] = len(refs_of_child) > 0
            except Exception as exc:  # noqa: BLE001
                log.debug("Kinderprüfung fehlgeschlagen: %s", exc)
                for entry in entries:
                    entry["hasChildren"] = entry["nodeClass"] in ("Object", "ObjectType", "VariableType", "DataType")

        if values:
            variables = [e for e in entries if e["nodeClass"] == "Variable"]
            if variables:
                try:
                    nodes = [session.client.get_node(ua.NodeId.from_string(e["nodeId"])) for e in variables]
                    data_values = await session.client.read_attributes(nodes, ua.AttributeIds.Value)
                    for entry, dv in zip(variables, data_values):
                        payload = datavalue_payload(dv)
                        entry["preview"] = payload["text"][:80]
                        entry["status"] = payload["status"]["severity"]
                        entry["dataType"] = payload["type"]
                except Exception as exc:  # noqa: BLE001
                    log.debug("Werte-Vorschau fehlgeschlagen: %s", exc)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ua_error(exc, session)
    return {"nodeId": nodeId, "children": entries}


ATTRIBUTES = [
    ua.AttributeIds.NodeId,
    ua.AttributeIds.NodeClass,
    ua.AttributeIds.BrowseName,
    ua.AttributeIds.DisplayName,
    ua.AttributeIds.Description,
    ua.AttributeIds.DataType,
    ua.AttributeIds.ValueRank,
    ua.AttributeIds.ArrayDimensions,
    ua.AttributeIds.AccessLevel,
    ua.AttributeIds.UserAccessLevel,
    ua.AttributeIds.MinimumSamplingInterval,
    ua.AttributeIds.Historizing,
    ua.AttributeIds.Executable,
    ua.AttributeIds.UserExecutable,
    ua.AttributeIds.IsAbstract,
    ua.AttributeIds.Symmetric,
    ua.AttributeIds.InverseName,
    ua.AttributeIds.EventNotifier,
    ua.AttributeIds.WriteMask,
    ua.AttributeIds.UserWriteMask,
]


@app.get("/api/sessions/{session_id}/node")
async def api_node(session_id: str, nodeId: str):
    session = get_session(session_id)
    node = to_node(session, nodeId)
    try:
        results = await node.read_attributes(ATTRIBUTES)
    except Exception as exc:  # noqa: BLE001
        raise ua_error(exc, session)

    attributes: list[dict[str, Any]] = []
    raw: dict[str, Any] = {}
    for attr, dv in zip(ATTRIBUTES, results):
        if dv.StatusCode is not None and not dv.StatusCode.is_good():
            continue
        value = dv.Value.Value if dv.Value is not None else None
        raw[attr.name] = value
        text = display_value(value)
        if attr == ua.AttributeIds.DataType and isinstance(value, ua.NodeId):
            text = await friendly_type_name(session, value)
        elif attr in (ua.AttributeIds.AccessLevel, ua.AttributeIds.UserAccessLevel):
            names = access_level_names(value)
            text = ", ".join(names) if names else "keine"
        elif attr == ua.AttributeIds.NodeClass:
            text = node_class_name_of(value)
        elif attr == ua.AttributeIds.ValueRank:
            text = {-3: "Skalar oder Array", -2: "beliebig", -1: "Skalar", 0: "Array"}.get(
                value if isinstance(value, int) else -1, f"{value}-dimensional"
            )
        attributes.append({"name": attr.name, "text": text, "value": jsonable(value)})

    node_class_name = node_class_name_of(raw.get("NodeClass"))
    writable = "CurrentWrite" in access_level_names(raw.get("UserAccessLevel", raw.get("AccessLevel", 0)))

    payload = {
        "nodeId": nodeId,
        "browseName": display_value(raw.get("BrowseName")),
        "displayName": display_value(raw.get("DisplayName")),
        "description": display_value(raw.get("Description")) if raw.get("Description") else "",
        "nodeClass": node_class_name,
        "writable": writable and not READ_ONLY,
        "attributes": attributes,
        "path": await node_path(session, node),
    }

    if node_class_name == "Variable":
        try:
            dv = await node.read_data_value()
            payload["value"] = datavalue_payload(dv)
        except Exception as exc:  # noqa: BLE001
            payload["value"] = {"error": str(exc)}
    if node_class_name == "Method":
        payload["arguments"] = await method_arguments(session, node)
    return payload


async def friendly_type_name(session: Session, type_id: ua.NodeId) -> str:
    try:
        name = await session.client.get_node(type_id).read_browse_name()
        return f"{name.Name} ({type_id.to_string()})"
    except Exception:  # noqa: BLE001
        return type_id.to_string()


async def node_path(session: Session, node: Node) -> list[dict[str, str]]:
    """Pfad von der Wurzel bis zum Knoten, für die Brotkrumenleiste."""
    path: list[dict[str, str]] = []
    current = node
    for _ in range(32):
        try:
            name = await current.read_browse_name()
        except Exception:  # noqa: BLE001
            break
        path.insert(0, {"nodeId": current.nodeid.to_string(), "name": name.Name})
        if current.nodeid == ua.NodeId(84):
            break
        try:
            parent = await current.get_parent()
        except Exception:  # noqa: BLE001
            break
        if parent is None:
            break
        current = parent
    return path


async def method_arguments(session: Session, node: Node) -> dict[str, list]:
    out: dict[str, list] = {"input": [], "output": []}
    for prop_name, key in (("InputArguments", "input"), ("OutputArguments", "output")):
        try:
            prop = await node.get_child(f"0:{prop_name}")
            for arg in await prop.read_value() or []:
                out[key].append(
                    {
                        "name": arg.Name,
                        "dataType": arg.DataType.to_string(),
                        "description": arg.Description.Text if arg.Description else "",
                    }
                )
        except Exception:  # noqa: BLE001
            continue
    return out


@app.get("/api/sessions/{session_id}/references")
async def api_references(session_id: str, nodeId: str):
    session = get_session(session_id)
    node = to_node(session, nodeId)
    try:
        refs = await node.get_references()
    except Exception as exc:  # noqa: BLE001
        raise ua_error(exc, session)
    out = []
    for ref in refs:
        entry = ref_to_entry(ref)
        entry["isForward"] = bool(ref.IsForward)
        entry["referenceType"] = await friendly_reference_name(session, ref.ReferenceTypeId)
        out.append(entry)
    out.sort(key=lambda e: (not e["isForward"], e["referenceType"], e["displayName"].lower()))
    return {"references": out}


_ref_name_cache: dict[str, str] = {}


async def friendly_reference_name(session: Session, type_id: ua.NodeId) -> str:
    key = type_id.to_string()
    if key in _ref_name_cache:
        return _ref_name_cache[key]
    try:
        name = (await session.client.get_node(type_id).read_browse_name()).Name
    except Exception:  # noqa: BLE001
        name = key
    _ref_name_cache[key] = name
    return name


# ------------------------------------------------------------- Werte lesen/schreiben
@app.get("/api/sessions/{session_id}/values")
async def api_values(session_id: str, nodeId: list[str] = Query(default=[])):
    session = get_session(session_id)
    if not nodeId:
        return {"values": {}}
    nodes = [to_node(session, n) for n in nodeId]
    try:
        data_values = await session.client.read_attributes(nodes, ua.AttributeIds.Value)
    except Exception as exc:  # noqa: BLE001
        raise ua_error(exc, session)
    return {"values": {n: datavalue_payload(dv) for n, dv in zip(nodeId, data_values)}}


def parse_scalar(text: str, vtype: ua.VariantType) -> Any:
    name = vtype.name
    text = text.strip()
    if name == "Boolean":
        return text.lower() in ("1", "true", "wahr", "ja", "on", "high")
    if name in ("SByte", "Byte", "Int16", "UInt16", "Int32", "UInt32", "Int64", "UInt64"):
        return int(text, 0) if text.lower().startswith(("0x", "0b", "0o")) else int(float(text.replace(",", ".")))
    if name in ("Float", "Double"):
        return float(text.replace(",", "."))
    if name in ("String", "XmlElement"):
        return text
    if name == "LocalizedText":
        return ua.LocalizedText(text)
    if name == "DateTime":
        return dt.datetime.fromisoformat(text)
    if name == "Guid":
        return uuid.UUID(text)
    if name == "ByteString":
        return base64.b64decode(text)
    if name == "NodeId":
        return ua.NodeId.from_string(text)
    if name == "QualifiedName":
        return ua.QualifiedName.from_string(text)
    # Enumerationen und alles Übrige: Zahl oder JSON versuchen
    try:
        return int(text)
    except ValueError:
        return json.loads(text)


@app.post("/api/sessions/{session_id}/write")
async def api_write(session_id: str, req: WriteRequest):
    if READ_ONLY:
        raise HTTPException(status_code=403, detail="Dieser Browser läuft im Nur-Lesen-Betrieb.")
    session = get_session(session_id)
    node = to_node(session, req.nodeId)
    try:
        current = await node.read_data_value()
        vtype = current.Value.VariantType if current.Value is not None else None
        is_array = isinstance(current.Value.Value, (list, tuple)) if current.Value is not None else False
    except Exception:  # noqa: BLE001
        try:
            vtype = await node.read_data_type_as_variant_type()
            is_array = False
        except Exception as exc:  # noqa: BLE001
            raise ua_error(exc, session)

    try:
        if is_array or req.value.strip().startswith("["):
            items = json.loads(req.value)
            if not isinstance(items, list):
                raise ValueError("Für ein Array wird eine Liste in eckigen Klammern erwartet.")
            value: Any = [parse_scalar(str(i), vtype) if not isinstance(i, (int, float, bool)) else i for i in items]
        else:
            value = parse_scalar(req.value, vtype)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"'{req.value}' passt nicht zum Typ {vtype.name}: {exc}")

    try:
        await node.write_value(ua.DataValue(ua.Variant(value, vtype)))
        dv = await node.read_data_value()
    except Exception as exc:  # noqa: BLE001
        raise ua_error(exc, session)
    log.info("Wert geschrieben: %s = %s", req.nodeId, req.value)
    return {"ok": True, "value": datavalue_payload(dv)}


# ----------------------------------------------------------------------- Suche
@app.get("/api/sessions/{session_id}/search")
async def api_search(
    session_id: str,
    q: str,
    root: str = ROOT_NODE,
    limit: int = Query(default=50, ge=1, le=200),
    maxNodes: int = Query(default=6000, ge=100, le=50000),
):
    session = get_session(session_id)
    needle = q.strip().lower()
    if len(needle) < 2:
        raise HTTPException(status_code=400, detail="Bitte mindestens zwei Zeichen eingeben.")

    start = ua.NodeId.from_string(root)
    queue: deque[tuple[ua.NodeId, list[dict[str, str]]]] = deque([(start, [])])
    seen: set[str] = {start.to_string()}
    hits: list[dict[str, Any]] = []
    visited = 0
    deadline = time.monotonic() + 20

    try:
        while queue and len(hits) < limit and visited < maxNodes and time.monotonic() < deadline:
            batch = [queue.popleft() for _ in range(min(40, len(queue)))]
            results = await browse_many(session.client, [nid for nid, _ in batch])
            visited += len(batch)
            for (_, parents), refs in zip(batch, results):
                for ref in refs:
                    entry = ref_to_entry(ref)
                    key = entry["nodeId"]
                    if key in seen:
                        continue
                    seen.add(key)
                    trail = parents + [{"nodeId": key, "name": entry["displayName"]}]
                    if needle in entry["displayName"].lower() or needle in entry["browseName"].lower():
                        entry["path"] = trail
                        hits.append(entry)
                        if len(hits) >= limit:
                            break
                    if entry["nodeClass"] in ("Object", "Variable", "View"):
                        queue.append((ua.NodeId.from_string(key), trail))
                if len(hits) >= limit:
                    break
    except Exception as exc:  # noqa: BLE001
        raise ua_error(exc, session)

    return {"hits": hits, "visited": visited, "truncated": bool(queue) and len(hits) >= limit}


# -------------------------------------------------------------------- Oberfläche
@app.get("/healthz")
async def healthz():
    return JSONResponse({"status": "ok", "sessions": len(manager.all())})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
