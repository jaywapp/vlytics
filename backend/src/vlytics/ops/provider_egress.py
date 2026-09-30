"""Bounded single-provider HTTP CONNECT relay for private deployments."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal, cast

type EgressProvider = Literal["openai", "anthropic", "google"]

PROVIDER_HOSTS: dict[EgressProvider, str] = {
    "openai": "api.openai.com",
    "anthropic": "api.anthropic.com",
    "google": "generativelanguage.googleapis.com",
}
DEFAULT_PROVIDER: EgressProvider = "openai"
ALLOWED_HOST = PROVIDER_HOSTS[DEFAULT_PROVIDER]
ALLOWED_PORT = 443
DEFAULT_BIND = "127.0.0.1:8081"
MAX_HEADER_BYTES = 8 * 1024
HEADER_READ_TIMEOUT_SECONDS = 5.0
UPSTREAM_CONNECT_TIMEOUT_SECONDS = 10.0
TUNNEL_IDLE_TIMEOUT_SECONDS = 60.0
MAX_CONNECTIONS = 32
CLOSE_TIMEOUT_SECONDS = 1.0

_HEADER_END = b"\r\n\r\n"
_HEADER_NAME = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


@dataclass(frozen=True)
class RelayConfig:
    """Resource limits and bind address for the relay."""

    bind_host: str = "127.0.0.1"
    bind_port: int = 8081
    provider: EgressProvider = DEFAULT_PROVIDER
    max_header_bytes: int = MAX_HEADER_BYTES
    header_read_timeout_seconds: float = HEADER_READ_TIMEOUT_SECONDS
    upstream_connect_timeout_seconds: float = UPSTREAM_CONNECT_TIMEOUT_SECONDS
    tunnel_idle_timeout_seconds: float = TUNNEL_IDLE_TIMEOUT_SECONDS
    max_connections: int = MAX_CONNECTIONS

    def __post_init__(self) -> None:
        if self.provider not in PROVIDER_HOSTS:
            raise ValueError("VLYTICS_EGRESS_PROVIDER is invalid")
        if not self.bind_host.strip() or not 0 <= self.bind_port <= 65535:
            raise ValueError("egress bind address is invalid")
        if self.max_header_bytes < 256:
            raise ValueError("egress header limit is too small")
        if (
            self.header_read_timeout_seconds <= 0
            or self.upstream_connect_timeout_seconds <= 0
            or self.tunnel_idle_timeout_seconds <= 0
            or self.max_connections <= 0
        ):
            raise ValueError("egress resource limits must be positive")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> RelayConfig:
        values = os.environ if environ is None else environ
        host, port = _parse_bind(values.get("VLYTICS_EGRESS_BIND", DEFAULT_BIND))
        provider = values.get("VLYTICS_EGRESS_PROVIDER", DEFAULT_PROVIDER)
        if provider not in PROVIDER_HOSTS:
            raise ValueError("VLYTICS_EGRESS_PROVIDER is invalid")
        return cls(bind_host=host, bind_port=port, provider=cast(EgressProvider, provider))


@dataclass(frozen=True)
class ResolvedEndpoint:
    """A DNS result that has passed the public-address policy."""

    host: str
    port: int
    family: int
    protocol: int


class RelayRequestError(Exception):
    """A sanitized response that may be returned to the relay client."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


Resolver = Callable[[str, int], Awaitable[tuple[ResolvedEndpoint, ...]]]
Connector = Callable[
    [ResolvedEndpoint], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
]


def _parse_bind(value: str) -> tuple[str, int]:
    if value.startswith("["):
        closing = value.find("]")
        if closing < 1 or value[closing + 1 : closing + 2] != ":":
            raise ValueError("VLYTICS_EGRESS_BIND must be host:port")
        host = value[1:closing]
        port_text = value[closing + 2 :]
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator:
            raise ValueError("VLYTICS_EGRESS_BIND must be host:port")
    try:
        port = int(port_text)
    except ValueError as error:
        raise ValueError("VLYTICS_EGRESS_BIND port must be an integer") from error
    if not host.strip() or not 1 <= port <= 65535:
        raise ValueError("VLYTICS_EGRESS_BIND is invalid")
    return host, port


