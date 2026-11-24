"""Paper-only authenticated HTTP; submission uncertainty is never retried."""
from dataclasses import dataclass, field
from http.client import HTTPException
import json
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

PAPER_URL = "https://paper-api.alpaca.markets"


class AlpacaError(RuntimeError):
    def __init__(self, status, code=None, message=None):
        self.status, self.code, self.message = status, code, message
        super().__init__(f"Alpaca HTTP {status}, code={code}" + (f": {message}" if message else ""))


class AuthenticationError(AlpacaError):
    pass


class PermissionDenied(AlpacaError):
    pass


class RateLimited(AlpacaError):
    pass


class ServiceError(AlpacaError):
    pass


class OutcomeUnknown(TimeoutError):
    pass


@dataclass(frozen=True)
class AlpacaConfig:
    key_id: str = field(repr=False)
    secret_key: str = field(repr=False)
    base_url: str = PAPER_URL
    timeout: float = 15

    def __post_init__(self):
        if self.base_url != PAPER_URL:
            raise ValueError("only the Alpaca Paper API address is allowed")
        if not self.key_id or not self.secret_key:
            raise ValueError("Alpaca credentials required")
        if not 0 < self.timeout <= 60:
            raise ValueError("timeout must be between 0 and 60 seconds")

    @classmethod
    def from_env(cls):
        return cls(os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"],
                   os.environ.get("APCA_API_BASE_URL", PAPER_URL))

    @classmethod
    def from_file(cls, path):
        """Read a user-provisioned private JSON file; callers own its OS permissions."""
        with open(path, encoding="utf-8-sig") as stream:
            values = json.load(stream)
        return cls(values["APCA_API_KEY_ID"], values["APCA_API_SECRET_KEY"],
                   values.get("APCA_API_BASE_URL", PAPER_URL))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PaperHTTP:
    def __init__(self, config, *, transport=None):
        self.config = config
        self.transport = transport or self._send
        self.opener = build_opener(_NoRedirect())

    def _send(self, method, path, params, body):
        url = self.config.base_url + path
        if params:
            url += "?" + urlencode(params)
        request = Request(url, data=None if body is None else json.dumps(body).encode(), method=method,
                          headers={"APCA-API-KEY-ID": self.config.key_id,
                                   "APCA-API-SECRET-KEY": self.config.secret_key,
                                   "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except HTTPError as error:
            try:
                payload = json.loads(error.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = None
            finally:
                error.close()
            return error.code, payload

    def request(self, method, path, *, params=None, body=None):
        if not path.startswith("/v2/") or "?" in path or "#" in path or ".." in path:
            raise ValueError("expected an Alpaca v2 relative path")
        try:
            status, payload = self.transport(method, path, params, body)
        except (URLError, OSError, socket.timeout, TimeoutError, HTTPException, json.JSONDecodeError):
            if method in {"POST", "DELETE"}:
                raise OutcomeUnknown("Paper request outcome requires reconciliation") from None
            raise ConnectionError("Alpaca Paper read failed") from None
        if 200 <= status < 300:
            return payload
        cls = {401: AuthenticationError, 403: PermissionDenied, 429: RateLimited}.get(status, AlpacaError)
        if status >= 500:
            if method in {"POST", "DELETE"}:
                raise OutcomeUnknown("Paper service error requires reconciliation")
            cls = ServiceError
        code = payload.get("code") if isinstance(payload, dict) else None
        message = payload.get("message") if isinstance(payload, dict) else None
        if isinstance(message, str):
            message = message.replace(self.config.key_id, "[redacted]").replace(self.config.secret_key, "[redacted]")
            message = " ".join(message.split())[:512]
        else:
            message = None
        raise cls(status, code, message)
