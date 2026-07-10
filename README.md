# Meeting Recorder + Local Transcriber (Windows, fully offline)

Records Microsoft Teams (or any) meeting audio — **system audio + microphone mixed** —
and transcribes it **locally** with whisper.cpp (`large-v3-turbo`, CPU-only, Greek).
A glossary pass then fixes known asset-management terms. Output is plain `.txt`
transcripts, ready to paste elsewhere.

- No cloud calls. No API keys. No telemetry. No internet needed at runtime.
- Batch only: record first, transcribe afterwards.
- Summarization is explicitly **out of scope** — this app ends at the corrected transcript.

<p align="center">
  <img src="[https://github.com/user-attachments/assets/419f68e5-abfb-4f1a-b00f-dceb69da3402](https://github.com/user-attachments/assets/768f04e7-bd7f-499c-b61a-f06eda0c1fff)" alt="Meeting Recorder + Transcriber — main window" width="640">
</p>

## Privacy & consent

This app records **everyone in the meeting**, not just you — it captures whatever
plays through your speakers/headphones plus your microphone. Before using it:

- Get consent from other participants where required. Recording rules vary by
  jurisdiction (e.g. one-party vs. two-party/all-party consent) and by your
  organization's policies — check both before recording calls with colleagues,
  clients, or anyone else.
- Check your employer's policy on recording internal meetings and on running
  unofficial/unsigned tools, especially ones that handle meeting audio.
- Recordings and transcripts are written to `Transcripts/` next to the app and
  are **never uploaded anywhere** — but that also means *you* are responsible
  for storing, sharing, and deleting them appropriately (they are not
  encrypted at rest).
- `glossary.json` ships with only generic, publicly-known asset-management
  terminology. If you add your own organization's product names, internal
  codenames, or other sensitive terms to it, treat that file as sensitive too.

## Getting the app (no build tools needed)

1. Go to **Releases** on this repo and download `MeetingScribe-win64.zip`.
2. Unzip anywhere (e.g. `C:\MeetingScribe`). You get:

   ```
   MeetingScribe/
     app.exe               ← the app
     _internal/            ← app runtime files (leave as-is)
     whisper-cli.exe       ← whisper.cpp binary (CPU-only, standalone)
     glossary.json         ← editable correction glossary
     models/               ← put the model file here (step 3)
     download-model.ps1
     Transcripts/          ← created on first run; recordings + transcripts land here
   ```

3. **Download the model separately** (once, on any machine with internet, ~1.6 GB):

   https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin

   …or run `download-model.ps1`. Put the `.bin` into the `models/` folder.

4. Copy the whole folder to any target PC (no internet, no Python needed) and run `app.exe`.

> **SmartScreen note:** the exe is unsigned, so the first launch may show
> "Windows protected your PC". Click **More info → Run anyway**. This is expected
> for unsigned binaries; see "Code signing" below.

## Using it

1. **Record** — click `● Record` before/during the meeting, `■ Stop` when done.
   It captures whatever plays through your speakers/headphones (the other
   participants) *plus* your microphone, mixed into one 16 kHz mono WAV in `Transcripts/`.
2. **Transcribe** — select the recording, keep language = Ελληνικά (el)
   (mixed Greek/English meetings are handled as well as Whisper allows), click
   **Transcribe**. CPU-only, so a 1-hour meeting takes a while — progress is shown.
3. Output files in `Transcripts/` (kept forever, never auto-deleted):
   - `YYYY-MM-DD_HHMM_recording.wav`
   - `YYYY-MM-DD_HHMM_transcript.txt` (raw Whisper output)
   - `YYYY-MM-DD_HHMM_transcript_corrected.txt` (after glossary pass — this is
     what the app displays and what you paste into your summarization workflow)

## The glossary

`glossary.json` (next to the exe) maps misheard variants → canonical terms
(ΟΣΕΚΑ, ΑΕΔΑΚ, AUM, Moody's ratings, YTM, PV01, …). Edit it with any text
editor — no code changes needed. After editing, use **Re-apply glossary to
selected** in the app to regenerate the corrected transcript without
re-transcribing. The shipped variants are conservative, generic starting
guesses for asset-management terminology; add real mistakes as you spot them,
including your own firm- or product-specific terms. Keep "wrong" variants
distinctive — very short or common words will misfire on normal text.

## Code signing / SmartScreen

The exe is unsigned. Options, in increasing cost:
1. Live with the one-time "More info → Run anyway" click (documented above) — fine
   for internal use on a handful of PCs.
2. An OV code-signing certificate (~€200–400/yr) removes most warnings after
   reputation builds up.
Given internal distribution to known machines, option 1 is the sensible default.

## Troubleshooting

- **"Could not open any audio device" / "No audio was captured"** — the app now
  writes full details to `audio_debug.log` (device inventory, every open attempt,
  per-stream errors) and any crash to `error_log.txt`, both next to `app.exe`.
  Check those first; share them when reporting a problem.
- **Microphone permission:** Windows Settings → Privacy & security → Microphone →
  enable "Let desktop apps access your microphone". Corporate policies sometimes
  block this — the log will show a permission-style open failure.
- **System audio needs something playing:** loopback capture records whatever
  goes through the default output device. If you record with headphones, the
  loopback follows the headphones (the default output), not the speakers.

## Known limitations (v1)

- **Crosstalk:** heavy overlapping speech degrades Whisper output — inherent to
  the model, not fixable here. Expect garbled passages where people talk over
  each other.
- **No speaker diarization** (by design for v1 — annotate manually if needed).
- Recording quality is capped at 16 kHz mono (that's all Whisper uses anyway).
- The app records the **default** output and input devices. If you switch
  headsets mid-meeting, stop and restart the recording.

## Repo layout

```
app.py            tkinter UI
recorder.py       WASAPI loopback + mic capture (pyaudiowpatch), mixdown
transcriber.py    whisper-cli.exe wrapper
glossary.py       correction pass
glossary.json     editable term glossary (shipped next to the exe)
scripts/download-model.ps1
.github/workflows/build.yml
models/           model drop-zone (gitignored, .bin files never committed)
```

## License

MIT — see [LICENSE](LICENSE). whisper.cpp and the Whisper model are separate
projects under their own licenses; this repo does not vendor either.
