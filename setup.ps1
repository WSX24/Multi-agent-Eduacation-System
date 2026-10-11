# PPT 生成链路 —— 一键安装 + 自检（Windows PowerShell）
#
# 用法（在包含 PPT-Agent / PromptTemplate 的目录，即仓库根）：
#     powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# 前提：已装 Python 3.10+（`python --version` 能跑通）、已装 git。
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host "`n== 1/5 准备 ppt-master（skill 依赖，第三方仓库）==" -ForegroundColor Cyan
$skillCheck = Join-Path $root "ppt-master\skills\ppt-master"
if (Test-Path $skillCheck) {
    Write-Host "  已存在，跳过"
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "  ❌ 没装 git，无法自动拉取 ppt-master。" -ForegroundColor Red
        Write-Host "     请手动: git clone https://github.com/hugohe3/ppt-master.git `"$root\ppt-master`"" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "  正在 clone hugohe3/ppt-master …"
    git clone --depth 1 https://github.com/hugohe3/ppt-master.git (Join-Path $root "ppt-master")
}

Write-Host "`n== 2/5 安装依赖 ==" -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install "click>=8.2.1" "openai>=1.91.0" "PyYAML>=6.0"
python -m pip install -r "$root\ppt-master\skills\ppt-master\requirements.txt"

Write-Host "`n== 3/5 生成 .env 模板 ==" -ForegroundColor Cyan
$envFile = Join-Path $root "PPT-Agent\.env"
if (-not (Test-Path $envFile)) {
    "DEEPSEEK_API_KEY=sk-在这里填你的key`nPPT_AGENT_PROVIDER=deepseek" |
        Set-Content -Path $envFile -Encoding UTF8
    Write-Host "  已生成 $envFile —— 请打开填入 DEEPSEEK_API_KEY" -ForegroundColor Yellow
} else {
    Write-Host "  $envFile 已存在，跳过"
}

Write-Host "`n== 4/5 离线自检（不花钱）==" -ForegroundColor Cyan
Push-Location (Join-Path $root "PPT-Agent")
python plot_math.py --selftest
python pipeline.py --check
Pop-Location

Write-Host "`n== 5/5 完成 ==" -ForegroundColor Green
Write-Host @"

下一步（务必先 --dry-run 看提示词，再真跑）：
    cd PPT-Agent
    python run_unit_ppt.py --unit "导数的几何意义" --chapter "第2章" ``
        --subject "高中数学" --level 中等 --pages 10 --dry-run
    # 确认无误后去掉 --dry-run 真跑（会调 LLM，有成本）

单跑 PPT（不经过单元渲染）：
    python pipeline.py --unit-file unit.md "做一份 10 页的《XXX》课件"

快速档（P3 关思考）：
    python run_unit_ppt.py ... --reasoning-exec none --reasoning-repair low

"@ -ForegroundColor Gray
