# Copy the release compose file into place and say what to do next.
#
# Osprey runs as a container on Windows the same way it does everywhere else:
# the package installs the compose file and the launcher scripts, and the
# operator starts the container.  The package does not pull the image and does
# not start anything, so installing it never touches a running camera.
$ErrorActionPreference = 'Stop'

$tools = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $env:ProgramData 'osprey'

New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item (Join-Path $tools 'compose.release.yaml') (Join-Path $target 'compose.release.yaml') -Force

Write-Host "Osprey: compose file written to $(Join-Path $target 'compose.release.yaml')"
Write-Host "Osprey: set the camera address and MAC in it, then run:"
Write-Host "Osprey:   docker compose -f $(Join-Path $target 'compose.release.yaml') up -d"
Write-Host "Osprey: the operator console is on port 8000 once the container is up."