def _address_is_public(value: str) -> bool:
    address = ipaddress.ip_address(value.split("%", maxsplit=1)[0])
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_unspecified
    )


async def resolve_public_endpoints(host: str, port: int) -> tuple[ResolvedEndpoint, ...]:
    """Resolve once and reject the complete answer if any address is not public."""

    loop = asyncio.get_running_loop()
    try:
        answers = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as error:
        raise RelayRequestError(502, "Bad Gateway") from error
    if not answers:
        raise RelayRequestError(502, "Bad Gateway")

    endpoints: list[ResolvedEndpoint] = []
    seen: set[tuple[str, int, int, int]] = set()
    for family, _socket_type, protocol, _canonical_name, socket_address in answers:
        address = str(socket_address[0])
        try:
            public = _address_is_public(address)
        except ValueError as error:
            raise RelayRequestError(502, "Bad Gateway") from error
        if not public:
            raise RelayRequestError(502, "Bad Gateway")
        endpoint = ResolvedEndpoint(address, port, family, protocol)
        identity = (endpoint.host, endpoint.port, endpoint.family, endpoint.protocol)
        if identity not in seen:
            seen.add(identity)
            endpoints.append(endpoint)
    if not endpoints:
        raise RelayRequestError(502, "Bad Gateway")
    return tuple(endpoints)


