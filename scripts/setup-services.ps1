param(
    [string]$WslDistribution = 'Ubuntu',
    [string]$WslUser = 'root'
)
$ErrorActionPreference = 'Stop'
$taskProjectPath = [System.IO.Path]::GetFullPath("$PSScriptRoot\..")
$taskPythonPath = Join-Path $taskProjectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPythonPath)) {
    throw 'Create .venv and install the project before setting up the databases.'
}
$taskSetupPath = Join-Path $taskProjectPath 'tools\setup_services.py'
& docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    & $taskPythonPath $taskSetupPath --project downloader-bot-dev
} else {
    & "$PSScriptRoot\keep-wsl-alive.ps1" -WslDistribution $WslDistribution
    & $taskPythonPath $taskSetupPath --project downloader-bot-dev --wsl-distribution $WslDistribution --wsl-user $WslUser
}
if ($LASTEXITCODE -ne 0) { throw 'Database setup failed.' }
