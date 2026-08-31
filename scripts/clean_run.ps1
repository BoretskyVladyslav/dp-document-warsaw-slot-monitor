param(
    [int]$Port = 9222
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Output "Stopping src.main Python processes..."
$pythonMatches = @(
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'src\.main' }
)
foreach ($proc in $pythonMatches) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Output "Stopping dedicated CDP Chrome (CDP_Profile)..."
$chromeMatches = @(
    Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'CDP_Profile' }
)
foreach ($proc in $chromeMatches) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 1

$freePort = Join-Path $PSScriptRoot "free_cdp_port.ps1"
if (Test-Path $freePort) {
    & $freePort -Port $Port
}

Write-Output "Processes stopped. Waiting before SQLite reset..."

$profileDir = Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data\CDP_Profile"
$defaultDir = Join-Path $profileDir "Default"
if (Test-Path -LiteralPath $defaultDir) {
    Write-Output "Clearing CDP_Profile cache and cookies (profile structure kept)..."
    $cacheRelPaths = @(
        "Cache",
        "Code Cache",
        "GPUCache",
        "GrShaderCache",
        "ShaderCache",
        "DawnCache",
        "Application Cache",
        "Service Worker\CacheStorage",
        "Service Worker\ScriptCache",
        "Local Storage",
        "Session Storage",
        "Cookies",
        "Cookies-journal",
        "Network\Cookies",
        "Network\Cookies-journal",
        "Network\Network Persistent State",
        "Network\TransportSecurity"
    )
    foreach ($relative in $cacheRelPaths) {
        $target = Join-Path $defaultDir $relative
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
} else {
    Write-Output "CDP_Profile Default directory not found; skipping cache wipe."
}
