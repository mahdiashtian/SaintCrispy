#Requires -Version 5.1
param([switch]$InstallDriver)
# Portable dependency, pinned to an official GitHub release and SHA-256.
$ErrorActionPreference = 'Stop'
$url = 'https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip'
$expected = '086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001'
$runtime = Join-Path $PSScriptRoot '.runtime'
$archive = Join-Path $runtime 'LibreHardwareMonitor.zip'
$destination = Join-Path $runtime 'LibreHardwareMonitor'
New-Item -ItemType Directory -Force $runtime | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if (-not (Test-Path $archive) -or (Get-FileHash $archive -Algorithm SHA256).Hash -ne $expected) {
    Write-Host 'Downloading Libre Hardware Monitor 0.9.6 from its official GitHub release...'
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive -TimeoutSec 120
}
if ((Get-FileHash $archive -Algorithm SHA256).Hash -ne $expected) {
    throw 'Download checksum mismatch. The archive will not be loaded. Run Setup.ps1 again.'
}
Expand-Archive -LiteralPath $archive -DestinationPath $destination -Force
if (-not (Test-Path (Join-Path $destination 'LibreHardwareMonitorLib.dll'))) {
    throw 'The official archive does not contain LibreHardwareMonitorLib.dll.'
}
Get-ChildItem $destination -File -Recurse | Unblock-File
$assembly = [Reflection.Assembly]::LoadFrom((Join-Path $destination 'LibreHardwareMonitor.exe'))
$stream = $assembly.GetManifestResourceStream('LibreHardwareMonitor.Resources.PawnIO_setup.exe')
if ($null -eq $stream) { throw 'The official release is missing its PawnIO installer.' }
$installer = Join-Path $runtime 'PawnIO_setup.exe'
$output = [IO.File]::Create($installer)
try { $stream.CopyTo($output) } finally { $output.Dispose(); $stream.Dispose() }
if ($InstallDriver) {
    $key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO'
    $installed = Get-ItemProperty $key -ErrorAction SilentlyContinue
    if (-not $installed -or [version]$installed.DisplayVersion -lt [version]'2.0.0.0') {
        Write-Host 'Installing the PawnIO sensor driver bundled by Libre Hardware Monitor...'
        $process = Start-Process $installer -ArgumentList '-install' -WindowStyle Hidden -PassThru -Wait
        if ($process.ExitCode -ne 0 -and $process.ExitCode -ne 3010) {
            throw "PawnIO setup failed with exit code $($process.ExitCode)."
        }
        if (-not (Test-Path $key)) { throw 'PawnIO setup completed but its installation was not detected.' }
    }
}
Write-Host 'Sensor library is ready.'
