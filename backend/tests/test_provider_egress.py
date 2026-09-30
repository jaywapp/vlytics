from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import pytest

from vlytics.engine.providers.transports import (
    AnthropicProviderTransport,
    GoogleProviderTransport,
    OpenAIProviderTransport,
)
from vlytics.ops.provider_egress import (
    ALLOWED_HOST,
    ALLOWED_PORT,
    PROVIDER_HOSTS,
    EgressProvider,
    ProviderEgressRelay,
    RelayConfig,
    RelayRequestError,
    ResolvedEndpoint,
    resolve_public_endpoints,
)


@asynccontextmanager
async def _running_server(server: asyncio.Server) -> AsyncIterator[asyncio.Server]:
    try:
        yield server
    finally:
        server.close()
        await server.wait_closed()


def _server_port(server: asyncio.Server) -> int:
    sockets = server.sockets
    assert sockets
    return int(sockets[0].getsockname()[1])


async def _open_relay(server: asyncio.Server) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", _server_port(server))


async def _read_response(reader: asyncio.StreamReader) -> bytes:
    return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1)


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(ConnectionError, OSError):
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_allowed_connect_relays_opaque_bytes_without_logging(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while chunk := await reader.read(64 * 1024):
                writer.write(chunk)
                await writer.drain()
        finally:
            await _close(writer)

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    async with _running_server(upstream):
        upstream_port = _server_port(upstream)

        async def resolve(host: str, port: int) -> tuple[ResolvedEndpoint, ...]:
            assert (host, port) == (ALLOWED_HOST, ALLOWED_PORT)
            return (
                ResolvedEndpoint(
                    "127.0.0.1",
                    upstream_port,
                    socket.AF_INET,
                    socket.IPPROTO_TCP,
                ),
            )

        relay = ProviderEgressRelay(
            RelayConfig(bind_host="127.0.0.1", bind_port=0),
            resolver=resolve,
        )
        server = await relay.start()
        async with _running_server(server):
            reader, writer = await _open_relay(server)
            writer.write(b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com:443\r\n\r\n")
            await writer.drain()
            response = await _read_response(reader)
            assert response.startswith(b"HTTP/1.1 200 Connection Established\r\n")

            opaque_tls = b"\x16\x03\x03private-request-body"
            writer.write(opaque_tls)
            await writer.drain()
            relayed = await asyncio.wait_for(reader.readexactly(len(opaque_tls)), timeout=1)
            assert relayed == opaque_tls
            await _close(writer)

    captured = capsys.readouterr()
    assert "private-request-body" not in captured.out
    assert "private-request-body" not in captured.err


@pytest.mark.parametrize(
    ("raw_request", "status"),
    [
        (b"GET api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com\r\n\r\n", 405),
        (b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n", 403),
        (b"CONNECT api.openai.com:80 HTTP/1.1\r\nHost: api.openai.com\r\n\r\n", 403),
        (b"CONNECT 8.8.8.8:443 HTTP/1.1\r\nHost: 8.8.8.8\r\n\r\n", 403),
    ],
)
@pytest.mark.asyncio
async def test_rejects_non_allowlisted_requests_before_dns(
    raw_request: bytes,
    status: int,
) -> None:
    async def fail_resolve(_host: str, _port: int) -> tuple[ResolvedEndpoint, ...]:
        pytest.fail("a rejected request must not resolve or connect")

    relay = ProviderEgressRelay(
        RelayConfig(bind_host="127.0.0.1", bind_port=0),
        resolver=fail_resolve,
    )
    server = await relay.start()
    async with _running_server(server):
        reader, writer = await _open_relay(server)
        writer.write(raw_request)
        await writer.drain()
        assert (await _read_response(reader)).startswith(f"HTTP/1.1 {status} ".encode())
        assert await asyncio.wait_for(reader.read(), timeout=1) == b""
        await _close(writer)


@pytest.mark.parametrize(
    ("provider", "own_host", "other_host"),
    [
        ("openai", "api.openai.com", "api.anthropic.com"),
        ("anthropic", "api.anthropic.com", "generativelanguage.googleapis.com"),
        ("google", "generativelanguage.googleapis.com", "api.openai.com"),
    ],
)
@pytest.mark.asyncio
async def test_each_relay_instance_allows_only_its_selected_provider(
    provider: EgressProvider,
    own_host: str,
    other_host: str,
) -> None:
    resolved: list[tuple[str, int]] = []

    async def record_resolve(host: str, port: int) -> tuple[ResolvedEndpoint, ...]:
        resolved.append((host, port))
        raise RelayRequestError(502, "Bad Gateway")

    config = RelayConfig(bind_host="127.0.0.1", bind_port=0, provider=provider)
    relay = ProviderEgressRelay(config, resolver=record_resolve)
    server = await relay.start()
    async with _running_server(server):
        own_reader, own_writer = await _open_relay(server)
        own_writer.write(
            f"CONNECT {own_host}:443 HTTP/1.1\r\nHost: {own_host}:443\r\n\r\n".encode()
        )
        await own_writer.drain()
        assert (await _read_response(own_reader)).startswith(b"HTTP/1.1 502 ")
        await _close(own_writer)
        assert resolved == [(own_host, ALLOWED_PORT)]

        other_reader, other_writer = await _open_relay(server)
        other_writer.write(
            f"CONNECT {other_host}:443 HTTP/1.1\r\nHost: {other_host}:443\r\n\r\n".encode()
        )
        await other_writer.drain()
        assert (await _read_response(other_reader)).startswith(b"HTTP/1.1 403 ")
        await _close(other_writer)
        assert resolved == [(own_host, ALLOWED_PORT)]


@pytest.mark.asyncio
async def test_header_size_and_read_time_are_bounded() -> None:
    relay = ProviderEgressRelay(
        RelayConfig(
            bind_host="127.0.0.1",
            bind_port=0,
            max_header_bytes=256,
            header_read_timeout_seconds=0.05,
        )
    )
    server = await relay.start()
    async with _running_server(server):
        oversized_reader, oversized_writer = await _open_relay(server)
        oversized_writer.write(b"CONNECT api.openai.com:443 HTTP/1.1\r\nX-Test: " + b"x" * 256)
        await oversized_writer.drain()
        assert (await _read_response(oversized_reader)).startswith(b"HTTP/1.1 431 ")
        await _close(oversized_writer)

        timeout_reader, timeout_writer = await _open_relay(server)
        assert (await _read_response(timeout_reader)).startswith(b"HTTP/1.1 408 ")
        await _close(timeout_writer)


@pytest.mark.asyncio
async def test_dns_timeout_returns_504_and_releases_connection_slot() -> None:
    resolver_started = asyncio.Event()

    async def stalled_resolver(_host: str, _port: int) -> tuple[ResolvedEndpoint, ...]:
        resolver_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    relay = ProviderEgressRelay(
        RelayConfig(
            bind_host="127.0.0.1",
            bind_port=0,
            max_connections=1,
            upstream_connect_timeout_seconds=0.05,
        ),
        resolver=stalled_resolver,
    )
    server = await relay.start()
    async with _running_server(server):
        first_reader, first_writer = await _open_relay(server)
        first_writer.write(b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: relay\r\n\r\n")
        await first_writer.drain()
        await asyncio.wait_for(resolver_started.wait(), timeout=1)
        assert (await _read_response(first_reader)).startswith(b"HTTP/1.1 504 ")
        assert await asyncio.wait_for(first_reader.read(), timeout=1) == b""
        await _close(first_writer)

        second_reader, second_writer = await _open_relay(server)
        second_writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
        await second_writer.drain()
        assert (await _read_response(second_reader)).startswith(b"HTTP/1.1 403 ")
        await _close(second_writer)


@pytest.mark.asyncio
async def test_connection_count_and_tunnel_idle_time_are_bounded() -> None:
    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read()
        finally:
            await _close(writer)

    upstream = await asyncio.start_server(hold, "127.0.0.1", 0)
    async with _running_server(upstream):
        endpoint = ResolvedEndpoint(
            "127.0.0.1",
            _server_port(upstream),
            socket.AF_INET,
            socket.IPPROTO_TCP,
        )

        async def resolve(_host: str, _port: int) -> tuple[ResolvedEndpoint, ...]:
            return (endpoint,)

        relay = ProviderEgressRelay(
            RelayConfig(
                bind_host="127.0.0.1",
                bind_port=0,
                max_connections=1,
                tunnel_idle_timeout_seconds=0.1,
            ),
            resolver=resolve,
        )
        server = await relay.start()
        async with _running_server(server):
            first_reader, first_writer = await _open_relay(server)
            first_writer.write(b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: relay\r\n\r\n")
            await first_writer.drain()
            assert (await _read_response(first_reader)).startswith(b"HTTP/1.1 200 ")

            second_reader, second_writer = await _open_relay(server)
            assert (await _read_response(second_reader)).startswith(b"HTTP/1.1 503 ")
            await _close(second_writer)

            assert await asyncio.wait_for(first_reader.read(), timeout=1) == b""
            await _close(first_writer)


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.1",
        "127.0.0.1",
        "169.254.10.20",
        "192.0.2.1",
        "224.0.0.1",
        "::1",
        "fe80::1",
    ],
)
@pytest.mark.asyncio
async def test_dns_rejects_non_public_addresses(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    socket_address: tuple[object, ...]
    if family == socket.AF_INET6:
        socket_address = (address, ALLOWED_PORT, 0, 0)
    else:
        socket_address = (address, ALLOWED_PORT)

    async def getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", socket_address)]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    with pytest.raises(RelayRequestError, match="Bad Gateway"):
        await resolve_public_endpoints(ALLOWED_HOST, ALLOWED_PORT)


@pytest.mark.asyncio
async def test_dns_rejects_mixed_public_and_private_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", ALLOWED_PORT),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.1", ALLOWED_PORT),
            ),
        ]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    with pytest.raises(RelayRequestError, match="Bad Gateway"):
        await resolve_public_endpoints(ALLOWED_HOST, ALLOWED_PORT)


@pytest.mark.asyncio
async def test_dns_accepts_only_public_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", ALLOWED_PORT),
            ),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2606:4700:4700::1111", ALLOWED_PORT, 0, 0),
            ),
        ]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    endpoints = await resolve_public_endpoints(ALLOWED_HOST, ALLOWED_PORT)

    assert [(endpoint.host, endpoint.family) for endpoint in endpoints] == [
        ("8.8.8.8", socket.AF_INET),
        ("2606:4700:4700::1111", socket.AF_INET6),
    ]


