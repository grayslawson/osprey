# Install the release compose file and an `osprey` command that drives it.
#
# The package does not pull the image or start anything, so installing it
# never touches a running camera.
$ErrorActionPreference = 'Stop'

$tools = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $env:ProgramData 'osprey'
$compose = Join-Path $target 'compose.release.yaml'

New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item (Join-Path $tools 'compose.release.yaml') $compose -Force

# `osprey up -d`, `osprey logs -f`, ... run `docker compose` against that file.
$shim = Join-Path $tools 'osprey.ps1'
@'
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
docker compose --project-directory '__TARGET__' -f '__COMPOSE__' @Arguments
exit $LASTEXITCODE
'@.Replace('__TARGET__', $target).Replace('__COMPOSE__', $compose) |
  Set-Content -Path $shim -Encoding UTF8
Install-BinFile -Name 'osprey' -Path $shim

Write-Host "Osprey: compose file installed at $compose"
Write-Host "Osprey: create $(Join-Path $target '.env') with OSPREY_HOST (this machine's LAN address)"
Write-Host "Osprey: and OSPREY_IMAGE (a release tag, e.g. ghcr.io/grayslawson/osprey:v<version>), then run:"
Write-Host "Osprey:   osprey up -d"
Write-Host "Osprey: the operator console is on port 8000 once the container is up."
