# ============================================================================
# add_python_to_path.ps1
#
# Adds Python and its Scripts directory to the USER PATH (not System PATH —
# safer, doesn't require admin). Idempotent: re-running won't duplicate.
#
# Run from PowerShell:
#   .\scripts\add_python_to_path.ps1
#
# If execution is blocked:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\scripts\add_python_to_path.ps1
# ============================================================================

$ErrorActionPreference = "Stop"

# --- Detect Python install location ----------------------------------------
# Try common Windows install paths, picking the most recent if multiple exist.
$candidatePythonDirs = @(
    "C:\Users\Drama\AppData\Local\Python\pythoncore-3.14-64",
    "C:\Users\Drama\AppData\Local\Python\pythoncore-3.13-64",
    "C:\Users\Drama\AppData\Local\Programs\Python\Python313",
    "C:\Users\Drama\AppData\Local\Programs\Python\Python312",
    "C:\Users\Drama\AppData\Local\Programs\Python\Python311"
)

$pythonDir = $null
foreach ($candidate in $candidatePythonDirs) {
    if (Test-Path (Join-Path $candidate "python.exe")) {
        $pythonDir = $candidate
        break
    }
}

if (-not $pythonDir) {
    Write-Host "Could not auto-detect Python install. Searching..." -ForegroundColor Yellow
    $found = Get-ChildItem -Path "C:\Users\Drama\AppData\Local\Python","C:\Users\Drama\AppData\Local\Programs\Python" `
                -Recurse -Filter "python.exe" -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if ($found) {
        $pythonDir = $found.Directory.FullName
        Write-Host "  Found: $pythonDir" -ForegroundColor Green
    } else {
        Write-Host "ERROR: No python.exe found anywhere under AppData\Local\Python." -ForegroundColor Red
        Write-Host "Pass the path explicitly:" -ForegroundColor Yellow
        Write-Host "  .\scripts\add_python_to_path.ps1 -PythonDir 'C:\full\path\to\python\folder'" -ForegroundColor Gray
        exit 1
    }
}

$scriptsDir = Join-Path $pythonDir "Scripts"

Write-Host ""
Write-Host "Will add to USER PATH:" -ForegroundColor Cyan
Write-Host "  $pythonDir"
Write-Host "  $scriptsDir"
Write-Host ""

# --- Read current User PATH -------------------------------------------------
$currentPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
if (-not $currentPath) {
    $currentPath = ""
}

# Split into array, ignore empties, trim whitespace
$pathEntries = $currentPath.Split(";") | Where-Object { $_ -ne "" } | ForEach-Object { $_.Trim() }

# --- Check what needs adding ------------------------------------------------
$toAdd = @()
foreach ($p in @($pythonDir, $scriptsDir)) {
    # Case-insensitive check
    if (-not ($pathEntries -contains $p) -and -not ($pathEntries -contains $p.TrimEnd("\")) ) {
        $toAdd += $p
    } else {
        Write-Host "  Already on PATH: $p" -ForegroundColor DarkGray
    }
}

if ($toAdd.Count -eq 0) {
    Write-Host ""
    Write-Host "Nothing to add. Both directories are already on your PATH." -ForegroundColor Green
    Write-Host "If python/dotenv/etc still aren't found, restart your PowerShell window." -ForegroundColor Yellow
    exit 0
}

# --- Add and persist --------------------------------------------------------
$newPathEntries = $pathEntries + $toAdd
$newPath = ($newPathEntries -join ";")

[System.Environment]::SetEnvironmentVariable("Path", $newPath, "User")
Write-Host ""
Write-Host "Added the following to your User PATH:" -ForegroundColor Green
foreach ($p in $toAdd) {
    Write-Host "  + $p"
}

# --- Update current session so user doesn't have to restart shell ----------
foreach ($p in $toAdd) {
    if (-not ($env:Path.Split(";") -contains $p)) {
        $env:Path = "$env:Path;$p"
    }
}

# --- Verify -----------------------------------------------------------------
Write-Host ""
Write-Host "Verifying:" -ForegroundColor Cyan
$tools = @("python", "pip", "ruff", "pytest", "dotenv")
foreach ($tool in $tools) {
    $cmd = Get-Command $tool -ErrorAction SilentlyContinue
    if ($cmd) {
        Write-Host "  OK   $tool -> $($cmd.Source)" -ForegroundColor Green
    } else {
        Write-Host "  miss $tool (may not be installed; not necessarily a PATH problem)" -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "Done. New PowerShell windows will pick up the change automatically." -ForegroundColor Green
Write-Host "If a tool is still 'missing' above, install it: pip install <name>" -ForegroundColor Yellow
