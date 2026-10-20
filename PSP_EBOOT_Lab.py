#!/usr/bin/env python3
"""Optional Windows PSP PRX/EBOOT decryption launcher and static scanner.

The bundled pspdecrypt.exe is user-supplied and is only invoked after an explicit
user action. This module never executes a game EBOOT. Decrypted outputs are
analysis artifacts and must not be inserted into retail PSP images directly.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import threading
import queue
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

MODULE_DIR = Path(__file__).resolve().parent
TOOL_RELATIVE_CANDIDATES = (
    Path("Tools") / "pspdecrypt" / "pspdecrypt.exe",
    Path("tools") / "pspdecrypt" / "pspdecrypt.exe",
)
MAX_ANALYZE_BYTES = 512 * 1024 * 1024
ASCII_STRING_RE = re.compile(rb"[\x20-\x7e]{4,}")
UTF16_STRING_RE = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
INTERESTING_RE = re.compile(
    r"radio|music|audio|atrac|\.at3|at3header|at3hedeu|m_lookup|\bkjah\b|\bvcpr\b|"
    r"stream|track|length|duration|random|seek|sample|wave|rpc|\.vb\b|\.bin\b|head",
    re.IGNORECASE,
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_pspdecrypt(app_dir: str | Path | None = None) -> Path | None:
    roots: list[Path] = []
    if app_dir is not None:
        roots.append(Path(app_dir).expanduser().resolve())
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass).resolve())
    roots.append(MODULE_DIR)
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        for rel in TOOL_RELATIVE_CANDIDATES:
            candidate = root / rel
            if candidate.is_file():
                return candidate
    return None


def build_decrypt_command(tool_path: str | Path, input_path: str | Path, output_path: str | Path) -> list[str]:
    """Return argv without shell expansion; user paths remain separate arguments."""
    tool = Path(tool_path).expanduser().resolve()
    src = Path(input_path).expanduser().resolve()
    dst = Path(output_path).expanduser().resolve()
    if src == dst:
        raise ValueError("Decrypted output must be a separate file; the encrypted input cannot be overwritten.")
    if not tool.is_file():
        raise FileNotFoundError(f"pspdecrypt was not found: {tool}")
    if not src.is_file():
        raise FileNotFoundError(f"Input PSP executable was not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    # -o is supported by the supplied pspdecrypt utility. It accepts a PRX/PSP
    # executable as the positional input; a PBP is also accepted by upstream.
    return [str(tool), "-o", str(dst), str(src)]


def _elf_summary(data: bytes) -> list[str]:
    lines: list[str] = []
    if len(data) < 0x34 or data[:4] != b"\x7fELF":
        return lines
    elf_class = data[4]
    data_encoding = data[5]
    endian = "little" if data_encoding == 1 else "big" if data_encoding == 2 else None
    lines.append(f"ELF class: { {1: '32-bit', 2: '64-bit'}.get(elf_class, f'unknown ({elf_class})') }")
    lines.append(f"ELF byte order: {endian or f'unknown ({data_encoding})'}")
    if endian is None:
        return lines
    prefix = "<" if endian == "little" else ">"
    try:
        e_type, e_machine = struct.unpack_from(prefix + "HH", data, 16)
        e_version = struct.unpack_from(prefix + "I", data, 20)[0]
        if elf_class == 1 and len(data) >= 52:
            entry, phoff, shoff = struct.unpack_from(prefix + "III", data, 24)
            ehsize, phentsize, phnum, shentsize, shnum, shstrndx = struct.unpack_from(prefix + "HHHHHH", data, 40)
        elif elf_class == 2 and len(data) >= 64:
            entry = struct.unpack_from(prefix + "Q", data, 24)[0]
            phoff = struct.unpack_from(prefix + "Q", data, 32)[0]
            shoff = struct.unpack_from(prefix + "Q", data, 40)[0]
            ehsize, phentsize, phnum, shentsize, shnum, shstrndx = struct.unpack_from(prefix + "HHHHHH", data, 52)
        else:
            return lines
        machine_names = {8: "MIPS", 3: "x86", 40: "ARM", 62: "x86-64", 183: "AArch64"}
        type_names = {1: "REL", 2: "EXEC", 3: "DYN", 4: "CORE"}
        lines.extend([
            f"ELF type: {type_names.get(e_type, str(e_type))}",
            f"Machine: {machine_names.get(e_machine, f'unknown ({e_machine})')}",
            f"ELF version: {e_version}",
            f"Entry point: 0x{entry:X}",
            f"Program headers: offset 0x{phoff:X}, entry size {phentsize}, count {phnum}",
            f"Section headers: offset 0x{shoff:X}, entry size {shentsize}, count {shnum}, string table index {shstrndx}",
            f"ELF header size: {ehsize}",
        ])
    except (struct.error, IndexError):
        lines.append("ELF header appears truncated or malformed.")
    return lines


def _interesting_strings(data: bytes, limit: int = 400) -> list[tuple[int, str, str]]:
    found: dict[tuple[int, str], tuple[int, str, str]] = {}
    for match in ASCII_STRING_RE.finditer(data):
        raw = match.group(0)
        try:
            value = raw.decode("ascii", errors="replace")
        except Exception:
            continue
        if INTERESTING_RE.search(value):
            found[(match.start(), "ASCII")] = (match.start(), "ASCII", value[:240])
    # UTF-16LE strings are useful in mixed PSP resources, too. Avoid interpreting
    # every ASCII string again: only consider alternating printable/NUL bytes.
    for match in UTF16_STRING_RE.finditer(data):
        raw = match.group(0)
        try:
            value = raw.decode("utf-16le", errors="replace")
        except Exception:
            continue
        if INTERESTING_RE.search(value):
            found[(match.start(), "UTF-16LE")] = (match.start(), "UTF-16LE", value[:240])
    return sorted(found.values(), key=lambda item: item[0])[:limit]


def analyze_binary_bytes(data: bytes, display_name: str = "input") -> dict:
    if len(data) > MAX_ANALYZE_BYTES:
        raise ValueError(f"Analysis is limited to {MAX_ANALYZE_BYTES // (1024 * 1024)} MiB per file.")
    header_lines: list[str] = []
    kind = "Unknown binary"
    if data.startswith(b"\x7fELF"):
        kind = "ELF executable/object"
        header_lines.extend(_elf_summary(data))
    elif data.startswith(b"~PSP"):
        kind = "PSP PRX/EBOOT container"
        header_lines.append("PSP magic: ~PSP")
        if len(data) >= 0xD4:
            tag = data[0xD0:0xD4]
            header_lines.append(f"PRX tag at 0xD0: {tag.hex(' ').upper()} (little-endian 0x{int.from_bytes(tag, 'little'):08X})")
        if len(data) >= 0x2C:
            declared_size = struct.unpack_from("<I", data, 0x2C)[0]
            header_lines.append(f"Header-declared size candidate at 0x2C: {declared_size:,} bytes")
        header_lines.append("A PSP header/tag does not by itself reveal a reusable encryption key.")
    elif data.startswith(b"\x00PBP"):
        kind = "PSP PBP container"
        header_lines.append("PBP magic: 00 50 42 50")
        if len(data) >= 40:
            version = struct.unpack_from("<I", data, 4)[0]
            offsets = struct.unpack_from("<8I", data, 8)
            header_lines.append(f"PBP version field: 0x{version:08X}")
            header_lines.append("PBP section offsets: " + ", ".join(f"0x{x:X}" for x in offsets))
    elif data.startswith(b"PSAR"):
        kind = "PSAR update archive"
        header_lines.append(f"PSAR version byte: {data[4] if len(data) > 4 else 'unavailable'}")
    elif len(data) >= 0x100 and data[0xD0:0xD4] in (b"\x7c\x16\xcb\xc0", b"\xc0\xcb\x16\x7c"):
        kind = "Possible tagged PSP PRX (signature not at offset 0)"
        header_lines.append(f"Possible PRX tag at 0xD0: {data[0xD0:0xD4].hex(' ').upper()}")
    strings = _interesting_strings(data)
    return {
        "display_name": display_name,
        "size_bytes": len(data),
        "sha256": sha256_bytes(data),
        "kind": kind,
        "header_lines": header_lines,
        "strings": strings,
    }


def analyze_file(path: str | Path) -> dict:
    file_path = Path(path).expanduser().resolve()
    size = file_path.stat().st_size
    if size > MAX_ANALYZE_BYTES:
        raise ValueError(f"Analysis is limited to {MAX_ANALYZE_BYTES // (1024 * 1024)} MiB per file.")
    data = file_path.read_bytes()
    result = analyze_binary_bytes(data, file_path.name)
    result["path"] = str(file_path)
    return result


def format_analysis_report(result: dict) -> str:
    lines = [
        "PSP EBOOT STATIC ANALYSIS",
        "=" * 72,
        f"File: {result.get('path', result.get('display_name', 'input'))}",
        f"Classification: {result['kind']}",
        f"Size: {result['size_bytes']:,} bytes",
        f"SHA-256: {result['sha256']}",
        "",
        "HEADER SUMMARY",
        "-" * 72,
    ]
    lines.extend(result["header_lines"] or ["No recognized executable/container header fields were decoded."])
    lines.extend(["", "INTERESTING PRINTABLE STRINGS", "-" * 72])
    if result["strings"]:
        for offset, encoding, value in result["strings"]:
            lines.append(f"0x{offset:08X} [{encoding}] {value}")
    else:
        lines.append("No matching radio/audio strings found by the basic scanner.")
    lines.extend([
        "",
        "LIMITATIONS",
        "-" * 72,
        "This is a static triage report, not a full disassembly or proof of runtime behaviour.",
        "String offsets are file offsets and must be mapped through ELF segments before treating them as code addresses.",
        "pspdecrypt is a decryptor; it does not automatically re-encrypt edited retail EBOOTs.",
    ])
    return "\n".join(lines) + "\n"


def run_pspdecrypt(tool_path: str | Path, input_path: str | Path, output_path: str | Path) -> tuple[int, str, str]:
    if os.name != "nt":
        raise OSError("The bundled pspdecrypt.exe is a Windows x64 console program. Run decryption from the Windows build of GTA Modding Studio.")
    argv = build_decrypt_command(tool_path, input_path, output_path)
    proc = subprocess.run(argv, cwd=str(Path(tool_path).resolve().parent), capture_output=True, text=True, errors="replace", timeout=600)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


class PSPEbootLab:
    def __init__(self, parent: tk.Misc, app_dir: str | Path):
        self.parent = parent
        self.app_dir = Path(app_dir).resolve()
        self.window = tk.Toplevel(parent)
        self.window.title("PSP EBOOT Lab — Decrypt & Static Analysis")
        self.window.geometry("980x690")
        self.window.minsize(760, 520)
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.tool_var = tk.StringVar(value=str(find_pspdecrypt(self.app_dir) or ""))
        self.status_var = tk.StringVar(value="Ready — choose an encrypted PSP EBOOT/PRX or PBP file.")
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="PSP EBOOT Lab", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="Decrypt and statically inspect a PSP executable. This never runs the game binary and never edits the source file.",
            wraplength=900,
        ).pack(fill="x", anchor="w", pady=(2, 8))
        self._file_row(frame, "Encrypted input", self.input_var, self._choose_input)
        self._file_row(frame, "Decrypted output", self.output_var, self._choose_output, save=True)
        self._file_row(frame, "pspdecrypt.exe", self.tool_var, self._choose_tool)
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="Inspect Input Header", command=self._inspect_input).pack(side="left", padx=(0, 6))
        self.decrypt_button = ttk.Button(actions, text="Decrypt + Static Scan", command=self._start_decrypt)
        self.decrypt_button.pack(side="left", padx=6)
        ttk.Button(actions, text="Open Upstream Project", command=self._open_upstream).pack(side="left", padx=6)
        self.output_text = ScrolledText(frame, wrap="word", height=23, bg="#0b1220", fg="#e5edf7", insertbackground="white")
        self.output_text.pack(fill="both", expand=True)
        self.output_text.insert("1.0", self._tool_status() + "\n\nReference: https://github.com/John-K/pspdecrypt\n")
        ttk.Label(frame, textvariable=self.status_var).pack(fill="x", anchor="w", pady=(7, 0))
        if os.name != "nt":
            self.decrypt_button.configure(state="disabled")
            self.status_var.set("Bundled tool is Windows x64; decryption runs on Windows, not in this Linux analysis environment.")

    def _file_row(self, parent, label, var, callback, save: bool = False) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, width=18).pack(side="left")
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Browse…", command=callback).pack(side="left")

    def _choose_input(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.window,
            title="Choose encrypted PSP EBOOT/PRX or PBP",
            filetypes=[("PSP executable/container", "*.BIN *.bin *.PSP *.psp *.PRX *.prx *.PBP *.pbp *.ELF *.elf"), ("All files", "*.*")],
        )
        if path:
            self.input_var.set(path)
            source = Path(path)
            proposed = source.with_name(source.stem + "_decrypted.bin")
            if not self.output_var.get().strip() or Path(self.output_var.get()).resolve() == source.resolve():
                self.output_var.set(str(proposed))

    def _choose_output(self) -> None:
        source = Path(self.input_var.get()) if self.input_var.get().strip() else None
        initial = source.stem + "_decrypted.bin" if source else "EBOOT_DECRYPTED.bin"
        path = filedialog.asksaveasfilename(
            parent=self.window, title="Choose decrypted output", initialfile=initial,
            defaultextension=".bin", filetypes=[("Binary output", "*.bin"), ("ELF output", "*.elf"), ("All files", "*.*")],
        )
        if path:
            self.output_var.set(path)

    def _choose_tool(self) -> None:
        path = filedialog.askopenfilename(parent=self.window, title="Choose pspdecrypt.exe", filetypes=[("Windows executable", "*.exe"), ("All files", "*.*")])
        if path:
            self.tool_var.set(path)

    def _choose_input_default_output(self) -> None:
        if self.input_var.get().strip() and not self.output_var.get().strip():
            src = Path(self.input_var.get())
            self.output_var.set(str(src.with_name(src.stem + "_decrypted.bin")))

    def _inspect_input(self) -> None:
        path = self.input_var.get().strip()
        if not path:
            messagebox.showinfo("PSP EBOOT Lab", "Choose an input file first.", parent=self.window)
            return
        try:
            result = analyze_file(path)
            report = format_analysis_report(result)
            self.output_text.delete("1.0", "end")
            self.output_text.insert("1.0", report)
            self.status_var.set(f"Inspected {Path(path).name}; source was read-only.")
        except Exception as exc:
            messagebox.showerror("Input inspection failed", str(exc), parent=self.window)

    def _start_decrypt(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        if os.name != "nt":
            messagebox.showerror("pspdecrypt unavailable", "The bundled pspdecrypt.exe requires Windows x64.", parent=self.window)
            return
        source = self.input_var.get().strip()
        output = self.output_var.get().strip()
        tool = self.tool_var.get().strip()
        if not source or not output or not tool:
            messagebox.showwarning("Choose files", "Select the input, output and pspdecrypt.exe first.", parent=self.window)
            return
        src = Path(source).expanduser()
        dst = Path(output).expanduser()
        if not src.is_file():
            messagebox.showerror("Missing input", f"Input file was not found:\n{src}", parent=self.window)
            return
        if src.resolve() == dst.resolve():
            messagebox.showerror("Unsafe output", "Output must be a separate file. The encrypted source will never be overwritten.", parent=self.window)
            return
        if dst.exists() and not messagebox.askyesno("Replace output?", f"The output exists:\n{dst}\n\nOverwrite it? The source input remains untouched.", parent=self.window):
            return
        self.decrypt_button.configure(state="disabled")
        self.status_var.set("Decrypting in background…")
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", "Running pspdecrypt with argument-safe subprocess invocation…\n")
        def worker() -> None:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                rc, stdout, stderr = run_pspdecrypt(tool, src, dst)
                if rc != 0:
                    raise RuntimeError(f"pspdecrypt exited with code {rc}.\n\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
                if not dst.is_file() or dst.stat().st_size == 0:
                    raise RuntimeError(f"pspdecrypt reported success but produced no non-empty output.\n\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
                result = analyze_file(dst)
                report_path = dst.with_name(dst.stem + "_analysis.txt")
                report = format_analysis_report(result)
                report_path.write_text(report, encoding="utf-8")
                self._queue.put(("ok", report, dst, report_path, stdout, stderr))
            except Exception as exc:
                self._queue.put(("error", str(exc)))
        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self.window.after(150, self._poll_worker)

    def _poll_worker(self) -> None:
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            if self._worker and self._worker.is_alive():
                self.window.after(150, self._poll_worker)
            else:
                self.decrypt_button.configure(state="normal")
            return
        self.decrypt_button.configure(state="normal")
        if item[0] == "error":
            self.status_var.set("Decryption failed — source left unchanged.")
            messagebox.showerror("PSP decryption failed", item[1], parent=self.window)
            self.output_text.delete("1.0", "end")
            self.output_text.insert("1.0", item[1])
            return
        _, report, output_path, report_path, stdout, stderr = item
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", report)
        self.status_var.set(f"Decrypted output created: {output_path.name}; analysis saved to {report_path.name}.")
        if stdout.strip() or stderr.strip():
            self.output_text.insert("end", "\n\nPSPDECRYPT PROCESS LOG\n" + (stdout or "") + ("\nSTDERR:\n" + stderr if stderr else ""))
        messagebox.showinfo("Decryption complete", f"Output:\n{output_path}\n\nStatic analysis report:\n{report_path}\n\nThe encrypted source was not changed. The decrypted ELF must not be inserted directly into a retail PSP ISO.", parent=self.window)

    def _tool_status(self) -> str:
        tool = Path(self.tool_var.get()) if self.tool_var.get().strip() else None
        if tool and tool.is_file():
            return f"Bundled PSPDecrypt: {tool}\nSHA-256: {sha256_file(tool)}"
        return "pspdecrypt.exe not found. The release should contain Tools/pspdecrypt/pspdecrypt.exe."

    def _open_upstream(self) -> None:
        import webbrowser
        webbrowser.open("https://github.com/John-K/pspdecrypt")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_window(parent: tk.Misc, app_dir: str | Path | None = None) -> PSPEbootLab:
    return PSPEbootLab(parent, app_dir or MODULE_DIR)


def run_self_test() -> None:
    # PSP header/tag detection
    psp = bytearray(0x220)
    psp[:4] = b"~PSP"
    psp[0xD0:0xD4] = bytes.fromhex("7C16CBC0")
    psp.extend(b"radio_kjah AUDIO/AT3HEADERS.BIN M_LOOKUP.BIN\x00")
    parsed = analyze_binary_bytes(bytes(psp), "synthetic_eboot.bin")
    assert parsed["kind"] == "PSP PRX/EBOOT container"
    assert any("7C 16 CB C0" in line for line in parsed["header_lines"])
    assert any("AT3HEADERS.BIN" in value for _, _, value in parsed["strings"])

    # ELF32 little-endian MIPS header parsing and radio string scan
    elf = bytearray(0x100)
    elf[:7] = b"\x7fELF\x01\x01\x01"
    struct.pack_into("<HHI", elf, 16, 2, 8, 1)
    struct.pack_into("<III", elf, 24, 0x08900000, 0x34, 0x80)
    struct.pack_into("<HHHHHH", elf, 40, 52, 32, 1, 40, 1, 0)
    elf[0x60:0x8D] = b"radio_kjah AUDIO/AT3HEADERS.BIN M_LOOKUP.BIN\x00"
    result = analyze_binary_bytes(bytes(elf), "synthetic.elf")
    assert result["kind"] == "ELF executable/object"
    assert any("Machine: MIPS" in line for line in result["header_lines"])
    assert any("radio_kjah" in value for _, _, value in result["strings"])

    # Refuse in-place operations independently of platform/tool availability.
    try:
        build_decrypt_command(Path("pspdecrypt.exe"), Path("some.elf"), Path("some.elf"))
    except ValueError:
        pass
    else:
        raise AssertionError("The PSP EBOOT lab allowed an in-place output path.")


if __name__ == "__main__":
    run_self_test()
    print("PSP EBOOT Lab self-tests passed.")
