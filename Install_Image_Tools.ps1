$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$targetRoot = Join-Path $scriptRoot 'Tools\image_tools'
$targetMaxCso = Join-Path $targetRoot 'maxcso.exe'
$targetIsz = Join-Path $targetRoot 'isz-tool.py'
New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$headers = @{ 'User-Agent' = 'GTA-Modding-Studio'; 'Accept' = 'application/vnd.github+json' }
$tempRoot = Join-Path $env:TEMP ('GTAStudio_ImageTools_' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
$script:InstallDiagnostics = New-Object 'System.Collections.Generic.List[string]'

function Add-Diagnostic([string]$Message) {
    if (-not [string]::IsNullOrWhiteSpace($Message)) {
        [void]$script:InstallDiagnostics.Add($Message.Trim())
        Write-Host ('  ' + $Message.Trim()) -ForegroundColor Yellow
    }
}

function Test-Is7ZipArchive([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $stream = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        if ($stream.Length -lt 6) { return $false }
        [byte[]]$signature = New-Object byte[] 6
        [void]$stream.Read($signature, 0, 6)
        return (($signature[0] -eq 0x37) -and ($signature[1] -eq 0x7A) -and
                ($signature[2] -eq 0xBC) -and ($signature[3] -eq 0xAF) -and
                ($signature[4] -eq 0x27) -and ($signature[5] -eq 0x1C))
    } catch { return $false }
    finally { if ($stream) { $stream.Dispose() } }
}

function Test-PortableExecutable([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $stream = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        if ($stream.Length -lt 50000) { return $false }
        return (($stream.ReadByte() -eq 0x4D) -and ($stream.ReadByte() -eq 0x5A))
    } catch { return $false }
    finally { if ($stream) { $stream.Dispose() } }
}

function Get-7ZipExtractors([string]$TempRoot) {
    $results = New-Object 'System.Collections.Generic.List[object]'
    $seen = @{}
    $names = @('7z.exe', '7za.exe', '7zr.exe')
    foreach ($name in $names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
            $full = [System.IO.Path]::GetFullPath($command.Source)
            if (-not $seen.ContainsKey($full)) {
                $seen[$full] = $true
                $results.Add([pscustomobject]@{ Path = $full; Label = $name + ' from PATH' })
            }
        }
    }

    $programFiles = [Environment]::GetFolderPath('ProgramFiles')
    $programFilesX86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
    foreach ($basePath in @($programFiles, $programFilesX86)) {
        if ([string]::IsNullOrWhiteSpace($basePath)) { continue }
        foreach ($name in @('7z.exe', '7za.exe', '7zr.exe')) {
            $candidate = Join-Path (Join-Path $basePath '7-Zip') $name
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $full = [System.IO.Path]::GetFullPath($candidate)
                if (-not $seen.ContainsKey($full)) {
                    $seen[$full] = $true
                    $results.Add([pscustomobject]@{ Path = $full; Label = $candidate })
                }
            }
        }
    }

    # Always have a known standalone fallback when no installed extractor exists.
    # 7zr supports the .7z format needed for MaxCSO's official Windows archive.
    $portable = Join-Path $TempRoot '7zr.exe'
    if (-not (Test-PortableExecutable $portable)) {
        try {
            Write-Host 'Downloading the official standalone 7-Zip extractor (required to unpack the MaxCSO .7z release)...'
            Invoke-WebRequest -Uri 'https://www.7-zip.org/a/7zr.exe' -Headers @{ 'User-Agent' = 'GTA-Modding-Studio' } -OutFile $portable -UseBasicParsing
            if (-not (Test-PortableExecutable $portable)) {
                throw 'Downloaded 7zr.exe did not pass the Windows executable signature/size check.'
            }
        } catch {
            Add-Diagnostic ('Could not obtain the standalone 7-Zip extractor: ' + $_.Exception.Message)
        }
    }
    if (Test-PortableExecutable $portable) {
        $full = [System.IO.Path]::GetFullPath($portable)
        if (-not $seen.ContainsKey($full)) {
            $seen[$full] = $true
            $results.Add([pscustomobject]@{ Path = $full; Label = 'official standalone 7zr.exe downloaded to TEMP' })
        }
    }
    return $results.ToArray()
}

