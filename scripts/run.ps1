# Forward CLI switches such as -o without PowerShell common-parameter binding.
$ErrorActionPreference = "Stop"
$CliArgs = @($args)
$Command = if ($args.Count -gt 0) { [string]$args[0] } else { "" }
if ([string]::IsNullOrWhiteSpace($Command)) {
    throw "用法：.\scripts\run.ps1 cut|review|render <视频> [参数]"
}

$ScriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillDir = Split-Path -Parent $ScriptsDir
$PythonBin = Join-Path $SkillDir ".venv\Scripts\python.exe"
$EditorScript = Join-Path $ScriptsDir "video_editor.py"
$CredentialProfile = Join-Path $ScriptsDir "credential-ui\src\profile.ts"

if (-not (Test-Path -LiteralPath $PythonBin)) {
    throw "找不到 Windows 虚拟环境，请先运行 .\setup.ps1"
}

$HasHelp = ($CliArgs -contains "--help") -or ($CliArgs -contains "-h")
if ($Command -eq "cut" -and -not $HasHelp) {
    $Backend = & $PythonBin -c "import sys; sys.path.insert(0, sys.argv[1]); from video_editor import get_config; print(str(get_config().get('asr_backend', 'bailian')).strip().lower())" $ScriptsDir
    if ($LASTEXITCODE -ne 0) { throw "Cannot read ASR configuration." }
    if ($Backend -notin @("bailian", "volcengine")) { throw "Unsupported ASR backend." }
    $Profile = if ($Backend -eq "volcengine") { "volcengine" } else { "default" }
    $KeyCheck = "import sys; sys.path.insert(0, sys.argv[1]); from transcriber import load_api_key`ntry: load_api_key(sys.argv[2])`nexcept RuntimeError: sys.exit(2)"
    & $PythonBin -c $KeyCheck $ScriptsDir $Backend
    if ($LASTEXITCODE -ne 0) {
        if ($null -eq (Get-Command node -ErrorAction SilentlyContinue)) {
            throw "未找到 Node.js，无法打开凭据配置入口。"
        }
        & node $CredentialProfile run $Profile -- $PythonBin $EditorScript @CliArgs
        exit $LASTEXITCODE
    }
}

& $PythonBin $EditorScript @CliArgs
exit $LASTEXITCODE
