[CmdletBinding()]
param(
    # Paste your documents folder between these quotes, for example 'C:\CPI-Documents'.
    # You can leave it empty now and set it later, or pass -DocumentsFolder when running setup.
    [string]$DocumentsFolder = '',
    [switch]$InstallOllama,
    [switch]$DownloadModels,
    [switch]$SkipPython,
    [string]$PythonExe,
    [string]$ChatModel = 'qwen3:4b',
    [string]$EmbeddingModel = 'nomic-embed-text'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$projectRoot = $PSScriptRoot
$runtimeDir = Join-Path $projectRoot '.runtime'
$modelDir = Join-Path $runtimeDir 'models'
$ollamaDir = Join-Path $runtimeDir 'ollama'
$venvDir = Join-Path $projectRoot '.venv'
New-Item -ItemType Directory -Path $runtimeDir, $modelDir -Force | Out-Null

function Find-SetupPython {
    if ($PythonExe) {
        if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
            throw "Python executable does not exist: $PythonExe"
        }
        return @{ Executable = $PythonExe; Prefix = @() }
    }
    $candidates = @('C:\Program Files\Python314\python.exe')
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return @{ Executable = $candidate; Prefix = @() }
        }
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) { return @{ Executable = $launcher.Source; Prefix = @('-3') } }
    throw 'Install Python 3.10 or newer, or provide -PythonExe with its full path.'
}

function Find-SetupOllama {
    $portable = Join-Path $ollamaDir 'ollama.exe'
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

if ($DocumentsFolder -or $PSBoundParameters.ContainsKey('DocumentsFolder') -or
    $PSBoundParameters.ContainsKey('ChatModel') -or $PSBoundParameters.ContainsKey('EmbeddingModel')) {
    $configPath = Join-Path $projectRoot 'config.json'
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        if ($null -eq $config -or $config -isnot [pscustomobject]) {
            throw 'config.json must contain a JSON object. Existing configuration was not changed.'
        }
    } else {
        $config = [pscustomobject]@{
            model = 'qwen3:4b'
            embedding_model = 'nomic-embed-text'
            documents_folder = ''
            auto_import = $true
        }
    }
    if ($DocumentsFolder -or $PSBoundParameters.ContainsKey('DocumentsFolder')) {
        $savedFolder = ''
        if (-not [string]::IsNullOrWhiteSpace($DocumentsFolder)) {
            if ([IO.Path]::IsPathRooted($DocumentsFolder)) {
                $savedFolder = [IO.Path]::GetFullPath($DocumentsFolder)
            } else {
                $savedFolder = [IO.Path]::GetFullPath((Join-Path $projectRoot $DocumentsFolder))
            }
        }
        $config | Add-Member -NotePropertyName 'documents_folder' -NotePropertyValue $savedFolder -Force
        if ($savedFolder) {
            Write-Host "Documents folder saved: $savedFolder"
            if (-not (Test-Path -LiteralPath $savedFolder -PathType Container)) {
                Write-Warning 'That folder does not exist yet. You can create it and add documents before starting the website.'
            }
        } else {
            Write-Host 'No automatic documents folder is configured.'
        }
    }
    if ($PSBoundParameters.ContainsKey('ChatModel')) {
        $config | Add-Member -NotePropertyName 'model' -NotePropertyValue $ChatModel -Force
    }
    if ($PSBoundParameters.ContainsKey('EmbeddingModel')) {
        $config | Add-Member -NotePropertyName 'embedding_model' -NotePropertyValue $EmbeddingModel -Force
    }
    $configJson = ($config | ConvertTo-Json -Depth 10) + [Environment]::NewLine
    [IO.File]::WriteAllText($configPath, $configJson, (New-Object System.Text.UTF8Encoding $false))
}

if (-not $SkipPython) {
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $python = Find-SetupPython
        $pythonPrefix = $python.Prefix
        & $python.Executable @pythonPrefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }
        Write-Host 'Creating the project Python environment...'
        & $python.Executable @pythonPrefix -m venv $venvDir
        if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv.' }
    }
    Write-Host 'Installing PDF parsing and local vector-search support...'
    & $venvPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed. Check your connection or company package mirror.' }
}

