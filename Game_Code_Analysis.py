#!/usr/bin/env python3
"""Hash-gated static checks for the documented LCS PS2 radio-length flow.

This is an analysis/validation recipe, not a machine-code patch. It proves the
presence of selected control-flow signatures only on the bundled SLUS_214.23
build. It deliberately does not infer unobserved IOP service behaviour.
"""
from __future__ import annotations
import hashlib
from pathlib import Path

REFERENCE_RELATIVE = Path("Patch_References") / "PS2_Executables" / "SLUS_214.23"
EXPECTED_SHA256 = "65bc2ec3d8a1b9f8482fb7e69427f2539e7e7b49d9dd262bf31aa8aa54ec53b2"
EXPECTED_SIZE = 2_994_708
# For this ELF's mapped .text: VA 0x100000 starts at file offset 0x1000,
# so file_offset = VA - 0xFF000. This is verified against the exact reference.
CHECKS = [
    {"label": "Random-start loop uses the per-track length in divu", "va": 0x1676E8, "file_offset": 0x686E8, "expected_hex": "1b00070210100000", "meaning": "divu candidate, length; mfhi stores the remainder"},
    {"label": "Random-start loop is limited to ten entries", "va": 0x167750, "file_offset": 0x68750, "expected_hex": "0a00822ab8ff4014", "meaning": "slti index, 10; loop while below ten"},
    {"label": "Initializer calls the length-query helper for station index", "va": 0x167898, "file_offset": 0x68898, "expected_hex": "5654040c80f56424", "meaning": "JAL 0x115158 with index in a1"},
    {"label": "Initializer stores helper result into table length field", "va": 0x1678BC, "file_offset": 0x688BC, "expected_hex": "000062ac", "meaning": "SW v0, 0(v1) where v1 points to entry + 0x18"},
    {"label": "Length-query helper sends key 0x90000 to resource query", "va": 0x11515C, "file_offset": 0x1615C, "expected_hex": "0900043c0000bfff0e52040cff00a530", "meaning": "LUI a0,9; JAL 0x114838; delay slot masks station index"},
    {"label": "Runtime offset uses the same per-entry length and modulo", "va": 0x16A034, "file_offset": 0x6B034, "expected_hex": "1800a48c21186900f401632401008050cd0100001b00640010100000", "meaning": "loads entry+0x18, builds position, DIVU by stored length, MFHI remainder"},
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_lcs_ps2_length_trace(app_dir: str | Path | None = None) -> dict:
    root = Path(app_dir or Path(__file__).resolve().parent).resolve()
    path = (root / REFERENCE_RELATIVE).resolve()
    refs_root = (root / "Patch_References").resolve()
    try:
        path.relative_to(refs_root)
    except ValueError as exc:
        raise ValueError("LCS reference path escaped the bundled reference directory.") from exc
    if not path.is_file():
        raise FileNotFoundError(f"Bundled LCS PS2 executable reference is missing: {path}")
    data = path.read_bytes()
    digest = sha256_bytes(data)
    if len(data) != EXPECTED_SIZE or digest != EXPECTED_SHA256:
        raise ValueError(
            "LCS radio-length analysis is pinned to the exact SLUS_214.23 reference. "
            f"Expected {EXPECTED_SIZE:,} bytes / {EXPECTED_SHA256}, got {len(data):,} bytes / {digest}."
        )
    results = []
    for check in CHECKS:
        offset = int(check["file_offset"])
        expected = bytes.fromhex(check["expected_hex"])
        actual = data[offset:offset + len(expected)]
        ok = actual == expected
        results.append({
            "label": check["label"], "va": int(check["va"]), "offset": offset,
            "expected_hex": check["expected_hex"], "actual_hex": actual.hex(),
            "status": "MATCH" if ok else "MISMATCH", "ok": ok, "meaning": check["meaning"],
        })
    failures = [r for r in results if not r["ok"]]
    if failures:
        raise ValueError("The LCS length trace did not match its executable signatures: " + "; ".join(r["label"] for r in failures))
    return {
        "reference": "PS2_Executables/SLUS_214.23",
        "size_bytes": len(data), "sha256": digest,
        "checks": results,
        "runtime_origin_conclusion": (
            "PROVEN: the first ten radio entries (stride 0x0C) have a length field at +0x18; "
            "the random-start initializer and runtime offset routine use that field as the unsigned modulo divisor. "
            "The initializer obtains the field by calling helper 0x115158 with station index; that helper queries "
            "resource key 0x90000 plus the station index through helper 0x114838. The EE initializer does not directly "
            "parse the .VB stream header or call a filesystem size query at these sites. UNRESOLVED: whether the returned "
            "resource value is recomputed from the current replaced .VB file by the IOP/resource service or inherited "
            "from a cached/stock descriptor; that requires tracing the resource response path or a controlled runtime test."
        ),
    }


def run_self_test() -> None:
    result = verify_lcs_ps2_length_trace(Path(__file__).resolve().parent)
    assert len(result["checks"]) == len(CHECKS)
    assert all(item["ok"] for item in result["checks"])
    assert "UNRESOLVED" in result["runtime_origin_conclusion"]


if __name__ == "__main__":
    result = verify_lcs_ps2_length_trace()
    for item in result["checks"]:
        print(f"{item['status']} VA 0x{item['va']:08X} / offset 0x{item['offset']:X}: {item['label']}")
    print(f"SHA-256: {result['sha256']}")
    print(result["runtime_origin_conclusion"])
