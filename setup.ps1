[CmdletBinding()]
param(
    [switch]$SkipDependencies
)

$ErrorActionPreference = "Stop"
$SkillDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $SkillDir

function Get-RequiredCommand([string[]]$Names) {
    foreach ($Name in $Names) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($null -ne $Command) { return $Command.Source }
    }
    throw "找不到 $($Names -join ' 或 ')。请先安装并加入 PATH。"
}

$BootstrapPython = Get-RequiredCommand @("python", "py")
Get-RequiredCommand @("ffmpeg") | Out-Null
Get-RequiredCommand @("ffprobe") | Out-Null

Write-Host "=== video-editor Windows Setup ==="
Write-Host "Skill directory: $SkillDir"

$VenvDir = Join-Path $SkillDir ".venv"
$PythonBin = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonBin)) {
    & $BootstrapPython -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Python virtual environment creation failed." }
}

if (-not $SkipDependencies) {
    $AsrProvider = & $PythonBin -c "import sys; sys.path.insert(0, sys.argv[1]); from video_editor import get_config; print(str(get_config().get('asr_backend', 'bailian')).strip().lower())" (Join-Path $SkillDir "scripts")
    if ($LASTEXITCODE -ne 0) { throw "Cannot read ASR configuration." }
    $DependencyList = @("silero-vad", "torch")
    if ($AsrProvider -eq "bailian") { $DependencyList += "dashscope" }
    & $PythonBin -m pip install --quiet --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
    & $PythonBin -m pip install --quiet @DependencyList -r (Join-Path $SkillDir "requirements-render.txt")
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
    & $PythonBin -c "import silero_vad, av, smartcut; print('silero-vad and smartcut verified; PyAV ' + av.__version__)"
    if ($LASTEXITCODE -ne 0) { throw "Dependency verification failed." }
} else {
    Write-Host "Skipping Python dependency installation."
}

if ($null -ne (Get-Command bl -ErrorAction SilentlyContinue)) {
    Write-Host "bl CLI: available"
} else {
    Write-Host "bl CLI: not found; use the configured ASR provider credential profile."
}

Write-Host "=== Setup complete ==="
Write-Host "Use: .\scripts\run.ps1 cut <video>"
