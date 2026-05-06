$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
node (Join-Path $root "bin\saki-codex-pet.js") install
