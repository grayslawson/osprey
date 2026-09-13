[CmdletBinding()]
param(
    [string]$EnvFile = ".env.physical"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Missing $EnvFile. Copy .env.physical.example and verify each value."
}

$values = @{}
foreach ($line in Get-Content -LiteralPath $EnvFile) {
    if ($line -match '^\s*([^#=]+)=(.*)$') {
        $values[$matches[1].Trim()] = $matches[2].Trim()
    }
}

foreach ($name in 'CUCKOO_PHYSICAL_HOST', 'G5_PTZ_IP', 'G5_PTZ_MAC') {
    if (-not $values[$name]) {
        throw "Missing $name in $EnvFile"
    }
}

$hostIp = $values['CUCKOO_PHYSICAL_HOST']
$cameraIp = $values['G5_PTZ_IP']
$cameraMac = $values['G5_PTZ_MAC'] -replace '[:-]', ''
if ($cameraMac -notmatch '^[0-9A-Fa-f]{12}$') {
    throw "G5_PTZ_MAC must contain exactly 12 hexadecimal digits"
}

$local = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $hostIp -ErrorAction SilentlyContinue
if (-not $local) {
    throw "CUCKOO_PHYSICAL_HOST $hostIp is not assigned to this workstation"
}

if (-not (Test-Connection -TargetName $cameraIp -Count 1 -Quiet)) {
    throw "Camera $cameraIp did not answer a targeted ping"
}

$https = Test-NetConnection -ComputerName $cameraIp -Port 443 -InformationLevel Quiet
if (-not $https) {
    throw "Camera $cameraIp did not accept HTTPS on TCP 443"
}

foreach ($port in 7442, 7444, 7550) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($listener) {
        throw "TCP $port is already in use; stop the conflicting listener before handoff"
    }
}

docker compose --env-file $EnvFile -f compose.yaml -f compose.physical.yaml config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Physical Compose profile did not validate"
}

Write-Output "Physical preflight passed for $cameraIp ($cameraMac) via $hostIp."
Write-Output "No camera settings, firewall rules, or Protect state were changed."
