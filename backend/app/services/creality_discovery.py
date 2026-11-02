"""Find Creality / Klipper printers on the local network.

Bambu printers announce themselves over SSDP; a Klipper printer does not. What it does have is a
Moonraker API on a known port that answers ``/printer/info`` with its host name and version, so a
subnet scan looks for that: connect to the port, then ask. Results are only ever *candidates* the
person picks from. Nothing is added automatically.
"""

import asyncio
import ipaddress
import logging
from dataclasses import asdict, dataclass

import httpx

from backend.app.services.moonraker_client import DEFAULT_MOONRAKER_PORT

logger = logging.getLogger(__name__)

MAX_HOSTS = 1024
BATCH_SIZE = 64


@dataclass
class FoundCrealityPrinter:
    ip_address: str
    port: int
    hostname: str | None
    firmware_version: str | None
    klipper_state: str | None


class CrealityScanner:
    """One scan at a time; progress and results are read by polling."""

    def __init__(self) -> None:
        self._running = False
        self._scanned = 0
        self._total = 0
        self._found: dict[str, FoundCrealityPrinter] = {}

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def progress(self) -> tuple[int, int]:
        return self._scanned, self._total

    @property
    def found(self) -> list[dict]:
        return [asdict(p) for p in sorted(self._found.values(), key=lambda p: ipaddress.ip_address(p.ip_address))]

    def stop(self) -> None:
        self._running = False

    async def scan(self, subnet: str, timeout: float = 1.0, port: int = DEFAULT_MOONRAKER_PORT) -> None:
        if self._running:
            return
        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            logger.warning("Creality scan: invalid subnet %r", subnet)
            return
        hosts = list(network.hosts())[:MAX_HOSTS]
        self._running, self._scanned, self._total = True, 0, len(hosts)
        self._found.clear()
        logger.info("Creality scan of %s (%d hosts) on port %d", subnet, len(hosts), port)
        try:
            async with httpx.AsyncClient(timeout=max(timeout, 1.0)) as http:
                for start in range(0, len(hosts), BATCH_SIZE):
                    if not self._running:
                        break
                    batch = hosts[start : start + BATCH_SIZE]
                    await asyncio.gather(*(self._probe(http, str(ip), timeout, port) for ip in batch))
                    self._scanned = min(start + BATCH_SIZE, len(hosts))
        finally:
            self._running = False
            logger.info("Creality scan finished: %d printer(s) found", len(self._found))

    async def _probe(self, http: httpx.AsyncClient, ip: str, timeout: float, port: int) -> None:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
        except (OSError, TimeoutError):
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        try:
            response = await http.get(f"http://{ip}:{port}/printer/info")
            result = response.json().get("result") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return
        if not isinstance(result, dict) or "software_version" not in result:
            return  # something listens on that port, but it is not Moonraker
        self._found[ip] = FoundCrealityPrinter(
            ip_address=ip,
            port=port,
            hostname=result.get("hostname"),
            firmware_version=result.get("software_version"),
            klipper_state=result.get("state"),
        )


creality_scanner = CrealityScanner()
