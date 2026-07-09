"""
recorder.py — capture system audio (WASAPI loopback) + microphone simultaneously,
mix into a single 16 kHz mono WAV (the format whisper.cpp reads natively).

Windows-only. Uses pyaudiowpatch (PyAudio fork with WASAPI loopback support).

v1.0.1 hardening:
- Streams are opened in the MAIN thread with multiple format fallbacks, so a
  device that can't be opened fails LOUDLY at Record time (not silently at Stop).
- COM is initialized on every thread that touches WASAPI (a classic cause of
  zero-frame captures / native crashes in threaded Python audio apps).
- One persistent PyAudio instance for the whole app lifetime (repeated
  init/terminate cycles are a known source of access-violation crashes).
- Everything is logged to audio_debug.log next to the exe.
"""

import ctypes
import logging
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio

TARGET_RATE = 16000
CHUNK = 1024

_PA_LOCK = threading.Lock()
_PA_INSTANCE = None


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _get_logger() -> logging.Logger:
    logger = logging.getLogger("recorder")
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        try:
            fh = logging.FileHandler(
                _base_dir() / "audio_debug.log", encoding="utf-8"
            )
            fh.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            logger.addHandler(fh)
        except Exception:
            logger.addHandler(logging.NullHandler())
    return logger


def _coinit():
    """Initialize COM on the current thread (WASAPI needs COM)."""
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    except Exception:
        pass


def _get_pa():
    """One PyAudio instance for the app's lifetime (never terminated mid-run)."""
    global _PA_INSTANCE
    with _PA_LOCK:
        if _PA_INSTANCE is None:
            _coinit()
            _PA_INSTANCE = pyaudio.PyAudio()
        return _PA_INSTANCE


