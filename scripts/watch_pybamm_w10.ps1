[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDir,

    [ValidateRange(0, 2147483647)]
    [int]$ProcessId = 0,

    [ValidateRange(1, 86400)]
    [int]$IntervalSeconds = 30,

    [switch]$Once
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$resolvedOutput = (Resolve-Path -LiteralPath $OutputDir).Path
$progressPath = Join-Path $resolvedOutput "run_progress.json"
$statusPath = Join-Path $resolvedOutput "run_status.json"
$configPath = Join-Path $resolvedOutput "run_config.json"
$failurePath = Join-Path $resolvedOutput "failures"
$trackedProcessId = $ProcessId
$previousCpuSeconds = $null

function Read-JsonSafe {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Get-PropertyValue {
    param($Object, [string]$Name, $Default)
    if ($null -eq $Object) {
        return $Default
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        return $Default
    }
    return $property.Value
}

function Get-StepLabel {
    param($Progress)
    $stage = [string](Get-PropertyValue $Progress "stage" "")
    $phase = [string](Get-PropertyValue $Progress "phase" "UNKNOWN")
    $labels = @{
        "initial_rpt" = "Initial RPT"
        "rpt_preconditioning" = "RPT preconditioning"
        "rpt_completed" = "RPT completed"
        "post_rpt_recovery" = "Post-RPT recovery"
        "3c_cc" = "Step 1 - 3C CC charge to 4.0 V"
        "4v_cv" = "Step 2 - 4.0 V CV hold"
        "c4_cc" = "Step 3 - C/4 CC charge to 4.2 V"
        "4p2v_cv" = "Step 4 - 4.2 V CV hold"
        "post_charge_rest" = "Post-charge rest"
        "step5_c4_discharge" = "Step 5 - C/4 capacity discharge"
        "step6_udds" = "Step 6 - UDDS"
    }
    if ($labels.ContainsKey($stage)) {
        return $labels[$stage]
    }
    $phaseLabels = @{
        "INITIAL_RPT" = "Initial RPT"
        "STANDARD_CHARGE" = "Steps 1-4 - standard charge"
        "POST_RPT_RECOVERY" = "Post-charge/RPT recovery"
        "STEP5_C4_DISCHARGE" = "Step 5 - C/4 capacity discharge"
        "STEP6_UDDS" = "Step 6 - UDDS"
        "CYCLE_COMPLETED" = "Cycle boundary"
        "RUN_COMPLETED" = "Run completed"
    }
    if ($phaseLabels.ContainsKey($phase)) {
        return $phaseLabels[$phase]
    }
    return $phase
}

function Find-RunProcess {
    param([int]$RequestedId)
    if ($RequestedId -gt 0) {
        return Get-Process -Id $RequestedId -ErrorAction SilentlyContinue
    }
    $pythonProcesses = @(Get-Process -Name python -ErrorAction SilentlyContinue)
    if ($pythonProcesses.Count -eq 1) {
        return $pythonProcesses[0]
    }
    return $null
}

$config = Read-JsonSafe $configPath
$maxCycles = Get-PropertyValue (Get-PropertyValue $config "protocol" $null) "max_aging_cycles" "?"

while ($true) {
    $progress = Read-JsonSafe $progressPath
    $runStatus = Read-JsonSafe $statusPath
    $runProcess = Find-RunProcess $trackedProcessId
    if ($null -ne $runProcess -and $trackedProcessId -eq 0) {
        $trackedProcessId = $runProcess.Id
    }

    $processLabel = "UNKNOWN"
    $cpuLabel = "n/a"
    $memoryLabel = "n/a"
    if ($null -ne $runProcess) {
        $processLabel = "RUNNING pid=$($runProcess.Id)"
        $cpuSeconds = [double]$runProcess.CPU
        if ($null -ne $previousCpuSeconds) {
            $cpuLabel = "{0:N2}s since last sample" -f ($cpuSeconds - $previousCpuSeconds)
        }
        else {
            $cpuLabel = "{0:N2}s total" -f $cpuSeconds
        }
        $previousCpuSeconds = $cpuSeconds
        $memoryLabel = "{0:N2} GB" -f ($runProcess.WorkingSet64 / 1GB)
    }
    elseif ($trackedProcessId -gt 0) {
        $processLabel = "STOPPED pid=$trackedProcessId"
    }

    $phase = [string](Get-PropertyValue $progress "phase" "UNKNOWN")
    $stageLabel = Get-StepLabel $progress
    $currentCycle = Get-PropertyValue $progress "current_cycle" "?"
    $completedCycles = Get-PropertyValue $progress "completed_cycles" "?"
    $businessStatus = [string](Get-PropertyValue $progress "business_status" "UNKNOWN")
    $updatedAt = [string](Get-PropertyValue $progress "updated_at_utc" "")
    $heartbeatLabel = "unavailable"
    if ($updatedAt) {
        try {
            $heartbeatTime = [DateTimeOffset]::Parse($updatedAt)
            $heartbeatAge = [DateTimeOffset]::Now - $heartbeatTime
            $heartbeatLabel = "{0:N0}s ago ({1})" -f $heartbeatAge.TotalSeconds, $heartbeatTime.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss")
        }
        catch {
            $heartbeatLabel = "invalid timestamp"
        }
    }

    $failureCount = 0
    if (Test-Path -LiteralPath $failurePath) {
        $failureCount = @(Get-ChildItem -LiteralPath $failurePath -File -Recurse -ErrorAction SilentlyContinue).Count
    }

    $sampleTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Output "[$sampleTime] process=$processLabel | cycle=$currentCycle/$maxCycles completed=$completedCycles | $stageLabel | phase=$phase | status=$businessStatus"
    Write-Output "  cpu=$cpuLabel | memory=$memoryLabel | heartbeat=$heartbeatLabel | failure_files=$failureCount"

    if ($null -ne $runStatus) {
        $terminalStatus = Get-PropertyValue $runStatus "status" "UNKNOWN"
        Write-Output "  terminal_status=$terminalStatus"
        break
    }
    if ($trackedProcessId -gt 0 -and $null -eq $runProcess) {
        Write-Output "  monitor stopped: target process is no longer running; progress may be stale."
        break
    }
    if ($Once) {
        break
    }
    Start-Sleep -Seconds $IntervalSeconds
}
