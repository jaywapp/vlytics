"""Private operator authentication and opaque cursor signing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Authenticator:
    """Authenticate opaque deployment secrets without putting them in app state or logs."""

    operator_secret: SecretStr | None
    readonly_secret: SecretStr | None = None

    def require_operator(
        self,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> Literal["operator"]:
        if self.operator_secret is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "operator_auth_unconfigured"},
            )
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "authentication_required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        candidate = credentials.credentials.encode("utf-8")
        operator = self.operator_secret.get_secret_value().encode("utf-8")
        if hmac.compare_digest(candidate, operator):
            return "operator"
        if self.readonly_secret is not None and hmac.compare_digest(
            candidate, self.readonly_secret.get_secret_value().encode("utf-8")
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "operator_role_required"},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_credentials"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    def cursor_key(self) -> bytes:
        if self.operator_secret is None:
            return b"unconfigured-operator-api"
        return hashlib.sha256(
            b"vlytics-cursor-v1\0" + self.operator_secret.get_secret_value().encode("utf-8")
        ).digest()


class CursorCodec:
    """Sign filter-bound keyset cursors so clients cannot forge pagination state."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def encode(self, payload: dict[str, object]) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self._key, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + signature).decode("ascii").rstrip("=")

    def decode(self, value: str) -> dict[str, Any]:
        try:
            padded = value + "=" * (-len(value) % 4)
            encoded = base64.urlsafe_b64decode(padded.encode("ascii"))
            canonical = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
            if not hmac.compare_digest(canonical, value):
                raise ValueError
            if len(encoded) <= hashlib.sha256().digest_size:
                raise ValueError
            body = encoded[: -hashlib.sha256().digest_size]
            signature = encoded[-hashlib.sha256().digest_size :]
            expected = hmac.new(self._key, body, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "invalid_cursor", "message": "cursor is malformed or expired"},
            ) from error
