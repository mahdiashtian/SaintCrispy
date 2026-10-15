#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Sensors.ps1')
function Assert-Equal($Actual, $Expected, $Label) {
    if ($Actual -ne $Expected) { throw "$Label expected '$Expected', got '$Actual'" }
}
$cpu = [pscustomobject]@{
    HardwareType = 'Cpu'; Name = 'Test CPU'
    Sensors = @(
        [pscustomobject]@{ SensorType = 'Temperature'; Name = 'CPU Package'; Value = 73.25 },
        [pscustomobject]@{ SensorType = 'Temperature'; Name = 'CPU Core #1'; Value = 0 },
        [pscustomobject]@{ SensorType = 'Temperature'; Name = 'Missing'; Value = $null },
        [pscustomobject]@{ SensorType = 'Temperature'; Name = 'Invalid'; Value = [double]::NaN },
        [pscustomobject]@{ SensorType = 'Temperature'; Name = 'CPU Core #1 Distance to TjMax'; Value = 95 },
        [pscustomobject]@{ SensorType = 'Load'; Name = 'CPU Total'; Value = 42.5 }
    )
}
$cpu | Add-Member ScriptMethod Update { }
$gpu = [pscustomobject]@{
    HardwareType = 'GpuNvidia'; Name = 'GPU'
    Sensors = @([pscustomobject]@{ SensorType = 'Temperature'; Name = 'GPU'; Value = 99 })
}
$sample = Get-CpuSample ([pscustomobject]@{ Hardware = @($cpu, $gpu) })
Assert-Equal $sample.MaxC 73.2 'Maximum CPU temperature excludes GPU and distance to TjMax'
Assert-Equal $sample.LoadPercent 42.5 'CPU load'
Assert-Equal $sample.Sensors.Count 2 'Null/NaN excluded; zero preserved'
[void][DateTimeOffset]::Parse($sample.Time)
$cpu.Sensors = @()
$missing = Get-CpuSample ([pscustomobject]@{ Hardware = @($cpu) })
Assert-Equal ($null -eq $missing.MaxC) $true 'Missing temperature stays null'
Assert-Equal ($null -eq $missing.LoadPercent) $true 'Missing load stays null'
$empty = Get-CpuSample ([pscustomobject]@{ Hardware = @() })
Assert-Equal $empty.Sensors.Count 0 'No hardware'
Write-Host 'PASS: CPU filtering, maximum, load, missing/NaN/zero readings, timestamp, no hardware.'
