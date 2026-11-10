"""Integration tests: adding Anycubic (LAN Mode) printers."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.printer import Printer

PROBE = "backend.app.services.printer_manager.printer_manager.test_connection"
CONNECT = "backend.app.services.printer_manager.printer_manager.connect_printer"

OK = {"success": True, "state": None, "model": "Kobra 3 Max", "reason": None, "serial": "AC1", "supported": True}


@pytest.fixture(autouse=True)
def _no_real_connection():
    with patch(CONNECT, new=AsyncMock(return_value=True)):
        yield


class TestAnycubicPrintersAPI:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_needs_only_name_and_ip(self, async_client: AsyncClient, db_session):
        with patch(PROBE, new=AsyncMock(return_value=OK)) as probe:
            response = await async_client.post(
                "/api/v1/printers/",
                json={"name": "Kobra", "ip_address": "192.168.1.60", "printer_type": "anycubic"},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["printer_type"] == "anycubic"
        assert body["serial_number"] == "ANYCUBIC-192.168.1.60"
        assert body["model"] == "Kobra 3 Max"  # filled from the handshake
        assert probe.await_args.kwargs["printer_type"] == "anycubic"
        row = (await db_session.execute(select(Printer).where(Printer.id == body["id"]))).scalar_one()
        assert row.access_code == ""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_lan_mode_off_is_explained(self, async_client: AsyncClient):
        failed = {"success": False, "state": None, "model": None, "reason": "lan_mode_off"}
        with patch(PROBE, new=AsyncMock(return_value=failed)):
            response = await async_client.post(
                "/api/v1/printers/",
                json={"name": "Kobra", "ip_address": "192.168.1.61", "printer_type": "anycubic"},
            )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["reason"] == "lan_mode_off" and "LAN Mode" in detail["message"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_other_anycubic_models_are_refused(self, async_client: AsyncClient):
        other = {**OK, "model": "Kobra S1", "supported": False}
        with patch(PROBE, new=AsyncMock(return_value=other)):
            response = await async_client.post(
                "/api/v1/printers/",
                json={"name": "S1", "ip_address": "192.168.1.62", "printer_type": "anycubic"},
            )
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "unsupported_model"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unreachable(self, async_client: AsyncClient):
        failed = {"success": False, "state": None, "model": None, "reason": "unreachable"}
        with patch(PROBE, new=AsyncMock(return_value=failed)):
            response = await async_client.post(
                "/api/v1/printers/",
                json={"name": "Kobra", "ip_address": "192.168.1.63", "printer_type": "anycubic"},
            )
        assert response.status_code == 400
        assert "18910" in response.json()["detail"]["message"]
