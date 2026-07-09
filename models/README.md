# models/

Drop the Whisper model file(s) here. The app picks up any `ggml-*.bin` in this
folder and shows them in the Model dropdown.

Recommended (download from https://huggingface.co/ggerganov/whisper.cpp/tree/main):

| File | Size | Notes |
|---|---|---|
| `ggml-large-v3-turbo.bin` | ~1.6 GB | Default. Best quality for Greek. |
| `ggml-large-v3-turbo-q5_0.bin` | ~0.6 GB | Quantized turbo — meaningfully faster on old CPUs (i7-4790), slightly lower accuracy. Worth having both. |
| `ggml-medium.bin` | ~1.5 GB | Fallback if turbo proves too slow. |

Direct link for the default model:
https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin

Or run `download-model.ps1` from the app folder on a machine with internet.

Model files are intentionally NOT in git and NOT in the Release zip
(too large for GitHub). Download once, copy with the folder.
