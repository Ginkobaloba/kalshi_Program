# ============================================================================
# migrate_and_launch.ps1
#
# One-shot PowerShell script to:
#   1. Copy Kalashi_Program out of OneDrive to C:\code\Kalashi_Program
#   2. Set up a Python virtual environment
#   3. Install dependencies
#   4. Run tests to verify everything works
#   5. Print next steps for GitHub + paper trading
#
# Run from PowerShell:
#   cd C:\Users\Drama\OneDrive\Kalashi_Program
#   .\migrate_and_launch.ps1
#
# If execution is blocked:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\migrate_and_launch.ps1
# ============================================================================

$ErrorActionPreference = "Stop"

$src = "C:\Users\Drama\OneDrive\Kalashi_Program"
$dst = "C:\Users\Drama\code\Kalashi_Program"

Write-Host ""
Write-Host "=== Step 1/5: Copying repo out of OneDrive ===" -ForegroundColor Cyan

if (-not (Test-Path "C:\Users\Drama\code")) {
    New-Item -ItemType Directory -Path "C:\Users\Drama\code" | Out-Null
}

if (Test-Path $dst) {
    Write-Host "Destination already exists: $dst" -ForegroundColor Yellow
    $answer = Read-Host "Overwrite? (y/N)"
    if ($answer -ne "y") {
        Write-Host "Aborting." -ForegroundColor Red
        exit 1
    }
    Remove-Item -Recurse -Force $dst
}

# Robocopy: fast, excludes caches, keeps timestamps
# /E = all subdirs including empty, /XD = exclude dirs, /XF = exclude files
robocopy $src $dst /E `
    /XD ".mypy_cache" ".pytest_cache" "__pycache__" "pytest-cache-files-*" ".git" `
    /XF "*.pyc" "*.pyo" "*.swp" `
    /NFL /NDL /NJH /NJS /NC /NS | Out-Null

# robocopy exits 0-7 on success, >=8 on failure
if ($LASTEXITCODE -ge 8) {
    Write-Host "Robocopy failed with exit code $LASTEXITCODE" -ForegroundColor Red
    exit 1
}

Write-Host "  Copied to $dst" -ForegroundColor Green

cd $dst

Write-Host ""
Write-Host "=== Step 2/5: Setting up Python virtual environment ===" -ForegroundColor Cyan

python -m venv .venv
if ($LASTEXITCODE -ne 0) {
    Write-Host "Python venv creation failed. Is Python 3.10+ on PATH?" -ForegroundColor Red
    exit 1
}

& .\.venv\Scripts\Activate.ps1
Write-Host "  Virtual env activated" -ForegroundColor Green

Write-Host ""
Write-Host "=== Step 3/5: Installing dependencies ===" -ForegroundColor Cyan

python -m pip install --upgrade pip --quiet
python -m pip install -e ".[dev]" --quiet

Write-Host "  Dependencies installed" -ForegroundColor Green

Write-Host ""
Write-Host "=== Step 4/5: Running tests ===" -ForegroundColor Cyan

python -m pytest tests/ -v
if ($LASTEXITCODE -ne 0) {
    Write-Host "Tests failed. Fix before continuing." -ForegroundColor Red
    exit 1
}
Write-Host "  All tests green" -ForegroundColor Green

Write-Host ""
Write-Host "=== Step 5/5: Initializing git ===" -ForegroundColor Cyan

# Only init if no .git already exists
if (-not (Test-Path ".git")) {
    git init -b main
    git add .
    git status --short

    Write-Host ""
    Write-Host "Review the file list above. Press Enter to commit, Ctrl+C to abort." -ForegroundColor Yellow
    Read-Host

    git commit -m "feat: initial v0.2.0 - pm_bot scaffolding + v1 scanner"

    # Create develop branch
    git checkout -b develop
    Write-Host "  Git initialized, on branch 'develop'" -ForegroundColor Green
} else {
    Write-Host "  Git already initialized" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  MIGRATION COMPLETE" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host ""
Write-Host "1. Create the GitHub repo:" -ForegroundColor White
Write-Host "   https://github.com/new" -ForegroundColor Gray
Write-Host "   Name: Kalashi_Program" -ForegroundColor Gray
Write-Host "   Visibility: Private (for now)" -ForegroundColor Gray
Write-Host "   Do NOT initialize with README/license/.gitignore" -ForegroundColor Gray
Write-Host ""
Write-Host "2. Hook up the remote (replace YOUR_USERNAME):" -ForegroundColor White
Write-Host "   git remote add origin https://github.com/YOUR_USERNAME/Kalashi_Program.git" -ForegroundColor Gray
Write-Host "   git checkout main" -ForegroundColor Gray
Write-Host "   git push -u origin main" -ForegroundColor Gray
Write-Host "   git checkout develop" -ForegroundColor Gray
Write-Host "   git push -u origin develop" -ForegroundColor Gray
Write-Host ""
Write-Host "3. Create .env for paper trading (minimal):" -ForegroundColor White
Write-Host "   Copy-Item .env.example .env" -ForegroundColor Gray
Write-Host "   notepad .env   # optional - paper mode works without Kalshi creds" -ForegroundColor Gray
Write-Host ""
Write-Host "4. Start paper trading:" -ForegroundColor White
Write-Host "   python run_bot.py" -ForegroundColor Gray
Write-Host ""
Write-Host "5. Watch what it's doing (in another terminal):" -ForegroundColor White
Write-Host "   Get-Content logs\pm_bot.log -Wait -Tail 20" -ForegroundColor Gray
Write-Host ""
Write-Host "6. Inspect the trade log database:" -ForegroundColor White
Write-Host "   # Install DB Browser for SQLite or:" -ForegroundColor Gray
Write-Host "   python -c `"import sqlite3; c=sqlite3.connect('data/pm_bot.db'); print([r[0] for r in c.execute('SELECT name FROM sqlite_master WHERE type=\\`"table\\`"')])`"" -ForegroundColor Gray
Write-Host ""
Write-Host "You can now safely delete $src if you want." -ForegroundColor Yellow
Write-Host "OneDrive will forget about it eventually." -ForegroundColor Yellow
Write-Host ""
