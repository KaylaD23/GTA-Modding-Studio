$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$targetBin = Join-Path $scriptRoot 'tools\ffmpeg\bin'
$targetExe = Join-Path $targetBin 'ffmpeg.exe'

Write-Host ''
Write-Host 'Checking FFmpeg for GTA Modding Studio / Audio Preparation...'
if (Test-Path $targetExe) {
    try {
        $null = & $targetExe -version 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Local FFmpeg is already installed: $targetExe" -ForegroundColor Green
            exit 0
        }
    } catch {}
}

$systemFfmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if ($systemFfmpeg -and $systemFfmpeg.Source) {
    try {
        $null = & $systemFfmpeg.Source -version 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host "FFmpeg is already available on PATH: $($systemFfmpeg.Source)" -ForegroundColor Green
            Write-Host 'The app will detect it automatically; no local download is needed.'
            exit 0
        }
    } catch {}
}

$tempRoot = Join-Path $env:TEMP ('GTAStudio_FFmpeg_' + [Guid]::NewGuid().ToString('N'))
$zipPath = Join-Path $tempRoot 'ffmpeg-release-essentials.zip'
$extractPath = Join-Path $tempRoot 'extracted'
$url = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'

try {
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    Write-Host 'Downloading the FFmpeg Windows essentials build (internet required on first setup)...'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
    Write-Host 'Extracting FFmpeg...'
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractPath -Force
    $ffmpegItem = Get-ChildItem -Path $extractPath -Filter 'ffmpeg.exe' -File -Recurse |
        Where-Object { $_.Directory.Name -eq 'bin' } | Select-Object -First 1
    if (-not $ffmpegItem) { throw 'The downloaded archive did not contain bin\ffmpeg.exe.' }
    $sourceBin = $ffmpegItem.Directory.FullName

    # Copy the complete bin directory so any DLLs shipped with the build are retained.
    if (Test-Path $targetBin) { Remove-Item -LiteralPath $targetBin -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $targetBin | Out-Null
    Copy-Item -Path (Join-Path $sourceBin '*') -Destination $targetBin -Recurse -Force

    if (-not (Test-Path $targetExe)) { throw 'FFmpeg extraction finished, but ffmpeg.exe is missing from the local tools folder.' }
    $null = & $targetExe -version 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'The downloaded FFmpeg executable did not start successfully.' }
    Write-Host "FFmpeg installed for this application: $targetExe" -ForegroundColor Green
}
catch {
    Write-Host ("ERROR: Automatic FFmpeg setup failed: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Check your internet connection, then rerun Install_Dependencies.bat.' -ForegroundColor Yellow
    Write-Host 'You can also manually install FFmpeg and select ffmpeg.exe in the Audio Preparation tab.' -ForegroundColor Yellow
    exit 1
}
finally {
    if (Test-Path $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
exit 0