async def open_resolved_endpoint(
    endpoint: ResolvedEndpoint,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the validated IP without performing another DNS lookup."""

    return await asyncio.open_connection(
        host=endpoint.host,
        port=endpoint.port,
        family=endpoint.family,
        proto=endpoint.protocol,
        flags=socket.AI_NUMERICHOST,
    )


def _validate_connect_request(header: bytes, allowed_host: str) -> None:
    lines = header[: -len(_HEADER_END)].split(b"\r\n")
    if not lines or not lines[0]:
        raise RelayRequestError(400, "Bad Request")
    try:
        method, target, version = lines[0].decode("ascii").split(" ")
    except (UnicodeDecodeError, ValueError) as error:
        raise RelayRequestError(400, "Bad Request") from error
    if method != "CONNECT":
        raise RelayRequestError(405, "Method Not Allowed")
    if version != "HTTP/1.1":
        raise RelayRequestError(400, "Bad Request")

    host, authority_separator, port = target.rpartition(":")
    if (
        not authority_separator
        or host.casefold() != allowed_host
        or port != str(ALLOWED_PORT)
        or "@" in host
    ):
        raise RelayRequestError(403, "Forbidden")

    for line in lines[1:]:
        if not line or line[:1] in {b" ", b"\t"}:
            raise RelayRequestError(400, "Bad Request")
        name, header_separator, _value = line.partition(b":")
        if not header_separator or _HEADER_NAME.fullmatch(name) is None:
            raise RelayRequestError(400, "Bad Request")


async def _read_connect_header(
    reader: asyncio.StreamReader,
    *,
    max_bytes: int,
    timeout: float,
) -> bytes:
    try:
        header = await asyncio.wait_for(reader.readuntil(_HEADER_END), timeout=timeout)
    except TimeoutError as error:
        raise RelayRequestError(408, "Request Timeout") from error
    except asyncio.LimitOverrunError as error:
        raise RelayRequestError(431, "Request Header Fields Too Large") from error
    except asyncio.IncompleteReadError as error:
        raise RelayRequestError(400, "Bad Request") from error
    if len(header) > max_bytes:
        raise RelayRequestError(431, "Request Header Fields Too Large")
    return header


async def _connect_first(
    endpoints: Sequence[ResolvedEndpoint],
    connector: Connector,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    for endpoint in endpoints:
        try:
            return await connector(endpoint)
        except OSError:
            continue
    raise RelayRequestError(502, "Bad Gateway")


async def _resolve_and_connect(
    resolver: Resolver,
    connector: Connector,
    allowed_host: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    endpoints = await resolver(allowed_host, ALLOWED_PORT)
    return await _connect_first(endpoints, connector)


async def _relay_tunnel(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    *,
    idle_timeout: float,
) -> None:
    loop = asyncio.get_running_loop()
    last_activity = loop.time()

    async def copy(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
        nonlocal last_activity
        while chunk := await source.read(64 * 1024):
            last_activity = loop.time()
            destination.write(chunk)
            await destination.drain()

    async def reject_idle() -> None:
        while True:
            remaining = idle_timeout - (loop.time() - last_activity)
            if remaining <= 0:
                raise TimeoutError
            await asyncio.sleep(remaining)

    tasks = {
        asyncio.create_task(copy(client_reader, upstream_writer)),
        asyncio.create_task(copy(upstream_reader, client_writer)),
        asyncio.create_task(reject_idle()),
    }
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            with suppress(ConnectionError, OSError, TimeoutError):
                task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _send_response(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    if status == 200:
        response = f"HTTP/1.1 {status} {reason}\r\n\r\n".encode("ascii")
    else:
        response = (
            f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
        ).encode("ascii")
    writer.write(response)
    await writer.drain()


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    with suppress(ConnectionError, OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=CLOSE_TIMEOUT_SECONDS)


class ProviderEgressRelay:
    """Allow TLS tunnels to exactly one configured provider API hostname."""

    def __init__(
        self,
        config: RelayConfig | None = None,
        *,
        resolver: Resolver = resolve_public_endpoints,
        connector: Connector = open_resolved_endpoint,
    ) -> None:
        self.config = config or RelayConfig()
        self._resolver = resolver
        self._connector = connector
        self._active_connections = 0

    @property
    def allowed_host(self) -> str:
        return PROVIDER_HOSTS[self.config.provider]

    async def start(self) -> asyncio.Server:
        return await asyncio.start_server(
            self.handle_client,
            self.config.bind_host,
            self.config.bind_port,
            limit=self.config.max_header_bytes + 1,
        )

    async def handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        if self._active_connections >= self.config.max_connections:
            try:
                await _send_response(client_writer, 503, "Service Unavailable")
            finally:
                await _close_writer(client_writer)
            return

        self._active_connections += 1
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            header = await _read_connect_header(
                client_reader,
                max_bytes=self.config.max_header_bytes,
                timeout=self.config.header_read_timeout_seconds,
            )
            _validate_connect_request(header, self.allowed_host)
            upstream_reader, connected_writer = await asyncio.wait_for(
                _resolve_and_connect(self._resolver, self._connector, self.allowed_host),
                timeout=self.config.upstream_connect_timeout_seconds,
            )
            upstream_writer = connected_writer
            await _send_response(client_writer, 200, "Connection Established")
            await _relay_tunnel(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
                idle_timeout=self.config.tunnel_idle_timeout_seconds,
            )
        except RelayRequestError as error:
            with suppress(ConnectionError, OSError):
                await _send_response(client_writer, error.status, error.reason)
        except TimeoutError:
            with suppress(ConnectionError, OSError):
                await _send_response(client_writer, 504, "Gateway Timeout")
        except (ConnectionError, OSError):
            with suppress(ConnectionError, OSError):
                await _send_response(client_writer, 502, "Bad Gateway")
        finally:
            await _close_writer(upstream_writer)
            await _close_writer(client_writer)
            self._active_connections -= 1


async def serve(config: RelayConfig | None = None) -> None:
    relay = ProviderEgressRelay(config)
    server = await relay.start()
    async with server:
        await server.serve_forever()


def main() -> int:
    try:
        asyncio.run(serve(RelayConfig.from_environment()))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
