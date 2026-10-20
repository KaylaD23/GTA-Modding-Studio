#!/usr/bin/env python3
"""SAAT PS2 Studio v1.3 - GTA San Andreas PS2 Radio PAK Tool

Extracts/rebuilds PS2 streamed-audio PAK files using AUDIO/CONFIG/TRAKLKUP.DAT.
Standard-library only (Tkinter GUI).

Important: GTA SA PS2 audio packages can vary between regional/revision builds.
This tool validates candidate offsets against VAGp headers and offers manual
interpretation overrides. It always writes NEW-* outputs and never overwrites
source files. Test the rebuilt ISO in PCSX2 before using it elsewhere.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

RECORD_SIZE = 12
SECTOR_SIZE = 2048
VAG_HEADER_SIZE = 0x30
VAG_MAGIC = b"VAGp"
GTASA_STREAM_HEADER_SIZE = 8068
GTASA_STREAM_RATE_OFFSETS = (0x1F54, 0x1F5C)
PS2_RADIO_SAMPLE_RATE = 24000

ARCHIVES = {
    "AA.PAK": "Police / ambient radio",
    "ADVERTS.PAK": "Advertisements",
    "AMBIENCE.PAK": "Ambience",
    "BEATS.PAK": "Beats / user music data",
    "CH.PAK": "Playback FM",
    "CO.PAK": "K-Rose",
    "CR.PAK": "K-DST",
    "CUTSCENE.PAK": "Cutscene audio",
    "DS.PAK": "Bounce FM",
    "HC.PAK": "SF-UR",
    "MH.PAK": "Radio Los Santos",
    "MR.PAK": "Radio X",
    "NJ.PAK": "CSR 103.9",
    "RE.PAK": "K-Jah Radio West",
    "RG.PAK": "Master Sounds 98.3",
    "TK.PAK": "WCTR",
}
# PS2-specific lookup IDs, verified against the user's supplied PS2 TRAKLKUP.DAT.
# Unlike the PC SAAT import table described in its manual, this PS2 table is compact:
# IDs 0x00..0x0F map to the 16 PS2 PAKs with no unused 0x02 slot.
# The per-group record counts in the reference table match this mapping.
KNOWN_ARCHIVE_IDS = {
    "AA.PAK": 0x00,
    "ADVERTS.PAK": 0x01,
    "AMBIENCE.PAK": 0x02,
    "BEATS.PAK": 0x03,
    "CH.PAK": 0x04,
    "CO.PAK": 0x05,
    "CR.PAK": 0x06,
    "CUTSCENE.PAK": 0x07,
    "DS.PAK": 0x08,
    "HC.PAK": 0x09,
    "MH.PAK": 0x0A,
    "MR.PAK": 0x0B,
    "NJ.PAK": 0x0C,
    "RE.PAK": 0x0D,
    "RG.PAK": 0x0E,
    "TK.PAK": 0x0F,
}
ARCHIVE_FILENAME_BY_ID = {value: key for key, value in KNOWN_ARCHIVE_IDS.items()}


class FormatError(Exception):
    pass


class ExistingStagingFilesError(FileExistsError):
    """Raised when the ISO-patching staging target already contains output files."""


@dataclass
class LookupRecord:
    index: int
    archive_id: int
    marker: bytes
    offset_value: int
    size_value: int

    @property
    def table_pos(self) -> int:
        return self.index * RECORD_SIZE


@dataclass
class TrackInfo:
    record: LookupRecord
    offset_bytes: int
    stored_size_bytes: Optional[int]
    exact_size: Optional[int]
    is_vag: bool
    sample_rate: Optional[int] = None
    payload_size: Optional[int] = None
    has_stream_header: bool = False
    stream_header_size: int = 0


def read_u32le(data: bytes, pos: int) -> int:
    return struct.unpack_from("<I", data, pos)[0]


def write_u32le(data: bytearray, pos: int, val: int) -> None:
    if not 0 <= val <= 0xFFFFFFFF:
        raise FormatError(f"Lookup value out of 32-bit range: {val}")
    struct.pack_into("<I", data, pos, val)


def parse_lookup(path: Path) -> tuple[bytes, list[LookupRecord]]:
    data = path.read_bytes()
    if len(data) == 0 or len(data) % RECORD_SIZE:
        raise FormatError(
            f"TRAKLKUP.DAT is {len(data):,} bytes. Expected a non-empty file whose size is a multiple of {RECORD_SIZE} bytes."
        )
    records: list[LookupRecord] = []
    for i in range(len(data) // RECORD_SIZE):
        p = i * RECORD_SIZE
        records.append(LookupRecord(
            index=i,
            archive_id=data[p],
            marker=data[p + 1:p + 4],
            offset_value=read_u32le(data, p + 4),
            size_value=read_u32le(data, p + 8),
        ))
    return data, records


def vag_header(blob: bytes, offset: int = 0) -> Optional[dict]:
    """Return parsed VAGp details if header is sane."""
    if offset < 0 or offset + VAG_HEADER_SIZE > len(blob):
        return None
    if blob[offset:offset + 4] != VAG_MAGIC:
        return None
    payload = struct.unpack_from(">I", blob, offset + 0x0C)[0]
    rate = struct.unpack_from(">I", blob, offset + 0x10)[0]
    # Real PS2 VAG ADPCM payload lengths are normally frame-aligned by 16 bytes.
    # Reject absurd values but allow a zero-rate header (some tools omit it).
    if payload > (len(blob) - offset - VAG_HEADER_SIZE):
        return None
    if payload > 0 and payload % 16 != 0:
        # Some encoders may include a small non-frame-aligned tail; don't fully reject.
        if payload + 16 > (len(blob) - offset - VAG_HEADER_SIZE):
            return None
    return {"payload_size": payload, "exact_size": VAG_HEADER_SIZE + payload, "sample_rate": rate}


def gta_sa_stream_header(blob: bytes, offset: int = 0) -> Optional[dict]:
    """Recognize the 8,068-byte GTA SA PS2 stream record header.

    In the PS2 stream PAK format, TRAKLKUP offsets point to an 8,068-byte
    metadata header, while the table size describes only the ADPCM payload after
    that header. The PS2 variant uses a 0xFFFFFFFF marker and a 4-byte terminal
    tag (04 00 00 00). For the supplied original game archive, the DWORDs at
    0x1F54 and 0x1F5C both contain 24,000 for all 101 entries. Replacement audio
    must be encoded at a rate consistent with these preserved header fields.
    """
    end = offset + GTASA_STREAM_HEADER_SIZE
    if offset < 0 or end > len(blob):
        return None
    h = blob[offset:end]
    if h[:4] != b"\xff\xff\xff\xff":
        return None
    if h[0x1F80:0x1F84] != b"\x04\x00\x00\x00":
        return None
    rate_a = struct.unpack_from("<I", h, GTASA_STREAM_RATE_OFFSETS[0])[0]
    rate_b = struct.unpack_from("<I", h, GTASA_STREAM_RATE_OFFSETS[1])[0]
    # Use the matching values present in the game's real stream headers. The
    # supplied TK.PAK has 24,000 in both locations for every inspected entry.
    # A conflicting or implausible pair should not accidentally force the
    # replacement encoder to a rate that disagrees with the preserved header.
    if rate_a == rate_b and 8000 <= rate_a <= 48000:
        playback_rate = rate_a
    else:
        playback_rate = PS2_RADIO_SAMPLE_RATE
    return {
        "header_size": GTASA_STREAM_HEADER_SIZE,
        "sample_rate": playback_rate,
        "header_rate_values": (rate_a, rate_b),
    }


def offset_to_bytes(value: int, unit: str) -> int:
    if unit == "Bytes":
        return value
    if unit == "2048-byte LBA sectors":
        return value * SECTOR_SIZE
    raise ValueError(unit)


def size_to_bytes(value: int, unit: str) -> int:
    if unit == "Bytes":
        return value
    if unit == "2048-byte LBA sectors":
        return value * SECTOR_SIZE
    raise ValueError(unit)


def infer_length_unit(records: list[LookupRecord], pak: bytes, off_unit: str) -> str:
    """Infer whether the size field stores bytes or 2048-byte sector counts."""
    scores = {"Bytes": 0, "2048-byte LBA sectors": 0}
    checked = 0
    for rec in records:
        off = offset_to_bytes(rec.offset_value, off_unit)
        if off < 0 or off >= len(pak):
            continue
        vh = vag_header(pak, off)
        if not vh:
            continue
        checked += 1
        exact = vh["exact_size"]
        payload = vh["payload_size"]
        candidates = {
            "Bytes": {exact, payload},
            "2048-byte LBA sectors": {
                math.ceil(exact / SECTOR_SIZE), math.ceil(payload / SECTOR_SIZE),
                (exact + SECTOR_SIZE - 1) // SECTOR_SIZE,
            },
        }
        for u in scores:
            if rec.size_value in candidates[u]:
                scores[u] += 3
            else:
                # Some builds store allocated sector span; allow padded track lengths.
                size_bytes = rec.size_value if u == "Bytes" else rec.size_value * SECTOR_SIZE
                if exact <= size_bytes <= exact + SECTOR_SIZE:
                    scores[u] += 1
    if checked == 0:
        # Raw PS2 stream fragments often have no VAGp headers. Infer the size unit
        # from whether the lookup ranges fit this PAK and stop before the next entry.
        raw_scores = {"Bytes": 0.0, "2048-byte LBA sectors": 0.0}
        ordered = sorted(records, key=lambda r: offset_to_bytes(r.offset_value, off_unit))
        for i, rec in enumerate(ordered):
            off = offset_to_bytes(rec.offset_value, off_unit)
            next_off = offset_to_bytes(ordered[i + 1].offset_value, off_unit) if i + 1 < len(ordered) else len(pak)
            for unit in raw_scores:
                size = size_to_bytes(rec.size_value, unit)
                if size > 0 and 0 <= off < len(pak) and off + size <= len(pak):
                    raw_scores[unit] += 2.0
                    if off + size <= next_off + SECTOR_SIZE:
                        raw_scores[unit] += 1.0
        if max(raw_scores.values()) > 0:
            return max(raw_scores, key=raw_scores.get)
        return "Bytes"
    return max(scores, key=scores.get)


def detect_raw_group_by_extent(pak: bytes, records: list[LookupRecord], off_choice: str):
    """Infer a raw PS2 archive ID from lookup ranges reaching this PAK's end.

    This is useful when the PAK has a nonstandard filename and contains no VAGp
    headers. It only returns a result when one group clearly describes the file's
    physical extent; ambiguous tables still require a manual archive-ID choice.
    """
    groups = sorted({r.archive_id for r in records})
    units = [off_choice] if off_choice in ("Bytes", "2048-byte LBA sectors") else ["Bytes", "2048-byte LBA sectors"]
    size_units = ["Bytes", "2048-byte LBA sectors"]
    candidates = []
    pak_len = len(pak)
    if pak_len <= 0:
        return None
    for gid in groups:
        group_records = [r for r in records if r.archive_id == gid]
        if not group_records:
            continue
        for off_unit in units:
            for size_unit in size_units:
                ranges = []
                in_bounds = 0
                valid_ranges = 0
                for rec in group_records:
                    off = offset_to_bytes(rec.offset_value, off_unit)
                    size = size_to_bytes(rec.size_value, size_unit)
                    if 0 <= off < pak_len:
                        in_bounds += 1
                    if 0 <= off < pak_len and size > 0 and off + size <= pak_len:
                        valid_ranges += 1
                        ranges.append((off, off + size))
                count = len(group_records)
                in_ratio = in_bounds / count
                valid_ratio = valid_ranges / count
                if not ranges:
                    continue
                end = max(b for _, b in ranges)
                coverage = end / pak_len
                edge_error = abs(pak_len - end) / pak_len
                # A correct raw archive group should have nearly all ranges in bounds
                # and at least one indexed range near the end of the physical PAK.
                if in_ratio < 0.95 or valid_ratio < 0.90 or coverage < 0.95 or edge_error > 0.03:
                    continue
                by_offset = sorted(ranges)
                monotonic = sum(1 for i in range(len(by_offset) - 1) if by_offset[i][0] < by_offset[i + 1][0])
                monotonic_ratio = monotonic / max(1, len(by_offset) - 1)
                score = valid_ratio * 100 + in_ratio * 50 + coverage * 10 - edge_error * 100 + monotonic_ratio * 5
                stats = (score, gid, off_unit, count, 0, 0, in_bounds)
                candidates.append((score, gid, off_unit, size_unit, stats, edge_error, valid_ratio))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    best = candidates[0]
    # If another group is nearly as plausible but describes a different ID/unit,
    # don't silently guess which archive the user intended.
    if len(candidates) > 1:
        second = candidates[1]
        if (best[1], best[2], best[3]) != (second[1], second[2], second[3]) and best[0] - second[0] < 2.0:
            return None
    return best[1], best[2], best[4], best[3]


def detect_group(pak: bytes, records: list[LookupRecord], requested_group: Optional[int], off_choice: str):
    """Find the best archive ID/offset unit using VAGp or GTA SA stream headers."""
    groups = sorted({r.archive_id for r in records})
    units = [off_choice] if off_choice in ("Bytes", "2048-byte LBA sectors") else ["Bytes", "2048-byte LBA sectors"]
    candidates = []
    for gid in groups:
        if requested_group is not None and gid != requested_group:
            continue
        group_records = [r for r in records if r.archive_id == gid]
        for unit in units:
            sigs = 0
            valid = 0
            vag_valid = 0
            stream_valid = 0
            in_bounds = 0
            for rec in group_records:
                off = offset_to_bytes(rec.offset_value, unit)
                if 0 <= off < len(pak):
                    in_bounds += 1
                    vh = vag_header(pak, off)
                    if vh and vh["exact_size"] <= len(pak) - off:
                        sigs += 1
                        valid += 1
                        vag_valid += 1
                    else:
                        # GTA SA PS2 radio tracks use a distinct 8,068-byte
                        # stream metadata header, not the standalone VAGp container.
                        sh = gta_sa_stream_header(pak, off)
                        if sh is not None and off + sh["header_size"] <= len(pak):
                            sigs += 1
                            valid += 1
                            stream_valid += 1
            # Both recognized header kinds count as a strong location hit.
            score = valid * 1000 + sigs * 10 + in_bounds * 0.001
            # Tuple fields 4-6 remain compatibility stats used by callers:
            # recognized headers, signature hits, and in-bounds offsets.
            candidates.append((score, gid, unit, len(group_records), valid, sigs, in_bounds, vag_valid, stream_valid))
    if not candidates:
        raise FormatError("No lookup-table archive groups matched the selected group ID.")
    candidates.sort(reverse=True)
    best = candidates[0]
    if best[4] == 0:
        if requested_group is None:
            # For arbitrary PAK filenames, use this lookup table's offset/size ranges
            # to identify which group spans the selected archive. This is especially
            # important for PS2 raw fragments that have no VAGp headers.
            extent_match = detect_raw_group_by_extent(pak, records, off_choice)
            if extent_match is not None:
                return extent_match[0], extent_match[1], extent_match[2]
        # PS2 radio PAK entries are often raw stream fragments, not standalone VAGp
        # files. If the archive ID was supplied explicitly (or via the station-name
        # map), permit a conservative raw-data fallback when offsets demonstrably
        # land inside the selected PAK. For an unknown archive ID, fail rather than
        # choose one of several overlapping groups by guesswork.
        if requested_group is not None:
            raw_candidates = []
            for gid2 in groups:
                if gid2 != requested_group:
                    continue
                group_records = [r for r in records if r.archive_id == gid2]
                for unit in units:
                    offsets = [offset_to_bytes(r.offset_value, unit) for r in group_records]
                    in_bounds_offsets = [off for off in offsets if 0 <= off < len(pak)]
                    if not in_bounds_offsets:
                        continue
                    # Favor interpretations that span a meaningful part of the PAK.
                    # For LBA-based tables, multiplying by 2048 generally places the
                    # last entry near the end of the archive; byte interpretation of
                    # LBA values would cluster every entry near the beginning.
                    coverage = max(in_bounds_offsets) / max(1, len(pak))
                    monotonic = sum(1 for a, b in zip(offsets, offsets[1:]) if b > a)
                    distinct = len(set(in_bounds_offsets))
                    score = len(in_bounds_offsets) * 1000 + coverage * 100 + monotonic * 0.01 + distinct * 0.001
                    raw_candidates.append((score, gid2, unit, len(group_records), 0, 0, len(in_bounds_offsets)))
            if raw_candidates:
                # If auto-detect can choose between both interpretations, use the one
                # whose offsets span more of the physical PAK. If coverage is nearly
                # equal, require manual selection instead of guessing.
                raw_candidates.sort(reverse=True)
                raw_best = raw_candidates[0]
                if off_choice == "Auto-detect" and len(raw_candidates) > 1:
                    a, b = raw_candidates[0], raw_candidates[1]
                    cov_a = a[0] - a[6] * 1000
                    cov_b = b[0] - b[6] * 1000
                    if a[2] != b[2] and abs(cov_a - cov_b) < 1.0:
                        raise FormatError(
                            "The selected PAK has raw entries but the offset unit is ambiguous. "
                            "Try selecting 2048-byte LBA sectors (the usual PS2 stream layout) and inspect again."
                        )
                return raw_best[1], raw_best[2], raw_best
        details = "; ".join(
            f"ID 0x{c[1]:02X} / {c[2]}: {c[4]} recognized VAGp/stream headers in {c[3]} records"
            for c in candidates[:6]
        )
        raise FormatError(
            "No recognized VAGp or GTA SA PS2 stream headers were found at the candidate offsets. "
            "The PAK/lookup pair may not match, or the lookup offset unit may be wrong. "
            "Try selecting the archive ID and offset unit manually.\n" + details
        )
    # If equally good variants remain, favour byte offsets unless sector offsets identify more tracks.
    ties = [c for c in candidates if c[4] == best[4] and c[5] == best[5]]
    if len(ties) > 1 and requested_group is None and off_choice == "Auto-detect":
        # Prefer an unambiguous score; if there is a tie across units, fail safely.
        distinct = {(c[1], c[2]) for c in ties}
        if len(distinct) > 1:
            # When offsets are identical for small values, sectors usually fails bounds. If not, require user selection.
            raise FormatError(
                "Archive detection is ambiguous. Try selecting the archive ID and offset unit manually, then Inspect again.\n" +
                "; ".join(f"ID 0x{c[1]:02X} / {c[2]}: {c[4]} VAGp headers" for c in ties[:8])
            )
    return best[1], best[2], best


def inspect_archive(pak_path: Path, lookup_path: Path, group_value: str, offset_choice: str, size_choice: str):
    pak = pak_path.read_bytes()
    lookup_data, all_records = parse_lookup(lookup_path)
    requested_group = None if group_value == "Auto-detect" else int(group_value, 16)
    if requested_group is None:
        preferred_group = KNOWN_ARCHIVE_IDS.get(pak_path.name.upper())
        if preferred_group is not None:
            try:
                gid, off_unit, stats = detect_group(pak, all_records, preferred_group, offset_choice)
                if stats[4] == 0:
                    # A known filename is only a hint. PS2 lookup tables can differ from
                    # the PC SAAT numbering, so prefer the group's physical extent when
                    # the payload is raw and the table gives a confident match.
                    extent_match = detect_raw_group_by_extent(pak, all_records, offset_choice)
                    if extent_match is not None:
                        gid, off_unit, stats = extent_match[0], extent_match[1], extent_match[2]
            except FormatError as preferred_error:
                extent_match = detect_raw_group_by_extent(pak, all_records, offset_choice)
                if extent_match is not None:
                    gid, off_unit, stats = extent_match[0], extent_match[1], extent_match[2]
                else:
                    try:
                        gid, off_unit, stats = detect_group(pak, all_records, None, offset_choice)
                    except FormatError:
                        raise FormatError(
                            f"Could not match {pak_path.name} to a group in TRAKLKUP.DAT. "
                            "The tool checked the filename hint and the lookup ranges but could not identify a confident match. "
                            "Choose the archive ID manually if this is a custom or unusual PAK. Details: " + str(preferred_error)
                        ) from preferred_error
        else:
            gid, off_unit, stats = detect_group(pak, all_records, None, offset_choice)
    else:
        gid, off_unit, stats = detect_group(pak, all_records, requested_group, offset_choice)
    records = [r for r in all_records if r.archive_id == gid]
    if size_choice == "Auto-detect":
        size_unit = infer_length_unit(records, pak, off_unit)
    else:
        size_unit = size_choice
    tracks = []
    for rec in sorted(records, key=lambda r: offset_to_bytes(r.offset_value, off_unit)):
        off = offset_to_bytes(rec.offset_value, off_unit)
        vh = vag_header(pak, off) if 0 <= off < len(pak) else None
        stream_h = gta_sa_stream_header(pak, off) if 0 <= off < len(pak) else None
        stored_len = size_to_bytes(rec.size_value, size_unit)
        tracks.append(TrackInfo(
            record=rec,
            offset_bytes=off,
            stored_size_bytes=stored_len,
            # For radio stream records the lookup size counts the ADPCM payload,
            # not the 8,068-byte metadata header at the lookup offset.
            exact_size=(stored_len if stream_h else vh["exact_size"] if vh else None),
            is_vag=bool(vh),
            sample_rate=vh["sample_rate"] if vh else stream_h["sample_rate"] if stream_h else None,
            payload_size=vh["payload_size"] if vh else stored_len if stream_h else None,
            has_stream_header=bool(stream_h),
            stream_header_size=stream_h["header_size"] if stream_h else 0,
        ))
    return pak, lookup_data, all_records, gid, off_unit, size_unit, tracks, stats


def safe_vag_track_bytes(pak: bytes, track: TrackInfo, track_index: int, sorted_tracks: list[TrackInfo]) -> bytes:
    off = track.offset_bytes
    if off < 0 or off >= len(pak):
        raise FormatError(f"Track/table record {track.record.index} points outside the PAK (offset {off}).")
    vh = vag_header(pak, off)
    if vh:
        end = off + vh["exact_size"]
        if end > len(pak):
            raise FormatError(f"VAGp track at table record {track.record.index} extends beyond the PAK.")
        return pak[off:end]
    if track.has_stream_header:
        payload_start = off + track.stream_header_size
        payload_end = payload_start + (track.stored_size_bytes or 0)
        if track.stored_size_bytes is None or track.stored_size_bytes <= 0 or payload_end > len(pak):
            raise FormatError(
                f"PS2 stream record {track.record.index} declares {track.stored_size_bytes} payload bytes, "
                f"but its header + payload is outside the PAK."
            )
        return pak[payload_start:payload_end]
    # If no recognizable header exists, preserve the opaque entry range.
    candidates = []
    if track.stored_size_bytes and 0 < track.stored_size_bytes <= len(pak) - off:
        candidates.append(track.stored_size_bytes)
    if track_index + 1 < len(sorted_tracks):
        nxt = sorted_tracks[track_index + 1].offset_bytes
        if nxt > off:
            candidates.append(nxt - off)
    if not candidates:
        raise FormatError(f"No VAGp header or usable size found for table record {track.record.index}.")
    return pak[off:off + min(candidates)]


def extract_tracks(pak_path: Path, lookup_path: Path, out_dir: Path, group_value: str,
                   offset_choice: str, size_choice: str) -> tuple[int, int, str, str]:
    pak, lookup_data, all_records, gid, off_unit, size_unit, tracks, _ = inspect_archive(
        pak_path, lookup_path, group_value, offset_choice, size_choice
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    # Tracks are named by their stable global lookup-table record index.
    manifest = {
        "format": "GTA San Andreas PS2 radio PAK extraction manifest",
        "source_pak": str(pak_path.resolve()),
        "source_lookup": str(lookup_path.resolve()),
        "archive_id": gid,
        "offset_unit": off_unit,
        "size_unit": size_unit,
        "sector_size": SECTOR_SIZE,
        "tracks": [],
    }
    ok = 0
    skipped = 0
    tracks_sorted = sorted(tracks, key=lambda t: t.offset_bytes)
    for i, tr in enumerate(tracks_sorted):
        filename = f"entry_{tr.record.index:04d}.vag" if tr.is_vag else f"entry_{tr.record.index:04d}.bin"
        header_filename = f"entry_{tr.record.index:04d}.hdr" if tr.has_stream_header else None
        try:
            data = safe_vag_track_bytes(pak, tr, i, tracks_sorted)
            (out_dir / filename).write_bytes(data)
            if tr.has_stream_header and header_filename:
                h_start = tr.offset_bytes
                h_end = h_start + tr.stream_header_size
                (out_dir / header_filename).write_bytes(pak[h_start:h_end])
            ok += 1
            manifest["tracks"].append({
                "table_record": tr.record.index,
                "archive_id": tr.record.archive_id,
                "file": filename,
                "header_file": header_filename,
                "has_stream_header": tr.has_stream_header,
                "stream_header_size": tr.stream_header_size,
                "sample_rate": tr.sample_rate,
                "original_offset_value": tr.record.offset_value,
                "original_size_value": tr.record.size_value,
                "original_offset_bytes": tr.offset_bytes,
                "original_exact_size": tr.exact_size,
                "is_vagp": tr.is_vag,
            })
        except Exception:
            skipped += 1
            raise
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out_dir / "README.txt").write_text(
        "GTA SA PS2 PAK extraction\n\n"
        "Track files are named by their TRAKLKUP.DAT table-record index. Keep each filename.\n"
        "For recognized PS2 stream entries, the audio payload is stored as .bin and its original\n"
        "8,068-byte stream metadata header is saved as a matching .hdr sidecar. Leave those .hdr\n"
        "files in place. The GUI can transcode common audio files to stereo PS2 SPU ADPCM for these\n"
        "entries. Other entries must keep their detected PS2 format. Rebuild PAK + lookup together\n"
        "and test the result in PCSX2 before use.\n",
        encoding="utf-8"
    )
    return ok, skipped, off_unit, size_unit


def _natural_filename_key(name: str):
    """Natural sort: track2 precedes track10, independent of case."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)]


