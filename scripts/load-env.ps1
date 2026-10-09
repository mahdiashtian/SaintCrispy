param([string]$Path = "$PSScriptRoot\..\.env")

# Configuration is loaded by the shell before starting the async Python process.
foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
        $taskEnvName = $Matches[1]
        $taskEnvValue = $Matches[2].Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($taskEnvName, $taskEnvValue, 'Process')
    }
}
