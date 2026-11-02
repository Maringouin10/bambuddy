"""Finding Creality / Klipper printers on the network: a Moonraker API answering on its port."""

import asyncio
import json

import pytest

from backend.app.services.creality_discovery import CrealityScanner


async def _serve(body: bytes, status: int = 200):
    """A one-shot-per-connection HTTP server on an ephemeral localhost port."""

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


MOONRAKER = json.dumps(
    {"result": {"state": "ready", "hostname": "ender3v3", "software_version": "v0.11.0-creality"}}
).encode()


@pytest.mark.asyncio
async def test_a_moonraker_printer_is_found_with_its_hostname_and_version():
    server, port = await _serve(MOONRAKER)
    async with server:
        scanner = CrealityScanner()
        await scanner.scan("127.0.0.1/32", timeout=1.0, port=port)

    assert scanner.is_running is False
    assert scanner.found == [
        {
            "ip_address": "127.0.0.1",
            "port": port,
            "hostname": "ender3v3",
            "firmware_version": "v0.11.0-creality",
            "klipper_state": "ready",
        }
    ]
    assert scanner.progress == (1, 1)


@pytest.mark.asyncio
async def test_something_else_on_the_port_is_not_a_printer():
    server, port = await _serve(b'{"hello": "router"}')
    async with server:
        scanner = CrealityScanner()
        await scanner.scan("127.0.0.1/32", timeout=1.0, port=port)
    assert scanner.found == []


@pytest.mark.asyncio
async def test_a_non_json_answer_is_not_a_printer():
    server, port = await _serve(b"<html>admin</html>")
    async with server:
        scanner = CrealityScanner()
        await scanner.scan("127.0.0.1/32", timeout=1.0, port=port)
    assert scanner.found == []


@pytest.mark.asyncio
async def test_a_closed_port_finds_nothing_and_does_not_hang():
    server, port = await _serve(MOONRAKER)
    server.close()
    await server.wait_closed()
    scanner = CrealityScanner()
    await asyncio.wait_for(scanner.scan("127.0.0.1/32", timeout=0.5, port=port), timeout=5)
    assert scanner.found == []


@pytest.mark.asyncio
async def test_a_bad_subnet_is_ignored_cleanly():
    scanner = CrealityScanner()
    await scanner.scan("not-a-subnet")
    assert scanner.is_running is False and scanner.found == [] and scanner.progress == (0, 0)


@pytest.mark.asyncio
async def test_a_huge_range_is_capped():
    scanner = CrealityScanner()
    scanner.stop()  # stop is a no-op before a scan
    # a /16 has 65k hosts; only the first 1024 are ever probed (and none answer on this port)
    task = asyncio.create_task(scanner.scan("10.255.0.0/16", timeout=0.01, port=9))
    await asyncio.sleep(0.05)
    assert scanner.progress[1] == 1024
    scanner.stop()
    await asyncio.wait_for(task, timeout=10)


@pytest.mark.asyncio
async def test_results_are_sorted_by_address():
    scanner = CrealityScanner()
    from backend.app.services.creality_discovery import FoundCrealityPrinter

    for ip in ("192.168.1.20", "192.168.1.3", "192.168.1.100"):
        scanner._found[ip] = FoundCrealityPrinter(ip, 7125, None, "v1", "ready")
    assert [p["ip_address"] for p in scanner.found] == ["192.168.1.3", "192.168.1.20", "192.168.1.100"]