def test_provider_hosts_match_pinned_transports() -> None:
    expected = {
        "openai": OpenAIProviderTransport.allowed_host,
        "anthropic": AnthropicProviderTransport.allowed_host,
        "google": GoogleProviderTransport.allowed_host,
    }
    assert expected == PROVIDER_HOSTS


@pytest.mark.parametrize("provider", ["", "OPENAI", "unknown"])
def test_environment_rejects_invalid_provider(provider: str) -> None:
    with pytest.raises(ValueError, match="VLYTICS_EGRESS_PROVIDER"):
        RelayConfig.from_environment({"VLYTICS_EGRESS_PROVIDER": provider})


def test_environment_defaults_to_openai_and_loopback_until_sidecar_is_explicit() -> None:
    default = RelayConfig.from_environment({})
    sidecar = RelayConfig.from_environment(
        {
            "VLYTICS_EGRESS_BIND": "0.0.0.0:8081",
            "VLYTICS_EGRESS_PROVIDER": "anthropic",
        }
    )

    assert (default.bind_host, default.bind_port, default.provider) == (
        "127.0.0.1",
        8081,
        "openai",
    )
    assert (sidecar.bind_host, sidecar.bind_port, sidecar.provider) == (
        "0.0.0.0",
        8081,
        "anthropic",
    )
