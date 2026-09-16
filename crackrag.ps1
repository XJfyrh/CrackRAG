& (Join-Path $PSScriptRoot 'scripts/release.ps1') @args
if (-not $?) { exit 1 }