if ($InstallOllama) {
    $portableExe = Join-Path $ollamaDir 'ollama.exe'
    if (Test-Path -LiteralPath $portableExe -PathType Leaf) {
        Write-Host 'The project already contains Ollama; keeping the existing installation.'
    } else {
        if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -and $env:PROCESSOR_ARCHITEW6432 -ne 'AMD64') {
            throw 'The portable installer supports x64 Windows. Install an appropriate Ollama build separately on other architectures.'
        }
        # Official standalone release, pinned so the checksum can be verified before extraction.
        $ollamaVersion = 'v0.40.13'
        $expectedSha256 = '7afa38624fa62ee5d8293422d0ab084c1bb4603134c7f4dafce11a033b57cc19'
        $assetName = 'ollama-windows-amd64.zip'
        $downloadUri = "https://github.com/ollama/ollama/releases/download/$ollamaVersion/$assetName"
        $downloadsDir = Join-Path $runtimeDir 'downloads'
        New-Item -ItemType Directory -Path $downloadsDir -Force | Out-Null
        $archivePath = Join-Path $downloadsDir "ollama-$ollamaVersion-windows-amd64.zip"
        $needsDownload = $true
        if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
            $needsDownload = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $expectedSha256
        }
        if ($needsDownload) {
            Write-Host "Downloading official Ollama $ollamaVersion (a large download)..."
            $oldProgressPreference = $ProgressPreference
            try {
                $ProgressPreference = 'SilentlyContinue'
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                Invoke-WebRequest -UseBasicParsing -Uri $downloadUri -OutFile $archivePath
            } finally {
                $ProgressPreference = $oldProgressPreference
            }
        }
        Write-Host 'Verifying the Ollama archive...'
        if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $expectedSha256) {
            throw "The Ollama download failed checksum verification. The archive was not installed: $archivePath"
        }
        Write-Host 'Extracting Ollama into .runtime\ollama...'
        Expand-Archive -LiteralPath $archivePath -DestinationPath $ollamaDir -Force
        if (-not (Test-Path -LiteralPath $portableExe -PathType Leaf)) {
            throw 'The archive did not contain ollama.exe at the expected location.'
        }
        Write-Host 'Portable Ollama is installed. No system installation or PATH change was made.'
    }
}

if ($DownloadModels) {
    $ollamaExe = Find-SetupOllama
    if (-not $ollamaExe) {
        throw 'Ollama is missing. Rerun setup.ps1 with -InstallOllama -DownloadModels, or install Ollama separately.'
    }
    $env:OLLAMA_HOST = '127.0.0.1:11434'
    $env:OLLAMA_NO_CLOUD = '1'
    $env:OLLAMA_MODELS = $modelDir
    if (Test-OllamaReady) {
        Write-Host 'Using the existing local Ollama server on port 11434.'
        Write-Host 'Downloads will use that server''s configured model folder.'
    } else {
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
        if (-not $ready) { throw 'Ollama did not start. See its logs in .runtime.' }
    }
    $localModelStatus = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 10
    $installedModelNames = @($localModelStatus.models | ForEach-Object { $_.name })
    foreach ($modelName in @($ChatModel, $EmbeddingModel)) {
        if ([string]::IsNullOrWhiteSpace($modelName) -or $modelName -match '(^-|cloud|://)') {
            throw "Use a local Ollama model name: $modelName"
        }
        $canonicalName = if ($modelName.Contains(':')) { $modelName } else { "${modelName}:latest" }
        if ($installedModelNames -contains $modelName -or $installedModelNames -contains $canonicalName) {
            Write-Host "$modelName is already available locally; skipping its download."
            continue
        }
        Write-Host "Downloading local model $modelName..."
        & $ollamaExe pull $modelName
        if ($LASTEXITCODE -ne 0) { throw "Could not download $modelName. Rerun setup with -DownloadModels to retry." }
    }
}

Write-Host ''
Write-Host 'Setup finished. Double-click start.cmd to open the local website.'
if (-not $DownloadModels) {
    Write-Host 'Local AI models can be added with: .\setup.ps1 -InstallOllama -DownloadModels'
}
