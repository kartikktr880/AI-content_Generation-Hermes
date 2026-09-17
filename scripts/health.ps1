# Runs the AYCE baseline health check without requiring package installation.
# Usage: ./scripts/health.ps1 [--json]
$repoRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $repoRoot "src"
python -m ayce health @args
exit $LASTEXITCODE
