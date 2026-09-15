$ErrorActionPreference = 'Stop'
$script = Join-Path $toolsDir 'osprey.ps1'
$compose = Join-Path $toolsDir 'compose.release.yaml'
@'
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
docker compose -f '__COMPOSE__' @Arguments
'@ | ForEach-Object { $_.Replace('__COMPOSE__', $compose) } | Set-Content -Path $script -Encoding UTF8
Install-BinFile -Name 'osprey' -Path $script
