#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateRange(1, 60)][int]$IntervalSeconds = 2,
    [ValidateRange(40, 110)][int]$WarnTemperature = 90,
    [switch]$Once,
    [switch]$SmokeTest
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Sensors.ps1')
$library = Join-Path $PSScriptRoot '.runtime/LibreHardwareMonitor/LibreHardwareMonitorLib.dll'
if (-not (Test-Path $library)) {
    throw 'Sensor library is missing. Double-click Start.cmd to set it up.'
}
# Windows PowerShell 5.1 uses the .NET Framework build from the release zip.
Add-Type -Path $library
$computer = New-Object LibreHardwareMonitor.Hardware.Computer
$computer.IsCpuEnabled = $true
try {
    $computer.Open()
    if ($Once) {
        Start-Sleep -Milliseconds 500
        Get-CpuSample -Computer $computer | ConvertTo-Json -Depth 5
        return
    }
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [Windows.Forms.Application]::EnableVisualStyles()
    $logDirectory = if ($SmokeTest) {
        Join-Path $PSScriptRoot '.runtime/smoke-logs'
    } else { Join-Path $PSScriptRoot 'logs' }
    New-Item -ItemType Directory -Force $logDirectory | Out-Null
    $session = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $samplePath = Join-Path $logDirectory "temperatures-$session.csv"
    $markerPath = Join-Path $logDirectory "beeps-$session.csv"
    $eventPath = Join-Path $logDirectory "system-events-$session.csv"
    $script:peak = $null
    $script:lastSample = $null
    $script:failures = 0

    $form = New-Object Windows.Forms.Form
    $form.Text = 'CPU temperature / Beep investigation'
    $form.ClientSize = New-Object Drawing.Size(720, 520)
    $form.MinimumSize = $form.Size
    $form.StartPosition = 'CenterScreen'
    $form.Font = New-Object Drawing.Font('Segoe UI', 10)

    $temperature = New-Object Windows.Forms.Label
    $temperature.SetBounds(20, 15, 680, 65)
    $temperature.Font = New-Object Drawing.Font('Segoe UI', 30, [Drawing.FontStyle]::Bold)
    $temperature.Text = 'Reading sensors...'
    $form.Controls.Add($temperature)
    $summary = New-Object Windows.Forms.Label
    $summary.SetBounds(24, 85, 670, 45)
    $form.Controls.Add($summary)
    $status = New-Object Windows.Forms.Label
    $status.SetBounds(24, 135, 670, 55)
    $form.Controls.Add($status)
    $grid = New-Object Windows.Forms.ListView
    $grid.SetBounds(24, 195, 670, 155)
    $grid.View = 'Details'
    $grid.FullRowSelect = $true
    [void]$grid.Columns.Add('CPU sensor', 470)
    [void]$grid.Columns.Add('Temperature (C)', 175)
    $form.Controls.Add($grid)

    $mark = New-Object Windows.Forms.Button
    $mark.SetBounds(24, 365, 215, 40)
    $mark.Text = 'I heard a beep (F8)'
    $form.Controls.Add($mark)
    $events = New-Object Windows.Forms.Button
    $events.SetBounds(249, 365, 215, 40)
    $events.Text = 'Save recent system events'
    $form.Controls.Add($events)
    $openLogs = New-Object Windows.Forms.Button
    $openLogs.SetBounds(474, 365, 220, 40)
    $openLogs.Text = 'Open logs folder'
    $form.Controls.Add($openLogs)
    $note = New-Object Windows.Forms.Label
    $note.SetBounds(24, 420, 670, 85)
    $note.Text = "Samples saved every $IntervalSeconds seconds. F8 works while this window is focused.`r`nOrange means >= $WarnTemperature C (a configurable observation threshold).`r`nThis tool makes no sounds. A beep alone does not identify its cause."
    $form.Controls.Add($note)

    $recordBeep = {
        $sample = $script:lastSample
        [pscustomobject]@{
            Time = [DateTimeOffset]::Now.ToString('o')
            SampleTime = $(if ($sample) { $sample.Time } else { '' })
            CpuMaxC = $(if ($sample) { $sample.MaxC } else { $null })
            CpuLoadPercent = $(if ($sample) { $sample.LoadPercent } else { $null })
            Note = $(if ($SmokeTest) { 'Synthetic smoke test' } else { 'User heard a beep; cause unknown' })
        } | Export-Csv $markerPath -NoTypeInformation -Encoding UTF8 -Append
        $status.Text = 'Beep time saved: ' + (Get-Date -Format 'HH:mm:ss')
    }
    $mark.Add_Click($recordBeep)
    $form.KeyPreview = $true
    $form.Add_KeyDown({
        if ($_.KeyCode -eq [Windows.Forms.Keys]::F8) {
            & $recordBeep
            $_.SuppressKeyPress = $true
        }
    })
    $openLogs.Add_Click({ Start-Process explorer.exe -ArgumentList ('"{0}"' -f $logDirectory) })
    $events.Add_Click({
        try {
            $entries = @(Get-WinEvent -FilterHashtable @{
                LogName = 'System'; Level = 1, 2, 3; StartTime = (Get-Date).AddHours(-48)
            } -MaxEvents 200 -ErrorAction Stop)
            $entries | Select-Object TimeCreated, ProviderName, Id, LevelDisplayName, Message |
                Export-Csv $eventPath -NoTypeInformation -Encoding UTF8
            $status.Text = "Saved $($entries.Count) recent warnings/errors. Events may be unrelated to beeps."
        } catch {
            $status.Text = 'Event export: ' + $_.Exception.Message
        }
    })
    $update = {
        try {
            $sample = Get-CpuSample -Computer $computer
            $script:lastSample = $sample
            $script:failures = 0
            if ($null -ne $sample.MaxC) {
                if ($null -eq $script:peak -or $sample.MaxC -gt $script:peak) {
                    $script:peak = $sample.MaxC
                }
                $temperature.Text = '{0:0.0} C' -f $sample.MaxC
                $temperature.ForeColor = if ($sample.MaxC -ge $WarnTemperature) {
                    [Drawing.Color]::DarkOrange
                } else { [Drawing.Color]::ForestGreen }
                $status.Text = 'Live CPU sensors. Compare the beep timestamp with the saved readings.'
            } else {
                $temperature.Text = 'Temperature unavailable'
                $temperature.ForeColor = [Drawing.Color]::DimGray
                $status.Text = if (-not [LibreHardwareMonitor.PawnIo.PawnIo]::IsInstalled) {
                    'PawnIO sensor driver is missing. Run Start.cmd to install the bundled driver with administrator access.'
                } else {
                    'Run Start.cmd as administrator. If still unavailable, the sensor driver may be blocked or unsupported.'
                }
            }
            $loadText = if ($null -eq $sample.LoadPercent) { 'N/A' } else { '{0:0}%' -f $sample.LoadPercent }
            $peakText = if ($null -eq $script:peak) { 'N/A' } else { '{0:0.0} C' -f $script:peak }
            $summary.Text = "$($sample.CpuName)`r`nCPU load: $loadText     Session peak: $peakText     Updated: $(Get-Date -Format 'HH:mm:ss')"
            $grid.Items.Clear()
            foreach ($sensor in $sample.Sensors) {
                $item = New-Object Windows.Forms.ListViewItem($sensor.Name)
                [void]$item.SubItems.Add(('{0:0.0}' -f $sensor.Celsius))
                [void]$grid.Items.Add($item)
            }
            [pscustomobject]@{
                Time = $sample.Time
                CpuMaxC = $sample.MaxC
                CpuLoadPercent = $sample.LoadPercent
                SessionPeakC = $script:peak
                Sensors = (($sample.Sensors | ForEach-Object { '{0}={1:0.0}' -f $_.Name, $_.Celsius }) -join '; ')
            } | Export-Csv $samplePath -NoTypeInformation -Encoding UTF8 -Append
        } catch {
            $script:failures++
            $script:lastSample = $null
            $temperature.Text = 'Read/log error'
            $temperature.ForeColor = [Drawing.Color]::Firebrick
            $status.Text = $_.Exception.Message
            if ($script:failures -ge 3) {
                $timer.Stop()
                $status.Text += ' Monitoring stopped after 3 errors; close and restart after fixing this.'
            }
        }
    }
    $timer = New-Object Windows.Forms.Timer
    $timer.Interval = $IntervalSeconds * 1000
    $timer.Add_Tick($update)
    $form.Add_Shown({ & $update; $timer.Start() })
    if ($SmokeTest) {
        # Exercise the real form and timer without leaving a helper process running.
        $smokeTimer = New-Object Windows.Forms.Timer
        $smokeTimer.Interval = 4500
        $smokeTimer.Add_Tick({ & $recordBeep; $form.Close() })
        $smokeTimer.Start()
    }
    try { [void]$form.ShowDialog() } finally {
        $timer.Stop()
        $timer.Dispose()
        if ($SmokeTest) { $smokeTimer.Stop(); $smokeTimer.Dispose() }
        $form.Dispose()
    }
} finally {
    $computer.Close()
}
