#!/usr/bin/env python3
"""Conservative ISO9660 patching for GTA PS2/PSP radio replacements.

GTA San Andreas PS2 uses PAK + TRAKLKUP.DAT; selected other PS2/PSP profiles
replace a prepared VB/AT3 whole-station file. LCS/VCS PSP replacements also update
the selected 0x800-byte AT3 header-cache slot. VCS M_LOOKUP.BIN is preserved while
its record semantics remain unresolved. ISO/CSO/ISZ conversion is handled with strict
game identity, file hash, and source-image preservation checks.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import PS2_PAK_Core as core
import Executable_Patch_Core as executable_patch_core
from Native_ISZ import encode_isz, inspect_isz

SECTOR_SIZE = 2048
PVD_FIRST_SECTOR = 16
MAX_DIRECTORY_DEPTH = 64
SA_PS2_SERIALS = {"SLUS-20946", "SLES-52541", "SLES-52927", "SLPM-65984"}
Progress = Callable[[str], None]

# PSP station mappings extracted from the supplied decrypted executables and their
# matching regional 0x800-byte header caches. Indexes are zero-based.
def _psp_station_rows(rows):
    return [{"id": sid, "station": label, "asset_filename": filename,
             "asset_extension": ".at3",
             "target_path": f"PSP_GAME/USRDIR/AUDIO/MUSIC/{filename}",
             "header_slot_index": index}
            for index, (sid, label, filename) in enumerate(rows)]

LCS_PSP_STATIONS = _psp_station_rows([
    ("head", "Head Radio", "HEAD.AT3"),
    ("double", "Double Clef FM", "DOUBLE.AT3"),
    ("kjah", "K-JAH Radio", "KJAH.AT3"),
    ("rise", "Rise FM", "RISE.AT3"),
    ("lips", "Lips 106", "LIPS.AT3"),
    ("mundo", "Radio Del Mundo", "MUNDO.AT3"),
    ("msx", "MSX 98", "MSX.AT3"),
    ("flash", "Flashback FM", "FLASH.AT3"),
    ("lcj", "LCJ Radio", "LCJ.AT3"),
    ("lcfr", "LCFR", "LCFR.AT3"),
])
VCS_PSP_EU_STATIONS = _psp_station_rows([
    ("flash", "Flash FM", "FLASH.AT3"),
    ("vrock", "V-Rock", "VROCK.AT3"),
    ("paradise", "Paradise FM", "PARADISE.AT3"),
    ("vcpr", "VCPR", "VCPR.AT3"),
    ("vcfl", "VCFL", "VCFL.AT3"),
    ("wave_eu", "Wave 103 (Europe)", "WAVE_EU.AT3"),
    ("fresh", "Fresh 105 FM", "FRESH.AT3"),
    ("espant", "Espantoso", "ESPANT.AT3"),
    ("emotion", "Emotion 98.3", "EMOTION.AT3"),
])

# Direct whole-station targets for platforms that do not use GTA SA's PAK +
# TRAKLKUP.DAT stream archive. These are deliberately limited to the radio
# payloads identified in the project's supplied reference material. A selected
# asset must already be compatible with the target game's radio format.
GAME_RADIO_PROFILES = {
    "gta3_ps2": {
        "title": "Grand Theft Auto III (PS2)", "platform": "PS2", "mode": "direct",
        "station": "K-JAH Radio", "asset_filename": "KJAH.VB", "asset_extension": ".vb",
        "target_path": "AUDIO/MUSIC/KJAH.VB", "identity_file": "SYSTEM.CNF",
        "serials": {"SLUS-20062", "SLES-50330", "SLES-50793", "SLPM-65382", "SLPM-65760", "SLPM-66789", "SLPM-55293"},
        "asset_description": "PS2 VB radio stream (pre-encoded; copied byte-for-byte)",
    },
    "gta_vc_ps2": {
        "title": "Grand Theft Auto: Vice City (PS2)", "platform": "PS2", "mode": "direct",
        "station": "VCPR", "asset_filename": "VCPR.VB", "asset_extension": ".vb",
        "target_path": "AUDIO/MUSIC/VCPR.VB", "identity_file": "SYSTEM.CNF",
        "serials": {"SLUS-20552", "SLES-51061", "SLES-51316", "SLES-51595", "SLPM-65488", "SLPM-66034", "SLPM-66790", "SLPM-55291"},
        "asset_description": "PS2 VB radio stream (pre-encoded; copied byte-for-byte)",
    },
    "gta_sa_ps2": {
        "title": "Grand Theft Auto: San Andreas (PS2)", "platform": "PS2", "mode": "sa_pak",
        "station": "Radio PAK", "asset_filename": "TK.PAK", "asset_extension": ".pak",
        "target_path": "AUDIO/STREAMS/TK.PAK", "identity_file": "SYSTEM.CNF",
        "serials": set(SA_PS2_SERIALS),
        "asset_description": "Station PAK + TRAKLKUP.DAT, with type-4 dual-stream batch replacement",
    },
    "gta_lcs_ps2": {
        "title": "Grand Theft Auto: Liberty City Stories (PS2)", "platform": "PS2", "mode": "direct",
        "station": "K-JAH Radio", "asset_filename": "KJAH.VB", "asset_extension": ".vb",
        "target_path": "AUDIO/MUSIC/KJAH.VB", "identity_file": "SYSTEM.CNF",
        "serials": {"SLUS-21423", "SLES-54135", "SLES-54136", "SLPM-66851", "SLPM-55038"},
        "asset_description": "PS2 VB radio stream (pre-encoded; copied byte-for-byte)",
    },
    "gta_vcs_ps2": {
        "title": "Grand Theft Auto: Vice City Stories (PS2)", "platform": "PS2", "mode": "research_required",
        "station": "VCPR", "asset_filename": None, "asset_extension": None,
        "target_path": None, "identity_file": "SYSTEM.CNF",
        "serials": {"SLUS-21590", "SLES-54622", "SLES-54623", "SLPM-66917", "SLPM-55135"},
        "asset_description": "Unresolved PS2 audio lookup/stream layout; requires original disc data verification",
        "research_note": (
            "The supplied VCS PS2 executable (SLUS_215.90) contains the path pattern "
            "AUDIO/M_LOOKUP.BIN and stream identifiers radio_1 through radio_9, plus a VCPR identifier. "
            "It does not confirm AUDIO/MUSIC/VCPR.VB as the replaceable station payload. "
            "Patch ISO and staging are disabled for this profile until the exact on-disc lookup/stream relationship "
            "is verified from the original VCS PS2 files. This prevents patching the wrong file."
        ),
    },
    "gta_lcs_psp": {
        "title": "Grand Theft Auto: Liberty City Stories (PSP)", "platform": "PSP", "mode": "direct",
        "station": "K-JAH Radio", "asset_filename": "KJAH.AT3", "asset_extension": ".at3",
        "target_path": "PSP_GAME/USRDIR/AUDIO/MUSIC/KJAH.AT3", "identity_file": "PSP_GAME/PARAM.SFO",
        "serials": {"ULUS10041", "ULES00151", "ULES00182", "ULJM05255", "ULJM05885"},
        "header_companion_path": "PSP_GAME/USRDIR/AUDIO/AT3HEADERS.BIN", "header_slot_index": 2,
        "header_block_size": 0x800,
        "asset_description": "PSP ATRAC3plus station stream plus matching AT3HEADERS.BIN cache slot (pre-encoded)",
        "stations": LCS_PSP_STATIONS, "default_station_id": "kjah",
    },
    "gta_vcs_psp": {
        "title": "Grand Theft Auto: Vice City Stories (PSP)", "platform": "PSP", "mode": "direct",
        "station": "VCPR", "asset_filename": "VCPR.AT3", "asset_extension": ".at3",
        "target_path": "PSP_GAME/USRDIR/AUDIO/MUSIC/VCPR.AT3", "identity_file": "PSP_GAME/PARAM.SFO",
        "serials": {"ULES00502", "ULES00503"},
        "header_companion_path": "PSP_GAME/USRDIR/AUDIO/AT3HEDEU.BIN", "header_slot_index": 3,
        "header_block_size": 0x800, "preserve_dependency_path": "PSP_GAME/USRDIR/AUDIO/M_LOOKUP.BIN",
        "asset_description": "PSP ATRAC3plus station stream plus matching AT3HEDEU.BIN cache slot (pre-encoded)",
        "patch_warning": "M_LOOKUP.BIN contains 1,752 32-bit entries and is searched using computed resource keys. Its full key algorithm/semantics are not decoded, so it is preserved unchanged. Stream/header RIFF metadata and ISO file lengths are updated for the selected station, but changed-duration playback must still be tested in PPSSPP.",
        "stations": VCS_PSP_EU_STATIONS, "default_station_id": "vcpr",
    },
}


def resolve_game_radio_profile(game_id: str, station_id: str | None = None) -> dict:
    """Return the selected station's profile while preserving its game's identity rules."""
    base = GAME_RADIO_PROFILES.get(game_id)
    if not base:
        raise ValueError(f"Unsupported game/platform selection: {game_id}")
    profile = dict(base)
    stations = base.get("stations") or []
    if not stations:
        profile.setdefault("station_id", None)
        return profile
    requested = str(station_id or base.get("default_station_id") or stations[0]["id"]).strip().casefold()
    selected = next((row for row in stations if requested in {
        str(row.get("id", "")).casefold(), str(row.get("station", "")).casefold(),
        str(row.get("asset_filename", "")).casefold()}), None)
    if selected is None:
        raise ValueError(f"Unknown station {station_id!r} for {base['title']}.")
    profile.update(selected)
    profile["station_id"] = selected["id"]
    profile["station_slot_count"] = len(stations)
    return profile


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_both32(data: bytes, what: str) -> int:
    if len(data) != 8:
        raise ValueError(f"Malformed ISO {what} field.")
    little = struct.unpack("<I", data[:4])[0]
    big = struct.unpack(">I", data[4:])[0]
    if little != big:
        raise ValueError(f"ISO has inconsistent little-/big-endian {what} values.")
    return little


def _write_both32(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("The ISO9660 value exceeds the supported 32-bit field size.")
    return struct.pack("<I", value) + struct.pack(">I", value)


def _clean_iso_name(raw: bytes) -> str:
    if raw == b"\x00":
        return "."
    if raw == b"\x01":
        return ".."
    name = raw.decode("ascii", errors="replace").rstrip(" .")
    if ";" in name:
        name = name.rsplit(";", 1)[0]
    return name


@dataclass
class IsoRecord:
    path: str
    name: str
    directory: bool
    extent: int
    size: int
    flags: int
    ext_attr_length: int
    file_unit_size: int
    interleave_gap: int
    record_offset: int
    record_length: int

    @property
    def data_extent(self) -> int:
        return self.extent + self.ext_attr_length

    @property
    def sectors(self) -> int:
        return (self.size + SECTOR_SIZE - 1) // SECTOR_SIZE

    @property
    def end_sector(self) -> int:
        return self.data_extent + self.sectors


class Iso9660:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        self.file_size = self.path.stat().st_size
        if self.file_size % SECTOR_SIZE:
            raise ValueError(
                "This is not a plain 2048-byte-sector ISO9660 image after conversion. Check that the ISO/CSO/ISZ source decompressed correctly."
            )
        self.total_sectors = self.file_size // SECTOR_SIZE
        if self.total_sectors <= PVD_FIRST_SECTOR:
            raise ValueError("The selected file is too small to be a valid ISO image.")
        self.records: list[IsoRecord] = []
        self.visited_dirs: set[tuple[int, int]] = set()
        self.root: IsoRecord | None = None
        self.has_supplementary_descriptor = False

    def _read_exact_at(self, fh, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.file_size:
            raise ValueError("ISO directory points outside the image; refusing to patch it.")
        fh.seek(offset)
        data = fh.read(size)
        if len(data) != size:
            raise ValueError("Unexpected end of ISO image.")
        return data

    def _find_pvd_root(self, fh) -> IsoRecord:
        pvd = None
        for sector in range(PVD_FIRST_SECTOR, min(self.total_sectors, 256)):
            data = self._read_exact_at(fh, sector * SECTOR_SIZE, SECTOR_SIZE)
            if data[1:6] != b"CD001" or data[6] != 1:
                if sector > PVD_FIRST_SECTOR:
                    break
                continue
            if data[0] == 2:
                self.has_supplementary_descriptor = True
            if data[0] == 1:
                pvd = data
            if data[0] == 255:
                break
        if pvd is None:
            raise ValueError("No ISO9660 Primary Volume Descriptor was found at the standard location.")
        rec = pvd[156:190]
        if len(rec) < 34 or rec[0] < 34 or rec[32] != 1 or rec[33] != 0:
            raise ValueError("The ISO9660 root directory record is malformed.")
        return IsoRecord(
            "", ".", True,
            _read_both32(rec[2:10], "root extent"),
            _read_both32(rec[10:18], "root size"),
            rec[25], rec[1], rec[26], rec[27],
            16 * SECTOR_SIZE + 156, rec[0]
        )

    def _scan_dir(self, directory: IsoRecord, parts: tuple[str, ...], depth: int) -> None:
        if depth > MAX_DIRECTORY_DEPTH:
            raise ValueError("ISO directory nesting is unexpectedly deep; refusing to patch it.")
        if not directory.directory:
            return
        key = (directory.data_extent, directory.size)
        if key in self.visited_dirs:
            return
        self.visited_dirs.add(key)
        start = directory.data_extent * SECTOR_SIZE
        if start + directory.size > self.file_size:
            raise ValueError("An ISO directory points outside the image.")
        entries: list[IsoRecord] = []
        with self.path.open("rb") as source:
            pos = 0
            while pos < directory.size:
                absolute = start + pos
                first = self._read_exact_at(source, absolute, 1)[0]
                if first == 0:
                    pos = ((pos // SECTOR_SIZE) + 1) * SECTOR_SIZE
                    continue
                if first < 34 or pos + first > directory.size:
                    raise ValueError(f"Malformed directory record in {'/'.join(parts) or 'root'}.")
                raw = self._read_exact_at(source, absolute, first)
                ident_len = raw[32]
                if 33 + ident_len > first:
                    raise ValueError("Malformed ISO filename field.")
                name = _clean_iso_name(raw[33:33 + ident_len])
                extent = _read_both32(raw[2:10], "extent")
                size = _read_both32(raw[10:18], "file size")
                entry_parts = parts if name in (".", "..") else parts + (name,)
                entry_path = "/".join(entry_parts)
                entry = IsoRecord(entry_path, name, bool(raw[25] & 0x02), extent, size, raw[25], raw[1], raw[26], raw[27], absolute, first)
                if name not in (".", ".."):
                    entries.append(entry)
                    self.records.append(entry)
                pos += first
        for entry in entries:
            if entry.directory:
                self._scan_dir(entry, tuple(entry.path.split("/")) if entry.path else (), depth + 1)

    def scan(self) -> list[IsoRecord]:
        with self.path.open("rb") as fh:
            self.root = self._find_pvd_root(fh)
        self._scan_dir(self.root, (), 0)
        return self.records


def _identify_sa_ps2_iso(image: Iso9660) -> str:
    entries = [r for r in image.records if not r.directory and r.path.upper() == "SYSTEM.CNF"]
    if len(entries) != 1:
        raise ValueError("Could not uniquely locate SYSTEM.CNF. Select a GTA San Andreas PS2 ISO.")
    record = entries[0]
    if record.file_unit_size or record.interleave_gap or (record.flags & 0x80):
        raise ValueError("SYSTEM.CNF uses an unsupported ISO layout; refusing to guess the game identity.")
    if record.size <= 0 or record.size > 1024 * 1024:
        raise ValueError("SYSTEM.CNF has an invalid or unexpectedly large directory entry.")
    with image.path.open("rb") as fh:
        offset = record.data_extent * SECTOR_SIZE
        if offset < 0 or offset + record.size > image.file_size:
            raise ValueError("SYSTEM.CNF points outside the ISO image.")
        fh.seek(offset)
        text = fh.read(record.size).decode("ascii", errors="ignore").upper()
    found = {
        f"{match.group(1)}-{match.group(2)}{match.group(3)}"
        for match in re.finditer(r"\b(SLUS|SLES|SLPM)[_-]?(\d{3})[._-]?(\d{2})\b", text)
    }
    matches = sorted(found & SA_PS2_SERIALS)
    if matches:
        return matches[0]
    detected = ", ".join(sorted(found)) if found else "no recognizable PS2 serial"
    raise ValueError(
        f"This image could not be verified as a supported GTA San Andreas PS2 disc ({detected}). "
        "No ISO was patched."
    )


def inspect_staging(staging_root: Path) -> dict:
    """Validate prepared files and the staging manifest; also works after app restart."""
    root = Path(staging_root).expanduser().resolve()
    manifest_path = root / "ISO_PATCH_MANIFEST.json"
    if not manifest_path.is_file():
        raise ValueError(f"Prepared staging manifest not found: {manifest_path}\nRun Prepare Files for ISO Patching first.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read the staging manifest: {exc}") from exc
    purpose = str(manifest.get("purpose", ""))
    accepted_purposes = ("Prepared files for an external ISO patching tool", "Prepared files for GTA San Andreas PS2 ISO patching")
    if not purpose.startswith(accepted_purposes):
        raise ValueError("The selected manifest is not a GTA Modding Studio radio ISO-staging manifest.")
    archive = manifest.get("archive")
    if not isinstance(archive, dict):
        raise ValueError("The staging manifest has no archive metadata.")
    archive_name = str(archive.get("filename", "")).upper()
    if archive_name not in core.KNOWN_ARCHIVE_IDS:
        raise ValueError(f"Unsupported or missing station filename in the manifest: {archive_name or '(blank)'}")
    archive_id = core.KNOWN_ARCHIVE_IDS[archive_name]
    if str(archive.get("archive_id", "")).upper() != f"0X{archive_id:02X}":
        raise ValueError("The station name and archive ID in the staging manifest do not match.")
    pak_path = root / "AUDIO" / "STREAMS" / archive_name
    lookup_path = root / "AUDIO" / "CONFIG" / "TRAKLKUP.DAT"
    if not pak_path.is_file() or pak_path.stat().st_size <= 0:
        raise ValueError(f"Staged radio archive is missing or empty: {pak_path}")
    if not lookup_path.is_file() or lookup_path.stat().st_size <= 0:
        raise ValueError(f"Staged TRAKLKUP.DAT is missing or empty: {lookup_path}")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list):
        raise ValueError("The staging manifest has no file verification records.")
    by_game_path = {str(item.get("game_disc_path", "")).replace("\\", "/").upper(): item for item in manifest_files if isinstance(item, dict)}
    expected = {
        f"AUDIO/STREAMS/{archive_name}": pak_path,
        "AUDIO/CONFIG/TRAKLKUP.DAT": lookup_path,
    }
    for game_path, target in expected.items():
        info = by_game_path.get(game_path.upper())
        if not info:
            raise ValueError(f"Manifest does not describe the expected staged file {game_path}.")
        actual_size = target.stat().st_size
        expected_size = int(info.get("size_bytes", -1))
        if actual_size != expected_size:
            raise ValueError(f"Staged file size no longer matches the manifest: {target}")
        expected_hash = str(info.get("sha256", "")).lower()
        if not expected_hash or _sha256_path(target) != expected_hash:
            raise ValueError(f"Staged file hash no longer matches the manifest: {target}\nPrepare the files again before patching.")
    return {"root": root, "manifest_path": manifest_path, "manifest": manifest, "archive_name": archive_name,
            "archive_id": archive_id, "pak_path": pak_path, "lookup_path": lookup_path}


def _write_replacement(out, target: IsoRecord, payload_path: Path, next_free_sector: int) -> tuple[int, bool]:
    payload_size = payload_path.stat().st_size
    if payload_size <= 0 or payload_size > 0xFFFFFFFF:
        raise ValueError(f"Replacement file has an invalid ISO9660 size: {payload_path}")
    relocated = payload_size > target.sectors * SECTOR_SIZE
    if relocated:
        new_extent = next_free_sector
        ext_attr_bytes = target.ext_attr_length * SECTOR_SIZE
        if ext_attr_bytes:
            out.seek(target.extent * SECTOR_SIZE)
            attr_data = out.read(ext_attr_bytes)
            if len(attr_data) != ext_attr_bytes:
                raise ValueError(f"Could not read extended attributes for '{target.path}'.")
            out.seek(new_extent * SECTOR_SIZE)
            out.write(attr_data)
        data_sector = new_extent + target.ext_attr_length
        end_sector = data_sector + (payload_size + SECTOR_SIZE - 1) // SECTOR_SIZE
        if end_sector > 0xFFFFFFFF:
            raise ValueError("Patched image exceeds ISO9660's 32-bit sector address space.")
    else:
        new_extent = target.extent
        data_sector = target.data_extent
        end_sector = next_free_sector
    out.seek(data_sector * SECTOR_SIZE)
    with Path(payload_path).open("rb") as payload:
        shutil.copyfileobj(payload, out, length=1024 * 1024)
    if relocated:
        padded_end = end_sector * SECTOR_SIZE
        actual_end = data_sector * SECTOR_SIZE + payload_size
        if padded_end > actual_end:
            out.seek(actual_end)
            out.write(b"\0" * (padded_end - actual_end))
        out.seek(target.record_offset + 2)
        out.write(_write_both32(new_extent))
        next_free_sector = end_sector
    out.seek(target.record_offset + 10)
    out.write(_write_both32(payload_size))
    return next_free_sector, relocated


def _update_volume_space_size(out, total_sectors: int) -> None:
    if not 0 < total_sectors <= 0xFFFFFFFF:
        raise ValueError("Patched ISO sector count exceeds the ISO9660 limit.")
    for sector in range(PVD_FIRST_SECTOR, 256):
        start = sector * SECTOR_SIZE
        out.seek(start)
        desc = out.read(SECTOR_SIZE)
        if len(desc) != SECTOR_SIZE or desc[1:6] != b"CD001" or desc[6] != 1:
            if sector > PVD_FIRST_SECTOR:
                break
            continue
        if desc[0] in (1, 2):
            out.seek(start + 80)
            out.write(_write_both32(total_sectors))
        if desc[0] == 255:
            break


def patch_sa_ps2_radio_iso(iso_path: Path, output_path: Path, staging_root: Path,
                           progress_callback: Progress = lambda _text: None,
                           overwrite: bool = False) -> dict:
    """Patch staged radio PAK + lookup into a recognized SA PS2 ISO copy.

    The selected source image is never opened for writing. Output is built beside
    the requested destination as a temporary file, verified, then published.
    """
    iso_path = Path(iso_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    if not iso_path.is_file():
        raise ValueError("Select an existing GTA San Andreas PS2 ISO image.")
    if iso_path == output_path:
        raise ValueError("The output ISO path must differ from the source ISO. The source is never overwritten.")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Output ISO already exists: {output_path}")
    if output_path.suffix.lower() != ".iso":
        raise ValueError("Choose a .iso output. Compressed CSO/ISZ and raw 2352-byte images are not supported.")

    staging = inspect_staging(staging_root)
    archive_name = staging["archive_name"]
    payloads = {
        f"AUDIO/STREAMS/{archive_name}": staging["pak_path"],
        "AUDIO/CONFIG/TRAKLKUP.DAT": staging["lookup_path"],
    }
    progress_callback("Scanning ISO9660 filesystem…")
    image = Iso9660(iso_path)
    records = image.scan()
    serial = _identify_sa_ps2_iso(image)
    progress_callback(f"Verified GTA San Andreas PS2 disc ({serial}).")
    records_by_path: dict[str, list[IsoRecord]] = {}
    for record in records:
        records_by_path.setdefault(record.path.upper(), []).append(record)

    targets: dict[str, IsoRecord] = {}
    for target_path in payloads:
        matches = records_by_path.get(target_path.upper(), [])
        if len(matches) != 1:
            if not matches:
                raise ValueError(f"Could not find '{target_path}' in the selected ISO. No changes were made.")
            raise ValueError(f"More than one ISO entry matches '{target_path}'. Refusing an ambiguous patch.")
        target = matches[0]
        if target.directory:
            raise ValueError(f"ISO target '{target_path}' is a directory, not a file.")
        if target.file_unit_size or target.interleave_gap or (target.flags & 0x80):
            raise ValueError(f"ISO target '{target_path}' uses interleaving or multiple extents, which is not supported.")
        if target.size <= 0 or target.end_sector > image.total_sectors:
            raise ValueError(f"ISO target '{target_path}' has an invalid extent; refusing to patch it.")
        targets[target_path] = target

    # Refuse targets whose existing sectors overlap any other ISO directory entry.
    for target_path, target in targets.items():
        for other in records:
            if other.record_offset == target.record_offset or other.sectors == 0:
                continue
            if other.data_extent < target.end_sector and target.data_extent < other.end_sector:
                raise ValueError(f"ISO layout check failed: '{other.path}' overlaps '{target_path}'. No changes were made.")
    extents = sorted((record.data_extent, record.end_sector, path) for path, record in targets.items())
    for left, right in zip(extents, extents[1:]):
        if left[1] > right[0]:
            raise ValueError(f"ISO target extents overlap ({left[2]} and {right[2]}). No changes were made.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.patching")
    relocated_targets = set()
    next_free_sector = image.total_sectors
    try:
        progress_callback("Copying source ISO to a separate output image…")
        total_bytes = iso_path.stat().st_size
        copied = 0
        source_digest = hashlib.sha256()
        with iso_path.open("rb") as src, temporary.open("xb") as dst:
            while True:
                block = src.read(8 * 1024 * 1024)
                if not block:
                    break
                dst.write(block)
                source_digest.update(block)
                copied += len(block)
                progress_callback(f"Copying ISO: {100.0 * copied / max(1, total_bytes):.0f}%")
            dst.flush()
            os.fsync(dst.fileno())
        with temporary.open("r+b") as out:
            for index, (target_path, payload_path) in enumerate(payloads.items(), start=1):
                progress_callback(f"Patching {index}/2: {target_path}…")
                next_free_sector, relocated = _write_replacement(out, targets[target_path], payload_path, next_free_sector)
                if relocated:
                    relocated_targets.add(target_path)
                    progress_callback(f"Relocated larger replacement to new ISO sectors: {target_path}")
            _update_volume_space_size(out, next_free_sector)
            out.flush()
            os.fsync(out.fileno())

        # Reopen the candidate ISO and confirm each directory record and actual file bytes.
        progress_callback("Verifying patched ISO contents…")
        verification = Iso9660(temporary)
        verify_records = verification.scan()
        verify_by_path: dict[str, list[IsoRecord]] = {}
        for record in verify_records:
            verify_by_path.setdefault(record.path.upper(), []).append(record)
        verified = []
        for target_path, payload_path in payloads.items():
            matches = verify_by_path.get(target_path.upper(), [])
            if len(matches) != 1:
                raise ValueError(f"Post-patch verification could not uniquely find {target_path} in the output ISO.")
            record = matches[0]
            if record.size != payload_path.stat().st_size:
                raise ValueError(f"Post-patch size verification failed for {target_path}.")
            with temporary.open("rb") as fh:
                fh.seek(record.data_extent * SECTOR_SIZE)
                digest = hashlib.sha256()
                remaining = record.size
                while remaining:
                    block = fh.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError(f"Output ISO ended while verifying {target_path}.")
                    digest.update(block)
                    remaining -= len(block)
            expected_hash = _sha256_path(payload_path)
            if digest.hexdigest() != expected_hash:
                raise ValueError(f"Post-patch content hash failed for {target_path}.")
            verified.append({
                "game_disc_path": target_path,
                "old_size_bytes": targets[target_path].size,
                "new_size_bytes": record.size,
                "sha256": digest.hexdigest(),
                "relocated": target_path in relocated_targets,
            })
        if output_path.exists() and not overwrite:
            raise FileExistsError(f"Output ISO appeared during patching: {output_path}")
        os.replace(temporary, output_path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    report_path = output_path.with_suffix(output_path.suffix + ".patch_report.json")
    report = {
        "tool": "GTA Modding Studio PS2 ISO Patcher",
        "source_iso": str(iso_path),
        "patched_iso": str(output_path),
        "verified_disc_serial": serial,
        "station_archive": archive_name,
        "staging_manifest": str(staging["manifest_path"]),
        "source_iso_sha256": source_digest.hexdigest(),
        "patched_iso_size_bytes": output_path.stat().st_size,
        "files": verified,
        "warning": "Structural verification passed. In-game audio playback must still be tested in PCSX2.",
    }
    report_warning = None
    try:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError as exc:
        # The ISO itself is already complete and hash-verified. Do not misreport a
        # report-file permissions problem as a failed patch or leave the user unsure
        # whether the output image exists.
        report_warning = f"The ISO was patched and verified, but its report could not be written: {exc}"
        report_path = None
    return {"source_iso": iso_path, "output_iso": output_path, "report_path": report_path,
            "report_warning": report_warning, "serial": serial, "archive_name": archive_name,
            "files": verified, "output_size_bytes": output_path.stat().st_size}



def _image_tool_root() -> Path:
    """Folder populated by Install_Image_Tools.ps1."""
    base = Path(__file__).resolve().parent
    for candidate in (base / "Tools" / "image_tools", base / "tools" / "image_tools"):
        if candidate.is_dir():
            return candidate
    return base / "Tools" / "image_tools"


def _find_maxcso() -> Path | None:
    """Find the locally installed MaxCSO executable, or a PATH installation."""
    root = _image_tool_root()
    candidates = [root / "maxcso.exe", root / "maxcso", root / "maxcso32.exe"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for name in ("maxcso.exe", "maxcso"):
        located = shutil.which(name)
        if located:
            return Path(located)
    return None


def _find_isz_tool() -> Path | None:
    """Find bundled ISZ decoder without requiring the dependency installer."""
    base = Path(__file__).resolve().parent
    for candidate in (base / "Tools" / "image_tools" / "isz-tool.py",
                      base / "tools" / "image_tools" / "isz-tool.py"):
        if candidate.is_file():
            return candidate
    return None


def _run_image_tool(args: list[str], label: str, progress_callback: Progress,
                    cwd: Path | None = None) -> str:
    progress_callback(label)
    try:
        completed = subprocess.run(
            args, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, timeout=None,
        )
    except OSError as exc:
        raise RuntimeError(f"Could not start image conversion tool: {exc}") from exc
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip())
    if completed.returncode != 0:
        detail = output[-3500:] if output else "The tool did not provide error details."
        raise RuntimeError(f"{label.rstrip('.')} failed (exit code {completed.returncode}).\n\n{detail}")
    return output


def _run_maxcso_decompress(source: Path, destination: Path,
                           progress_callback: Progress) -> None:
    maxcso = _find_maxcso()
    if maxcso is None:
        raise FileNotFoundError(
            "MaxCSO was not found. Run Install_Dependencies.bat to install CSO/ISZ image support, "
            "or put maxcso.exe in tools\\image_tools."
        )
    destination.unlink(missing_ok=True)
    output = _run_image_tool(
        [str(maxcso), "--decompress", str(source), "-o", str(destination)],
        f"Decompressing CSO image {source.name}…", progress_callback,
    )
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("MaxCSO returned success but did not produce a non-empty ISO." + ("\n" + output if output else ""))
    if destination.stat().st_size % SECTOR_SIZE:
        raise ValueError("MaxCSO output is not a 2048-byte-sector ISO. The compressed source may be damaged or use an unsupported variant.")


def _isz_info(source: Path, tool: Path) -> str:
    try:
        result = subprocess.run(
            [sys.executable, str(tool), "info", str(source)], cwd=str(source.parent),
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"Could not start the ISZ conversion tool using Python: {exc}") from exc
    info = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    if result.returncode != 0:
        raise RuntimeError(f"Could not inspect this ISZ image.\n\n{info[-2500:] or 'The ISZ tool returned no details.'}")
    if re.search(r"Password protected|Encrypted AES(?:128|192|256)", info, re.IGNORECASE):
        raise ValueError(
            "This ISZ image is encrypted or password-protected. The bundled isz-tool supports unencrypted ISZ only; "
            "remove the encryption with a compatible tool and try again. No output ISO was created."
        )
    return info


def _run_isz_decompress(source: Path, destination: Path,
                        progress_callback: Progress) -> None:
    tool = _find_isz_tool()
    if tool is None:
        raise FileNotFoundError(
            "The bundled ISZ decoder is missing. Re-extract the complete GTA Modding Studio ZIP "
            "(including Tools\\image_tools\\isz-tool.py)."
        )
    # ISZ can be split across .isz/.i01 or .partNN.isz companions; the converter
    # resolves the sibling segment names from the selected first .isz path.
    info = _isz_info(source, tool)
    destination.unlink(missing_ok=True)
    try:
        output = _run_image_tool(
            [sys.executable, str(tool), "isz2iso", str(source), str(destination)],
            f"Decompressing ISZ image {source.name}…", progress_callback, cwd=source.parent,
        )
    except RuntimeError as exc:
        text = str(exc)
        if re.search(r"password|encrypt|AES|unsupported.*ISZ", text, re.IGNORECASE):
            raise ValueError(
                "This ISZ image could not be decompressed. It may be encrypted/password-protected or use an unsupported ISZ variant. "
                "The bundled converter supports unencrypted ISZ files only.\n\n" + text
            ) from exc
        raise
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("The ISZ converter returned success but did not produce a non-empty ISO." + ("\n" + output if output else ""))
    if destination.stat().st_size % SECTOR_SIZE:
        raise ValueError("The ISZ converter output is not a 2048-byte-sector ISO; refusing to patch it.")


def _probe_uncompressed_size(source: Path, image_format: str,
                             progress_callback: Progress) -> int | None:
    """Read an image's declared size when available to estimate temporary disk use."""
    if image_format == ".iso":
        return source.stat().st_size
    if image_format == ".cso":
        try:
            with source.open("rb") as handle:
                header = handle.read(24)
            # CSO v1 header: CISO, header size, uncompressed byte count (uint64 LE).
            if len(header) >= 16 and header[:4] == b"CISO":
                value = struct.unpack("<Q", header[8:16])[0]
                return value if value > 0 else None
        except OSError:
            pass
        return None
    if image_format == ".isz":
        # The 64-byte v1 header contains the exact sector count. Reading it
        # directly avoids the old rounded-down integer MB string estimate.
        # Reject encrypted input before staging gigabytes of temporary files.
        return inspect_isz(source)["bytes"]
    return None




