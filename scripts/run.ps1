. "$PSScriptRoot\load-env.ps1"
$env:PYTHONPATH = [System.IO.Path]::GetFullPath("$PSScriptRoot\..\src")
& "$PSScriptRoot\..\.venv\Scripts\python.exe" -m downloader_bot
