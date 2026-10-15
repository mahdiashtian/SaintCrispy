#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        $arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $PSCommandPath
        Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList $arguments
        exit 0
    }
    & (Join-Path $PSScriptRoot 'Setup.ps1') -InstallDriver
    & (Join-Path $PSScriptRoot 'Monitor.ps1')
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Add-Type -AssemblyName System.Windows.Forms
    [void][Windows.Forms.MessageBox]::Show($_.Exception.Message, 'CPU Monitor startup error')
    exit 1
}
