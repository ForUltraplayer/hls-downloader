# @file build.ps1
# @author: rudals252
# @brief 수동 재생 흐름을 지원하는 Windows 실행 파일 빌드.
param(
    [string]$Python = "python",
    [Parameter(Mandatory = $true)]
    [string]$BuildDirectory
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Path $BuildDirectory -Force | Out-Null
& $Python -m PyInstaller --noconfirm --onefile --windowed `
    --name HLS_Downloader --collect-all patchright `
    --distpath $PSScriptRoot --workpath (Join-Path $BuildDirectory "cache") `
    --specpath $BuildDirectory (Join-Path $PSScriptRoot "hls_downloader.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed (exit $LASTEXITCODE)."
}
