param(
    [string]$WslDistribution = 'Ubuntu-24.04',
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'
$taskProjectPath = [System.IO.Path]::GetFullPath("$PSScriptRoot\..")
$taskRuntimePath = Join-Path $taskProjectPath '.runtime'
$taskStatePath = Join-Path $taskRuntimePath 'wsl-keepalive.json'
$taskWslExecutable = Join-Path $env:WINDIR 'System32\wsl.exe'

# A live WSL client keeps this project's Docker services available while idle.
# Check both PID and command before reusing or stopping our own helper.
$taskExistingProcess = $null
if (Test-Path -LiteralPath $taskStatePath) {
    $taskSavedState = Get-Content -LiteralPath $taskStatePath -Raw | ConvertFrom-Json
    $taskCandidate = Get-CimInstance Win32_Process -Filter "ProcessId = $($taskSavedState.ProcessId)"
    $taskExpectedCommand = '--distribution ' + $taskSavedState.Distribution + ' --exec sleep infinity'
    if ($taskCandidate -and
        $taskCandidate.ExecutablePath -eq $taskWslExecutable -and
        $taskCandidate.CommandLine.EndsWith($taskExpectedCommand)) {
        $taskExistingProcess = $taskCandidate
    }
}

if ($Stop) {
    if ($taskExistingProcess) { Stop-Process -Id $taskExistingProcess.ProcessId }
    if (Test-Path -LiteralPath $taskStatePath) { Remove-Item -LiteralPath $taskStatePath }
    return
}

if ($taskExistingProcess -and $taskSavedState.Distribution -eq $WslDistribution) { return }
if ($taskExistingProcess) { throw 'Stop the existing project WSL helper before changing distributions.' }
if ($WslDistribution -notmatch '^[a-zA-Z0-9_.-]+$') { throw 'Unsupported WSL distribution name.' }
New-Item -ItemType Directory -Path $taskRuntimePath -Force | Out-Null
$taskHelper = Start-Process -FilePath $taskWslExecutable -ArgumentList @(
    '--distribution', $WslDistribution, '--exec', 'sleep', 'infinity'
) -WindowStyle Hidden -PassThru
@{ ProcessId = $taskHelper.Id; Distribution = $WslDistribution } |
    ConvertTo-Json | Set-Content -LiteralPath $taskStatePath -Encoding UTF8