def _auto_file_extension(path: Path) -> str:
    """Return a supported output suffix without pretending to transcode audio."""
    suffix = path.suffix.lower()
    try:
        with path.open("rb") as stream:
            header = stream.read(VAG_HEADER_SIZE)
        file_size = path.stat().st_size
    except OSError as exc:
        raise FormatError(f"Could not read {path.name}: {exc}") from exc
    if len(header) >= VAG_HEADER_SIZE and header[:4] == VAG_MAGIC:
        payload = struct.unpack_from(">I", header, 0x0C)[0]
        if payload <= max(0, file_size - VAG_HEADER_SIZE):
            return ".vag"
        # Retain the suspicious .vag suffix so rebuild validation will reject it;
        # do not relabel a broken VAG header as raw data and hide the problem.
        if suffix in (".vag", ".vagp"):
            return ".vag"
    if suffix in (".vag", ".vagp"):
        return ".vag"
    return ".bin"


def build_auto_rename_plan(tracks_dir: Path, tracks: list[TrackInfo]):
    """Build a safe filename-normalization plan from the selected lookup records.

    Files with `entry_<table-record>` names are matched directly. Other supported
    track files can be assigned in natural filename order only when their count
    exactly matches the lookup records still unassigned. This avoids guessing which
    track a single arbitrary filename belongs to.
    """
    if not tracks_dir.is_dir():
        raise FormatError(f"Track folder does not exist: {tracks_dir}")
    allowed_suffixes = {".vag", ".vagp", ".bin", ".raw", ".dat"}
    ignored_names = {"manifest.json", "readme.txt", "traklkup.dat"}
    files = sorted(
        (p for p in tracks_dir.iterdir()
         if p.is_file() and p.name.casefold() not in ignored_names and p.suffix.lower() in allowed_suffixes),
        key=lambda p: _natural_filename_key(p.name),
    )
    if not files:
        raise FormatError(
            "No supported PS2 track files were found. Put the audio entries in this folder using .VAG/.BIN "
            "or .RAW/.DAT extensions. WAV/AT3/MP3/Ogg files must be converted before they can be packed."
        )

    by_index = {tr.record.index: tr for tr in tracks}
    assigned: dict[int, Path] = {}
    unlabelled: list[Path] = []
    pattern = re.compile(r"^entry[ _.-]?(\d+)$", re.IGNORECASE)
    for path in files:
        match = pattern.fullmatch(path.stem)
        if not match:
            unlabelled.append(path)
            continue
        idx = int(match.group(1))
        if idx in by_index:
            if idx in assigned:
                raise FormatError(f"More than one file maps to lookup record {idx}: {assigned[idx].name} and {path.name}.")
            assigned[idx] = path
        else:
            # This is likely a local/ordinal numbering scheme from another tool;
            # only map it in sequence if the complete remaining set is present.
            unlabelled.append(path)

    remaining_tracks = [tr for tr in tracks if tr.record.index not in assigned]
    mapping_methods = {idx: "lookup record index" for idx in assigned}
    if unlabelled:
        if len(unlabelled) != len(remaining_tracks):
            raise FormatError(
                f"Found {len(unlabelled)} file(s) with unsupported names but {len(remaining_tracks)} lookup record(s) "
                f"remain unmapped. The app cannot safely guess which entries those files belong to. "
                "For a full-station import, put every replacement track in one folder so the counts match; "
                "for one or a few tracks, use Replace selected or rename them to entry_<table-record> first."
            )
        unlabelled.sort(key=lambda p: _natural_filename_key(p.name))
        for path, tr in zip(unlabelled, remaining_tracks):
            if tr.record.index in assigned:
                raise FormatError(f"Conflicting mapping for lookup record {tr.record.index}.")
            assigned[tr.record.index] = path
            mapping_methods[tr.record.index] = "natural order + lookup table order"

    source_for_record = assigned
    plan = []
    target_seen = set()
    source_paths = {str(path.resolve()).casefold() for path in source_for_record.values()}
    for tr in tracks:
        source = source_for_record.get(tr.record.index)
        if source is None:
            continue
        ext = _auto_file_extension(source)
        target = tracks_dir / f"entry_{tr.record.index:04d}{ext}"
        target_key = str(target.resolve()).casefold()
        if target_key in target_seen:
            raise FormatError(f"Multiple source files would be renamed to {target.name}.")
        target_seen.add(target_key)
        if target.exists() and target_key not in source_paths:
            raise FormatError(f"Cannot rename {source.name} to {target.name}: a separate file already exists at the target path.")
        plan.append({
            "source": source,
            "target": target,
            "record_index": tr.record.index,
            "method": mapping_methods.get(tr.record.index, "lookup record index"),
        })
    return plan


