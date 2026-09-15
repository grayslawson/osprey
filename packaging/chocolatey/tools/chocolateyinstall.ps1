$ErrorActionPreference = 'Stop'
$script = Join-Path $env:ChocolateyInstall 'bin\osprey.ps1'
@'
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
docker compose -f compose.release.yaml @Arguments
'@ | Set-Content -Path $script -Encoding UTF8
