"""
app.py — Meeting Recorder + Local Transcriber (Windows, offline).

Records system audio + microphone (WASAPI), transcribes with whisper.cpp
(large-v3-turbo, CPU-only), applies a domain glossary correction pass,
and saves everything into ./Transcripts next to the exe.
"""

import faulthandler
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import wave
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import glossary as glossary_mod
import transcriber
from recorder import Recorder

APP_TITLE = "Meeting Recorder + Transcriber"

LANG_CHOICES = {
    "Ελληνικά (el)": "el",
    "Auto-detect": "auto",
    "English (en)": "en",
}

# whisper-cli prints segments like: [00:01:23.400 --> 00:01:27.120]   text
SEGMENT_RE = re.compile(
    r"^\[(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})\]\s*(.*)$"
)

_CRASH_LOG_FILE = None  # kept open for faulthandler


def install_crash_logging():
    """Write any unhandled Python or native error to error_log.txt next to the exe."""
    global _CRASH_LOG_FILE
    log_path = transcriber.base_dir() / "error_log.txt"

    try:
        _CRASH_LOG_FILE = open(log_path, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=_CRASH_LOG_FILE)  # catches native crashes too
    except Exception:
        pass

    def _write(kind, exc_type, exc, tb):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n=== {kind} @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                f.write("".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass

    def excepthook(exc_type, exc, tb):
        _write("unhandled exception", exc_type, exc, tb)

    def thread_hook(args):
        _write(f"thread exception ({args.thread.name})",
               args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = excepthook
    threading.excepthook = thread_hook


def fmt_mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def wav_duration_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getnframes() / max(1, wf.getframerate())
    except Exception:
        return 0.0


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("900x680")
        root.minsize(760, 560)

        self.base = transcriber.base_dir()
        self.transcripts_dir = self.base / "Transcripts"
        self.transcripts_dir.mkdir(exist_ok=True)
        self.glossary_path = self.base / "glossary.json"

        self.recorder = Recorder(self.transcripts_dir)
        self.record_started = None
        self.busy = False
        self.transcribe_started = None
        self.audio_duration = 0.0
        self.live_preview_active = False
        self.ui_queue = queue.Queue()

        self._style()
        self._build_ui()
        self.refresh_recordings()
        self.refresh_models()
        self._poll_queue()

    # ------------------------------------------------------------------ style

    def _style(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure(".", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI Semibold", 14))
        style.configure("Sub.TLabel", font=("Segoe UI", 9), foreground="#666666")
        style.configure("Timer.TLabel", font=("Consolas", 20, "bold"))
        style.configure("Meter.Horizontal.TProgressbar", thickness=8)
        style.configure("Trans.Horizontal.TProgressbar", thickness=14)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=10, pady=(10, 2))
        ttk.Label(header, text=APP_TITLE, style="Header.TLabel").pack(side="left")
        ttk.Label(
            header, text="   offline · whisper.cpp · Greek", style="Sub.TLabel"
        ).pack(side="left", pady=(6, 0))

        # --- Recording section
        rec_frame = ttk.LabelFrame(self.root, text=" 1 · Record ")
        rec_frame.pack(fill="x", **pad)

        left = ttk.Frame(rec_frame)
        left.pack(side="left", padx=12, pady=10)

        self.record_btn = tk.Button(
            left,
            text="●  Record",
            command=self.toggle_record,
            font=("Segoe UI Semibold", 11),
            width=14,
            bg="#2e7d32",
            fg="white",
            activebackground="#1b5e20",
            activeforeground="white",
            relief="flat",
            cursor="hand2",
        )
        self.record_btn.pack()

        self.timer_label = ttk.Label(rec_frame, text="00:00", style="Timer.TLabel")
        self.timer_label.pack(side="left", padx=16)

        meters = ttk.Frame(rec_frame)
        meters.pack(side="left", padx=16, fill="x", expand=True, pady=8)

        ttk.Label(meters, text="System audio", style="Sub.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.sys_meter = ttk.Progressbar(
            meters, style="Meter.Horizontal.TProgressbar", maximum=100, length=260
        )
        self.sys_meter.grid(row=1, column=0, sticky="we", pady=(0, 6))

        ttk.Label(meters, text="Microphone", style="Sub.TLabel").grid(
            row=2, column=0, sticky="w"
        )
        self.mic_meter = ttk.Progressbar(
            meters, style="Meter.Horizontal.TProgressbar", maximum=100, length=260
        )
        self.mic_meter.grid(row=3, column=0, sticky="we")
        meters.columnconfigure(0, weight=1)

        # --- Transcription section
        tr_frame = ttk.LabelFrame(self.root, text=" 2 · Transcribe ")
        tr_frame.pack(fill="x", **pad)

        row1 = ttk.Frame(tr_frame)
        row1.pack(fill="x", padx=10, pady=(8, 4))

        ttk.Label(row1, text="Recording:").pack(side="left")
        self.recording_var = tk.StringVar()
        self.recording_combo = ttk.Combobox(
            row1, textvariable=self.recording_var, state="readonly", width=40
        )
        self.recording_combo.pack(side="left", padx=6)
        ttk.Button(row1, text="↻ Refresh", command=self.refresh_recordings, width=10).pack(
            side="left", padx=4
        )

        row2 = ttk.Frame(tr_frame)
        row2.pack(fill="x", padx=10, pady=4)

        ttk.Label(row2, text="Model:").pack(side="left")
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(
            row2, textvariable=self.model_var, state="readonly", width=32
        )
        self.model_combo.pack(side="left", padx=6)

        ttk.Label(row2, text="Language:").pack(side="left", padx=(12, 0))
        self.lang_var = tk.StringVar(value="Ελληνικά (el)")
        lang_combo = ttk.Combobox(
            row2,
            textvariable=self.lang_var,
            state="readonly",
            values=list(LANG_CHOICES.keys()),
            width=15,
        )
        lang_combo.pack(side="left", padx=6)

        self.transcribe_btn = tk.Button(
            row2,
            text="Transcribe  ▶",
            command=self.start_transcription,
            font=("Segoe UI Semibold", 10),
            width=15,
            bg="#1565c0",
            fg="white",
            activebackground="#0d47a1",
            activeforeground="white",
            relief="flat",
            cursor="hand2",
        )
        self.transcribe_btn.pack(side="left", padx=14)

        prog_row = ttk.Frame(tr_frame)
        prog_row.pack(fill="x", padx=10, pady=(4, 10))
        self.progressbar = ttk.Progressbar(
            prog_row, style="Trans.Horizontal.TProgressbar", maximum=100
        )
        self.progressbar.pack(fill="x")
        self.progress_label = ttk.Label(prog_row, text="", style="Sub.TLabel")
        self.progress_label.pack(anchor="w", pady=(3, 0))

        # --- Transcript text area
        txt_frame = ttk.LabelFrame(self.root, text=" Transcript ")
        txt_frame.pack(fill="both", expand=True, **pad)

        self.text = tk.Text(
            txt_frame,
            wrap="word",
            font=("Segoe UI", 10),
            relief="flat",
            padx=8,
            pady=6,
            background="#fcfcfc",
        )
        scroll = ttk.Scrollbar(txt_frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        scroll.pack(side="right", fill="y", pady=6)
        self.text.tag_configure("preview", foreground="#888888")
        self.text.tag_configure("preview_time", foreground="#bbbbbb")

        # --- Bottom buttons + status
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(
            bottom, text="Open Transcripts folder", command=self.open_transcripts
        ).pack(side="left", padx=4)
        ttk.Button(
            bottom, text="Open glossary file", command=self.open_glossary
        ).pack(side="left", padx=4)
        ttk.Button(
            bottom,
            text="Re-apply glossary to selected",
            command=self.reapply_glossary,
        ).pack(side="left", padx=4)

        self.status = ttk.Label(self.root, text="Ready", anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")

    # ------------------------------------------------------- record logic

    def toggle_record(self):
        if self.recorder.is_recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        # Device discovery/opening can stall on misbehaving drivers — never
        # do it on the UI thread.
        self.record_btn.config(state="disabled", text="Starting…")
        self.set_status("Opening audio devices…")

        def worker():
            try:
                notes = self.recorder.start()
                self.ui_queue.put(("record_started", notes))
            except Exception as exc:
                self.ui_queue.put(("record_start_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _tick_record_timer(self):
        if self.recorder.is_recording and self.record_started:
            elapsed = time.time() - self.record_started
            self.timer_label.config(text=fmt_mmss(elapsed))
            levels = self.recorder.get_levels()
            self.sys_meter["value"] = (levels.get("System audio", 0.0) ** 0.5) * 100
            self.mic_meter["value"] = (levels.get("Microphone", 0.0) ** 0.5) * 100
            self.root.after(100, self._tick_record_timer)
        else:
            self.sys_meter["value"] = 0
            self.mic_meter["value"] = 0

    def _stop_recording(self):
        self.record_btn.config(state="disabled", text="Saving…")
        self.set_status("Finalizing recording…")

        def worker():
            try:
                wav_path, notes = self.recorder.stop()
                self.ui_queue.put(("record_done", (wav_path, notes)))
            except Exception as exc:
                self.ui_queue.put(("record_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _set_record_button_idle(self):
        self.record_btn.config(
            state="normal", text="●  Record", bg="#2e7d32", activebackground="#1b5e20"
        )
        self.timer_label.config(text="00:00")

    # --------------------------------------------------- transcription

    def start_transcription(self):
        if self.busy:
            return
        if self.recorder.is_recording:
            messagebox.showinfo("Recording in progress", "Stop the recording first.")
            return
        rec_name = self.recording_var.get()
        if not rec_name:
            messagebox.showinfo("No recording selected", "Select a recording first.")
            return
        wav_path = self.transcripts_dir / rec_name
        if not wav_path.exists():
            messagebox.showerror("Missing file", f"File not found:\n{wav_path}")
            self.refresh_recordings()
            return

        model_name = self.model_var.get()
        if not model_name:
            messagebox.showerror(
                "No model found",
                "No .bin model file found in the models folder.\n\n"
                "Download ggml-large-v3-turbo.bin (see README) and place it in:\n"
                f"{transcriber.models_dir()}",
            )
            return
        model_path = transcriber.models_dir() / model_name
        language = LANG_CHOICES.get(self.lang_var.get(), "el")

        self.busy = True
        self.transcribe_btn.config(state="disabled")
        self.transcribe_started = time.time()
        self.audio_duration = wav_duration_seconds(wav_path)
        self.progressbar["value"] = 0
        self.progress_label.config(
            text="Loading model… (first progress can take a minute on older CPUs)"
        )
        self.text.delete("1.0", "end")
        self.text.insert(
            "1.0",
            "— live preview: segments appear below as they are transcribed; "
            "the corrected transcript replaces this when done —\n\n",
            "preview",
        )
        self.live_preview_active = True

        def on_line(line):
            m = SEGMENT_RE.match(line.strip())
            if m:
                h, mi, s, ms = int(m.group(5)), int(m.group(6)), int(m.group(7)), int(m.group(8))
                seg_end = h * 3600 + mi * 60 + s + ms / 1000.0
                seg_text = m.group(9).strip()
                self.ui_queue.put(("live_segment", (seg_end, seg_text)))

        def on_progress(pct):
            # coarse fallback (whisper reports every 5%); segment timestamps
            # usually give finer progress via on_line
            self.ui_queue.put(("progress_pct", pct))

        def worker():
            try:
                raw_path = transcriber.transcribe(
                    wav_path,
                    model_path,
                    language=language,
                    on_progress=on_progress,
                    on_line=on_line,
                )
                corrected_path, count = glossary_mod.correct_file(
                    raw_path, self.glossary_path
                )
                self.ui_queue.put(
                    (
                        "transcribe_done",
                        (raw_path, corrected_path, count, time.time() - self.transcribe_started),
                    )
                )
            except Exception as exc:
                self.ui_queue.put(("transcribe_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _update_progress(self, pct):
        pct = max(0.0, min(100.0, pct))
        self.progressbar["value"] = pct
        elapsed = time.time() - (self.transcribe_started or time.time())
        if pct >= 2:
            remaining = elapsed * (100.0 - pct) / pct
            eta = f"about {fmt_mmss(remaining)} left"
        else:
            eta = "estimating time…"
        self.progress_label.config(
            text=f"Transcribing… {pct:.0f}%   ·   elapsed {fmt_mmss(elapsed)}   ·   {eta}"
        )

    def reapply_glossary(self):
        """Re-run the glossary pass on the selected recording's raw transcript."""
        rec_name = self.recording_var.get()
        if not rec_name:
            messagebox.showinfo("No recording selected", "Select a recording first.")
            return
        stem = Path(rec_name).stem
        if stem.endswith("_recording"):
            stem = stem[: -len("_recording")]
        raw_path = self.transcripts_dir / f"{stem}_transcript.txt"
        if not raw_path.exists():
            messagebox.showinfo(
                "No transcript yet",
                "This recording has not been transcribed yet.",
            )
            return
        try:
            corrected_path, count = glossary_mod.correct_file(
                raw_path, self.glossary_path
            )
        except Exception as exc:
            messagebox.showerror("Glossary error", str(exc))
            return
        self._show_file(corrected_path)
        self.set_status(
            f"Glossary re-applied: {count} replacement(s) → {corrected_path.name}"
        )

    # ------------------------------------------------------------ helpers

    def refresh_recordings(self):
        wavs = sorted(
            self.transcripts_dir.glob("*.wav"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        names = [p.name for p in wavs]
        self.recording_combo["values"] = names
        if names and not self.recording_var.get():
            self.recording_var.set(names[0])
        if self.recording_var.get() not in names:
            self.recording_var.set(names[0] if names else "")

    def refresh_models(self):
        models = transcriber.list_models()
        names = [m.name for m in models]
        self.model_combo["values"] = names
        if names:
            preferred = next((n for n in names if "large-v3-turbo" in n), names[0])
            self.model_var.set(preferred)
        else:
            self.model_var.set("")

    def open_transcripts(self):
        self._open_path(self.transcripts_dir)

    def open_glossary(self):
        if not self.glossary_path.exists():
            messagebox.showerror("Missing file", f"Not found: {self.glossary_path}")
            return
        self._open_path(self.glossary_path)

    def _open_path(self, path: Path):
        try:
            import os

            os.startfile(str(path))  # Windows
        except Exception:
            try:
                subprocess.Popen(["explorer", str(path)])
            except Exception as exc:
                messagebox.showerror("Open failed", str(exc))

    def _show_file(self, path: Path):
        try:
            content = Path(path).read_text(encoding="utf-8-sig")
        except Exception as exc:
            content = f"(could not read {path}: {exc})"
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)

    def set_status(self, msg):
        self.status.config(text=msg)

    # --------------------------------------------------------- queue pump

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "record_started":
                    self.record_started = time.time()
                    self.record_btn.config(
                        state="normal",
                        text="■  Stop",
                        bg="#c62828",
                        activebackground="#8e0000",
                    )
                    self.set_status(" | ".join(payload))
                    self._tick_record_timer()
                elif kind == "record_start_error":
                    self._set_record_button_idle()
                    messagebox.showerror("Recording error", payload)
                    self.set_status("Could not start recording")
                elif kind == "record_done":
                    wav_path, notes = payload
                    self._set_record_button_idle()
                    self.refresh_recordings()
                    self.recording_var.set(wav_path.name)
                    warn = f"  ({'; '.join(notes)})" if notes else ""
                    self.set_status(f"Saved {wav_path.name}{warn}")
                elif kind == "record_error":
                    self._set_record_button_idle()
                    messagebox.showerror("Recording error", payload)
                    self.set_status("Recording failed")
                elif kind == "live_segment":
                    seg_end, seg_text = payload
                    if self.live_preview_active:
                        self.text.insert(
                            "end", f"[{fmt_mmss(seg_end)}] ", "preview_time"
                        )
                        self.text.insert("end", seg_text + "\n", "preview")
                        self.text.see("end")
                    if self.audio_duration > 0:
                        self._update_progress(100.0 * seg_end / self.audio_duration)
                elif kind == "progress_pct":
                    # only used until the first segment arrives / as fallback
                    if self.progressbar["value"] < payload:
                        self._update_progress(float(payload))
                elif kind == "transcribe_done":
                    raw_path, corrected_path, count, secs = payload
                    self.busy = False
                    self.live_preview_active = False
                    self.transcribe_btn.config(state="normal")
                    self.progressbar["value"] = 100
                    self.progress_label.config(text="")
                    self._show_file(corrected_path)
                    self.set_status(
                        f"Done in {fmt_mmss(secs)} — {corrected_path.name} "
                        f"({count} glossary fixes; raw kept as {raw_path.name})"
                    )
                elif kind == "transcribe_error":
                    self.busy = False
                    self.live_preview_active = False
                    self.transcribe_btn.config(state="normal")
                    self.progressbar["value"] = 0
                    self.progress_label.config(text="")
                    messagebox.showerror("Transcription error", payload)
                    self.set_status("Transcription failed")
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)


def main():
    install_crash_logging()
    root = tk.Tk()

    def tk_error(exc_type, exc, tb):
        try:
            log_path = transcriber.base_dir() / "error_log.txt"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"\n=== tkinter callback exception @ {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                )
                f.write("".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        try:
            messagebox.showerror(
                "Unexpected error",
                f"{exc_type.__name__}: {exc}\n\nDetails in error_log.txt",
            )
        except Exception:
            pass

    root.report_callback_exception = tk_error
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)  # crisp UI on high-DPI screens
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
