from __future__ import annotations

import json
import math
import random
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


USER_AGENT = "arxivmail/0.1 (personal research digest; contact: aniketdeshh@gmail.com)"


@dataclass(slots=True)
class HttpError(RuntimeError):
    status: int
    message: str

    def __str__(self) -> str:
        return f"HTTP {self.status}: {self.message}"


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 45,
    retries: int = 3,
) -> tuple[bytes, dict[str, str]]:
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=tls_context()) as response:
                response_body = response.read()
                response_headers = dict(response.headers.items())
                backoff = response_headers.get("Backoff")
                if backoff:
                    _honor_server_delay(backoff, header="Backoff")
                return response_body, response_headers
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == retries:
                detail = _safe_error_detail(exc.read())
                raise HttpError(exc.code, detail) from exc
            retry_after = exc.headers.get("Retry-After")
            delay = _server_delay(retry_after, header="Retry-After") if retry_after else 2**attempt
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise RuntimeError(f"Network request failed for {url.split('?')[0]}: {exc.reason}") from exc
            delay = 2**attempt
        time.sleep(delay + random.random() * 0.25)
    raise AssertionError("unreachable")


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: Any | None = None,
    timeout: float = 60,
    retries: int = 3,
) -> tuple[Any, dict[str, str]]:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    raw, response_headers = request_bytes(
        url,
        method=method,
        headers=request_headers,
        body=body,
        timeout=timeout,
        retries=retries,
    )
    return json.loads(raw), response_headers


def _safe_error_detail(raw: bytes) -> str:
    try:
        parsed = json.loads(raw)
        message = parsed.get("error", {}).get("message") or parsed.get("message")
        if message:
            return str(message)[:500]
    except (ValueError, AttributeError):
        pass
    return "remote service rejected the request"


def _server_delay(value: str, *, header: str) -> float:
    try:
        delay = float(value)
    except ValueError as exc:
        raise RuntimeError(f"Remote service returned an invalid {header} value") from exc
    if not math.isfinite(delay) or delay < 0:
        raise RuntimeError(f"Remote service returned an invalid {header} value")
    if delay > 300:
        raise RuntimeError(f"Remote service requested a {delay:g}s {header}; retry the run later")
    return delay


def _honor_server_delay(value: str, *, header: str) -> None:
    time.sleep(_server_delay(value, header=header))


def tls_context() -> ssl.SSLContext:
    defaults = ssl.get_default_verify_paths()
    if defaults.cafile:
        return ssl.create_default_context()
    system_bundle = Path("/etc/ssl/cert.pem")
    if system_bundle.exists():
        return ssl.create_default_context(cafile=str(system_bundle))
    return ssl.create_default_context()
