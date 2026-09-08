"""Verwaltung der offenen OPC-UA-Verbindungen."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from asyncua import Client, ua

log = logging.getLogger("opcua-browser.sessions")

APP_URI = os.environ.get("OPCUA_APPLICATION_URI", "urn:opcua-browser:client")
CERT_DIR = Path(os.environ.get("OPCUA_CERT_DIR", "/data/certs"))
IDLE_TIMEOUT = float(os.environ.get("OPCUA_SESSION_IDLE_TIMEOUT", "1800"))
MAX_SESSIONS = int(os.environ.get("OPCUA_MAX_SESSIONS", "20"))

SECURITY_POLICIES = ["Basic256Sha256", "Aes128Sha256RsaOaep", "Aes256Sha256RsaPss"]
SECURITY_MODES = ["Sign", "SignAndEncrypt"]


class ConnectError(RuntimeError):
    pass


@dataclass
class Session:
    id: str
    url: str
    client: Client
    server_name: str = ""
    security: str = "None"
    namespaces: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self) -> None:
        self.last_used = time.time()

    def summary(self) -> dict:
        return {
            "sessionId": self.id,
            "url": self.url,
            "serverName": self.server_name,
            "security": self.security,
            "namespaces": self.namespaces,
            "connectedSince": self.created,
        }


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._janitor: asyncio.Task | None = None

    # ------------------------------------------------------------------ Zugriff
    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        session.touch()
        return session

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    # ------------------------------------------------------------- Verbindungen
    async def connect(
        self,
        url: str,
        username: str | None = None,
        password: str | None = None,
        policy: str = "None",
        mode: str = "SignAndEncrypt",
        timeout: float = 8.0,
    ) -> Session:
        if not url.startswith("opc.tcp://"):
            raise ConnectError("Die Adresse muss mit opc.tcp:// beginnen.")
        if len(self._sessions) >= MAX_SESSIONS:
            raise ConnectError(f"Es sind bereits {MAX_SESSIONS} Verbindungen offen. Schließe zuerst eine davon.")

        client = Client(url=url, timeout=timeout)
        client.application_uri = APP_URI
        client.name = "OPC UA Browser"
        if username:
            client.set_user(username)
        if password:
            client.set_password(password)

        if policy and policy != "None":
            if policy not in SECURITY_POLICIES:
                raise ConnectError(f"Unbekannte Sicherheitsrichtlinie: {policy}")
            if mode not in SECURITY_MODES:
                raise ConnectError(f"Unbekannter Modus: {mode}")
            cert, key = await self._ensure_certificate(client)
            await client.set_security_string(f"{policy},{mode},{cert},{key}")

        try:
            await asyncio.wait_for(client.connect(), timeout=timeout + 5)
        except asyncio.TimeoutError as exc:
            raise ConnectError(f"Zeitüberschreitung beim Verbinden mit {url}.") from exc
        except ua.UaStatusCodeError as exc:
            raise ConnectError(f"Der Server hat die Verbindung abgelehnt: {exc}") from exc
        except OSError as exc:
            raise ConnectError(f"Server nicht erreichbar: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - alles Übrige lesbar melden
            raise ConnectError(f"Verbindung fehlgeschlagen: {exc}") from exc

        session = Session(
            id=secrets.token_urlsafe(12),
            url=url,
            client=client,
            security="None" if policy in (None, "", "None") else f"{policy} / {mode}",
        )

        try:
            session.namespaces = await client.get_namespace_array()
        except Exception:  # noqa: BLE001
            session.namespaces = []
        try:
            server_node = client.get_node(ua.NodeId(ua.ObjectIds.Server_ServerStatus_BuildInfo_ProductName))
            session.server_name = str(await server_node.read_value())
        except Exception:  # noqa: BLE001
            session.server_name = ""
        try:
            await client.load_data_type_definitions()
        except Exception as exc:  # noqa: BLE001 - eigene Strukturen sind optional
            log.info("Datentyp-Definitionen nicht ladbar: %s", exc)

        self._sessions[session.id] = session
        log.info("Verbunden: %s (%s)", url, session.id)
        return session

    async def disconnect(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        try:
            await asyncio.wait_for(session.client.disconnect(), timeout=5)
        except Exception as exc:  # noqa: BLE001
            log.info("Trennen von %s nicht sauber beendet: %s", session_id, exc)
        log.info("Getrennt: %s", session_id)

    async def endpoints(self, url: str, timeout: float = 8.0) -> list[dict]:
        """Endpunkte abfragen, ohne eine Sitzung zu öffnen."""
        client = Client(url=url, timeout=timeout)
        client.application_uri = APP_URI
        try:
            raw = await asyncio.wait_for(client.connect_and_get_server_endpoints(), timeout=timeout + 5)
        except Exception as exc:  # noqa: BLE001
            raise ConnectError(f"Endpunkte nicht abrufbar: {exc}") from exc
        result = []
        for ep in raw:
            policy = str(ep.SecurityPolicyUri).rsplit("#", 1)[-1]
            tokens = sorted({str(t.TokenType.name) for t in (ep.UserIdentityTokens or [])})
            result.append(
                {
                    "endpointUrl": ep.EndpointUrl,
                    "policy": policy,
                    "mode": ep.SecurityMode.name,
                    "level": ep.SecurityLevel,
                    "tokens": tokens,
                }
            )
        return result

    # ------------------------------------------------------------------- Technik
    async def _ensure_certificate(self, client: Client) -> tuple[Path, Path]:
        CERT_DIR.mkdir(parents=True, exist_ok=True)
        cert_file = CERT_DIR / "client-cert.der"
        key_file = CERT_DIR / "client-key.pem"
        result = client.setup_self_signed_certificate(
            key_file=key_file,
            cert_file=cert_file,
            subject_attrs={"organizationName": "OPC UA Browser"},
        )
        if inspect.isawaitable(result):
            result = await result
        cert, key = result
        return Path(cert), Path(key)

    async def start(self) -> None:
        self._janitor = asyncio.create_task(self._reap_idle())

    async def stop(self) -> None:
        if self._janitor:
            self._janitor.cancel()
            self._janitor = None
        for session_id in list(self._sessions):
            await self.disconnect(session_id)

    async def _reap_idle(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                now = time.time()
                for session in list(self._sessions.values()):
                    if now - session.last_used > IDLE_TIMEOUT:
                        log.info("Sitzung %s war zu lange untätig", session.id)
                        await self.disconnect(session.id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Aufräumen fehlgeschlagen: %s", exc)


manager = SessionManager()
