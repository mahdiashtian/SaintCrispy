param([string]$WslDistribution = 'Ubuntu-24.04')
$ErrorActionPreference = 'Stop'
$taskProjectPath = [System.IO.Path]::GetFullPath("$PSScriptRoot\..")
$taskEnvPath = Join-Path $taskProjectPath '.env'
if (-not (Test-Path -LiteralPath $taskEnvPath)) {
    Copy-Item -LiteralPath (Join-Path $taskProjectPath '.env.example') -Destination $taskEnvPath
}

function Set-ProjectEnvValue([string]$Key, [string]$Value) {
    $taskLines = @(Get-Content -LiteralPath $taskEnvPath -Encoding UTF8 |
        Where-Object { $_ -notmatch ('^' + [regex]::Escape($Key) + '=') })
    $taskLines += ($Key + '="' + $Value + '"')
    [System.IO.File]::WriteAllLines($taskEnvPath, $taskLines, [System.Text.UTF8Encoding]::new($false))
}

. "$PSScriptRoot\load-env.ps1"
if (-not $env:DEV_DB_PASSWORD -or $env:DEV_DB_PASSWORD -eq 'YOUR_PASSWORD') {
    $taskPassword = [Guid]::NewGuid().ToString('N')
    Set-ProjectEnvValue 'DEV_DB_PASSWORD' $taskPassword
    $env:DEV_DB_PASSWORD = $taskPassword
}
& docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
$taskUseWsl = $LASTEXITCODE -ne 0
if ($taskUseWsl) {
    & "$PSScriptRoot\keep-wsl-alive.ps1" -WslDistribution $WslDistribution
}

function Invoke-ProjectCompose([string[]]$ComposeArgs) {
    if ($taskUseWsl) {
        & wsl -d $WslDistribution --cd $taskProjectPath -- docker compose --progress quiet -p downloader-bot-dev @ComposeArgs
    } else {
        & docker compose --progress quiet --project-directory $taskProjectPath -p downloader-bot-dev @ComposeArgs
    }
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose command failed' }
}

Invoke-ProjectCompose @('up', '-d', 'postgres', 'redis')
$taskPgAddress = (Invoke-ProjectCompose @('port', 'postgres', '5432') | Select-Object -Last 1).Trim()
$taskRedisAddress = (Invoke-ProjectCompose @('port', 'redis', '6379') | Select-Object -Last 1).Trim()
Set-ProjectEnvValue 'DATABASE_URL' ('postgresql://downloader:' + $env:DEV_DB_PASSWORD + '@' + $taskPgAddress + '/downloader')
Set-ProjectEnvValue 'REDIS_URL' ('redis://' + $taskRedisAddress + '/0')
if (-not $env:FFMPEG_PATH) {
    $taskFfmpegCommand = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($taskFfmpegCommand) {
        Set-ProjectEnvValue 'FFMPEG_PATH' $taskFfmpegCommand.Source
    } else {
        $taskPythonPath = Join-Path $taskProjectPath '.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $taskPythonPath) {
            $taskBundledFfmpeg = & $taskPythonPath -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())' 2>$null
            if ($LASTEXITCODE -eq 0 -and $taskBundledFfmpeg) {
                Set-ProjectEnvValue 'FFMPEG_PATH' $taskBundledFfmpeg.Trim()
            }
        }
    }
}
Write-Output ('PostgreSQL: ' + $taskPgAddress)
Write-Output ('Redis: ' + $taskRedisAddress)
Write-Output 'Connection settings saved in .env; credentials omitted.'
