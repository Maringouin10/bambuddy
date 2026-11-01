"""A queued print aimed at a Creality (Moonraker) printer fails with a clear reason.

Bambuddy monitors and controls Creality printers but cannot yet push a sliced file
to them: the dispatch path is Bambu FTPS + MQTT ``project_file``. Reaching it would
end in an opaque "FTP connection refused" error, so ``_start_print`` stops first.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.app.models  # noqa: F401 - populate Base.metadata
import backend.app.services.print_scheduler as scheduler_module
from backend.app.core.database import Base
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.print_scheduler import PrintScheduler


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(session_maker, printer_type: str) -> int:
    async with session_maker() as db:
        printer = Printer(
            name="Ender",
            serial_number="CREALITY-192.168.1.50" if printer_type == "creality" else "SERIAL",
            ip_address="192.168.1.50",
            access_code="" if printer_type == "creality" else "access-code",
            model="Ender-3 V3 KE" if printer_type == "creality" else "A1MINI",
            printer_type=printer_type,
        )
        db.add(printer)
        await db.flush()
        archive = PrintArchive(
            printer_id=printer.id, filename="job.3mf", file_path="archives/job.3mf", file_size=1, status="completed"
        )
        db.add(archive)
        await db.flush()
        item = PrintQueueItem(printer_id=printer.id, archive_id=archive.id, status="pending")
        db.add(item)
        await db.commit()
        return item.id


@pytest.mark.asyncio
async def test_creality_target_fails_with_an_actionable_message_and_never_uploads(session_maker):
    item_id = await _seed(session_maker, "creality")
    scheduler = PrintScheduler()
    upload = AsyncMock(return_value=True)
    start_print = MagicMock(return_value=True)

    async with session_maker() as db:
        item = await db.get(PrintQueueItem, item_id)
        with (
            patch.object(scheduler_module, "upload_file_async", upload),
            patch.object(scheduler_module.printer_manager, "start_print", start_print),
            patch.object(scheduler, "_power_off_if_needed", AsyncMock()),
        ):
            await scheduler._start_print(db, item)

    async with session_maker() as db:
        final = await db.get(PrintQueueItem, item_id)

    assert final.status == "failed"
    assert "Creality" in final.error_message
    assert "Fluidd" in final.error_message
    assert final.completed_at is not None
    upload.assert_not_awaited()
    start_print.assert_not_called()
