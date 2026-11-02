"""Fill in the archive of a print running on a Creality / Klipper printer.

A Bambu print is archived from its 3MF, which carries the thumbnail, filament, layers and the file
itself. A Klipper printer reports only a file name, so the archive the print-start callback creates
is a name-and-timing stub. Moonraker does know a lot about the file though: it parses the slicer's
comments into metadata (estimated time, filament weight and type, layer count, thumbnails ...), and
the G-code itself can be fetched. This module copies that into the archive so it looks and behaves
like any other: a cover picture, a filament total, a cost, and a G-code that can be printed again.

Best effort throughout: a printer that cannot be reached, or has no metadata for the file, leaves
the stub exactly as it was.
"""

import asyncio
import hashlib
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.core.database import async_session
from backend.app.models.archive import PrintArchive
from backend.app.models.filament import Filament
from backend.app.services.moonraker_client import MoonrakerClient
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

MAX_GCODE_BYTES = 300 * 1024 * 1024


def _largest_thumbnail(metadata: dict) -> str | None:
    """The ``relative_path`` of the biggest thumbnail Moonraker lists, if any."""
    best, best_area = None, -1
    for thumb in metadata.get("thumbnails") or []:
        if not isinstance(thumb, dict) or not thumb.get("relative_path"):
            continue
        area = int(thumb.get("width") or 0) * int(thumb.get("height") or 0)
        if area > best_area:
            best, best_area = thumb["relative_path"], area
    return best


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def archive_fields_from_metadata(metadata: dict) -> dict:
    """The archive columns Moonraker's file metadata can answer, keeping only what it really knows."""
    fields: dict = {}
    if (seconds := _number(metadata.get("estimated_time"))) is not None:
        fields["print_time_seconds"] = int(seconds)
    if (grams := _number(metadata.get("filament_weight_total"))) is not None:
        fields["filament_used_grams"] = round(grams, 2)
    if ftype := str(metadata.get("filament_type") or "").strip():
        # Orca-family slicers write one entry per extruder, e.g. "PLA;PLA"; the first is the primary.
        fields["filament_type"] = ftype.replace(";", ",").split(",")[0].strip()
    if (layer := _number(metadata.get("layer_height"))) is not None:
        fields["layer_height"] = layer
    if (layers := _number(metadata.get("layer_count"))) is not None:
        fields["total_layers"] = int(layers)
    if (nozzle := _number(metadata.get("nozzle_diameter"))) is not None:
        fields["nozzle_diameter"] = nozzle
    if (bed := _number(metadata.get("first_layer_bed_temp"))) is not None:
        fields["bed_temperature"] = int(bed)
    if (hotend := _number(metadata.get("first_layer_extr_temp"))) is not None:
        fields["nozzle_temperature"] = int(hotend)
    return fields


async def _filament_cost(db, grams: float | None, filament_type: str | None) -> float | None:
    """Same rule as a Bambu archive: the filament's own price per kg, else the default setting."""
    if not grams or not filament_type:
        return None
    from backend.app.api.routes.settings import get_setting

    filament = (await db.execute(select(Filament).where(Filament.type == filament_type).limit(1))).scalar_one_or_none()
    if filament is not None:
        return round(grams / 1000 * filament.cost_per_kg, 2)
    default = await get_setting(db, "default_filament_cost")
    return round(grams / 1000 * (float(default) if default else 25.0), 2)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


async def enrich_archive(archive_id: int, printer_id: int, remote_filename: str) -> bool:
    """Complete ``archive_id`` from the printer's file metadata. True when anything was added."""
    try:
        client = printer_manager.get_client(printer_id)
        if not isinstance(client, MoonrakerClient):
            return False
        metadata = await asyncio.to_thread(client.get_file_metadata, remote_filename)
        if not metadata:
            return False

        async with async_session() as db:
            archive = await db.get(PrintArchive, archive_id)
            if archive is None or archive.printer_id != printer_id:
                return False

            fields = archive_fields_from_metadata(metadata)
            safe_name = Path(remote_filename).name
            folder = (
                settings.archive_dir / str(printer_id) / f"{datetime.now():%Y%m%d_%H%M%S}_{Path(safe_name).stem}"
            )  # SEC-PATH-OK: printer_id is an int; the last part is Path(...).name stripped of any directory
            folder.mkdir(parents=True, exist_ok=True)

            # cover picture
            relative = _largest_thumbnail(metadata)
            if relative:
                data = await asyncio.to_thread(client.download_thumbnail, remote_filename, relative)
                if data:
                    thumb = folder / "thumbnail.png"
                    thumb.write_bytes(data)
                    archive.thumbnail_path = str(thumb.relative_to(settings.base_dir))

            # the G-code itself, so the print can be repeated from the archive
            gcode = folder / safe_name
            size = await asyncio.to_thread(client.download_gcode, remote_filename, gcode, MAX_GCODE_BYTES)
            if size:
                archive.file_path = str(gcode.relative_to(settings.base_dir))
                archive.file_size = size
                archive.content_hash = await asyncio.to_thread(_sha256, gcode)
                from backend.app.api.routes.library import read_gcode_printer_model

                sliced_for = await asyncio.to_thread(read_gcode_printer_model, gcode)
                if sliced_for:
                    fields["sliced_for_model"] = sliced_for

            for name, value in fields.items():
                # the print time MQTT-style recovery may already hold a better live figure
                if name == "print_time_seconds" and archive.print_time_seconds:
                    continue
                setattr(archive, name, value)

            if (cost := await _filament_cost(db, archive.filament_used_grams, archive.filament_type)) is not None:
                archive.cost = cost

            # A Klipper print has no 3MF by design; do not let the Archives page nag about it.
            extra = dict(archive.extra_data or {})
            extra["no_3mf_available"] = False
            extra["source"] = "moonraker"
            archive.extra_data = extra
            await db.commit()

            try:
                from backend.app.core.websocket import ws_manager

                await ws_manager.send_archive_updated({"id": archive.id, "status": archive.status})
            except Exception:  # noqa: BLE001 - the UI refreshes on its own soon enough
                pass
            logger.info(
                "[%s] Archive %s completed from Moonraker metadata (%d fields, thumbnail=%s, gcode=%s)",
                client.ip_address,
                archive_id,
                len(fields),
                bool(archive.thumbnail_path),
                bool(size),
            )
            return True
    except Exception:  # noqa: BLE001 - never let archive enrichment disturb a running print
        logger.warning("Could not complete archive %s from Moonraker metadata", archive_id, exc_info=True)
        return False