function Find-MaxCsoExecutable([string]$Directory) {
    $candidate = Get-ChildItem -LiteralPath $Directory -Filter 'maxcso.exe' -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $candidate) { $candidate = Get-ChildItem -LiteralPath $Directory -Filter 'maxcso32.exe' -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 }
    return $candidate
}

function Extract-MaxCsoAsset([string]$ArchivePath, [string]$Extension, [string]$TempRoot) {
    if ($Extension -eq '.exe') { return $ArchivePath }
    $extractors = @()
    if ($Extension -eq '.7z') {
        if (-not (Test-Is7ZipArchive $ArchivePath)) {
            throw 'The downloaded file is not a valid .7z archive (the 7z magic signature is missing). The download may be an HTML error page.'
        }
        $extractors = @(Get-7ZipExtractors -TempRoot $TempRoot)
        if ($extractors.Count -eq 0) {
            throw 'No working 7-Zip extractor is available. The official standalone 7zr.exe download also failed.'
        }
    }

    $extractDirectory = Join-Path $TempRoot ('extract_' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $extractDirectory | Out-Null
    if ($Extension -eq '.zip') {
        Expand-Archive -LiteralPath $ArchivePath -DestinationPath $extractDirectory -Force
        $item = Find-MaxCsoExecutable -Directory $extractDirectory
        if (-not $item) { throw 'ZIP downloaded successfully but it did not contain maxcso.exe or maxcso32.exe.' }
        return $item.FullName
    }

    foreach ($extractor in $extractors) {
        # A system 7z.exe on PATH can be an incomplete shim without 7z.dll.
        # On extraction failure try the next extractor, including the standalone tool.
        $attemptDirectory = Join-Path $extractDirectory ([Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $attemptDirectory | Out-Null
        Write-Host ('Extracting the MaxCSO .7z archive with ' + $extractor.Label + '...')
        try {
            $output = & $extractor.Path x $ArchivePath (('-o' + $attemptDirectory)) -y -bd 2>&1
            $exitCode = $LASTEXITCODE
            $outputText = ($output | Out-String).Trim()
            $item = Find-MaxCsoExecutable -Directory $attemptDirectory
            if (($exitCode -eq 0) -and $item) {
                Write-Host ('Extracted MaxCSO executable: ' + $item.FullName) -ForegroundColor Green
                return $item.FullName
            }
            $detail = 'Extractor: ' + $extractor.Label + '; exit code: ' + $exitCode
            if ($outputText) { $detail += '; output: ' + $outputText }
            if (-not $item) { $detail += '; extracted files did not include maxcso.exe/maxcso32.exe' }
            Add-Diagnostic $detail
        } catch {
            Add-Diagnostic ('Extractor ' + $extractor.Label + ' failed: ' + $_.Exception.Message)
        }
    }
    throw 'The MaxCSO .7z file was downloaded, but no extractor could unpack it and locate maxcso.exe.'
}

function Test-MaxCsoRunnable([string]$ExecutablePath) {
    # PowerShell 5.1 turns native stderr (including normal --help/version text)
    # into an ErrorRecord under $ErrorActionPreference='Stop'. Launch through
    # System.Diagnostics.Process to capture the streams without that behavior.
    if (-not (Test-PortableExecutable $ExecutablePath)) {
        Add-Diagnostic ('MaxCSO executable is missing or has an invalid PE header: ' + $ExecutablePath)
        return $false
    }
    $proc = New-Object System.Diagnostics.Process
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $ExecutablePath
        $psi.Arguments = '--help'
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $proc.StartInfo = $psi
        if (-not $proc.Start()) {
            Add-Diagnostic 'MaxCSO process did not start.'
            return $false
        }
        # Read both streams asynchronously to avoid pipe deadlocks.
        $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
        $stderrTask = $proc.StandardError.ReadToEndAsync()
        if (-not $proc.WaitForExit(15000)) {
            try { $proc.Kill() } catch {}
            Add-Diagnostic 'MaxCSO help check timed out after 15 seconds.'
            return $false
        }
        $output = ($stdoutTask.GetAwaiter().GetResult() + "`n" + $stderrTask.GetAwaiter().GetResult()).Trim()
        $exitCode = $proc.ExitCode
        # Official MaxCSO v1.13.0 prints 'maxcso v1.13.0' during --help;
        # the tool may exit nonzero for --help, which is not an install failure.
        $versionShown = ($output -match '(?im)^\s*maxcso(?:\.exe)?\s+v?\d+\.\d+(?:\.\d+)?\b')
        $usageShown = ($output -match '(?i)usage\s*:\s*maxcso')
        if ($versionShown -or $usageShown) {
            Write-Host ('MaxCSO executable startup verified (help exit code ' + $exitCode + ').') -ForegroundColor Green
            return $true
        }
        $firstLine = ($output -split "`r?`n" | Select-Object -First 1)
        if (-not $firstLine) { $firstLine = '(no help output)' }
        Add-Diagnostic ('MaxCSO startup check failed (exit ' + $exitCode + '): ' + $firstLine)
        return $false
    } catch {
        Add-Diagnostic ('MaxCSO could not start: ' + $_.Exception.Message)
        return $false
    } finally {
        $proc.Dispose()
    }
}

try {
    Write-Host ''
    Write-Host 'Installing/checking MaxCSO (CSO decompression and recompression)...'
    $maxCsoReady = $false
    if (Test-Path -LiteralPath $targetMaxCso -PathType Leaf) {
        $maxCsoReady = Test-MaxCsoRunnable -ExecutablePath $targetMaxCso
    }

    if (-not $maxCsoReady) {
        $assets = @()
        try {
            $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/unknownbrackets/maxcso/releases/latest' -Headers $headers
            $allAssetNames = @($release.assets | ForEach-Object { [string]$_.name })
            if ($allAssetNames.Count -gt 0) { Write-Host ('GitHub release assets: ' + ($allAssetNames -join ', ')) }
            $assets = @($release.assets | Where-Object {
                $_.name -match '(?i)\.(7z|zip|exe)$' -and
                $_.name -match '(?i)(windows|win)' -and
                $_.name -notmatch '(?i)source|32.?bit|x86(?!.*64)'
            } | Sort-Object -Property @{Expression={
                if ($_.name -match '(?i)(x64|64.?bit|windows\.7z$)') { 0 } else { 1 }
            }})
        } catch {
            Add-Diagnostic ('Could not read the GitHub release API: ' + $_.Exception.Message)
        }

        $downloadCandidates = New-Object 'System.Collections.Generic.List[object]'
        foreach ($asset in $assets) {
            if ($asset.browser_download_url) {
                $downloadCandidates.Add([pscustomobject]@{
                    Name = [string]$asset.name
                    Url = [string]$asset.browser_download_url
                })
            }
        }
        # Stable known release asset fallback in case GitHub's API is rate-limited or blocked.
        $fallbackUrl = 'https://github.com/unknownbrackets/maxcso/releases/download/v1.13.0/maxcso_v1.13.0_windows.7z'
        if (-not ($downloadCandidates | Where-Object { $_.Url -eq $fallbackUrl })) {
            $downloadCandidates.Add([pscustomobject]@{ Name = 'maxcso_v1.13.0_windows.7z'; Url = $fallbackUrl })
        }

        $installed = $false
        foreach ($asset in $downloadCandidates) {
            $safeName = [IO.Path]::GetFileName($asset.Name)
            if ([string]::IsNullOrWhiteSpace($safeName)) { $safeName = 'maxcso_asset' }
            $download = Join-Path $tempRoot $safeName
            Write-Host ('Downloading official MaxCSO asset: ' + $asset.Name)
            try {
                Invoke-WebRequest -Uri $asset.Url -Headers @{ 'User-Agent' = 'GTA-Modding-Studio' } -OutFile $download -UseBasicParsing
                if (-not (Test-Path -LiteralPath $download -PathType Leaf) -or (Get-Item -LiteralPath $download).Length -lt 10000) {
                    throw 'Downloaded asset is missing or unexpectedly small.'
                }

                $extension = [IO.Path]::GetExtension($safeName).ToLowerInvariant()
                $candidateExe = Extract-MaxCsoAsset -ArchivePath $download -Extension $extension -TempRoot $tempRoot
                if (-not $candidateExe -or -not (Test-Path -LiteralPath $candidateExe -PathType Leaf)) {
                    throw 'MaxCSO archive extraction completed, but the executable could not be found.'
                }

                if ($extension -eq '.exe') {
                    Copy-Item -LiteralPath $candidateExe -Destination $targetMaxCso -Force
                } else {
                    # Copy the executable and any adjacent DLL/runtime files from its extracted folder.
                    $candidateDir = Split-Path -Parent $candidateExe
                    Get-ChildItem -LiteralPath $candidateDir -File | ForEach-Object {
                        $destName = if ($_.Name -match '(?i)^maxcso32\.exe$') { 'maxcso.exe' } else { $_.Name }
                        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $targetRoot $destName) -Force
                    }
                }

                if (-not (Test-Path -LiteralPath $targetMaxCso -PathType Leaf)) {
                    throw 'MaxCSO file copy completed, but tools\image_tools\maxcso.exe is missing.'
                }
                if (Test-MaxCsoRunnable -ExecutablePath $targetMaxCso) {
                    $installed = $true
                    break
                }
                throw 'The copied MaxCSO executable did not pass the process-launch and version/help output check.'
            } catch {
                Add-Diagnostic ('Asset ' + $asset.Name + ' failed: ' + $_.Exception.Message)
            }
        }
        if (-not $installed) {
            $details = ''
            if ($script:InstallDiagnostics.Count -gt 0) {
                $details = ' Attempts: ' + (($script:InstallDiagnostics.ToArray()) -join ' || ')
            }
            throw ('Could not install a runnable MaxCSO Windows binary. The installer now explicitly downloads and extracts the official .7z archive; see the preceding download/extraction messages.' + $details)
        }
    }
    Write-Host ('MaxCSO is ready: ' + $targetMaxCso) -ForegroundColor Green

    Write-Host ''
    Write-Host 'Checking bundled isz-tool.py (offline ISZ to ISO conversion)...'
    # isz-tool.py and its GPL licence are distributed with GTA Modding Studio.
    # Never download or replace this third-party source at install time: that
    # would unexpectedly change the shipped version and break offline installs.
    if (-not (Test-Path -LiteralPath $targetIsz -PathType Leaf)) {
        throw ('Bundled ISZ decoder missing: ' + $targetIsz + '. Re-extract the complete GTA Modding Studio ZIP (including Tools\image_tools).')
    }
    $currentText = Get-Content -LiteralPath $targetIsz -Raw -ErrorAction Stop
    if ($currentText -notmatch 'class ISZ_header' -or
        $currentText -notmatch 'def extract_to' -or
        $currentText -notmatch 'GNU General Public License') {
        throw 'The bundled isz-tool.py is incomplete or invalid. Re-extract the complete GTA Modding Studio ZIP.'
    }
    # Licence files accompany the *distributed* release, but are not runtime
    # dependencies. Users may remove local licence copies after installation;
    # do not incorrectly report a broken ISZ or CSO tool in that case.
    # Distribution completeness (licence attribution/notice files) is instead
    # checked by Test_Bundled_ISZ.py in the release packaging tests.
    Write-Host ('Bundled ISZ converter is ready (no download required): ' + $targetIsz) -ForegroundColor Green
    Write-Host 'CSO support: MaxCSO (ISC license), https://github.com/unknownbrackets/maxcso'
    Write-Host 'ISZ support: isz-tool.py (GPL-3.0-or-later), https://github.com/oserres/isz-tool'
    Write-Host 'ISZ: native Studio encoder can create unencrypted single-part ISZ v1; isz-tool.py handles ISZ input. Encrypted/password-protected ISZ is not supported.' -ForegroundColor Cyan
}
catch {
    Write-Host ('ERROR: CSO/ISZ tool installation failed: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
finally {
    if (Test-Path $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
exit 0
