[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$projectRoot = $PSScriptRoot
$runtimeDir = Join-Path $projectRoot '.runtime'
$modelDir = Join-Path $runtimeDir 'models'
New-Item -ItemType Directory -Path $runtimeDir, $modelDir -Force | Out-Null

function Find-LocalPython {
    $candidates = @(
        (Join-Path $projectRoot '.venv\Scripts\python.exe'),
        'C:\Program Files\Python314\python.exe'
    )
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) { $candidates += $pythonCommand.Source }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return @{ Executable = $candidate; Prefix = @() }
        }
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) { return @{ Executable = $launcher.Source; Prefix = @('-3') } }
    throw 'Python was not found. Install Python 3.10 or newer, then run setup.ps1.'
}

function Find-LocalOllama {
    $portable = Join-Path $runtimeDir 'ollama\ollama.exe'
    if (Test-Path -LiteralPath $portable -PathType Leaf) { return $portable }
    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    if ($env:LOCALAPPDATA) {
        $userInstall = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
        if (Test-Path -LiteralPath $userInstall -PathType Leaf) { return $userInstall }
    }
    return $null
}

function Test-OllamaReady {
    try {
        $response = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 2
        return [bool]($response.PSObject.Properties['version'])
    } catch { return $false }
}

$appUrl = "http://127.0.0.1:$Port"
$existingApp = $false
try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri "$appUrl/api/status" -TimeoutSec 5
    $serverHeader = $response.Headers['Server'] -join ' '
    $existingApp = $response.StatusCode -eq 200 -and $serverHeader -like '*CPI-Local*'
} catch {
    # A missing server is expected on the first launch.
}
if ($existingApp) {
    Write-Host "The local CPI knowledge assistant is already running at $appUrl"
    Write-Host 'Reusing the existing server. This launcher will not stop it.'
    try {
        $existingStatus = $response.Content | ConvertFrom-Json
        $reloadHeaders = @{ 'X-CSRF-Token' = [string]$existingStatus.csrf_token }
        $reloaded = Invoke-RestMethod -Method Post -Uri "$appUrl/api/reload-config" `
            -ContentType 'application/json' -Body '{}' -Headers $reloadHeaders -TimeoutSec 15
        Write-Host 'Applied the current settings from config.json.'
        if ($reloaded.job) { Write-Host 'The configured documents folder is being imported.' }
    } catch {
        $reloadFailure = $_
        $reloadStatusCode = 0
        if ($reloadFailure.Exception.PSObject.Properties['Response'] -and $reloadFailure.Exception.Response) {
            $reloadStatusCode = [int]$reloadFailure.Exception.Response.StatusCode
        }
        if ($reloadStatusCode -eq 409) {
            Write-Warning 'An import is already running. When it finishes, run start.cmd again to apply any new folder settings.'
        } else {
            $reloadMessage = $reloadFailure.Exception.Message
            if ($reloadFailure.ErrorDetails -and $reloadFailure.ErrorDetails.Message) {
                try {
                    $reloadErrorBody = $reloadFailure.ErrorDetails.Message | ConvertFrom-Json
                    if ($reloadErrorBody.PSObject.Properties['error']) { $reloadMessage = $reloadErrorBody.error }
                } catch { }
            }
            Write-Warning "The website is available, but config.json could not be reloaded: $reloadMessage"
        }
    }
    if (-not $NoBrowser) { Start-Process $appUrl }
    exit 0
}

# These settings apply only to this process and the child processes it launches.
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_NO_CLOUD = '1'
$env:OLLAMA_MODELS = $modelDir
$python = Find-LocalPython
$pythonPrefix = $python.Prefix
& $python.Executable @pythonPrefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }

if (Test-OllamaReady) {
    Write-Host 'Using the existing local Ollama server on port 11434.'
    Write-Host 'Its existing model folder and configuration apply.'
} else {
    $ollamaExe = Find-LocalOllama
    if ($ollamaExe) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
        $ollamaProcess = Start-Process -FilePath $ollamaExe -ArgumentList 'serve' `
            -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $runtimeDir "ollama-$stamp.out.log") `
            -RedirectStandardError (Join-Path $runtimeDir "ollama-$stamp.err.log")
        $ready = $false
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            if (Test-OllamaReady) { $ready = $true; break }
            if ($ollamaProcess.HasExited) { break }
            Start-Sleep -Milliseconds 500
        }
        if ($ready) {
            Write-Host 'Local Ollama server is ready.'
        } else {
            Write-Warning 'Ollama did not start. Document search is still available; check the logs in .runtime.'
        }
    } else {
        Write-Host 'Ollama is not installed. Document search is available without AI generation.'
        Write-Host 'For local AI answers, run: powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -InstallOllama -DownloadModels'
    }
}

$serverArgs = @((Join-Path $projectRoot 'app.py'), '--host', '127.0.0.1', '--port', [string]$Port)
if (-not $NoBrowser) { $serverArgs += '--open' }
Write-Host "Opening CPI knowledge assistant at http://127.0.0.1:$Port"
Write-Host 'Keep this window open. Press Ctrl+C to stop the website.'
Write-Host 'Ollama remains available locally after the website stops.'
Push-Location $projectRoot
try {
    & $python.Executable @pythonPrefix @serverArgs
    $serverExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $serverExitCode
