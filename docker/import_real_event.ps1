param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [ValidateRange(1.0, 10.0)]
    [double]$Speed = 3.0,

    [ValidateRange(1, 10)]
    [int]$BlankSeconds = 6,

    [string]$OutputName = "real-person-event.local.mp4"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-DockerBounded {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [ValidateRange(1, 3600)]
        [int]$TimeoutSeconds
    )

    $docker = (Get-Command docker -ErrorAction Stop).Source
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $docker
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in $Arguments) {
        [void]$startInfo.ArgumentList.Add($argument)
    }

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "Failed to start Docker"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill($true)
            $process.WaitForExit()
            throw "Docker command exceeded the ${TimeoutSeconds}-second timeout"
        }
        [Threading.Tasks.Task]::WaitAll(
            [Threading.Tasks.Task[]]@($stdoutTask, $stderrTask)
        )
        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        if ($stderr) {
            Write-Host $stderr.TrimEnd()
        }
        if ($process.ExitCode -ne 0) {
            throw "Docker command failed with exit code $($process.ExitCode)"
        }
        return $stdout
    } finally {
        $process.Dispose()
    }
}

$inputFile = Get-Item -LiteralPath (Resolve-Path -LiteralPath $InputPath) -ErrorAction Stop
if ($inputFile.PSIsContainer) {
    throw "InputPath must name a video file"
}
if ([IO.Path]::GetFileName($OutputName) -ne $OutputName) {
    throw "OutputName must be a filename without directory components"
}

$fixtureDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\fixtures")).Path
$outputPath = Join-Path $fixtureDirectory $OutputName
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$speedText = $Speed.ToString([Globalization.CultureInfo]::InvariantCulture)
$inputMount = "type=bind,source=$($inputFile.DirectoryName),target=/input,readonly"
$outputMount = "type=bind,source=$fixtureDirectory,target=/fixtures"
$containerInput = "/input/$($inputFile.Name)"
$containerOutput = "/fixtures/$OutputName"
$filter = "setpts=(PTS-STARTPTS)/$speedText,fps=15,tpad=start_duration=$BlankSeconds`:stop_duration=$BlankSeconds`:color=black,format=yuv420p[out]"

$null = Invoke-DockerBounded -TimeoutSeconds 600 -Arguments @(
    "run", "--rm", "--user", "1000:1000",
    "--mount", $inputMount,
    "--mount", $outputMount,
    "cuckoo-lab-dev:local",
    "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
    "-i", $containerInput,
    "-filter_complex", $filter, "-map", "[out]", "-map_metadata", "-1",
    "-map_chapters", "-1", "-an", "-dn",
    "-c:v", "libx264", "-preset", "fast", "-crf", "20",
    "-pix_fmt", "yuv420p", "-g", "15", "-keyint_min", "15",
    "-sc_threshold", "0", "-movflags", "+faststart", "-f", "mp4",
    $containerOutput
)

$probe = Invoke-DockerBounded -TimeoutSeconds 30 -Arguments @(
    "run", "--rm", "--mount",
    "type=bind,source=$fixtureDirectory,target=/fixtures,readonly",
    "cuckoo-lab-dev:local",
    "ffprobe", "-v", "error", "-show_entries",
    "stream=codec_name,width,height,avg_frame_rate", "-show_entries",
    "format=duration,size", "-of", "json", $containerOutput
)

$hash = Get-FileHash -LiteralPath $outputPath -Algorithm SHA256

$composeEnvPath = Join-Path $projectRoot ".env"
$fixtureSettings = @(
    "FINCH_FIXTURE=/fixtures/$OutputName",
    "FINCH_VIRTUAL_LENS=1"
)
$composeEnvLines = if (Test-Path -LiteralPath $composeEnvPath) {
    @(Get-Content -LiteralPath $composeEnvPath) |
        Where-Object {
            $_ -notmatch '^\s*FINCH_FIXTURE\s*=' -and
            $_ -notmatch '^\s*FINCH_VIRTUAL_LENS\s*='
        }
} else {
    @("# Auto-loaded local fixture selection; this file is ignored by Git.")
}
$composeEnvLines = @($composeEnvLines) + $fixtureSettings
[IO.File]::WriteAllLines(
    $composeEnvPath,
    $composeEnvLines,
    [Text.UTF8Encoding]::new($false)
)
Write-Host "Selected the imported private fixture in the ignored .env file."
[pscustomobject]@{
    Output = $outputPath
    SHA256 = $hash.Hash
    Probe = $probe.Trim()
}
