$hermesExe = Join-Path $env:LOCALAPPDATA "hermes\bin\hermes.exe"

Write-Output "=== mcp list ==="
$raw = cmd /c "`"$hermesExe`" mcp list 2>&1"
[string]::Join([Environment]::NewLine, $raw) | Write-Output

Write-Output "=== agent round-trip (safe mode, one-shot) ==="
$prompt = 'Use ONLY the ayce-readonly MCP tools (list_runs, get_run_state, get_run_artifacts) to inspect the available AYCE runs. First call list_runs. Then select the run with id run-20260919T110027Z-bc687f6516ac and retrieve its RunState with get_run_state and its artifacts with get_run_artifacts. Then summarize exactly: 1. run_id, 2. job_id, 3. number of stages, 4. successful stage count and failed stage count, 5. artifact count, 6. each artifact kind and its producing stage, 7. final run status. Do not read any files directly; do not use shell or any other tools; use only the three ayce-readonly MCP tools.'

$outFile = Join-Path $PSScriptRoot "agent-roundtrip-output.txt"
$errFile = Join-Path $PSScriptRoot "agent-roundtrip-err.txt"
$argString = '-z "' + $prompt + '"'
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$p = Start-Process $hermesExe -ArgumentList $argString `
    -RedirectStandardOutput $outFile -RedirectStandardError $errFile `
    -PassThru -WindowStyle Hidden
Write-Output "AGENT_PID=$($p.Id)"
Start-Sleep -Seconds 30
while (-not $p.HasExited -and $sw.Elapsed.TotalSeconds -lt 420) { Start-Sleep -Seconds 10 }
Write-Output "exited=$($p.HasExited) elapsed_seconds=$([math]::Round($sw.Elapsed.TotalSeconds,1))"
if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force; Write-Output "FORCE_STOPPED (timed out)" }
Write-Output "=== agent stdout ==="
Get-Content $outFile -ErrorAction SilentlyContinue
Write-Output "=== agent stderr (tail) ==="
Get-Content $errFile -Tail 15 -ErrorAction SilentlyContinue