def apply_auto_rename_plan(plan: list[dict]) -> int:
    """Apply a rename plan with temporary staging to avoid in-folder collisions."""
    moves = []
    for item in plan:
        source, target = Path(item["source"]), Path(item["target"])
        source_key = str(source.resolve()).casefold()
        target_key = str(target.resolve()).casefold()
        if source_key == target_key:
            continue
        moves.append((source, target, source.with_name(f".__saatps2_rename_{uuid.uuid4().hex}.tmp")))
    if not moves:
        return 0
    all_sources = {str(source.resolve()).casefold() for source, _, _ in moves}
    for _, target, _ in moves:
        if target.exists() and str(target.resolve()).casefold() not in all_sources:
            raise FormatError(f"Refusing to overwrite existing file: {target}")
    staged = []
    completed = []
    try:
        for source, target, temp in moves:
            if not source.is_file():
                raise FormatError(f"Source file disappeared before renaming: {source}")
            os.replace(source, temp)
            staged.append((source, target, temp))
        for source, target, temp in staged:
            os.replace(temp, target)
            completed.append((source, target, temp))
    except Exception:
        # Roll back best-effort so a failed rename does not leave the user's folder
        # half-normalized. Restore completed destinations and then temporary files.
        for source, target, temp in reversed(completed):
            try:
                if target.exists():
                    os.replace(target, source)
            except OSError:
                pass
        for source, target, temp in reversed(staged):
            try:
                if temp.exists():
                    os.replace(temp, source)
            except OSError:
                pass
        raise
    return len(moves)