def _run_isz_compress(source: Path, destination: Path, progress_callback: Progress) -> None:
    """Write unencrypted, single-file ISZ natively and verify its restored SHA-256."""
    progress_callback(f"Compressing patched ISO to native ISZ: {source.name}…")
    outcome = encode_isz(source, destination, overwrite=False, progress=progress_callback)
    if outcome["sha256"] != _sha256_path(source) or not outcome["verified"]["uncompressed_crc_ok"]:
        destination.unlink(missing_ok=True)
        raise ValueError("ISZ round-trip verification failed: output was not published.")
    progress_callback("ISZ uncompressed-stream SHA-256 and both CRCs verified.")

def patch_sa_ps2_radio_image(source_path: Path, output_path: Path, staging_root: Path,
                             progress_callback: Progress = lambda _text: None,
                             overwrite: bool = False) -> dict:
    """Patch an ISO/CSO/ISZ input into an ISO, CSO, or native unencrypted ISZ."""
    source_path = Path(source_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError("Select an existing GTA San Andreas PS2 ISO, CSO, or ISZ image.")
    if source_path == output_path:
        raise ValueError("The output image must differ from the source image. The source is never overwritten.")
    if output_path.suffix.lower() not in {".iso", ".cso", ".isz"}:
        raise ValueError("Choose .iso, .cso, or unencrypted .isz output.")
    source_format = source_path.suffix.lower()
    if source_format not in {".iso", ".cso", ".isz"}:
        raise ValueError("Only plain ISO (.iso), CSO (.cso), and ISZ (.isz) source images are supported.")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Output image already exists: {output_path}")

    # Check for the tools needed by the selected path before reserving temporary
    # disk space or beginning a long conversion.
    if source_format == ".cso" or output_path.suffix.lower() == ".cso":
        if _find_maxcso() is None:
            raise FileNotFoundError("MaxCSO is required for CSO input or output. Run Install_Dependencies.bat first.")
    if source_format == ".isz" and _find_isz_tool() is None:
        raise FileNotFoundError("Bundled isz-tool.py is missing. Re-extract the complete GTA Modding Studio ZIP.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    estimated = _probe_uncompressed_size(source_path, source_format, progress_callback)
    if estimated and estimated > 0:
        # During CSO/ISZ -> ISO, the decompressed source and patcher's temporary
        # output coexist. CSO output also needs the patched ISO and its decompressed
        # verification copy concurrently. Leave a little headroom for reports/files.
        if output_path.suffix.lower() in {".cso", ".isz"}:
            factor = 3 if source_format in {".cso", ".isz"} else 2
        else:
            factor = 2 if source_format in {".cso", ".isz"} else 1
        required = int(estimated * factor + max(256 * 1024 * 1024, estimated * 0.03))
        free = shutil.disk_usage(output_path.parent).free
        if free < required:
            raise OSError(
                "There may not be enough free disk space to safely patch this image.\n\n"
                f"Estimated temporary space needed: {required:,} bytes\n"
                f"Available in the output folder's drive: {free:,} bytes\n\n"
                "Choose a drive with more free space and try again. The source image has not been changed."
            )

    # Fast path: normal ISO -> ISO retains the existing implementation untouched.
    if source_format == ".iso" and output_path.suffix.lower() == ".iso":
        result = patch_sa_ps2_radio_iso(source_path, output_path, staging_root, progress_callback, overwrite)
        result["source_image"] = source_path
        result["output_image"] = output_path
        result["source_format"] = ".iso"
        result["output_format"] = ".iso"
        return result

    # Put all expanded images in the selected output drive to avoid consuming an
    # unrelated system drive's TEMP volume and to make free-space checks useful.
    with tempfile.TemporaryDirectory(prefix=".gta_sa_image_work_", dir=str(output_path.parent)) as temp_name:
        temp_root = Path(temp_name)
        if source_format == ".iso":
            patch_source_iso = source_path
        else:
            patch_source_iso = temp_root / "decompressed_source.iso"
            if source_format == ".cso":
                _run_maxcso_decompress(source_path, patch_source_iso, progress_callback)
            else:
                _run_isz_decompress(source_path, patch_source_iso, progress_callback)

        # Fail early if conversion output isn't a valid recognized SA PS2 ISO.
        progress_callback("Validating decompressed ISO structure and GTA SA PS2 disc identity…")
        source_image = Iso9660(patch_source_iso)
        source_image.scan()
        serial = _identify_sa_ps2_iso(source_image)
        progress_callback(f"Verified GTA San Andreas PS2 disc ({serial}).")

        if output_path.suffix.lower() == ".iso":
            result = patch_sa_ps2_radio_iso(
                patch_source_iso, output_path, staging_root, progress_callback, overwrite,
            )
            final_output = output_path
            roundtrip_verified = False
        else:
            patched_iso = temp_root / "patched_output.iso"
            result = patch_sa_ps2_radio_iso(
                patch_source_iso, patched_iso, staging_root, progress_callback, overwrite=False,
            )
            if output_path.suffix.lower() == ".isz":
                compressed_candidate = temp_root / "patched_output.isz"
                _run_isz_compress(patched_iso, compressed_candidate, progress_callback)
                progress_callback("Native ISZ round-trip verification passed.")
            else:
                maxcso = _find_maxcso()
                if maxcso is None:
                    raise FileNotFoundError("MaxCSO is required to create CSO output. Run Install_Dependencies.bat first.")
                compressed_candidate = temp_root / "patched_output.cso"
                _run_image_tool(
                    [str(maxcso), "--format=cso1", "--block=2048", str(patched_iso), "-o", str(compressed_candidate)],
                    "Compressing patched ISO into CSO…", progress_callback,
                )
                if not compressed_candidate.is_file() or compressed_candidate.stat().st_size <= 0:
                    raise RuntimeError("MaxCSO returned success but did not create a non-empty CSO output.")
                verified_iso = temp_root / "verify_compressed_output.iso"
                _run_maxcso_decompress(compressed_candidate, verified_iso, progress_callback)
                if _sha256_path(verified_iso) != _sha256_path(patched_iso):
                    raise ValueError("CSO recompression verification failed: decompressed output differs from the patched ISO. The output was not published.")
                progress_callback("CSO round-trip verification passed (SHA-256 matches patched ISO).")
            if output_path.exists() and not overwrite:
                raise FileExistsError(f"Output image appeared during patching: {output_path}")
            os.replace(compressed_candidate, output_path)
            final_output = output_path
            roundtrip_verified = True
            result["output_iso"] = output_path
            result["output_size_bytes"] = output_path.stat().st_size

        result["source_image"] = source_path
        result["output_image"] = final_output
        result["source_format"] = source_format
        result["output_format"] = final_output.suffix.lower()
        result["output_iso"] = final_output  # Keep the prior UI's result key compatible.
        result["output_size_bytes"] = final_output.stat().st_size

        # Publish one report beside the actual final image, not in the temporary
        # directory (which is cleaned up as soon as this function returns).
        report_path = final_output.with_suffix(final_output.suffix + ".patch_report.json")
        report = {
            "tool": "GTA Modding Studio PS2/PSP ISO Patcher v0.3.14",
            "source_image": str(source_path),
            "source_format": source_format,
            "patched_image": str(final_output),
            "output_format": final_output.suffix.lower(),
            "verified_disc_serial": result["serial"],
            "station_archive": result["archive_name"],
            "staging_manifest": str(Path(staging_root).expanduser().resolve() / "ISO_PATCH_MANIFEST.json"),
            "patched_image_size_bytes": final_output.stat().st_size,
            "files": result["files"],
            "full_image_roundtrip_sha256_verified": roundtrip_verified,
            "warning": "Structural verification passed. In-game audio playback must still be tested in PCSX2.",
        }
        report_warning = None
        try:
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        except OSError as exc:
            report_warning = f"The patched image is complete, but its report could not be written: {exc}"
            report_path = None
        result["report_path"] = report_path
        result["report_warning"] = report_warning
        return result



def prepare_game_radio_staging(source_file: Path, staging_root: Path, game_id: str,
                               overwrite: bool = False, station_id: str | None = None) -> dict:
    """Stage one pre-encoded whole-station file for a non-SA game/platform."""
    profile = resolve_game_radio_profile(game_id, station_id)
    if profile and profile.get("mode") == "research_required":
        raise ValueError(profile.get("research_note", "This game's radio target has not been verified yet."))
    if not profile or profile["mode"] != "direct":
        raise ValueError("This game does not use the direct whole-station staging workflow.")
    source = Path(source_file).expanduser().resolve()
    root = Path(staging_root).expanduser().resolve()
    if not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("Choose a non-empty, existing radio stream file.")
    if source.suffix.lower() != profile["asset_extension"]:
        raise ValueError(f"{profile['title']} requires a prepared {profile['asset_extension'].upper()} radio stream, not {source.suffix or '(no extension)' }.")
    target_path = root.joinpath(*profile["target_path"].split("/"))
    manifest_path = root / "ISO_PATCH_MANIFEST.json"
    if source == target_path.resolve():
        raise ValueError("The source file is already the staging destination. Choose a separate source file.")
    conflicts = [path for path in (target_path, manifest_path) if path.exists()]
    if conflicts and not overwrite:
        raise FileExistsError("These staging outputs already exist:\n" + "\n".join(str(x) for x in conflicts))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_asset = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
    temp_manifest = manifest_path.with_name(f".{manifest_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temp_asset)
        size = temp_asset.stat().st_size
        digest = _sha256_path(temp_asset)
        os.replace(temp_asset, target_path)
        manifest = {
            "tool": "GTA Modding Studio PS2/PSP Radio Installer",
            "purpose": "Prepared game-compatible radio stream for GTA PS2/PSP Radio Installer",
            "game_id": game_id,
            "game_title": profile["title"],
            "platform": profile["platform"],
            "station": profile["station"],
            "station_id": profile.get("station_id"),
            "station_slot_index": profile.get("header_slot_index"),
            "asset_filename": profile["asset_filename"],
            "target_path": profile["target_path"],
            "source_file": str(source),
            "size_bytes": size,
            "sha256": digest,
            "files": [{
                "game_disc_path": profile["target_path"],
                "staged_path": str(target_path),
                "size_bytes": size,
                "sha256": digest,
            }],
        }
        temp_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(temp_manifest, manifest_path)
    except Exception:
        temp_asset.unlink(missing_ok=True)
        temp_manifest.unlink(missing_ok=True)
        raise
    return {
        "root": root, "manifest_path": manifest_path, "profile": profile,
        "payload_path": target_path, "target_path": profile["target_path"],
        "size_bytes": size, "sha256": digest,
    }


def inspect_game_radio_staging(staging_root: Path, game_id: str, station_id: str | None = None) -> dict:
    """Verify that a direct radio staging manifest belongs to the selected station."""
    profile = resolve_game_radio_profile(game_id, station_id)
    if profile and profile.get("mode") == "research_required":
        raise ValueError(profile.get("research_note", "This game's radio target has not been verified yet."))
    if not profile or profile["mode"] != "direct":
        raise ValueError("The selected game does not use a direct radio-file manifest.")
    root = Path(staging_root).expanduser().resolve()
    manifest_path = root / "ISO_PATCH_MANIFEST.json"
    if not manifest_path.is_file():
        raise ValueError(f"Prepared radio manifest not found: {manifest_path}. Prepare the radio file first.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read the radio staging manifest: {exc}") from exc
    if manifest.get("purpose") != "Prepared game-compatible radio stream for GTA PS2/PSP Radio Installer":
        raise ValueError("This is not a prepared direct-radio manifest. Choose the matching game-specific staging folder.")
    if manifest.get("game_id") != game_id:
        raise ValueError(f"Prepared files are for {manifest.get('game_title', manifest.get('game_id', 'another game'))}, not {profile['title']}. Prepare the file again for the selected game.")
    if str(manifest.get("target_path", "")).replace("\\", "/").upper() != profile["target_path"].upper():
        raise ValueError("The staging manifest target path does not match the selected game's expected radio path.")
    if str(manifest.get("asset_filename", "")).upper() != profile["asset_filename"].upper():
        raise ValueError("The staged radio filename does not match the selected game's expected stream filename.")
    payload = root.joinpath(*profile["target_path"].split("/"))
    if not payload.is_file() or payload.stat().st_size <= 0:
        raise ValueError(f"Staged radio stream is missing or empty: {payload}")
    expected_size = int(manifest.get("size_bytes", -1))
    expected_hash = str(manifest.get("sha256", "")).lower()
    if payload.stat().st_size != expected_size:
        raise ValueError("The staged radio file size no longer matches its manifest. Prepare it again.")
    digest = _sha256_path(payload)
    if not expected_hash or digest != expected_hash:
        raise ValueError("The staged radio file SHA-256 no longer matches its manifest. Prepare it again.")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != 1:
        raise ValueError("The direct-radio manifest must contain exactly one station file record.")
    record = files[0]
    if str(record.get("game_disc_path", "")).replace("\\", "/").upper() != profile["target_path"].upper():
        raise ValueError("The file record in the manifest points to an unexpected game path.")
    if int(record.get("size_bytes", -1)) != expected_size or str(record.get("sha256", "")).lower() != digest:
        raise ValueError("The staged radio manifest file record does not match the actual stream file.")
    return {"root": root, "manifest_path": manifest_path, "manifest": manifest,
            "profile": profile, "payload_path": payload, "target_path": profile["target_path"],
            "size_bytes": expected_size, "sha256": digest}


def _identify_game_iso(image: Iso9660, game_id: str) -> str:
    profile = GAME_RADIO_PROFILES.get(game_id)
    if not profile:
        raise ValueError(f"Unknown game/platform selection: {game_id}")
    entries = [r for r in image.records if not r.directory and r.path.upper() == profile["identity_file"].upper()]
    if len(entries) != 1:
        raise ValueError(f"Could not uniquely locate {profile['identity_file']} to verify {profile['title']}.")
    record = entries[0]
    if record.file_unit_size or record.interleave_gap or (record.flags & 0x80):
        raise ValueError(f"{profile['identity_file']} has an unsupported ISO layout; refusing to guess the game identity.")
    if record.size <= 0 or record.size > 1024 * 1024:
        raise ValueError(f"{profile['identity_file']} has an invalid or unexpectedly large size.")
    offset = record.data_extent * SECTOR_SIZE
    if offset < 0 or offset + record.size > image.file_size:
        raise ValueError(f"{profile['identity_file']} points outside the image.")
    with image.path.open("rb") as handle:
        handle.seek(offset)
        payload = handle.read(record.size)
    if len(payload) != record.size:
        raise ValueError(f"Could not read {profile['identity_file']} to identify this image.")
    text = payload.decode("ascii", errors="ignore").upper()
    if profile["platform"] == "PSP":
        found = {f"{match.group(1)}{match.group(2)}" for match in re.finditer(r"\b(ULUS|ULES|ULJM)(\d{5})\b", text)}
    else:
        found = {f"{match.group(1)}-{match.group(2)}{match.group(3)}"
                 for match in re.finditer(r"\b(SLUS|SLES|SLPM)[_-]?(\d{3})[._-]?(\d{2})\b", text)}
    matched = sorted(found & set(profile["serials"]))
    if not matched:
        detected = ", ".join(sorted(found)) if found else "no recognized disc serial"
        raise ValueError(f"Selected image is not a supported {profile['title']} disc ({detected}). No changes were made.")
    return matched[0]


def _read_iso_record_prefix(iso_path: Path, record: IsoRecord, count: int) -> bytes:
    if record.directory or record.file_unit_size or record.interleave_gap or (record.flags & 0x80):
        raise ValueError(f"ISO file '{record.path}' uses an unsupported layout.")
    if count < 0 or count > record.size:
        raise ValueError(f"Could not read the requested bytes from '{record.path}'.")
    with Path(iso_path).open("rb") as handle:
        handle.seek(record.data_extent * SECTOR_SIZE)
        data = handle.read(count)
    if len(data) != count:
        raise ValueError(f"Unexpected end of ISO while reading '{record.path}'.")
    return data


def _hash_iso_record(iso_path: Path, record: IsoRecord) -> str:
    digest = hashlib.sha256()
    with Path(iso_path).open("rb") as handle:
        handle.seek(record.data_extent * SECTOR_SIZE)
        remaining = record.size
        while remaining:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"Unexpected end of ISO while hashing '{record.path}'.")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _parse_at3_prefix(prefix: bytes, complete_file_size: int, label: str) -> dict:
    """Parse a RIFF/ATRAC3plus prefix; data payload itself need not fit in prefix."""
    block_size = 0x800
    if len(prefix) < 64 or prefix[:4] != b"RIFF" or prefix[8:12] != b"WAVE":
        raise ValueError(f"{label} is not a supported RIFF/WAVE ATRAC stream.")
    declared_total = struct.unpack_from("<I", prefix, 4)[0] + 8
    if declared_total != complete_file_size:
        raise ValueError(
            f"{label} declares a RIFF length of {declared_total:,} bytes, but the actual stream is {complete_file_size:,} bytes."
        )
    offset = 12
    fmt_payload = None
    fact_samples = None
    fact_auxiliary = None
    data_payload_offset = None
    data_size = None
    has_smpl = False
    while offset + 8 <= len(prefix):
        tag = prefix[offset:offset + 4]
        size = struct.unpack_from("<I", prefix, offset + 4)[0]
        payload_start = offset + 8
        if tag == b"fmt ":
            if size < 16 or payload_start + size > len(prefix):
                raise ValueError(f"{label} has a malformed or truncated fmt chunk.")
            fmt_payload = bytes(prefix[payload_start:payload_start + size])
        elif tag == b"fact":
            if size < 4 or payload_start + size > len(prefix):
                raise ValueError(f"{label} has a malformed or truncated fact chunk.")
            fact_samples = struct.unpack_from("<I", prefix, payload_start)[0]
            if size >= 8:
                fact_auxiliary = struct.unpack_from("<I", prefix, payload_start + 4)[0]
        elif tag == b"smpl":
            has_smpl = True
        elif tag == b"data":
            data_payload_offset = payload_start
            data_size = size
            break
        next_offset = payload_start + size + (size & 1)
        if next_offset > len(prefix):
            raise ValueError(f"{label} has a chunk that extends beyond its 0x800-byte prefix.")
        offset = next_offset
    if fmt_payload is None or data_payload_offset is None or data_size is None:
        raise ValueError(f"{label} does not contain a complete fmt/data header within its first 0x800 bytes.")
    if fact_samples is None:
        raise ValueError(f"{label} does not contain a supported fact chunk in its header prefix.")
    # GTA LCS/VCS PSP station streams use Sony ATRAC3plus in
    # WAVEFORMATEXTENSIBLE, not PCM or legacy ATRAC3. Validate the actual
    # format GUID before accepting a station or its cached header block.
    if (len(fmt_payload) < 40 or
            struct.unpack_from("<H", fmt_payload, 0)[0] != 0xFFFE or
            struct.unpack_from("<H", fmt_payload, 16)[0] < 22 or
            fmt_payload[24:40] != bytes.fromhex("bfaa23e958cb7144a119fffa01e4ce62")):
        raise ValueError(f"{label} does not contain the expected PSP ATRAC3plus WAVEFORMATEXTENSIBLE codec descriptor.")
    channels = struct.unpack_from("<H", fmt_payload, 2)[0]
    sample_rate = struct.unpack_from("<I", fmt_payload, 4)[0]
    if channels < 1 or sample_rate < 1:
        raise ValueError(f"{label} has invalid channel or sample-rate metadata.")
    expected_size = data_payload_offset + data_size + (data_size & 1)
    if expected_size != complete_file_size:
        raise ValueError(f"{label} has an inconsistent RIFF data length.")
    if len(prefix) < block_size:
        raise ValueError(f"{label} must provide at least 0x800 bytes for the game's cached header block.")
    return {
        "declared_total": declared_total,
        "fmt_payload": fmt_payload,
        "data_payload_offset": data_payload_offset,
        "data_size": data_size,
        "has_smpl": has_smpl,
        "fact_samples": fact_samples,
        "fact_auxiliary": fact_auxiliary,
        "channels": channels,
        "sample_rate": sample_rate,
        "declared_duration_seconds": fact_samples / sample_rate,
    }


def _stage_verified_executable_patch(game_id: str, iso_path: Path, by_path: dict[str, list[IsoRecord]],
                                    workspace: Path, progress_callback: Progress) -> tuple[dict | None, tuple[str, IsoRecord, Path] | None]:
    """Stage an exact-hash verified executable patch, if one exists.

    Research-only recipes are reported but never written. Only an ISO executable
    byte-identical to the bundled reference can be patched. The patch core always
    creates a separate working copy; bundled references are never mutated.
    """
    status = executable_patch_core.status_for_game(game_id, Path(__file__).resolve().parent)
    recipe = executable_patch_core.get_verified_recipe_for_game(game_id, Path(__file__).resolve().parent)
    if recipe is None:
        return status, None
    target_path = str(recipe.get("target_iso_path", "")).replace("\\", "/").strip("/")
    matches = by_path.get(target_path.upper(), []) if target_path else []
    if len(matches) != 1:
        raise ValueError(
            f"Verified executable recipe '{recipe.get('recipe_id')}' could not uniquely locate '{target_path}' in the source ISO. "
            "No output was published."
        )
    record = matches[0]
    if record.directory or record.file_unit_size or record.interleave_gap or (record.flags & 0x80):
        raise ValueError(f"Executable target '{target_path}' uses an unsupported ISO layout; refusing code patching.")
    reference_path = executable_patch_core.reference_path_for_recipe(recipe, Path(__file__).resolve().parent)
    expected_hash = str(recipe.get("reference_sha256", "")).lower()
    reference_hash = executable_patch_core.sha256_path(reference_path)
    image_hash = _hash_iso_record(iso_path, record)
    if not expected_hash or reference_hash != expected_hash or image_hash != expected_hash:
        raise ValueError(
            f"Executable hash mismatch for '{target_path}'. This ISO does not contain the exact bundled game executable "
            "expected by the verified patch recipe; no code changes were applied."
        )
    progress_callback(f"Creating temporary executable copy for verified recipe {recipe.get('recipe_id')}…")
    patched_path = executable_patch_core.create_verified_patched_copy(
        recipe, workspace, Path(__file__).resolve().parent
    )
    if patched_path.stat().st_size != record.size:
        patched_path.unlink(missing_ok=True)
        raise ValueError("Verified recipe changed executable length, which this ISO integration does not permit.")
    status = {
        "status": "applied_verified_recipe",
        "applied": True,
        "recipe_id": recipe.get("recipe_id"),
        "target_iso_path": target_path,
        "reference_sha256": reference_hash,
        "patched_sha256": executable_patch_core.sha256_path(patched_path),
        "patch_count": len(recipe.get("patches", [])),
        "working_copy_only": True,
        "reason": "The exact-version executable was copied to a temporary workspace, patched with exact byte signatures, and verified before insertion.",
    }
    return status, (target_path, record, patched_path)


def _patch_direct_radio_iso(iso_path: Path, output_path: Path, staging_root: Path, game_id: str,
                            progress_callback: Progress, overwrite: bool = False,
                            reported_source: Path | None = None, station_id: str | None = None) -> dict:
    """Patch one selected VB/AT3 station and any confirmed PSP header-cache companion."""
    profile = resolve_game_radio_profile(game_id, station_id)
    if profile.get("mode") != "direct":
        raise ValueError(profile.get("research_note", f"{profile['title']} does not have a verified direct radio patch target."))
    staging = inspect_game_radio_staging(staging_root, game_id, profile.get("station_id"))
    iso_path = Path(iso_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    if not iso_path.is_file():
        raise ValueError(f"Select an existing {profile['title']} ISO image.")
    if iso_path == output_path:
        raise ValueError("The output image must differ from the source image. The source is never overwritten.")
    if output_path.suffix.lower() != ".iso":
        raise ValueError("The intermediate/output image for this operation must be a plain .iso file.")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Output image already exists: {output_path}")

    progress_callback(f"Scanning {profile['title']} ISO9660 filesystem…")
    image = Iso9660(iso_path)
    records = image.scan()
    serial = _identify_game_iso(image, game_id)
    progress_callback(f"Verified {profile['title']} disc ({serial}).")
    by_path: dict[str, list[IsoRecord]] = {}
    for rec in records:
        by_path.setdefault(rec.path.upper(), []).append(rec)

    target_path = profile["target_path"]
    matches = by_path.get(target_path.upper(), [])
    if len(matches) != 1:
        if not matches:
            raise ValueError(f"Could not find '{target_path}' in this {profile['title']} ISO. No changes were made.")
        raise ValueError(f"More than one ISO entry matches '{target_path}'. Refusing an ambiguous patch.")
    target = matches[0]
    if target.directory or target.file_unit_size or target.interleave_gap or (target.flags & 0x80):
        raise ValueError(f"ISO target '{target_path}' is a directory or uses an unsupported layout.")
    if target.size <= 0 or target.end_sector > image.total_sectors:
        raise ValueError(f"ISO target '{target_path}' has an invalid extent; refusing to patch it.")

    patch_targets: dict[str, IsoRecord] = {target_path: target}
    payloads_to_write: dict[str, Path] = {target_path: staging["payload_path"]}
    companion_path = profile.get("header_companion_path")
    header_temp_path: Path | None = None
    dependency_records: dict[str, tuple[IsoRecord, str]] = {}
    companion_report: dict | None = None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir_ctx = tempfile.TemporaryDirectory(prefix=".gta_radio_patch_work_", dir=str(output_path.parent))
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.patching")
    executable_code_patch: dict | None = None
    try:
        work_dir = Path(work_dir_ctx.__enter__())
        if companion_path:
            companion_matches = by_path.get(companion_path.upper(), [])
            if len(companion_matches) != 1:
                if not companion_matches:
                    raise ValueError(f"Could not find required PSP radio-header cache '{companion_path}'. No changes were made.")
                raise ValueError(f"More than one ISO entry matches '{companion_path}'. Refusing an ambiguous patch.")
            companion_record = companion_matches[0]
            if (companion_record.directory or companion_record.file_unit_size or companion_record.interleave_gap
                    or (companion_record.flags & 0x80) or companion_record.size > 16 * 1024 * 1024):
                raise ValueError(f"PSP radio-header cache '{companion_path}' has an unsupported layout or size.")
            slot_index = int(profile["header_slot_index"])
            block_size = int(profile.get("header_block_size", 0x800))
            slot_offset = slot_index * block_size
            if block_size != 0x800 or companion_record.size < slot_offset + block_size or companion_record.size % block_size:
                raise ValueError(
                    f"'{companion_path}' is not a valid sequence of 0x800-byte cache blocks for slot {slot_index}."
                )

            original_prefix = _read_iso_record_prefix(iso_path, target, block_size)
            original_info = _parse_at3_prefix(original_prefix, target.size, f"Original {profile['asset_filename']}")
            # Read only the 0x800-byte cache block; station streams can be tens of MB.
            with staging["payload_path"].open("rb") as replacement_stream:
                replacement_prefix = replacement_stream.read(block_size)
            replacement_info = _parse_at3_prefix(
                replacement_prefix, staging["size_bytes"], f"Replacement {profile['asset_filename']}"
            )
            if replacement_info["fmt_payload"] != original_info["fmt_payload"]:
                raise ValueError(
                    f"The replacement {profile['asset_filename']} uses different ATRAC/WAVE format parameters than the original stream. "
                    "Use a replacement encoded with matching channel, sample-rate and codec settings."
                )

            cache_bytes = bytearray(_read_iso_record_prefix(iso_path, companion_record, companion_record.size))
            original_slot = bytes(cache_bytes[slot_offset:slot_offset + block_size])
            slot_info = _parse_at3_prefix(original_slot, target.size, f"Original cache slot {slot_index} in {companion_path}")
            if slot_info["fmt_payload"] != original_info["fmt_payload"]:
                raise ValueError(
                    f"The selected 0x800-byte slot in '{companion_path}' does not match the original station's codec header. "
                    "Refusing to patch a possibly mismatched regional image."
                )
            if slot_info["data_payload_offset"] != original_info["data_payload_offset"]:
                raise ValueError(f"The selected header-cache slot layout does not match {profile['asset_filename']}; no changes were made.")
            data_offset = slot_info["data_payload_offset"]
            original_cache_metadata = original_slot[:data_offset]
            original_stream_metadata = original_prefix[:data_offset]
            if original_cache_metadata != original_stream_metadata:
                raise ValueError(
                    f"'{companion_path}' slot {slot_index} does not match the original {profile['asset_filename']} RIFF metadata. "
                    "This ISO may use another regional header-cache layout."
                )

            # The replacement may be shorter or longer. Its RIFF/fact/data headers,
            # cache prefix and ISO9660 file size are updated; the undocumented M_LOOKUP
            # table is preserved rather than modified on a guess.
            original_duration = original_info["declared_duration_seconds"]
            replacement_duration = replacement_info["declared_duration_seconds"]
            duration_delta = replacement_duration - original_duration
            cache_bytes[slot_offset:slot_offset + block_size] = replacement_prefix
            header_temp_path = work_dir / Path(companion_path).name
            header_temp_path.write_bytes(cache_bytes)
            payloads_to_write[companion_path] = header_temp_path
            patch_targets[companion_path] = companion_record
            companion_report = {
                "game_disc_path": companion_path,
                "slot_index": slot_index,
                "slot_offset_bytes": slot_offset,
                "slot_size_bytes": block_size,
                "old_size_bytes": companion_record.size,
                "new_size_bytes": len(cache_bytes),
                "source_slot_sha256": hashlib.sha256(original_slot).hexdigest(),
                "replacement_slot_sha256": hashlib.sha256(replacement_prefix).hexdigest(),
                "cache_slot_updated_from_replacement_stream_prefix": True,
                "original_stream_size_bytes": original_info["declared_total"],
                "replacement_stream_size_bytes": replacement_info["declared_total"],
                "original_fact_samples": original_info["fact_samples"],
                "replacement_fact_samples": replacement_info["fact_samples"],
                "sample_rate_hz": replacement_info["sample_rate"],
                "channels": replacement_info["channels"],
                "original_declared_duration_seconds": round(original_duration, 6),
                "replacement_declared_duration_seconds": round(replacement_duration, 6),
                "duration_delta_seconds": round(duration_delta, 6),
                "duration_changed": abs(duration_delta) > 0.01,
                "duration_handling": "Replacement RIFF/fact/data metadata and matching cache block are used; ISO9660 file size/extent is adjusted as needed. No undocumented lookup field is changed without evidence.",
            }

        dependency_path = profile.get("preserve_dependency_path")
        if dependency_path:
            dependency_matches = by_path.get(dependency_path.upper(), [])
            if len(dependency_matches) != 1:
                raise ValueError(f"Could not uniquely locate required companion file '{dependency_path}'. No changes were made.")
            dependency_record = dependency_matches[0]
            if dependency_record.directory or dependency_record.file_unit_size or dependency_record.interleave_gap or (dependency_record.flags & 0x80):
                raise ValueError(f"Companion dependency '{dependency_path}' uses an unsupported ISO layout.")
            dependency_records[dependency_path] = (dependency_record, _hash_iso_record(iso_path, dependency_record))

        executable_code_patch, staged_executable = _stage_verified_executable_patch(
            game_id, iso_path, by_path, work_dir, progress_callback
        )
        if executable_code_patch and executable_code_patch.get("status") == "research_only":
            progress_callback("Executable code patch is research-only; no game-code bytes will be changed in this build.")
        elif executable_code_patch and executable_code_patch.get("status") == "no_verified_recipe":
            progress_callback("No verified executable code patch is configured; the source executable will remain unchanged.")
        if staged_executable is not None:
            executable_path, executable_record, executable_payload = staged_executable
            if executable_path in patch_targets:
                raise ValueError(f"Executable target '{executable_path}' overlaps another patch target definition.")
            patch_targets[executable_path] = executable_record
            payloads_to_write[executable_path] = executable_payload

        # Every patched file must occupy a unique safe extent in the source image.
        for path, rec in patch_targets.items():
            if rec.size <= 0 or rec.end_sector > image.total_sectors:
                raise ValueError(f"ISO target '{path}' has an invalid extent; refusing to patch it.")
            for other in records:
                if other.record_offset == rec.record_offset or other.sectors == 0:
                    continue
                if other.data_extent < rec.end_sector and rec.data_extent < other.end_sector:
                    raise ValueError(f"ISO layout check failed: '{other.path}' overlaps '{path}'. No changes were made.")
        target_extents = sorted((r.data_extent, r.end_sector, path) for path, r in patch_targets.items())
        for left, right in zip(target_extents, target_extents[1:]):
            if left[1] > right[0]:
                raise ValueError(f"ISO target extents overlap ({left[2]} and {right[2]}). No changes were made.")

        progress_callback("Copying source ISO to a separate output image…")
        total = iso_path.stat().st_size
        copied = 0
        with iso_path.open("rb") as src, temporary.open("xb") as dst:
            while True:
                block = src.read(8 * 1024 * 1024)
                if not block:
                    break
                dst.write(block)
                copied += len(block)
                progress_callback(f"Copying ISO: {100.0 * copied / max(1, total):.0f}%")
            dst.flush()
            os.fsync(dst.fileno())
        relocated_targets: set[str] = set()
        next_free_sector = image.total_sectors
        with temporary.open("r+b") as out:
            for index, (path, payload_path) in enumerate(payloads_to_write.items(), start=1):
                progress_callback(f"Replacing {index}/{len(payloads_to_write)}: {path}…")
                next_free_sector, moved = _write_replacement(out, patch_targets[path], payload_path, next_free_sector)
                if moved:
                    relocated_targets.add(path)
            _update_volume_space_size(out, next_free_sector)
            out.flush(); os.fsync(out.fileno())

        progress_callback("Verifying patched radio stream and header-cache contents…")
        verification = Iso9660(temporary)
        verify_records = verification.scan()
        verify_serial = _identify_game_iso(verification, game_id)
        verify_by_path: dict[str, list[IsoRecord]] = {}
        for rec in verify_records:
            verify_by_path.setdefault(rec.path.upper(), []).append(rec)
        verified = []
        for path, payload_path in payloads_to_write.items():
            matches = [r for r in verify_by_path.get(path.upper(), []) if not r.directory]
            if len(matches) != 1:
                raise ValueError(f"Post-patch verification could not uniquely find {path}.")
            rec = matches[0]
            if rec.size != payload_path.stat().st_size:
                raise ValueError(f"Post-patch size verification failed for {path}.")
            digest = _hash_iso_record(temporary, rec)
            expected_digest = _sha256_path(payload_path)
            if digest != expected_digest:
                raise ValueError(f"Post-patch content hash failed for {path}.")
            verified.append({
                "game_disc_path": path,
                "old_size_bytes": patch_targets[path].size,
                "new_size_bytes": rec.size,
                "sha256": digest,
                "relocated": path in relocated_targets,
            })
        if companion_path:
            verified_cache = next(r for r in verify_records if not r.directory and r.path.upper() == companion_path.upper())
            slot_index = int(profile["header_slot_index"]); block_size = int(profile.get("header_block_size", 0x800))
            actual_slot = _read_iso_record_prefix(temporary, verified_cache, (slot_index + 1) * block_size)[slot_index * block_size:(slot_index + 1) * block_size]
            if actual_slot != replacement_prefix:
                raise ValueError(f"Post-patch cache slot verification failed for {companion_path} slot {slot_index}.")
        verified_dependencies = []
        for dependency_path, (_source_record, original_digest) in dependency_records.items():
            matches = [r for r in verify_records if not r.directory and r.path.upper() == dependency_path.upper()]
            if len(matches) != 1 or _hash_iso_record(temporary, matches[0]) != original_digest:
                raise ValueError(f"The companion dependency '{dependency_path}' changed unexpectedly during patching.")
            verified_dependencies.append({"game_disc_path": dependency_path, "preserved_unchanged": True, "sha256": original_digest})
        if verify_serial != serial:
            raise ValueError("The patched image's game identity changed unexpectedly.")
        if output_path.exists() and not overwrite:
            raise FileExistsError(f"Output image appeared during patching: {output_path}")
        os.replace(temporary, output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if header_temp_path is not None:
            header_temp_path.unlink(missing_ok=True)
        try:
            work_dir_ctx.__exit__(None, None, None)
        except Exception:
            pass

    final_warning = profile.get("patch_warning", "File-level structural verification passed. Audio playback must still be tested in the appropriate emulator/game.")
    if companion_report and companion_report.get("duration_changed"):
        if profile.get("preserve_dependency_path"):
            lookup_note = (f" {profile['preserve_dependency_path']} was preserved byte-for-byte; current static analysis "
                           "shows resource-key lookup behaviour, not a proven duration field, so it was not rewritten.")
        else:
            lookup_note = " No separate duration lookup table is defined for this selected profile."
        duration_note = (
            f"Declared duration estimate differs by {companion_report['duration_delta_seconds']:+.3f} seconds "
            f"({companion_report['original_declared_duration_seconds']:.3f}s → "
            f"{companion_report['replacement_declared_duration_seconds']:.3f}s). The replacement stream, "
            "its header-cache slot and ISO file-size metadata were updated."
            + lookup_note + " Verify station playback and next-track behaviour in the appropriate emulator."
        )
        final_warning = f"{final_warning} {duration_note}" if final_warning else duration_note
    report_path = output_path.with_suffix(output_path.suffix + ".patch_report.json")
    source_for_report = Path(reported_source or iso_path).expanduser().resolve()
    source_hash = _sha256_path(source_for_report) if source_for_report.is_file() else _sha256_path(iso_path)
    report = {
        "tool": "GTA Modding Studio PS2/PSP Radio Installer",
        "game_id": game_id, "game_title": profile["title"], "platform": profile["platform"],
        "source_image": str(source_for_report), "source_image_sha256": source_hash,
        "patched_image": str(output_path), "verified_disc_serial": serial,
        "station": profile["station"], "radio_asset": profile["asset_filename"],
        "game_disc_path": target_path, "files": verified,
        "header_cache_update": companion_report,
        "preserved_dependencies": verified_dependencies,
        "executable_code_patch": executable_code_patch or executable_patch_core.status_for_game(game_id, Path(__file__).resolve().parent),
        "patched_image_size_bytes": output_path.stat().st_size,
        "warning": final_warning,
    }
    warning = None
    try:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except OSError as exc:
        warning = f"The image was patched and verified, but its report could not be written: {exc}"
        report_path = None
    return {
        "game_id": game_id, "game_title": profile["title"], "platform": profile["platform"],
        "serial": serial, "station": profile["station"], "archive_name": profile["asset_filename"],
        "source_image": source_for_report, "output_image": output_path, "output_iso": output_path,
        "output_format": ".iso", "source_format": source_for_report.suffix.lower(),
        "output_size_bytes": output_path.stat().st_size, "report_path": report_path,
        "report_warning": warning, "files": verified,
        "header_cache_update": companion_report, "preserved_dependencies": verified_dependencies,
        "executable_code_patch": report.get("executable_code_patch"),
        "warning": final_warning,
    }


def patch_game_radio_image(source_path: Path, output_path: Path, staging_root: Path, game_id: str,
                           progress_callback: Progress = lambda _text: None,
                           overwrite: bool = False, station_id: str | None = None) -> dict:
    """Patch a prepared radio stream for a selected GTA PS2/PSP title.

    GTA SA PS2 keeps its specialized PAK/lookup patcher. Other profiles patch a
    pre-encoded VB/AT3 whole-station file. CSO and unencrypted ISZ input are
    normalized to temporary ISO files; output can be ISO, CSO or unencrypted ISZ.
    """
    profile = resolve_game_radio_profile(game_id, station_id)
    if not profile:
        raise ValueError(f"Unsupported game/platform selection: {game_id}")
    if profile["mode"] == "research_required":
        raise ValueError(profile.get("research_note", "This game's radio target has not been verified yet."))
    if profile["mode"] == "sa_pak":
        return patch_sa_ps2_radio_image(source_path, output_path, staging_root, progress_callback, overwrite)
    if profile["mode"] != "direct":
        raise ValueError(f"Unsupported radio patch mode for {profile['title']}: {profile['mode']}")
    staging = inspect_game_radio_staging(staging_root, game_id, profile.get("station_id"))
    source_path = Path(source_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError(f"Select an existing {profile['title']} ISO/CSO/ISZ image.")
    if source_path == output_path:
        raise ValueError("The output image must differ from the source image. The source is never overwritten.")
    if source_path.suffix.lower() not in {".iso", ".cso", ".isz"}:
        raise ValueError("Only .iso, .cso, or .isz input images are supported.")
    if output_path.suffix.lower() not in {".iso", ".cso", ".isz"}:
        raise ValueError("Choose .iso, .cso, or unencrypted .isz output.")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Output image already exists: {output_path}")
    source_format = source_path.suffix.lower()
    output_format = output_path.suffix.lower()
    if source_format == ".cso" or output_format == ".cso":
        if _find_maxcso() is None:
            raise FileNotFoundError("MaxCSO is required for CSO input/output. Run Install_Dependencies.bat first.")
    if source_format == ".isz" and _find_isz_tool() is None:
        raise FileNotFoundError("Bundled isz-tool.py is missing. Re-extract the complete GTA Modding Studio ZIP.")

    estimate = _probe_uncompressed_size(source_path, source_format, progress_callback)
    if estimate:
        factor = 3 if output_format in {".cso", ".isz"} and source_format in {".cso", ".isz"} else (2 if output_format in {".cso", ".isz"} or source_format in {".cso", ".isz"} else 1)
        required = int(estimate * factor + max(256 * 1024 * 1024, estimate * 0.03))
        free = shutil.disk_usage(output_path.parent).free
        if free < required:
            raise OSError(f"Not enough free disk space for safe conversion and patching. Estimated {required:,} bytes required; {free:,} bytes available.")

    if source_format == ".iso" and output_format == ".iso":
        result = _patch_direct_radio_iso(source_path, output_path, staging_root, game_id, progress_callback, overwrite, reported_source=source_path, station_id=profile.get("station_id"))
        result["source_format"] = source_format
        return result

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gta_radio_image_work_", dir=str(output_path.parent)) as temp_name:
        temp_root = Path(temp_name)
        if source_format == ".iso":
            patch_input = source_path
        else:
            patch_input = temp_root / "decompressed_source.iso"
            if source_format == ".cso":
                _run_maxcso_decompress(source_path, patch_input, progress_callback)
            else:
                _run_isz_decompress(source_path, patch_input, progress_callback)
        # The direct ISO patcher performs strict target and game identity validation.
        if output_format == ".iso":
            result = _patch_direct_radio_iso(patch_input, output_path, staging_root, game_id, progress_callback,
                                             overwrite, reported_source=source_path, station_id=profile.get("station_id"))
            result["source_format"] = source_format
            result["output_format"] = ".iso"
            return result
        patched_iso = temp_root / "patched_radio.iso"
        result = _patch_direct_radio_iso(patch_input, patched_iso, staging_root, game_id, progress_callback,
                                         overwrite=False, reported_source=source_path, station_id=profile.get("station_id"))
        if output_format == ".isz":
            compressed_candidate = temp_root / "patched_radio.isz"
            _run_isz_compress(patched_iso, compressed_candidate, progress_callback)
        else:
            compressed_candidate = temp_root / "patched_radio.cso"
            maxcso = _find_maxcso()
            _run_image_tool([str(maxcso), "--format=cso1", "--block=2048", str(patched_iso), "-o", str(compressed_candidate)],
                            "Compressing patched ISO into CSO…", progress_callback)
            if not compressed_candidate.is_file() or compressed_candidate.stat().st_size <= 0:
                raise RuntimeError("MaxCSO returned success but did not create a non-empty CSO output.")
            verified_iso = temp_root / "verified_cso.iso"
            _run_maxcso_decompress(compressed_candidate, verified_iso, progress_callback)
            if _sha256_path(verified_iso) != _sha256_path(patched_iso):
                raise ValueError("CSO recompression verification failed: decompressed output differs from the patched ISO.")
        if output_path.exists() and not overwrite:
            raise FileExistsError(f"Output image appeared during patching: {output_path}")
        os.replace(compressed_candidate, output_path)
        # Replace the temporary ISO's report with one beside the final image.
        report_path = output_path.with_suffix(output_path.suffix + ".patch_report.json")
        report = {
            "tool": "GTA Modding Studio PS2/PSP Radio Installer", "game_id": game_id,
            "game_title": profile["title"], "platform": profile["platform"],
            "source_image": str(source_path), "patched_image": str(output_path),
            "output_format": output_format, "verified_disc_serial": result["serial"],
            "station": profile["station"], "radio_asset": profile["asset_filename"],
            "game_disc_path": profile["target_path"], "patched_image_size_bytes": output_path.stat().st_size,
            "full_image_roundtrip_sha256_verified": True, "files": result["files"],
            "header_cache_update": result.get("header_cache_update"),
            "preserved_dependencies": result.get("preserved_dependencies", []),
            "warning": result.get("warning") or "Structural verification passed. Test radio playback in the appropriate game/emulator.",
        }
        warning = None
        try:
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        except OSError as exc:
            warning = f"Patched {output_format.upper()} created and verified, but report could not be written: {exc}"
            report_path = None
        result.update({"source_image": source_path, "output_image": output_path, "output_iso": output_path,
                       "source_format": source_format, "output_format": output_format, "output_size_bytes": output_path.stat().st_size,
                       "report_path": report_path, "report_warning": warning})
        return result


def _build_test_iso(path: Path) -> None:
    """Build a minimal synthetic ISO9660 image for the module self-test."""
    total_sectors = 27
    image = bytearray(total_sectors * SECTOR_SIZE)

    def both32(value: int) -> bytes:
        return struct.pack("<I", value) + struct.pack(">I", value)

    def record(identifier: bytes, extent: int, size: int, flags: int = 0, ext_attr: int = 0) -> bytes:
        length = 33 + len(identifier) + (1 if len(identifier) % 2 == 0 else 0)
        raw = bytearray(length)
        raw[0] = length
        raw[1] = ext_attr
        raw[2:10] = both32(extent)
        raw[10:18] = both32(size)
        raw[18:25] = bytes([126, 1, 1, 0, 0, 0, 0])
        raw[25] = flags
        raw[26] = 0
        raw[27] = 0
        raw[28:32] = struct.pack("<H", 1) + struct.pack(">H", 1)
        raw[32] = len(identifier)
        raw[33:33 + len(identifier)] = identifier
        return bytes(raw)

    def directory_sector(sector: int, records: list[bytes]) -> None:
        content = b"".join(records)
        if len(content) > SECTOR_SIZE:
            raise AssertionError("Synthetic test directory does not fit in one sector")
        start = sector * SECTOR_SIZE
        image[start:start + len(content)] = content

    pvd = memoryview(image)[16 * SECTOR_SIZE:17 * SECTOR_SIZE]
    pvd[0] = 1; pvd[1:6] = b"CD001"; pvd[6] = 1
    pvd[80:88] = both32(total_sectors)
    pvd[120:124] = struct.pack("<H", 1) + struct.pack(">H", 1)
    pvd[124:128] = struct.pack("<H", 1) + struct.pack(">H", 1)
    pvd[128:132] = struct.pack("<H", SECTOR_SIZE) + struct.pack(">H", SECTOR_SIZE)
    pvd[156:190] = record(b"\x00", 20, SECTOR_SIZE, flags=2)
    pvd[190:318] = bytes(128)
    term = memoryview(image)[17 * SECTOR_SIZE:18 * SECTOR_SIZE]
    term[0] = 255; term[1:6] = b"CD001"; term[6] = 1

    directory_sector(20, [record(b"\x00", 20, SECTOR_SIZE, 2), record(b"\x01", 20, SECTOR_SIZE, 2),
                          record(b"SYSTEM.CNF;1", 24, 40), record(b"AUDIO", 21, SECTOR_SIZE, 2)])
    directory_sector(21, [record(b"\x00", 21, SECTOR_SIZE, 2), record(b"\x01", 20, SECTOR_SIZE, 2),
                          record(b"STREAMS", 22, SECTOR_SIZE, 2), record(b"CONFIG", 23, SECTOR_SIZE, 2)])
    directory_sector(22, [record(b"\x00", 22, SECTOR_SIZE, 2), record(b"\x01", 21, SECTOR_SIZE, 2),
                          record(b"TK.PAK;1", 25, 1000)])
    directory_sector(23, [record(b"\x00", 23, SECTOR_SIZE, 2), record(b"\x01", 21, SECTOR_SIZE, 2),
                          record(b"TRAKLKUP.DAT;1", 26, 100)])
    image[24 * SECTOR_SIZE:24 * SECTOR_SIZE + 40] = b"BOOT2 = cdrom0:\\SLUS_209.46;1\n".ljust(40, b"\x00")
    image[25 * SECTOR_SIZE:25 * SECTOR_SIZE + 1000] = b"O" * 1000
    image[26 * SECTOR_SIZE:26 * SECTOR_SIZE + 100] = b"L" * 100
    path.write_bytes(image)



def _make_test_at3(total_size: int, include_smpl: bool, seed: int = 0x37) -> bytes:
    """Build a deterministic RIFF/ATRAC-like test stream for cache integration tests."""
    fmt_payload = bytes.fromhex(
        "fe ff 02 00 44 ac 00 00 1a 2f 00 00 30 02 00 00 22 00 00 08 03 00 00 00 bf aa 23 e9 58 cb 71 44 a1 19 ff fa 01 e4 ce 62 01 00 28 45 00 00 00 00 00 00 00 00"
    )
    assert len(fmt_payload) == 52
    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_payload)) + fmt_payload
    fact_payload = struct.pack("<II", max(1, total_size // 8), 0)
    fact_chunk = b"fact" + struct.pack("<I", len(fact_payload)) + fact_payload
    smpl_chunk = b"smpl" + struct.pack("<I", 60) + bytes((seed + i) & 0xFF for i in range(60)) if include_smpl else b""
    data_offset = 12 + len(fmt_chunk) + len(fact_chunk) + len(smpl_chunk) + 8
    data_size = total_size - data_offset
    if data_size <= 0 or data_size & 1:
        raise ValueError("Synthetic AT3 stream size must produce an even, positive payload.")
    data = bytes((seed + i * 17) & 0xFF for i in range(data_size))
    riff_body = b"WAVE" + fmt_chunk + fact_chunk + smpl_chunk + b"data" + struct.pack("<I", data_size) + data
    out = b"RIFF" + struct.pack("<I", len(riff_body)) + riff_body
    assert len(out) == total_size
    return out


def _build_direct_profile_test_iso(path: Path, game_id: str, station_id: str | None = None) -> None:
    """Build a tiny ISO with a selected station target and its companions."""
    profile = resolve_game_radio_profile(game_id, station_id)
    target_parts = tuple(profile["target_path"].split("/"))
    identity_parts = tuple(profile["identity_file"].split("/"))
    file_keys = [identity_parts, target_parts]
    companion_path = profile.get("header_companion_path")
    dependency_path = profile.get("preserve_dependency_path")
    if companion_path:
        file_keys.append(tuple(companion_path.split("/")))
    if dependency_path:
        file_keys.append(tuple(dependency_path.split("/")))
    dirs: list[tuple[str, ...]] = [()]
    for parts in file_keys:
        for i in range(1, len(parts)):
            node = parts[:i]
            if node not in dirs:
                dirs.append(node)
    dir_sector = {node: 20 + i for i, node in enumerate(dirs)}

    if profile["platform"] == "PSP":
        include_smpl = game_id == "gta_lcs_psp"
        target_data = _make_test_at3(0x1800, include_smpl, seed=0x21)
    else:
        target_data = b"ORIGINAL-RADIO-DATA".ljust(64, b"\x00")
    serial = sorted(profile["serials"])[0]
    if profile["platform"] == "PSP":
        identity_data = b"\x00DISC_ID\x00" + serial.encode("ascii") + b"\x00"
    else:
        prefix, number = serial.split("-")
        identity_data = f"BOOT2 = cdrom0:\\{prefix}_{number[:3]}.{number[3:]};1\n".encode("ascii")

    payload_by_key = {identity_parts: identity_data, target_parts: target_data}
    if companion_path:
        slot_count = 10
        slot_size = int(profile.get("header_block_size", 0x800))
        blocks = []
        for index in range(slot_count):
            if index == int(profile["header_slot_index"]):
                block = target_data[:slot_size]
            else:
                dummy = _make_test_at3(0x1800 + index * 0x100, include_smpl, seed=0x40 + index)
                block = dummy[:slot_size]
            blocks.append(block)
        payload_by_key[tuple(companion_path.split("/"))] = b"".join(blocks)
    if dependency_path:
        seed_bytes = bytes(range(256))
        payload_by_key[tuple(dependency_path.split("/"))] = (seed_bytes * ((7008 + 255) // 256))[:7008]

    next_sector = 20 + len(dirs)
    file_targets: dict[tuple[str, ...], tuple[str, int, bytes]] = {}
    for key, data in payload_by_key.items():
        extent = next_sector
        next_sector += max(1, (len(data) + SECTOR_SIZE - 1) // SECTOR_SIZE)
        file_targets[key] = (key[-1], extent, data)
    total_sectors = next_sector
    image = bytearray(total_sectors * SECTOR_SIZE)

    def both32(value: int) -> bytes:
        return struct.pack("<I", value) + struct.pack(">I", value)

    def record(identifier: bytes, extent: int, size: int, flags: int = 0) -> bytes:
        length = 33 + len(identifier) + (1 if len(identifier) % 2 == 0 else 0)
        raw = bytearray(length)
        raw[0] = length
        raw[2:10] = both32(extent)
        raw[10:18] = both32(size)
        raw[18:25] = bytes([126, 1, 1, 0, 0, 0, 0])
        raw[25] = flags
        raw[28:32] = struct.pack("<H", 1) + struct.pack(">H", 1)
        raw[32] = len(identifier)
        raw[33:33 + len(identifier)] = identifier
        return bytes(raw)

    pvd = memoryview(image)[16 * SECTOR_SIZE:17 * SECTOR_SIZE]
    pvd[0] = 1; pvd[1:6] = b"CD001"; pvd[6] = 1
    pvd[80:88] = both32(total_sectors)
    pvd[120:124] = struct.pack("<H", 1) + struct.pack(">H", 1)
    pvd[124:128] = struct.pack("<H", 1) + struct.pack(">H", 1)
    pvd[128:132] = struct.pack("<H", SECTOR_SIZE) + struct.pack(">H", SECTOR_SIZE)
    pvd[156:190] = record(b"\x00", dir_sector[()], SECTOR_SIZE, flags=2)
    term = memoryview(image)[17 * SECTOR_SIZE:18 * SECTOR_SIZE]
    term[0] = 255; term[1:6] = b"CD001"; term[6] = 1

    for directory in dirs:
        dir_records = [record(b"\x00", dir_sector[directory], SECTOR_SIZE, flags=2)]
        parent = directory[:-1] if directory else ()
        dir_records.append(record(b"\x01", dir_sector[parent], SECTOR_SIZE, flags=2))
        for child in dirs:
            if child and len(child) == len(directory) + 1 and child[:-1] == directory:
                dir_records.append(record(child[-1].encode("ascii"), dir_sector[child], SECTOR_SIZE, flags=2))
        for file_key, (filename, extent, data) in file_targets.items():
            if file_key[:-1] == directory:
                dir_records.append(record((filename + ";1").encode("ascii"), extent, len(data), flags=0))
        content = b"".join(dir_records)
        if len(content) > SECTOR_SIZE:
            raise AssertionError("Synthetic test directory overflowed its sector")
        start = dir_sector[directory] * SECTOR_SIZE
        image[start:start + len(content)] = content

    for _key, (_filename, extent, data) in file_targets.items():
        start = extent * SECTOR_SIZE
        image[start:start + len(data)] = data
    path.write_bytes(image)


def _test_generic_game_radio_patch(root: Path, game_id: str, station_id: str | None = None) -> None:
    profile = resolve_game_radio_profile(game_id, station_id)
    test_name = f"{game_id}_{profile.get('station_id') or profile['asset_filename']}"
    source_iso = root / f"{test_name}_source.iso"
    _build_direct_profile_test_iso(source_iso, game_id, profile.get("station_id"))
    source_hash = _sha256_path(source_iso)
    source_radio = root / f"{test_name}_{profile['asset_filename']}"
    if profile["platform"] == "PSP":
        # Alternate short/long station replacements to verify variable duration
        # metadata and ISO directory sizing for every slot, not just one direction.
        replacement_size = 0x800 if int(profile.get("header_slot_index", 0)) % 2 == 0 else 0x3000
        replacement = _make_test_at3(replacement_size, game_id == "gta_lcs_psp", seed=0x73)
    else:
        replacement = (f"REPLACEMENT RADIO FOR {game_id}\n".encode("ascii") * 600)
    source_radio.write_bytes(replacement)
    stage = root / f"{test_name}_staging"
    staged = prepare_game_radio_staging(source_radio, stage, game_id, station_id=profile.get("station_id"))
    assert inspect_game_radio_staging(stage, game_id, profile.get("station_id"))["sha256"] == hashlib.sha256(replacement).hexdigest()
    try:
        inspect_game_radio_staging(stage, "gta_sa_ps2")
    except ValueError:
        pass
    else:
        raise AssertionError("Direct-game staging should not validate as a GTA SA PAK manifest")
    output = root / f"{test_name}_patched.iso"
    progress: list[str] = []
    result = patch_game_radio_image(source_iso, output, stage, game_id, progress.append, station_id=profile.get("station_id"))
    assert output.is_file() and result["game_id"] == game_id
    assert result["serial"] in profile["serials"]
    assert result["files"][0]["sha256"] == hashlib.sha256(replacement).hexdigest()
    assert result["files"][0]["relocated"] == (len(replacement) > 0x1800)
    assert result["report_path"].is_file()
    if profile["platform"] == "PSP":
        cache_report = result.get("header_cache_update")
        assert cache_report is not None and cache_report["duration_changed"] is True
        assert cache_report["replacement_stream_size_bytes"] == len(replacement)
        assert cache_report["replacement_declared_duration_seconds"] != cache_report["original_declared_duration_seconds"]
    assert _sha256_path(source_iso) == source_hash
    verify = Iso9660(output); records = verify.scan()
    _identify_game_iso(verify, game_id)
    match = [r for r in records if r.path.upper() == profile["target_path"].upper() and not r.directory]
    assert len(match) == 1 and match[0].size == len(replacement)
    with output.open("rb") as handle:
        handle.seek(match[0].data_extent * SECTOR_SIZE)
        assert hashlib.sha256(handle.read(match[0].size)).hexdigest() == hashlib.sha256(replacement).hexdigest()


def run_self_test() -> None:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="sa_ps2_iso_patch_test_") as temp:
        root = Path(temp)
        source_iso = root / "source.iso"
        _build_test_iso(source_iso)
        source_hash_before = _sha256_path(source_iso)
        stage = root / "prepared"
        (stage / "AUDIO" / "STREAMS").mkdir(parents=True)
        (stage / "AUDIO" / "CONFIG").mkdir(parents=True)
        pak = stage / "AUDIO" / "STREAMS" / "TK.PAK"
        lookup = stage / "AUDIO" / "CONFIG" / "TRAKLKUP.DAT"
        pak.write_bytes(b"NEW-PAK-" * 400)  # larger than the original allocation: relocation required
        lookup.write_bytes(b"NEW-LOOKUP" * 20)
        manifest = {
            "tool": "GTA Modding Studio v0.3.8",
            "purpose": "Prepared files for GTA San Andreas PS2 ISO patching; staging does not modify an ISO.",
            "archive": {"filename": "TK.PAK", "archive_id": "0x0F"},
            "files": [
                {"game_disc_path": "AUDIO/STREAMS/TK.PAK", "staged_path": str(pak), "size_bytes": pak.stat().st_size, "sha256": _sha256_path(pak)},
                {"game_disc_path": "AUDIO/CONFIG/TRAKLKUP.DAT", "staged_path": str(lookup), "size_bytes": lookup.stat().st_size, "sha256": _sha256_path(lookup)},
            ]
        }
        (stage / "ISO_PATCH_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
        output_iso = root / "patched.iso"
        progress: list[str] = []
        result = patch_sa_ps2_radio_iso(source_iso, output_iso, stage, progress.append)
        assert output_iso.is_file() and output_iso != source_iso
        assert source_iso.stat().st_size == 27 * SECTOR_SIZE
        assert _sha256_path(source_iso) == source_hash_before
        assert result["serial"] == "SLUS-20946"
        assert len(result["files"]) == 2 and result["files"][0]["relocated"] is True
        patched = Iso9660(output_iso); patched.scan()
        assert patched.total_sectors > 27
        assert "Verifying patched ISO contents…" in progress
        assert result["report_path"].is_file()

        # Exercise CSO and ISZ integration deterministically without requiring the
        # external tools during self-test. These shims emulate lossless converter
        # commands while the ISO patcher and final hash checks remain real.
        from unittest.mock import patch as mock_patch
        fake_maxcso = root / "fake-maxcso.exe"
        fake_isz_tool = root / "fake-isz-tool.py"

        def pack_fake_cso(iso_path: Path, cso_path: Path) -> None:
            data = iso_path.read_bytes()
            header = b"CISO" + struct.pack("<I", 24) + struct.pack("<Q", len(data)) + struct.pack("<I", SECTOR_SIZE) + bytes((1, 0, 0, 0))
            cso_path.write_bytes(header + data)

        def fake_image_tool(args, label, progress_callback, cwd=None):
            progress_callback(label)
            if label.startswith("Compressing patched ISO into CSO"):
                pack_fake_cso(Path(args[args.index("-o") - 1]), Path(args[args.index("-o") + 1]))
            elif label.startswith("Decompressing CSO image"):
                source = Path(args[args.index("--decompress") + 1])
                destination = Path(args[args.index("-o") + 1])
                raw = source.read_bytes()
                assert raw[:4] == b"CISO" and len(raw) >= 24
                destination.write_bytes(raw[24:])
            elif label.startswith("Decompressing ISZ image"):
                source = Path(args[-2]); destination = Path(args[-1])
                raw = source.read_bytes()
                assert raw.startswith(b"FAKEISZ")
                destination.write_bytes(raw[7:])
            else:
                raise AssertionError(f"Unexpected mocked image tool action: {label}")
            return "mock converter OK"

        cso_source = root / "source.cso"
        pack_fake_cso(source_iso, cso_source)
        cso_input_snapshot = _sha256_path(cso_source)
        with mock_patch(f"{__name__}._find_maxcso", return_value=fake_maxcso), \
             mock_patch(f"{__name__}._run_image_tool", side_effect=fake_image_tool):
            cso_to_iso = patch_sa_ps2_radio_image(cso_source, root / "from_cso.iso", stage, progress.append)
            assert cso_to_iso["source_format"] == ".cso" and cso_to_iso["output_format"] == ".iso"
            assert cso_to_iso["output_iso"].is_file() and cso_to_iso["report_path"].is_file()
            iso_to_cso = patch_sa_ps2_radio_image(source_iso, root / "from_iso.cso", stage, progress.append)
            assert iso_to_cso["source_format"] == ".iso" and iso_to_cso["output_format"] == ".cso"
            assert iso_to_cso["output_iso"].read_bytes()[:4] == b"CISO"
            assert iso_to_cso["report_path"].is_file() and iso_to_cso["report_path"].read_text(encoding="utf-8").find('"full_image_roundtrip_sha256_verified": true') >= 0
            assert _sha256_path(cso_source) == cso_input_snapshot

        isz_source = root / "source.isz"
        isz_source.write_bytes(b"FAKEISZ" + source_iso.read_bytes())
        isz_snapshot = _sha256_path(isz_source)
        with mock_patch(f"{__name__}._find_isz_tool", return_value=fake_isz_tool), \
             mock_patch(f"{__name__}._find_maxcso", return_value=fake_maxcso), \
             mock_patch(f"{__name__}._isz_info", return_value="ISZ version 1, No password, uncompressed size=0.05 MB"), \
             mock_patch(f"{__name__}._probe_uncompressed_size", return_value=source_iso.stat().st_size), \
             mock_patch(f"{__name__}._run_image_tool", side_effect=fake_image_tool):
            isz_result = patch_sa_ps2_radio_image(isz_source, root / "from_isz.iso", stage, progress.append)
            assert isz_result["source_format"] == ".isz" and isz_result["output_format"] == ".iso"
            assert isz_result["output_iso"].is_file() and isz_result["report_path"].is_file()
            isz_to_cso = patch_sa_ps2_radio_image(isz_source, root / "from_isz.cso", stage, progress.append)
            assert isz_to_cso["source_format"] == ".isz" and isz_to_cso["output_format"] == ".cso"
            assert isz_to_cso["output_iso"].read_bytes()[:4] == b"CISO"
            assert isz_to_cso["report_path"].is_file()
            assert _sha256_path(isz_source) == isz_snapshot

        # Compressed-image workflow also dispatches correctly for a PSP whole-station profile.
        direct_game = "gta_lcs_psp"
        direct_iso = root / "lcs_psp_source.iso"
        _build_direct_profile_test_iso(direct_iso, direct_game)
        direct_stream = root / "KJAH.AT3"
        direct_stream.write_bytes(_make_test_at3(0x3000, True, seed=0x73))
        direct_stage = root / "lcs_psp_stage"
        prepare_game_radio_staging(direct_stream, direct_stage, direct_game)
        direct_cso = root / "lcs_psp_source.cso"
        pack_fake_cso(direct_iso, direct_cso)
        direct_cso_before = _sha256_path(direct_cso)
        with mock_patch(f"{__name__}._find_maxcso", return_value=fake_maxcso), \
             mock_patch(f"{__name__}._find_isz_tool", return_value=fake_isz_tool), \
             mock_patch(f"{__name__}._isz_info", return_value="ISZ version 1, No password, uncompressed size=0.05 MB"), \
             mock_patch(f"{__name__}._probe_uncompressed_size", return_value=direct_iso.stat().st_size), \
             mock_patch(f"{__name__}._run_image_tool", side_effect=fake_image_tool):
            direct_cso_result = patch_game_radio_image(direct_cso, root / "lcs_psp_from_cso.cso", direct_stage, direct_game, progress.append)
            assert direct_cso_result["output_format"] == ".cso" and direct_cso_result["report_path"].is_file()
            assert _sha256_path(direct_cso) == direct_cso_before
            direct_isz = root / "lcs_psp_source.isz"
            direct_isz.write_bytes(b"FAKEISZ" + direct_iso.read_bytes())
            direct_isz_before = _sha256_path(direct_isz)
            direct_isz_result = patch_game_radio_image(direct_isz, root / "lcs_psp_from_isz.iso", direct_stage, direct_game, progress.append)
            assert direct_isz_result["source_format"] == ".isz" and direct_isz_result["report_path"].is_file()
            assert _sha256_path(direct_isz) == direct_isz_before

        encrypted_isz = root / "encrypted.isz"
        encrypted_isz.write_bytes(isz_source.read_bytes())
        with mock_patch(f"{__name__}._find_isz_tool", return_value=fake_isz_tool), \
             mock_patch(f"{__name__}._probe_uncompressed_size", return_value=source_iso.stat().st_size), \
             mock_patch(f"{__name__}._isz_info", side_effect=ValueError("This ISZ image is encrypted or password-protected.")):
            try:
                patch_sa_ps2_radio_image(encrypted_isz, root / "encrypted_output.iso", stage, progress.append)
            except ValueError as exc:
                assert "encrypted" in str(exc).lower()
            else:
                raise AssertionError("Encrypted ISZ input should be rejected clearly")
            assert not (root / "encrypted_output.iso").exists()

        # A bad staging hash must prevent patching.
        data = json.loads((stage / "ISO_PATCH_MANIFEST.json").read_text(encoding="utf-8"))
        data["files"][0]["sha256"] = "0" * 64
        (stage / "ISO_PATCH_MANIFEST.json").write_text(json.dumps(data), encoding="utf-8")
        try:
            patch_sa_ps2_radio_iso(source_iso, root / "should_not_exist.iso", stage)
        except ValueError as exc:
            assert "hash" in str(exc).lower()
        else:
            raise AssertionError("A mismatched staging hash should be rejected")

        # Executable-led caution: VCS PS2 advertises AUDIO/M_LOOKUP.BIN and
        # radio_1..radio_9 rather than a verified VCPR.VB target. Never let the
        # generic direct-stream patcher write to an unconfirmed path.
        vcs_profile = GAME_RADIO_PROFILES["gta_vcs_ps2"]
        assert vcs_profile["mode"] == "research_required"
        assert vcs_profile["target_path"] is None
        try:
            prepare_game_radio_staging(root / "fake_vcpr.vb", root / "vcs_stage", "gta_vcs_ps2")
        except (ValueError, FileNotFoundError) as exc:
            assert "VCS PS2" in str(exc) or "VCS PS2" in vcs_profile["title"]
        else:
            raise AssertionError("VCS PS2 staging must stay disabled until its radio target is verified")
        try:
            patch_game_radio_image(root / "not_an_image.iso", root / "not_patched.iso", root / "vcs_stage", "gta_vcs_ps2")
        except ValueError as exc:
            assert "AUDIO/M_LOOKUP.BIN" in str(exc) or "target" in str(exc).lower()
        else:
            raise AssertionError("VCS PS2 patching must reject an unconfirmed target")

        # Direct stream profiles verify platform-specific identity parsing and
        # whole-station VB/AT3 replacement without touching the source image.
        _test_generic_game_radio_patch(root, "gta3_ps2")
        for station in LCS_PSP_STATIONS:
            _test_generic_game_radio_patch(root, "gta_lcs_psp", station["id"])
        for station in VCS_PSP_EU_STATIONS:
            _test_generic_game_radio_patch(root, "gta_vcs_psp", station["id"])
    print("PS2/PSP ISO patcher self-test passed: SA PAK/lookup patching, direct PS2 VB replacement, all 10 LCS PSP and 9 VCS PSP AT3 stations with matching header-cache updates, M_LOOKUP preservation, duration metadata reporting, CSO/ISZ workflows, manifests, relocation, and source-image preservation.")


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        run_self_test()
    else:
        print("This module is a backend for GTA Modding Studio. Launch GTA_Modding_Studio.py instead.")
