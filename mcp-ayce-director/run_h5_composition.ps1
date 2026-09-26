# H5 composition probe — ONE Hermes one-shot session that composes BOTH AYCE
# MCP servers: discover/classify tools, route to the director write boundary,
# invoke trigger_golden_path (deterministic idempotent request_id), then
# observe the resulting run through the read-only server.
# Modeled on mcp-ayce-readonly/run-agent-roundtrip.ps1 (H2).
#
# Usage (any shell):
#     powershell -NoProfile -ExecutionPolicy Bypass -File mcp-ayce-director\run_h5_composition.ps1
#
# Independent post-run verification (separate steps, NOT done here):
#     mcp-ayce-director\requests.json            -> ledger record for the request_id
#     mcp-ayce-readonly\.venv\Scripts\python mcp-ayce-director\verify_via_readonly.py <run_id>
#     mcp-ayce-readonly\.venv\Scripts\python mcp-ayce-director\dump_session_h5.py

$hermesExe = Join-Path $env:LOCALAPPDATA "hermes\bin\hermes.exe"
$promptFileName = if ($args.Count -gt 1 -and $args[1]) { $args[1] } else { "h5_compose_prompt.txt" }
$prompt = (Get-Content -Raw (Join-Path $PSScriptRoot $promptFileName) -ErrorAction Stop)
# the prompt file's single prompt line (line 2), trimmed — no embedded quotes
$lines = $prompt -split "`r?`n"
$prompt = ($lines | Where-Object { $_ -like 'CONSTRAINTS:*' } | Select-Object -First 1).Trim()
if (-not $prompt) { Write-Output "PROMPT LINE NOT FOUND"; exit 2 }

$outFile = Join-Path $PSScriptRoot "h5-compose-output.txt"
$errFile = Join-Path $PSScriptRoot "h5-compose-err.txt"
$argString = '-z "' + $prompt + '"'
if ($args.Count -gt 0 -and $args[0]) { $argString = '-m ' + $args[0] + ' ' + $argString }
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$p = Start-Process $hermesExe -ArgumentList $argString `
    -RedirectStandardOutput $outFile -RedirectStandardError $errFile `
    -PassThru -WindowStyle Hidden
Write-Output "AGENT_PID=$($p.Id) MODEL=$(if ($args.Count -gt 0) { $args[0] } else { 'default' })"
Start-Sleep -Seconds 30
while (-not $p.HasExited -and $sw.Elapsed.TotalSeconds -lt 420) { Start-Sleep -Seconds 10 }
Write-Output "exited=$($p.HasExited) elapsed_seconds=$([math]::Round($sw.Elapsed.TotalSeconds,1))"
if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force; Write-Output "FORCE_STOPPED (timed out)" }
Write-Output "=== agent stdout ==="
Get-Content $outFile -ErrorAction SilentlyContinue
Write-Output "=== agent stderr (tail) ==="
Get-Content $errFile -Tail 15 -ErrorAction SilentlyContinue