class _Reader(threading.Thread):
    """Reads an ALREADY-OPEN stream into memory until stop_event is set."""

    def __init__(self, stream, channels, rate, stop_event, label, log):
        super().__init__(daemon=True)
        self.stream = stream
        self.channels = channels
        self.rate = rate
        self.stop_event = stop_event
        self.label = label
        self.log = log
        self.frames = []
        self.error = None
        self.level = 0.0  # live peak level 0..1 for the UI meter

    def run(self):
        _coinit()
        self.log.info("[%s] reader thread started (%dch @ %d Hz)",
                      self.label, self.channels, self.rate)
        t0 = time.monotonic()
        samples = 0
        silence_chunk = b"\x00" * (CHUNK * self.channels * 2)  # int16
        last_log = t0
        try:
            while not self.stop_event.is_set():
                try:
                    avail = self.stream.get_read_available()
                except Exception:
                    avail = CHUNK  # if unsupported, fall back to blocking read
                if avail >= CHUNK:
                    data = self.stream.read(CHUNK, exception_on_overflow=False)
                    self.frames.append(data)
                    samples += CHUNK
                    try:
                        arr = np.frombuffer(data, dtype=np.int16)
                        self.level = float(np.max(np.abs(arr))) / 32768.0
                    except Exception:
                        pass
                else:
                    # WASAPI loopback devices deliver NOTHING while the output
                    # is silent — never block on them. Sleep briefly and insert
                    # silence to keep this track aligned with wall-clock time
                    # (so mic + system audio stay in sync when mixed).
                    time.sleep(0.005)
                    self.level *= 0.9  # decay the meter during silence
                    expected = int((time.monotonic() - t0) * self.rate)
                    if expected - samples >= self.rate // 2:
                        for _ in range((expected - samples) // CHUNK):
                            self.frames.append(silence_chunk)
                            samples += CHUNK
                now = time.monotonic()
                if now - last_log >= 15:
                    self.log.info("[%s] running: %.1f s captured",
                                  self.label, samples / self.rate)
                    last_log = now
        except Exception as exc:
            self.error = f"{self.label}: {type(exc).__name__}: {exc}"
            self.log.exception("[%s] reader error after %d chunks",
                               self.label, len(self.frames))
        finally:
            try:
                self.stream.stop_stream()
            except Exception:
                pass
            try:
                self.stream.close()
            except Exception:
                pass
            self.log.info("[%s] reader stopped: %d chunks captured (%.1f s)",
                          self.label, len(self.frames),
                          len(self.frames) * CHUNK / max(1, self.rate))

    def audio_16k_mono(self):
        """Return captured audio as float32 mono @ 16 kHz (may be empty)."""
        if not self.frames:
            return np.zeros(0, dtype=np.float32)
        raw = b"".join(self.frames)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if self.channels > 1:
            usable = (len(audio) // self.channels) * self.channels
            audio = audio[:usable].reshape(-1, self.channels).mean(axis=1)
        if self.rate != TARGET_RATE and len(audio) > 1:
            duration = len(audio) / self.rate
            n_out = max(1, int(round(duration * TARGET_RATE)))
            x_old = np.linspace(0.0, duration, num=len(audio), endpoint=False)
            x_new = np.linspace(0.0, duration, num=n_out, endpoint=False)
            audio = np.interp(x_new, x_old, audio).astype(np.float32)
        return audio.astype(np.float32)


def _open_stream_with_fallbacks(pa, dev, log, label):
    """Try several channel/rate combos. Returns (stream, channels, rate) or None."""
    native_rate = int(dev.get("defaultSampleRate", 48000) or 48000)
    native_ch = max(1, int(dev.get("maxInputChannels", 1) or 1))
    channel_options = list(dict.fromkeys([min(native_ch, 2), native_ch, 1]))
    rate_options = list(dict.fromkeys([native_rate, 48000, 44100, 32000, 16000]))

    attempts = []
    for channels in channel_options:
        for rate in rate_options:
            try:
                # NOTE: no probe read here — a WASAPI loopback device blocks
                # reads indefinitely while nothing is playing (this froze v1.0.1).
                log.debug("[%s] trying open: device #%s, %dch @ %d Hz",
                          label, dev.get("index"), channels, rate)
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=int(dev["index"]),
                    frames_per_buffer=CHUNK,
                )
                log.info("[%s] opened device #%s '%s' with %dch @ %d Hz",
                         label, dev.get("index"), dev.get("name"), channels, rate)
                return stream, channels, rate
            except Exception as exc:
                attempts.append(f"{channels}ch@{rate}Hz -> {type(exc).__name__}: {exc}")

    log.error("[%s] could NOT open device #%s '%s'. Attempts:\n  %s",
              label, dev.get("index"), dev.get("name"), "\n  ".join(attempts))
    return None


class Recorder:
    """start() begins capture; stop() mixes and writes the WAV, returns (path, notes)."""

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.log = _get_logger()
        self._stop_event = None
        self._readers = []
        self._started_at = None

    @property
    def is_recording(self):
        return self._stop_event is not None and not self._stop_event.is_set()

    def get_levels(self):
        """Live peak levels per source, e.g. {'System audio': 0.4, 'Microphone': 0.1}."""
        return {r.label: r.level for r in self._readers}

    # ---- device discovery -------------------------------------------------

    def _log_all_devices(self, pa):
        try:
            self.log.info("=== audio device inventory ===")
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                try:
                    api = pa.get_host_api_info_by_index(d["hostApi"])["name"]
                except Exception:
                    api = "?"
                self.log.info(
                    "  #%d [%s] '%s' in=%s out=%s rate=%s loopback=%s",
                    i, api, d.get("name"), d.get("maxInputChannels"),
                    d.get("maxOutputChannels"), d.get("defaultSampleRate"),
                    d.get("isLoopbackDevice", False),
                )
        except Exception:
            self.log.exception("device inventory failed")

    def _find_loopback_device(self, pa):
        try:
            dev = pa.get_default_wasapi_loopback()
            if dev:
                return dev
        except Exception as exc:
            self.log.info("get_default_wasapi_loopback failed: %s", exc)
        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_speakers = pa.get_device_info_by_index(
                wasapi_info["defaultOutputDevice"]
            )
            if default_speakers.get("isLoopbackDevice"):
                return default_speakers
            for loopback in pa.get_loopback_device_info_generator():
                if default_speakers["name"] in loopback["name"]:
                    return loopback
            # last resort: first loopback device of any kind
            for loopback in pa.get_loopback_device_info_generator():
                return loopback
        except Exception:
            self.log.exception("loopback discovery failed")
        return None

    def _find_mic_device(self, pa):
        # prefer the WASAPI default input; fall back to global default input
        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            idx = wasapi_info.get("defaultInputDevice", -1)
            if idx is not None and int(idx) >= 0:
                info = pa.get_device_info_by_index(int(idx))
                if info and not info.get("isLoopbackDevice"):
                    return info
        except Exception as exc:
            self.log.info("WASAPI default input lookup failed: %s", exc)
        try:
            info = pa.get_default_input_device_info()
            if info and not info.get("isLoopbackDevice"):
                return info
        except Exception as exc:
            self.log.info("default input lookup failed: %s", exc)
        return None

    # ---- lifecycle ---------------------------------------------------------

    def start(self):
        """Open devices NOW (fail fast), then start reader threads.

        Returns list of human-readable notes about what is being captured.
        """
        if self.is_recording:
            raise RuntimeError("Already recording")

        self.log.info("================ start() ================")
        pa = _get_pa()
        self._log_all_devices(pa)

        self._stop_event = threading.Event()
        self._readers = []
        notes = []
        problems = []

        loopback = self._find_loopback_device(pa)
        if loopback is not None:
            opened = _open_stream_with_fallbacks(pa, loopback, self.log, "System audio")
            if opened:
                stream, ch, rate = opened
                self._readers.append(
                    _Reader(stream, ch, rate, self._stop_event, "System audio", self.log)
                )
                notes.append(f"System audio: {loopback['name']}")
            else:
                problems.append(
                    f"System audio device '{loopback['name']}' could not be opened"
                )
        else:
            problems.append("No system-audio (loopback) device found")

        mic = self._find_mic_device(pa)
        if mic is not None:
            opened = _open_stream_with_fallbacks(pa, mic, self.log, "Microphone")
            if opened:
                stream, ch, rate = opened
                self._readers.append(
                    _Reader(stream, ch, rate, self._stop_event, "Microphone", self.log)
                )
                notes.append(f"Microphone: {mic['name']}")
            else:
                problems.append(f"Microphone '{mic['name']}' could not be opened")
        else:
            problems.append("No microphone found")

        if not self._readers:
            self._stop_event = None
            self.log.error("start() failed: %s", "; ".join(problems))
            raise RuntimeError(
                "Could not open any audio device.\n\n"
                + "\n".join(f"• {p}" for p in problems)
                + "\n\nDetails were written to audio_debug.log next to the app.\n"
                "Also check: Windows Settings → Privacy & security → Microphone → "
                "allow desktop apps to access the microphone."
            )

        if problems:
            notes.extend(f"WARNING: {p}" for p in problems)

        self._started_at = datetime.now()
        for r in self._readers:
            r.start()
        self.log.info("recording started: %s", "; ".join(notes))
        return notes

    def stop(self):
        """Stop capture, mix, write WAV. Returns (wav_path, notes)."""
        if not self.is_recording:
            raise RuntimeError("Not recording")

        self.log.info("================ stop() ================")
        self._stop_event.set()
        for r in self._readers:
            r.join(timeout=10)

        notes = [r.error for r in self._readers if r.error]
        for r in self._readers:
            self.log.info("[%s] final: %d chunks, error=%s",
                          r.label, len(r.frames), r.error)

        tracks = [r.audio_16k_mono() for r in self._readers]
        tracks = [t for t in tracks if len(t) > 0]
        readers, self._readers = self._readers, []
        self._stop_event = None

        if not tracks:
            detail = "; ".join(notes) if notes else "no stream produced any data"
            self.log.error("stop() -> no audio captured (%s)", detail)
            raise RuntimeError(
                "No audio was captured.\n\n"
                f"Reason: {detail}\n\n"
                "Details were written to audio_debug.log next to the app — "
                "please share that file if the problem persists."
            )

        length = max(len(t) for t in tracks)
        mix = np.zeros(length, dtype=np.float32)
        for t in tracks:
            mix[: len(t)] += t

        peak = float(np.max(np.abs(mix))) if len(mix) else 0.0
        if peak > 0.99:
            mix = mix * (0.99 / peak)

        pcm = (mix * 32767.0).astype(np.int16)

        stamp = self._started_at.strftime("%Y-%m-%d_%H%M")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        wav_path = self.output_dir / f"{stamp}_recording.wav"
        n = 2
        while wav_path.exists():
            wav_path = self.output_dir / f"{stamp}-{n}_recording.wav"
            n += 1

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TARGET_RATE)
            wf.writeframes(pcm.tobytes())

        self.log.info("saved %s (%.1f s of audio)", wav_path, length / TARGET_RATE)
        return wav_path, notes
