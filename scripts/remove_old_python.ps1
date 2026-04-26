# Script to remove old Python versions
# Keeps only Python 3.14.3

Write-Host "=== Removing old Python versions ===" -ForegroundColor Cyan
Write-Host ""

# Remove Python 3.9.4 (user installation)
Write-Host "[1/3] Removing Python 3.9.4 (user installation)..." -ForegroundColor Yellow
$python39User = "C:\Users\User\AppData\Local\Package Cache\{e300c142-10a9-46f4-a195-bd40cb90a84f}\python-3.9.4-amd64.exe"
if (Test-Path $python39User) {
    Start-Process -FilePath $python39User -ArgumentList "/uninstall /quiet" -Wait
    Write-Host "  [OK] Python 3.9.4 (user) removed" -ForegroundColor Green
} else {
    Write-Host "  [SKIP] Python 3.9.4 (user) not found" -ForegroundColor Gray
}

# Remove Python 3.11.9 (user installation)
Write-Host "[2/3] Removing Python 3.11.9 (user installation)..." -ForegroundColor Yellow
$python311User = "C:\Users\User\AppData\Local\Package Cache\{1da2e09b-199c-4def-9a99-93a8c1b8ddf2}\python-3.11.9-amd64.exe"
if (Test-Path $python311User) {
    Start-Process -FilePath $python311User -ArgumentList "/uninstall /quiet" -Wait
    Write-Host "  [OK] Python 3.11.9 (user) removed" -ForegroundColor Green
} else {
    Write-Host "  [SKIP] Python 3.11.9 (user) not found" -ForegroundColor Gray
}

# Remove Python 3.9.4 (system components via MSI)
Write-Host "[3/3] Removing Python 3.9.4 (system components)..." -ForegroundColor Yellow

$msiGuids39 = @(
    "{0C0FBC09-C0AA-4B66-92BF-E321BC8C9FA5}", # Utility Scripts
    "{2E65BC05-C532-4BD6-ACDD-3CFDE86F5E36}", # pip Bootstrap
    "{86FD19A0-F018-465C-B8C9-02EA01D35A4B}", # Test Suite
    "{A8C63C1D-BCF8-4446-AFAA-AE21DDA1DBEF}", # Executables
    "{C625291F-C4B5-45A7-B946-FFAB8535A64A}", # Documentation
    "{CCD8CD39-7BDE-46B9-9222-336226D0C346}", # Development Libraries
    "{D5076D33-101B-4402-AAC0-001C6D74D9AB}", # Add to Path
    "{D8D430E7-0DCE-418C-A937-735F329C1AD8}", # Standard Library
    "{DE09AD3C-F617-4EAF-B4F5-943473CB00DA}", # Core Interpreter
    "{E4228F0E-C40C-403A-9533-29BA5A9F9E99}"  # Tcl/Tk Support
)

foreach ($guid in $msiGuids39) {
    $result = Start-Process -FilePath "msiexec.exe" -ArgumentList "/X$guid /quiet /norestart" -Wait -PassThru
    if ($result.ExitCode -eq 0) {
        Write-Host "  [OK] Component $guid removed" -ForegroundColor Green
    } else {
        Write-Host "  [SKIP] Component $guid not found or already removed" -ForegroundColor Gray
    }
}

Write-Host ""
Write-Host "=== Cleanup completed ===" -ForegroundColor Cyan
Write-Host "Only Python 3.14.3 remains" -ForegroundColor Green
Write-Host ""
Write-Host "Please restart your terminal for changes to take effect." -ForegroundColor Yellow
