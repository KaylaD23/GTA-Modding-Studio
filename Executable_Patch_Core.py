#!/usr/bin/env python3
"""Safe, signature-verified game executable patch framework.

The bundled executables are reference inputs. They are never modified in place.
A patch may be applied only by a recipe explicitly marked `verified`, matching
an exact source SHA-256 and every expected byte signature. The current release
contains research-only recipes and therefore applies no machine-code edits.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
REFERENCES_DIR_NAME = "Patch_References"
MANIFEST_NAME = "reference_manifest.json"
RECIPES_NAME = "patch_recipes.json"


class PatchRecipeError(RuntimeError):
    """A patch recipe or reference did not satisfy its safety gates."""


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PatchRecipeError(f"Could not read patch metadata '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise PatchRecipeError(f"Patch metadata must be a JSON object: {path}")
    return data


def load_reference_manifest(app_dir: Path | None = None) -> dict[str, Any]:
    root = Path(app_dir or MODULE_DIR)
    return _read_json(root / REFERENCES_DIR_NAME / MANIFEST_NAME)


def verify_reference_manifest(app_dir: Path | None = None) -> dict[str, Any]:
    """Verify every bundled reference file and return a compact result."""
    root = Path(app_dir or MODULE_DIR)
    base = root / REFERENCES_DIR_NAME
    manifest = load_reference_manifest(root)
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    seen: set[str] = set()
    for item in manifest.get("files", []):
        if not isinstance(item, dict):
            failures.append("Invalid reference manifest item.")
            continue
        relative = str(item.get("path", ""))
        if not relative or relative in seen:
            failures.append(f"Missing or duplicate reference path: {relative!r}")
            continue
        seen.add(relative)
        path = (base / relative).resolve()
        try:
            path.relative_to(base.resolve())
        except ValueError:
            failures.append(f"Reference path escapes reference folder: {relative}")
            continue
        if not path.is_file():
            failures.append(f"Missing reference file: {relative}")
            continue
        actual_size = path.stat().st_size
        actual_hash = sha256_path(path)
        expected_size = int(item.get("size_bytes", -1))
        expected_hash = str(item.get("sha256", "")).lower()
        ok = actual_size == expected_size and actual_hash == expected_hash
        results.append({"path": relative, "ok": ok, "size_bytes": actual_size, "sha256": actual_hash})
        if not ok:
            failures.append(f"Reference hash/size mismatch: {relative}")
    if not manifest.get("files"):
        failures.append("Reference manifest contains no files.")
    return {
        "ok": not failures,
        "verified_count": sum(1 for result in results if result["ok"]),
        "file_count": len(results),
        "failures": failures,
        "files": results,
    }


def load_recipes(app_dir: Path | None = None) -> list[dict[str, Any]]:
    root = Path(app_dir or MODULE_DIR)
    data = _read_json(root / REFERENCES_DIR_NAME / RECIPES_NAME)
    rows = data.get("recipes", [])
    return [row for row in rows if isinstance(row, dict)]


def status_for_game(game_id: str, app_dir: Path | None = None) -> dict[str, Any]:
    """Describe what is known for a game without applying unverified changes."""
    rows = [row for row in load_recipes(app_dir) if row.get("game_id") == game_id]
    verified = [row for row in rows if row.get("status") == "verified"]
    research = [row for row in rows if row.get("status") != "verified"]
    if verified:
        return {
            "status": "verified_recipe_available",
            "applied": False,
            "recipe_ids": [row.get("recipe_id") for row in verified],
            "reason": "A verified recipe is available; it will only be applied after the ISO executable matches the bundled reference hash.",
        }
    if research:
        return {
            "status": "research_only",
            "applied": False,
            "recipe_ids": [row.get("recipe_id") for row in research],
            "reason": "Research evidence is bundled, but no validated machine-code edit is enabled. The executable is left unchanged.",
        }
    return {
        "status": "no_verified_recipe",
        "applied": False,
        "recipe_ids": [],
        "reason": "No verified executable patch recipe exists for this game/platform. Radio data patching does not change game code.",
    }


def get_verified_recipe_for_game(game_id: str, app_dir: Path | None = None) -> dict[str, Any] | None:
    rows = [row for row in load_recipes(app_dir) if row.get("game_id") == game_id and row.get("status") == "verified"]
    if not rows:
        return None
    if len(rows) != 1:
        raise PatchRecipeError(f"Multiple verified executable recipes exist for {game_id}; choose a single explicit recipe ID.")
    return rows[0]


def reference_path_for_recipe(recipe: dict[str, Any], app_dir: Path | None = None) -> Path:
    root = Path(app_dir or MODULE_DIR)
    rel = str(recipe.get("reference_path", "")).replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        raise PatchRecipeError("Recipe contains an unsafe or missing bundled reference path.")
    path = (root / REFERENCES_DIR_NAME / rel).resolve()
    try:
        path.relative_to((root / REFERENCES_DIR_NAME).resolve())
    except ValueError as exc:
        raise PatchRecipeError("Recipe reference path escapes Patch_References.") from exc
    if not path.is_file():
        raise PatchRecipeError(f"Bundled patch reference is missing: {rel}")
    return path


def create_verified_patched_copy(
    recipe: dict[str, Any],
    workspace: Path,
    app_dir: Path | None = None,
) -> Path:
    """Apply exact-signature, equal-length byte changes to a temporary copy.

    The returned file is a new working copy in `workspace`. The source reference
    is hash-checked before and after use and is never opened for writing.
    """
    if recipe.get("status") != "verified":
        raise PatchRecipeError(
            f"Recipe '{recipe.get('recipe_id', '<unnamed>')}' is research-only and cannot patch game code."
        )
    if recipe.get("binary_format") != "ps2_elf32_mips":
        raise PatchRecipeError(
            "This framework currently accepts only verified, same-length PS2 ELF/MIPS patches. "
            "PSP decrypted ELF files must not be inserted into an encrypted PSP ISO."
        )
    ref = reference_path_for_recipe(recipe, app_dir)
    before_hash = sha256_path(ref)
    expected_hash = str(recipe.get("reference_sha256", "")).lower()
    if not expected_hash or before_hash != expected_hash:
        raise PatchRecipeError(f"Reference executable hash does not match recipe '{recipe.get('recipe_id')}'.")
    if not ref.read_bytes()[:4] == b"\x7fELF":
        raise PatchRecipeError("The bundled PS2 reference does not have an ELF signature.")

    patches = recipe.get("patches")
    if not isinstance(patches, list) or not patches:
        raise PatchRecipeError("Verified recipe has no patch entries; refusing to create an unmodified output.")
    staged = bytearray(ref.read_bytes())
    claimed_ranges: list[tuple[int, int]] = []
    for patch in patches:
        try:
            offset = int(patch["offset"])
            expected = bytes.fromhex(str(patch["expected_hex"]))
            replacement = bytes.fromhex(str(patch["replacement_hex"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PatchRecipeError("Recipe contains malformed offset or byte-signature fields.") from exc
        if offset < 0 or not expected or len(expected) != len(replacement):
            raise PatchRecipeError("Only non-empty, equal-length patches with a valid offset are permitted.")
        end = offset + len(expected)
        if end > len(staged):
            raise PatchRecipeError("Patch byte range extends beyond the reference executable.")
        if any(offset < b and a < end for a, b in claimed_ranges):
            raise PatchRecipeError("Patch entries overlap; refusing ambiguous recipe.")
        claimed_ranges.append((offset, end))
        if bytes(staged[offset:end]) != expected:
            raise PatchRecipeError(
                f"Original byte signature mismatch at file offset 0x{offset:X}; executable was not modified."
            )
        staged[offset:end] = replacement

    patched_hash = hashlib.sha256(staged).hexdigest()
    if patched_hash == before_hash:
        raise PatchRecipeError("Recipe produced no binary change; refusing to publish an unchanged patched executable.")
    if sha256_path(ref) != before_hash:
        raise PatchRecipeError("The bundled original reference changed while patching; aborting.")

    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(recipe.get("recipe_id", "patch")))[:80]
    destination = workspace / f"{safe_id}_patched.elf"
    if destination.resolve() == ref.resolve():
        raise PatchRecipeError("Patch output must not overwrite the bundled reference.")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{safe_id}_", suffix=".tmp", dir=workspace, delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(staged)
            handle.flush()
            os.fsync(handle.fileno())
        check = temp_path.read_bytes()
        if hashlib.sha256(check).hexdigest() != patched_hash:
            raise PatchRecipeError("Patched temporary copy failed its SHA-256 verification.")
        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return destination


def run_self_test() -> None:
    """Offline self-tests for signature gates, research refusal, and copy safety."""
    with tempfile.TemporaryDirectory(prefix="gta_exec_patch_core_test_") as temp_name:
        root = Path(temp_name)
        ref_dir = root / REFERENCES_DIR_NAME
        ref_dir.mkdir()
        ref = ref_dir / "fake_ps2.elf"
        original = b"\x7fELF" + bytes(range(32))
        ref.write_bytes(original)
        original_hash = hashlib.sha256(original).hexdigest()
        recipes_path = ref_dir / RECIPES_NAME
        recipes_path.write_text(json.dumps({"schema_version": 1, "recipes": []}), encoding="utf-8")
        manifest_path = ref_dir / MANIFEST_NAME
        manifest_path.write_text(json.dumps({"schema_version": 1, "files": [{
            "path": "fake_ps2.elf", "size_bytes": len(original), "sha256": original_hash
        }]}), encoding="utf-8")
        check = verify_reference_manifest(root)
        assert check["ok"] and check["verified_count"] == 1
        recipe = {
            "recipe_id": "test_patch", "game_id": "test_game", "status": "verified",
            "binary_format": "ps2_elf32_mips", "reference_path": "fake_ps2.elf",
            "reference_sha256": original_hash,
            "patches": [{"offset": 8, "expected_hex": original[8:12].hex(),
                         "replacement_hex": b"ABCD".hex(), "description": "synthetic test"}],
        }
        workspace = root / "temp"; workspace.mkdir()
        patched = create_verified_patched_copy(recipe, workspace, root)
        assert patched.is_file() and patched.read_bytes()[8:12] == b"ABCD"
        assert ref.read_bytes() == original
        assert patched.read_bytes() != original and len(patched.read_bytes()) == len(original)
        try:
            create_verified_patched_copy({**recipe, "status": "research_only"}, workspace, root)
        except PatchRecipeError:
            pass
        else:
            raise AssertionError("Research-only recipes must be refused")
        try:
            create_verified_patched_copy({**recipe, "patches": [{
                "offset": 8, "expected_hex": "00000000", "replacement_hex": "41424344"
            }]}, workspace, root)
        except PatchRecipeError:
            pass
        else:
            raise AssertionError("Signature mismatch must be refused")
        assert ref.read_bytes() == original


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        run_self_test()
        print("Executable patch core self-test passed.")
    else:
        result = verify_reference_manifest()
        print(f"Reference manifest: {result['verified_count']}/{result['file_count']} files verified")
        for failure in result["failures"]:
            print(f"ERROR: {failure}")