def read_vag_size(path: Path) -> int:
    data = path.read_bytes()
    if len(data) < VAG_HEADER_SIZE or data[:4] != VAG_MAGIC:
        raise FormatError(f"{path.name} is not a VAGp file. Replace with a valid PS2 VAGp file, not WAV/AT3.")
    payload = struct.unpack_from(">I", data, 0x0C)[0]
    expected = VAG_HEADER_SIZE + payload
    if payload > len(data) - VAG_HEADER_SIZE:
        raise FormatError(f"{path.name}: VAGp header says {payload} payload bytes, but the file is only {len(data)} bytes.")
    if expected != len(data):
        # Remove only common trailing alignment padding; do not silently trim actual audio.
        if len(data) - expected <= SECTOR_SIZE and all(b in (0x00, 0xCD) for b in data[expected:]):
            return expected
        raise FormatError(f"{path.name}: header size is {expected} bytes, actual file is {len(data)} bytes. File may be malformed or have non-padding trailing data.")
    return expected


def rebuild_archive(pak_path: Path, lookup_path: Path, tracks_dir: Path, out_dir: Path,
                    group_value: str, offset_choice: str, size_choice: str) -> tuple[Path, Path, int, str, str]:
    pak, lookup_data, all_records, gid, off_unit, size_unit, tracks, _ = inspect_archive(
        pak_path, lookup_path, group_value, offset_choice, size_choice
    )
    manifest_path = tracks_dir / "manifest.json"
    manifest = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise FormatError(f"Could not read manifest.json: {exc}")
        if manifest.get("archive_id") != gid:
            raise FormatError("The extraction manifest belongs to a different archive ID than the current selection.")
        if manifest.get("offset_unit") != off_unit:
            raise FormatError("The extraction manifest's offset interpretation differs from the current setting. Re-extract or use the same setting.")
    # Need every table entry in this PAK; replacing a missing file would silently drop audio.
    ordered = sorted(tracks, key=lambda t: t.offset_bytes)
    # tuple: track, source path, bytes written into PAK, logical lookup-size bytes
    prepared: list[tuple[TrackInfo, Path, bytes, int]] = []
    for tr in ordered:
        candidate = tracks_dir / f"entry_{tr.record.index:04d}.vag"
        raw_candidate = tracks_dir / f"entry_{tr.record.index:04d}.bin"
        path = candidate if candidate.exists() else raw_candidate
        if not path.exists():
            raise FormatError(f"Missing extracted track for table record {tr.record.index}: expected {candidate.name} or {raw_candidate.name}.")
        data = path.read_bytes()
        if not data:
            raise FormatError(f"Replacement track {path.name} is empty.")

        if tr.has_stream_header:
            # Reconstruct PS2 stream record as preserved header + raw ADPCM payload.
            # A .VAGp import is accepted too: strip its 0x30-byte wrapper.
            if path.suffix.lower() == ".vag":
                exact = read_vag_size(path)
                vag_data = data[:exact]
                rate_header = vag_data[VAG_HEADER_SIZE:]
                data = rate_header
            else:
                # Backward compatibility with v1.2 extracted folders: those .bin files
                # contained the whole record including its 8,068-byte header.
                if len(data) > (tr.stored_size_bytes or 0) and gta_sa_stream_header(data, 0):
                    data = data[GTASA_STREAM_HEADER_SIZE:GTASA_STREAM_HEADER_SIZE + (tr.stored_size_bytes or len(data))]
                if tr.is_vag:
                    raise FormatError(f"Table record {tr.record.index} was VAGp in source but replacement is .bin.")
            if not data:
                raise FormatError(f"Replacement audio payload for {path.name} is empty.")
            if len(data) % 32 != 0:
                raise FormatError(
                    f"{path.name}: PS2 radio stereo ADPCM payload length ({len(data)} bytes) must be a multiple of 32. "
                    "Use the Track Browser drop/import conversion instead of manually renaming audio."
                )
            header_path = tracks_dir / f"entry_{tr.record.index:04d}.hdr"
            if header_path.is_file():
                header = header_path.read_bytes()
                if len(header) != tr.stream_header_size:
                    raise FormatError(f"{header_path.name} must be exactly {tr.stream_header_size} bytes; found {len(header)}.")
            else:
                # The original PAK remains the authoritative fallback for old projects.
                header = pak[tr.offset_bytes:tr.offset_bytes + tr.stream_header_size]
                if len(header) != tr.stream_header_size:
                    raise FormatError(f"Could not restore the PS2 stream header for record {tr.record.index}.")
            # Keep the source metadata / beat information and preserve its sample-rate
            # fields, which match the per-track target rate used by the encoder.
            packed = header + data
            logical_size = len(data)
        else:
            if path.suffix.lower() == ".vag":
                exact = read_vag_size(path)
                data = data[:exact]
            elif tr.is_vag:
                raise FormatError(f"Table record {tr.record.index} was VAGp in source but replacement is .bin.")
            packed = data
            logical_size = len(data)
        prepared.append((tr, path, packed, logical_size))

    # Repack in old physical order. LBA-based archives align every track to a 2048-byte boundary.
    out_pak = bytearray()
    rebuilt_lookup = bytearray(lookup_data)
    new_offsets_sizes: list[tuple[int, int, int]] = []
    for tr, path, packed_data, logical_size in prepared:
        if off_unit == "2048-byte LBA sectors":
            padding = (-len(out_pak)) % SECTOR_SIZE
            if padding:
                out_pak.extend(b"\x00" * padding)
        start = len(out_pak)
        out_pak.extend(packed_data)
        # PS2 radio lookup sizes describe only the stream payload, not its 8,068-byte header.
        # Generic entries use their complete stored size.
        if off_unit == "2048-byte LBA sectors":
            offset_value = start // SECTOR_SIZE
        else:
            offset_value = start
        if size_unit == "2048-byte LBA sectors":
            size_value = math.ceil(logical_size / SECTOR_SIZE)
        else:
            size_value = logical_size
        pos = tr.record.table_pos
        write_u32le(rebuilt_lookup, pos + 4, offset_value)
        write_u32le(rebuilt_lookup, pos + 8, size_value)
        new_offsets_sizes.append((tr.record.index, offset_value, size_value))

    # Keep sector-sized PAK length for LBA packs; zero-padded final track matches conventional sector packing.
    if off_unit == "2048-byte LBA sectors" and len(out_pak) % SECTOR_SIZE:
        out_pak.extend(b"\x00" * (SECTOR_SIZE - len(out_pak) % SECTOR_SIZE))

    out_dir.mkdir(parents=True, exist_ok=True)
    # Normalize a nonstandard input filename to the canonical station PAK name
    # associated with the detected PS2 lookup-table ID.
    canonical_name = ARCHIVE_FILENAME_BY_ID.get(gid, pak_path.name)
    pak_out = out_dir / f"NEW-{canonical_name}"
    lookup_out = out_dir / "NEW-TRAKLKUP.DAT"
    # Never overwrite the input paths even if user has selected same folder/name.
    if pak_out.resolve() == pak_path.resolve() or lookup_out.resolve() == lookup_path.resolve():
        raise FormatError("Output path would overwrite an input file; choose a different output folder.")
    pak_out.write_bytes(out_pak)
    lookup_out.write_bytes(rebuilt_lookup)

    # Round-trip verify the files just written. This catches the common workflow
    # mistake of converting an audio file outside tracks_dir (which means the
    # rebuilder would still pack the untouched original). It also detects genuine
    # offset/size/header bugs before reporting a successful rebuild to the user.
    verify_rows = []
    with tempfile.TemporaryDirectory(prefix="saatps2_verify_") as verify_tmp:
        verify_dir = Path(verify_tmp)
        verify_count, _, _, _ = extract_tracks(
            pak_out, lookup_out, verify_dir, f"0x{gid:02X}", off_unit, size_unit
        )
        if verify_count != len(prepared):
            verify_rows.append({
                "record": None, "passed": False,
                "reason": f"Expected {len(prepared)} extracted entries, found {verify_count}.",
            })
        for tr, path, packed_data, logical_size in prepared:
            output_ext = ".vag" if tr.is_vag else ".bin"
            check_path = verify_dir / f"entry_{tr.record.index:04d}{output_ext}"
            expected_data = packed_data[tr.stream_header_size:] if tr.has_stream_header else packed_data
            if not check_path.is_file():
                verify_rows.append({"record": tr.record.index, "passed": False, "reason": f"Missing {check_path.name}"})
                continue
            actual = check_path.read_bytes()
            passed = actual == expected_data
            row = {
                "record": tr.record.index,
                "file": check_path.name,
                "passed": passed,
                "expected_bytes": len(expected_data),
                "actual_bytes": len(actual),
                "expected_sha256": hashlib.sha256(expected_data).hexdigest(),
                "actual_sha256": hashlib.sha256(actual).hexdigest(),
            }
            if tr.has_stream_header:
                hdr_path = verify_dir / f"entry_{tr.record.index:04d}.hdr"
                expected_header = packed_data[:tr.stream_header_size]
                if not hdr_path.is_file() or hdr_path.read_bytes() != expected_header:
                    row["passed"] = False
                    row["header_passed"] = False
                else:
                    row["header_passed"] = True
            verify_rows.append(row)
    verification = {
        "passed": bool(verify_rows) and all(r.get("passed", False) for r in verify_rows),
        "entries_compared": len(prepared),
        "checks": verify_rows,
    }
    report = {
        "source_pak": str(pak_path.resolve()),
        "source_lookup": str(lookup_path.resolve()),
        "output_pak": str(pak_out.resolve()),
        "output_lookup": str(lookup_out.resolve()),
        "archive_id": gid,
        "canonical_archive_name": canonical_name,
        "offset_unit": off_unit,
        "size_unit": size_unit,
        "tracks": len(prepared),
        "bytes_written": len(out_pak),
        "table_records_updated": new_offsets_sizes,
        "roundtrip_verification": verification,
    }
    (out_dir / "rebuild_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not verification["passed"]:
        failures = [r for r in verify_rows if not r.get("passed", False)]
        preview = "; ".join(
            f"record {r.get('record', '?')}: {r.get('reason') or 'data/header mismatch'}"
            for r in failures[:8]
        )
        raise FormatError(
            f"PAK files were written, but round-trip verification failed for {len(failures)} check(s). "
            f"See {out_dir / 'rebuild_report.json'} for details. {preview}"
        )
    return pak_out, lookup_out, len(prepared), off_unit, size_unit



def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_files_for_iso_patching(
    pak_path: Path,
    lookup_path: Path,
    destination_root: Path,
    overwrite: bool = False,
) -> dict:
    """Prepare a rebuilt SA PS2 PAK + lookup pair for the ISO patching workflow.

    This stages the files at their game-disc-relative locations and never edits an
    ISO during preparation. The source names must be the rebuilder's NEW-* outputs,
    which helps avoid accidentally staging the untouched original game files.
    """
    pak_path = Path(pak_path).expanduser().resolve()
    lookup_path = Path(lookup_path).expanduser().resolve()
    destination_root = Path(destination_root).expanduser().resolve()

    if not pak_path.is_file():
        raise FormatError(f"Rebuilt PAK was not found: {pak_path}")
    if not lookup_path.is_file():
        raise FormatError(f"Rebuilt lookup table was not found: {lookup_path}")
    if not pak_path.name.upper().startswith("NEW-") or not pak_path.name.upper().endswith(".PAK"):
        raise FormatError(
            "Choose the rebuilt NEW-*.PAK file, not the original station PAK. "
            "Rebuild Archive creates the expected staging source."
        )
    if lookup_path.name.upper() != "NEW-TRAKLKUP.DAT":
        raise FormatError(
            "Choose NEW-TRAKLKUP.DAT from the same rebuild output folder, not the original TRAKLKUP.DAT."
        )
    if pak_path.parent != lookup_path.parent:
        raise FormatError("The rebuilt PAK and NEW-TRAKLKUP.DAT must come from the same output folder.")

    canonical_name = pak_path.name[4:].upper()
    if canonical_name not in KNOWN_ARCHIVE_IDS:
        raise FormatError(f"Unknown GTA SA PS2 station archive name: {canonical_name}")
    expected_group = KNOWN_ARCHIVE_IDS[canonical_name]

    # Validate the pair again using the known station ID and automatic units before
    # placing either file into the staged game-disc layout.
    inspection = inspect_archive(
        pak_path, lookup_path, f"0x{expected_group:02X}", "Auto-detect", "Auto-detect"
    )
    _, _, _, detected_group, offset_unit, size_unit, tracks, stats = inspection
    if detected_group != expected_group or not tracks:
        raise FormatError(
            f"The selected files did not validate as {canonical_name} with archive ID 0x{expected_group:02X}."
        )

    source_report_path = pak_path.parent / "rebuild_report.json"
    source_report = None
    if source_report_path.is_file():
        try:
            source_report = json.loads(source_report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FormatError(f"Could not read rebuild report {source_report_path}: {exc}") from exc
        roundtrip = source_report.get("roundtrip_verification", {})
        if isinstance(roundtrip, dict) and roundtrip.get("passed") is False:
            raise FormatError(
                "The sibling rebuild_report.json says round-trip validation failed. "
                "Rebuild the archive successfully before preparing it for ISO patching."
            )
        report_name = str(source_report.get("canonical_archive_name", canonical_name)).upper()
        if report_name != canonical_name:
            raise FormatError(
                f"The sibling rebuild report describes {report_name}, but the selected PAK is {canonical_name}."
            )
        reported_tracks = source_report.get("tracks")
        if reported_tracks is not None and int(reported_tracks) != len(tracks):
            raise FormatError(
                f"The sibling rebuild report lists {reported_tracks} tracks but the selected pair inspects as {len(tracks)}."
            )
        reported_checks = roundtrip.get("entries_compared") if isinstance(roundtrip, dict) else None
        if reported_checks is not None and int(reported_checks) != len(tracks):
            raise FormatError(
                f"The sibling rebuild report checked {reported_checks} entries but the selected pair inspects as {len(tracks)}."
            )

    stream_dir = destination_root / "AUDIO" / "STREAMS"
    config_dir = destination_root / "AUDIO" / "CONFIG"
    pak_target = stream_dir / canonical_name
    lookup_target = config_dir / "TRAKLKUP.DAT"
    manifest_target = destination_root / "ISO_PATCH_MANIFEST.json"
    targets = (pak_target, lookup_target, manifest_target)
    conflicts = [str(path) for path in targets if path.exists()]
    if conflicts and not overwrite:
        raise ExistingStagingFilesError(
            "The staging destination already contains one or more target files:\n" +
            "\n".join(conflicts) +
            "\nOverwrite these files? No backup files will be created."
        )

    # Avoid accidentally replacing the source files if someone selects an unusual
    # output root that aliases one of the inputs.
    if pak_target == pak_path or lookup_target == lookup_path:
        raise FormatError("The staging destination resolves to one of the source files; choose a different folder.")

    stream_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    destination_root.mkdir(parents=True, exist_ok=True)

    def file_info(source: Path, game_path: str, staged_path: Path) -> dict:
        return {
            "source": str(source),
            "game_disc_path": game_path,
            "staged_path": str(staged_path),
            "size_bytes": source.stat().st_size,
            "sha256": _sha256_path(source),
        }

    pak_info = file_info(pak_path, f"AUDIO/STREAMS/{canonical_name}", pak_target)
    lookup_info = file_info(lookup_path, "AUDIO/CONFIG/TRAKLKUP.DAT", lookup_target)
    manifest = {
        "tool": "GTA Modding Studio v0.3.8",
        "purpose": "Prepared files for GTA San Andreas PS2 ISO patching; staging does not modify an ISO.",
        "archive": {
            "filename": canonical_name,
            "archive_id": f"0x{expected_group:02X}",
            "track_records_inspected": len(tracks),
            "offset_unit": offset_unit,
            "size_unit": size_unit,
            "roundtrip_report": str(source_report_path) if source_report is not None else None,
        },
        "files": [pak_info, lookup_info],
        "next_step": "Use Patch ISO in GTA Modding Studio to create a separate patched ISO copy, then test playback in PCSX2.",
    }

    # Prepare all temporary copies and the manifest first. Each final replacement is
    # an atomic rename on its own filesystem, and temporary files are cleaned on error.
    temp_paths: list[Path] = []
    replacements: list[tuple[Path, Path]] = []
    try:
        for source, target in ((pak_path, pak_target), (lookup_path, lookup_target)):
            temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temp_paths.append(temp)
            shutil.copy2(source, temp)
            replacements.append((temp, target))
        manifest_temp = manifest_target.with_name(f".{manifest_target.name}.{uuid.uuid4().hex}.tmp")
        temp_paths.append(manifest_temp)
        manifest_temp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        replacements.append((manifest_temp, manifest_target))
        for temporary, target in replacements:
            os.replace(temporary, target)
    except Exception:
        for temporary in temp_paths:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise

    return {
        "pak_path": pak_target,
        "lookup_path": lookup_target,
        "manifest_path": manifest_target,
        "archive_name": canonical_name,
        "archive_id": expected_group,
        "tracks_inspected": len(tracks),
        "pak_size_bytes": pak_target.stat().st_size,
        "lookup_size_bytes": lookup_target.stat().st_size,
        "offset_unit": offset_unit,
        "size_unit": size_unit,
        "offset_bytes_min": min(track.offset_bytes for track in tracks),
        "offset_bytes_max": max(track.offset_bytes for track in tracks),
        "payload_bytes_min": min(track.stored_size_bytes or 0 for track in tracks),
        "payload_bytes_max": max(track.stored_size_bytes or 0 for track in tracks),
    }


def run_self_test():
    """Synthetic round trips, PS2 archive-ID mapping, extent detection, and auto-renaming."""
    import tempfile

    assert KNOWN_ARCHIVE_IDS["AMBIENCE.PAK"] == 0x02
    assert KNOWN_ARCHIVE_IDS["CH.PAK"] == 0x04
    assert KNOWN_ARCHIVE_IDS["RE.PAK"] == 0x0D
    assert KNOWN_ARCHIVE_IDS["TK.PAK"] == 0x0F
    assert ARCHIVE_FILENAME_BY_ID[0x0F] == "TK.PAK"

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pak_path = root / "RE.PAK"
        lookup_path = root / "TRAKLKUP.DAT"
        work = root / "out"

        def mk_vag(payload: bytes, rate=44100) -> bytes:
            h = bytearray(VAG_HEADER_SIZE)
            h[:4] = b"VAGp"
            struct.pack_into(">I", h, 0x04, 0x20)
            struct.pack_into(">I", h, 0x0C, len(payload))
            struct.pack_into(">I", h, 0x10, rate)
            return bytes(h) + payload

        # VAGp/LBA archive round trip.
        vag_tracks = [mk_vag(bytes([i]) * 160) for i in (1, 2, 3)]
        blob = bytearray()
        table = bytearray()
        for tr in vag_tracks:
            blob.extend(b"\x00" * ((-len(blob)) % SECTOR_SIZE))
            off_lba = len(blob) // SECTOR_SIZE
            blob.extend(tr)
            rec = bytearray(RECORD_SIZE)
            rec[0] = 0x0D
            rec[1:4] = b"\xCD\xCD\xCD"
            struct.pack_into("<I", rec, 4, off_lba)
            struct.pack_into("<I", rec, 8, math.ceil(len(tr) / SECTOR_SIZE))
            table.extend(rec)
        blob.extend(b"\x00" * ((-len(blob)) % SECTOR_SIZE))
        pak_path.write_bytes(blob)
        lookup_path.write_bytes(table)
        result = inspect_archive(pak_path, lookup_path, "Auto-detect", "Auto-detect", "Auto-detect")
        assert result[3] == 0x0D, f"Group mismatch: {result[3]:02X}"
        assert result[4] == "2048-byte LBA sectors", result[4]
        assert result[5] == "2048-byte LBA sectors", result[5]
        n, _, _, _ = extract_tracks(pak_path, lookup_path, work / "tracks", "Auto-detect", "Auto-detect", "Auto-detect")
        assert n == 3
        old = (work / "tracks" / "entry_0001.vag").read_bytes()
        h = bytearray(old[:VAG_HEADER_SIZE])
        payload = bytes([7]) * 320
        struct.pack_into(">I", h, 0x0C, len(payload))
        (work / "tracks" / "entry_0001.vag").write_bytes(bytes(h) + payload)
        pak_out, lookup_out, count, _, _ = rebuild_archive(pak_path, lookup_path, work / "tracks", work / "rebuilt", "Auto-detect", "Auto-detect", "Auto-detect")
        assert count == 3
        check = inspect_archive(pak_out, lookup_out, "Auto-detect", "Auto-detect", "Auto-detect")
        assert check[3] == 0x0D
        assert all(t.is_vag for t in check[6])
        assert check[6][1].exact_size == VAG_HEADER_SIZE + 320

        # Raw PS2 archive in bytes. Its arbitrary filename should be identified solely
        # from the extent described by its lookup-table records.
        raw_dir = root / "raw_case"
        raw_dir.mkdir()
        raw_pak = raw_dir / "my_custom_radio_payload.bin"
        raw_lookup = raw_dir / "TRAKLKUP.DAT"
        raw_blob = bytearray()
        raw_table = bytearray()
        for i in range(3):
            offset = len(raw_blob)
            payload = bytes([0x20 + i]) * 3000
            raw_blob.extend(payload)
            rec = bytearray(RECORD_SIZE)
            rec[0] = 0x0F
            rec[1:4] = b"\x00\x00\x00"
            struct.pack_into("<I", rec, 4, offset)
            struct.pack_into("<I", rec, 8, len(payload))
            raw_table.extend(rec)
        raw_blob.extend(b"\x00" * 37)
        # Make the final lookup range end exactly at the PAK end for deterministic detection.
        last_pos = 2 * RECORD_SIZE
        struct.pack_into("<I", raw_table, last_pos + 8, 3037)
        raw_pak.write_bytes(raw_blob)
        raw_lookup.write_bytes(raw_table)
        raw_info = inspect_archive(raw_pak, raw_lookup, "Auto-detect", "Auto-detect", "Auto-detect")
        assert raw_info[3] == 0x0F, f"Raw extent detection chose {raw_info[3]:02X}"
        assert raw_info[4] == "Bytes", raw_info[4]
        assert raw_info[5] == "Bytes", raw_info[5]
        raw_tracks = raw_dir / "tracks"
        n, _, _, _ = extract_tracks(raw_pak, raw_lookup, raw_tracks, "Auto-detect", "Auto-detect", "Auto-detect")
        assert n == 3
        assert (raw_tracks / "entry_0002.bin").stat().st_size == 3037
        # Nonstandard output archive names normalize to the standard station PAK name.
        raw_out, lookup_out, count, _, _ = rebuild_archive(raw_pak, raw_lookup, raw_tracks, raw_dir / "rebuilt", "Auto-detect", "Auto-detect", "Auto-detect")
        assert raw_out.name == "NEW-TK.PAK", raw_out.name
        assert lookup_out.name == "NEW-TRAKLKUP.DAT" and count == 3

        # Real GTA SA PS2-style stream-record regression. Importantly, replacement
        # audio is written INTO the extracted folder that rebuild_archive consumes,
        # not beside it. Rebuild's automatic round-trip verifier must observe it.
        stream_dir = root / "stream_case"
        stream_dir.mkdir()
        stream_pak = stream_dir / "TK.PAK"
        stream_lookup = stream_dir / "TRAKLKUP.DAT"
        stream_blob = bytearray()
        stream_table = bytearray()
        original_payloads = [bytes([0x31]) * 64, bytes([0x42]) * 96]
        for payload in original_payloads:
            h = bytearray(GTASA_STREAM_HEADER_SIZE)
            h[:4] = b"\xff\xff\xff\xff"
            struct.pack_into("<I", h, GTASA_STREAM_RATE_OFFSETS[0], 24000)
            struct.pack_into("<I", h, GTASA_STREAM_RATE_OFFSETS[1], 24000)
            h[0x1F80:0x1F84] = b"\x04\x00\x00\x00"
            offset = len(stream_blob)
            stream_blob.extend(h)
            stream_blob.extend(payload)
            rec = bytearray(RECORD_SIZE)
            rec[0] = 0x0F
            rec[1:4] = b"\xff\xff\xff"
            struct.pack_into("<I", rec, 4, offset)
            struct.pack_into("<I", rec, 8, len(payload))
            stream_table.extend(rec)
        stream_pak.write_bytes(stream_blob)
        stream_lookup.write_bytes(stream_table)
        extracted = stream_dir / "extracted"
        rebuilt_dir = stream_dir / "rebuilt"
        verify_dir = stream_dir / "verify"
        assert extract_tracks(stream_pak, stream_lookup, extracted, "Auto-detect", "Auto-detect", "Auto-detect")[0] == 2
        inspected = inspect_archive(stream_pak, stream_lookup, "Auto-detect", "Auto-detect", "Auto-detect")
        assert inspected[6][0].sample_rate == 24000, (
            "PS2 replacement rate must match the 24000 Hz values stored in the source stream header"
        )
        assert inspected[7][4] == 2, (
            "Archive detection must count GTA SA stream headers even when there are no standalone VAGp headers"
        )
        assert sum(t.has_stream_header for t in inspected[6]) == 2 and sum(t.is_vag for t in inspected[6]) == 0
        # Legacy fields are metadata, not a signature/rate constraint.
        unusual_header = bytearray(stream_pak.read_bytes()[:GTASA_STREAM_HEADER_SIZE])
        struct.pack_into("<I", unusual_header, GTASA_STREAM_RATE_OFFSETS[0], 0)
        struct.pack_into("<I", unusual_header, GTASA_STREAM_RATE_OFFSETS[1], 48000)
        assert gta_sa_stream_header(bytes(unusual_header))['sample_rate'] == 24000
        assert (extracted / "entry_0000.hdr").stat().st_size == GTASA_STREAM_HEADER_SIZE
        replacement_payload = bytes(range(32)) * 10
        # Put replacement in the active extraction folder, exactly as the GUI now does.
        (extracted / "entry_0000.bin").write_bytes(replacement_payload)
        new_pak, new_lookup, count, off_unit, size_unit = rebuild_archive(
            stream_pak, stream_lookup, extracted, rebuilt_dir,
            "Auto-detect", "Auto-detect", "Auto-detect"
        )
        report = json.loads((rebuilt_dir / "rebuild_report.json").read_text(encoding="utf-8"))
        assert report["roundtrip_verification"]["passed"]
        assert count == 2 and new_pak.name == "NEW-TK.PAK"
        assert extract_tracks(new_pak, new_lookup, verify_dir, "0x0F", off_unit, size_unit)[0] == 2
        assert (verify_dir / "entry_0000.bin").read_bytes() == replacement_payload
        assert (verify_dir / "entry_0001.bin").read_bytes() == original_payloads[1]
        assert (verify_dir / "entry_0000.hdr").read_bytes() == (extracted / "entry_0000.hdr").read_bytes()

        # ISO-patching staging: stage only the rebuilt pair at game-disc-relative paths.
        staging_root = stream_dir / "iso_patch_staging"
        staged = prepare_files_for_iso_patching(new_pak, new_lookup, staging_root)
        assert staged["archive_name"] == "TK.PAK" and staged["archive_id"] == 0x0F
        staged_pak = staging_root / "AUDIO" / "STREAMS" / "TK.PAK"
        staged_lookup = staging_root / "AUDIO" / "CONFIG" / "TRAKLKUP.DAT"
        manifest_path = staging_root / "ISO_PATCH_MANIFEST.json"
        assert staged_pak.read_bytes() == new_pak.read_bytes()
        assert staged_lookup.read_bytes() == new_lookup.read_bytes()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["purpose"].startswith("Prepared files for GTA San Andreas PS2 ISO patching")
        assert [x["game_disc_path"] for x in manifest["files"]] == [
            "AUDIO/STREAMS/TK.PAK", "AUDIO/CONFIG/TRAKLKUP.DAT"
        ]
        try:
            prepare_files_for_iso_patching(new_pak, new_lookup, staging_root, overwrite=False)
            raise AssertionError("Existing staging files should require an explicit overwrite decision.")
        except ExistingStagingFilesError:
            pass
        # Explicit overwrite works without creating backup files.
        prepare_files_for_iso_patching(new_pak, new_lookup, staging_root, overwrite=True)
        assert not list(staging_root.rglob("*.bak"))

        # Track auto-renamer: full folder of unsupported names maps naturally onto the
        # active lookup group, and corrects extensions based on VAGp signatures.
        rename_dir = root / "rename_case"
        rename_dir.mkdir()
        rename_tracks = raw_info[6]
        (rename_dir / "Song 10.bin").write_bytes(b"raw-ten")
        (rename_dir / "Song 2.bin").write_bytes(b"raw-two")
        (rename_dir / "Song 1.bin").write_bytes(b"raw-one")
        plan = build_auto_rename_plan(rename_dir, rename_tracks)
        assert [x["record_index"] for x in plan] == [0, 1, 2]
        assert [Path(x["source"]).name for x in plan] == ["Song 1.bin", "Song 2.bin", "Song 10.bin"]
        assert apply_auto_rename_plan(plan) == 3
        assert (rename_dir / "entry_0000.bin").read_bytes() == b"raw-one"
        assert (rename_dir / "entry_0001.bin").read_bytes() == b"raw-two"
        assert (rename_dir / "entry_0002.bin").read_bytes() == b"raw-ten"

    print("Self-test passed: PS2 compact IDs, raw archive extent detection, VAGp/raw extraction, stream-header payload replacement, automatic rebuild round-trip verification, ISO-patching staging paths/manifest/overwrite guard, canonical output names, and auto-renamer.")



if __name__ == "__main__":
    run_self_test()
