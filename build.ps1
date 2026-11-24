# 构建 Windows 单文件 exe：dist\phonecloud.exe
# 用法：  .\build.ps1            先跑测试再打包
#         .\build.ps1 -SkipTests  只打包
param([switch]$SkipTests)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Host '创建虚拟环境 .venv ...'
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败，请确认已安装 Python 3.10 或更高版本' }
}

Write-Host '安装依赖 ...'
& $python -m pip install --disable-pip-version-check -q -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw '安装依赖失败' }

if (-not $SkipTests) {
    Write-Host '运行测试 ...'
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw '测试未通过，已停止打包' }
}

Write-Host '打包 ...'
$icon = Join-Path $env:TEMP 'phonecloud-build.ico'
& $python -c "from pathlib import Path; from phonecloud.make_icon import write_icon; import os; write_icon(Path(os.environ['TEMP']) / 'phonecloud-build.ico')"
if ($LASTEXITCODE -ne 0) { throw '生成图标失败' }
# --windowed：双击不再闪出黑窗口。--icon：页面上的蓝色云。
# --add-data：把内嵌网页打进 exe；--collect-submodules uvicorn：uvicorn 按名字动态加载协议实现，需显式收集
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name phonecloud `
    --icon $icon `
    --add-data 'phonecloud/web;phonecloud/web' `
    --collect-submodules uvicorn `
    --exclude-module tkinter --exclude-module PIL.ImageTk --exclude-module PIL.ImageQt `
    --exclude-module pytest --exclude-module _pytest `
    run.py
if ($LASTEXITCODE -ne 0) { throw '打包失败' }

$exe = Get-Item (Join-Path $PSScriptRoot 'dist\phonecloud.exe')
Write-Host ('完成：{0}（{1:N1} MB）' -f $exe.FullName, ($exe.Length / 1MB))
