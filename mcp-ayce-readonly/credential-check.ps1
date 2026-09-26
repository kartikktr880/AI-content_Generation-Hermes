$hermes = "`"%LOCALAPPDATA%\hermes\bin\hermes.exe`""

Write-Output "=== hermes setup --help ==="
$raw = cmd /c "$hermes setup --help 2>&1"
$text = [string]::Join([Environment]::NewLine, $raw)
if ($text.Length -gt 1800) { $text.Substring(0, 1800) } else { $text }

Write-Output "=== provider credential presence (booleans only - values NEVER read) ==="
$names = @(
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY",
    "XAI_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY",
    "NOUS_API_KEY", "OPENAI_COMPATIBLE_BASE_URL", "OPENAI_COMPATIBLE_API_KEY"
)
foreach ($name in $names) {
    $user = [Environment]::GetEnvironmentVariable($name, "User")
    $proc = [Environment]::GetEnvironmentVariable($name)
    Write-Output ("{0}: user={1} process={2}" -f $name, ($null -ne $user -and $user -ne ""), ($null -ne $proc -and $proc -ne ""))
}
