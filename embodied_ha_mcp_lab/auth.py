"""Ingress-peer and direct bearer-token authentication."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Actor:
    name: str


class Authenticator:
    def __init__(self, token_path: Path, ingress_sources: set[str] | None = None) -> None:
        self.token_path = token_path.resolve()
        self.ingress_sources = ingress_sources or {"172.30.32.2"}
        self._token = self._load_or_create_token()
        self._token_identifier = hashlib.sha256(self._token.encode()).hexdigest()[:12]

    def authorize(self, peer_ip: str, authorization: str | None) -> Actor | None:
        # Authentication trusts the Supervisor ingress proxy's network peer, not
        # a client-controlled header. X-Ingress-Path is routing metadata only.
        if peer_ip in self.ingress_sources:
            return Actor("human_ingress")
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            return None
        supplied = authorization[len(prefix) :]
        if not hmac.compare_digest(supplied, self._token):
            return None
        return Actor(f"token:{self._token_identifier}")

    def _load_or_create_token(self) -> str:
        self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            value = self.token_path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            value = ""
        if value:
            if len(value) < 48:
                raise RuntimeError("existing MCP Lab API token is invalid")
            os.chmod(self.token_path, 0o600)
            return value

        value = secrets.token_urlsafe(48)
        temporary = self.token_path.with_name(
            f".{self.token_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(value + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.token_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return value
