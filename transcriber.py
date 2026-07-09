"""
transcriber.py — run whisper-cli.exe (whisper.cpp) on a WAV file, batch mode, CPU-only.

Layout expected next to the app executable:
    whisper-cli.exe
    models/ggml-*.bin   (downloaded separately, see README)
"""

import os
import re
import subprocess
import sys
from pathlib import Path

PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)%")


def base_dir() -> Path:
    """Folder the app runs from (next to app.exe when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_whisper_cli() -> Path | None:
    base = base_dir()
    for candidate in (base / "whisper-cli.exe", base / "whisper" / "whisper-cli.exe"):
        if candidate.exists():
            return candidate
    return None


def models_dir() -> Path:
    return base_dir() / "models"


def list_models() -> list[Path]:
    d = models_dir()
    if not d.exists():
        return []
    return sorted(d.glob("*.bin"))


def default_threads() -> int:
    return max(1, min(8, os.cpu_count() or 4))


def transcribe(
    wav_path,
    model_path,
    language="el",
    on_progress=None,
    on_line=None,
) -> Path:
    """Run whisper-cli on wav_path. Returns path to the raw transcript .txt.

    language: "el", "en", or "auto".
    on_progress: optional callback(percent:int).
    on_line: optional callback(str) for raw tool output lines.
    """
    wav_path = Path(wav_path)
    model_path = Path(model_path)
    cli = find_whisper_cli()
    if cli is None:
        raise FileNotFoundError(
            "whisper-cli.exe not found next to the app. "
            "Re-download the release package."
        )
    if not model_path.exists():
        raise FileNotFoundError(f"Model file not found: {model_path}")

    stem = wav_path.stem
    if stem.endswith("_recording"):
        out_base = wav_path.with_name(stem[: -len("_recording")] + "_transcript")
    else:
        out_base = wav_path.with_name(stem + "_transcript")

    cmd = [
        str(cli),
        "-m", str(model_path),
        "-f", str(wav_path),
        "-l", language,
        "-t", str(default_threads()),
        "-otxt",
        "-of", str(out_base),
        "-pp",  # print progress
    ]

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )

    output_tail = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        output_tail.append(line)
        if len(output_tail) > 50:
            output_tail.pop(0)
        if on_line:
            on_line(line)
        if on_progress:
            m = PROGRESS_RE.search(line)
            if m:
                try:
                    on_progress(int(m.group(1)))
                except Exception:
                    pass

    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(
            f"whisper-cli failed (exit {proc.returncode}).\n"
            + "\n".join(output_tail[-15:])
        )

    txt_path = Path(str(out_base) + ".txt")
    if not txt_path.exists():
        raise RuntimeError("whisper-cli finished but no transcript file was produced.")
    return txt_path
