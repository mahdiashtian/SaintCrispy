# Pure sample conversion is separate so unavailable sensors can be verified without a driver.
function ConvertTo-CpuReading {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return $null }
    $number = [double]$Value
    if ([double]::IsNaN($number) -or [double]::IsInfinity($number)) { return $null }
    return [math]::Round($number, 1)
}

function Get-CpuSample {
    param([Parameter(Mandatory)][object]$Computer)
    $temperatures = @()
    $loads = @()
    $names = @()
    foreach ($hardware in $Computer.Hardware) {
        if ([string]$hardware.HardwareType -ne 'Cpu') { continue }
        $hardware.Update()
        $names += $hardware.Name
        foreach ($sensor in $hardware.Sensors) {
            $value = ConvertTo-CpuReading $sensor.Value
            if ($null -eq $value) { continue }
            # Intel exposes the remaining headroom as a Temperature sensor too.
            # It is not an absolute CPU temperature and must not affect MaxC.
            if ([string]$sensor.SensorType -eq 'Temperature' -and $sensor.Name -notmatch 'Distance to TjMax') {
                $temperatures += [pscustomobject]@{ Name = $sensor.Name; Celsius = $value }
            }
            if ([string]$sensor.SensorType -eq 'Load' -and $sensor.Name -eq 'CPU Total') {
                $loads += $value
            }
        }
    }
    [pscustomobject]@{
        Time = [DateTimeOffset]::Now.ToString('o')
        CpuName = $names -join ' / '
        MaxC = $(if ($temperatures.Count) { ($temperatures.Celsius | Measure-Object -Maximum).Maximum } else { $null })
        LoadPercent = $(if ($loads.Count) { ($loads | Measure-Object -Average).Average } else { $null })
        Sensors = @($temperatures)
    }
}
