$ErrorActionPreference = 'Stop'
$reportRoot = $PSScriptRoot
$reportCompiler = $env:CODEX_TECTONIC_PATH
if (-not $reportCompiler) {
    $reportCompiler = (Get-Command tectonic -ErrorAction Stop).Source
}
$reportBuild = Join-Path $reportRoot 'build'
New-Item -ItemType Directory -Path $reportBuild -Force | Out-Null
# Tectonic ghi cảnh báo (vd. Fontconfig) ra stderr; Windows PowerShell 5.1 coi
# đó là lỗi khi ErrorActionPreference=Stop, nên chỉ dựa vào exit code.
$ErrorActionPreference = 'Continue'
& $reportCompiler --keep-logs --keep-intermediates --outdir $reportBuild (Join-Path $reportRoot 'main.tex') 2>&1 | Out-Host
$ErrorActionPreference = 'Stop'
if ($LASTEXITCODE -ne 0) { throw 'LaTeX compilation failed.' }
Copy-Item -LiteralPath (Join-Path $reportBuild 'main.pdf') -Destination (Join-Path $reportRoot 'bao-cao-thanh-v4.pdf') -Force
Write-Output 'PDF: bao-cao-thanh-v4.pdf'
