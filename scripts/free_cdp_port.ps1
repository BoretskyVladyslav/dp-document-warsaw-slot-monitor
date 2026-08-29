param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(1024, 65535)]
    [int]$Port
)

$ErrorActionPreference = "Stop"

$ownerPids = @(
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
)

if ($ownerPids.Count -eq 0) {
    exit 0
}

foreach ($listenerProcessId in $ownerPids) {
    if ($listenerProcessId -le 4 -or $listenerProcessId -eq $PID) {
        throw "Refusing to terminate protected PID $listenerProcessId on port $Port"
    }
    Stop-Process -Id $listenerProcessId -Force -ErrorAction Stop
}

$deadline = (Get-Date).AddSeconds(10)
do {
    $remaining = @(
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    )
    if ($remaining.Count -eq 0) {
        Write-Output "Freed port $Port from previous process"
        exit 0
    }
    Start-Sleep -Milliseconds 250
} while ((Get-Date) -lt $deadline)

throw "Port $Port is still occupied after terminating the previous process"
