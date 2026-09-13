[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$fixtureRoot = Join-Path $projectRoot 'fixtures'
$source = Join-Path $fixtureRoot 'frigate-person-cutout.png'
$output = Join-Path $fixtureRoot 'frigate-person.h265'

if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Missing generated person cutout: $source"
}

$filter = "[0:v]drawbox=x=0:y=500:w=1280:h=220:color=0xB8BDC4:t=fill[bg];[1:v]scale=-1:500[person];[bg][person]overlay=x='if(lt(t,8),-w+(W+w)*t/8,-w)':y='H-h-40':eval=frame:shortest=1,format=yuv420p"
$dockerArgs = @(
    'run', '--rm', '--user', '1000:1000',
    '--mount', "type=bind,source=$fixtureRoot,target=/fixtures",
    'cuckoo-lab-dev:local',
    'ffmpeg', '-hide_banner', '-loglevel', 'warning', '-y',
    '-f', 'lavfi', '-i', 'color=c=0xD8DCE0:s=1280x720:r=15:d=20',
    '-loop', '1', '-framerate', '15',
    '-i', '/fixtures/frigate-person-cutout.png',
    '-t', '20', '-an', '-filter_complex', $filter,
    '-c:v', 'libx265', '-pix_fmt', 'yuv420p',
    '-x265-params', 'keyint=15:min-keyint=15:scenecut=0:repeat-headers=1',
    '-f', 'hevc', '/fixtures/frigate-person.h265'
)

& docker @dockerArgs
if ($LASTEXITCODE -ne 0) {
    throw "FFmpeg container exited with code $LASTEXITCODE"
}

Get-FileHash -Algorithm SHA256 -LiteralPath $output
