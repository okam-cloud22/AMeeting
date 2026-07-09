# Downloads the Whisper model into the models/ folder next to this script.
# Run this ONCE on a machine WITH internet, then copy the whole app folder
# (including models/) to the offline PCs.
#
# Usage (from the app folder):
#   powershell -ExecutionPolicy Bypass -File .\download-model.ps1
#   powershell -ExecutionPolicy Bypass -File .\download-model.ps1 -Model large-v3-turbo-q5_0
#
# Models (from https://huggingface.co/ggerganov/whisper.cpp):
#   large-v3-turbo        (~1.6 GB, default — best quality/speed balance)
#   large-v3-turbo-q5_0   (~0.6 GB, quantized — noticeably faster on old CPUs,
#                          slightly lower accuracy; good choice for the i7-4790)
#   medium                (~1.5 GB, fallback if turbo is too slow)

param(
    [string]$Model = "large-v3-turbo"
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$modelsDir = Join-Path $here "models"
New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null

$fileName = "ggml-$Model.bin"
$url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$fileName"
$dest = Join-Path $modelsDir $fileName

if (Test-Path $dest) {
    Write-Host "$fileName already exists in models/ — nothing to do."
    exit 0
}

Write-Host "Downloading $fileName (this can take a while)..."
Invoke-WebRequest -Uri $url -OutFile $dest
Write-Host "Done -> $dest"
