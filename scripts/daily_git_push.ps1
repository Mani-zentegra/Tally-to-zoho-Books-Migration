# Daily Automated Git Backup
$repoPath = "C:\Users\Zen\OneDrive\Documents\Tally Migration Backup - 28-4-2026"
Set-Location $repoPath

$logFile = Join-Path $repoPath "scripts\daily_push.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

Add-Content -Path $logFile -Value "======================================================"
Add-Content -Path $logFile -Value "[$timestamp] Starting Daily Automated Git Backup"

try {
    $status = git status --porcelain
    if ($status) {
        Add-Content -Path $logFile -Value "[$timestamp] Changes detected. Staging modified and database files..."
        $addOutput = cmd.exe /c "git add -A 2>&1"
        if ($addOutput) { Add-Content -Path $logFile -Value $addOutput }
        
        $commitMsg = "chore: automated daily backup $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
        $commitOutput = cmd.exe /c "git commit -m `"$commitMsg`" 2>&1"
        if ($commitOutput) { Add-Content -Path $logFile -Value $commitOutput }
        
        Add-Content -Path $logFile -Value "[$timestamp] Pushing to origin main..."
        $pushOutput = cmd.exe /c "git push origin main 2>&1"
        if ($pushOutput) { Add-Content -Path $logFile -Value $pushOutput }
        
        if ($LASTEXITCODE -eq 0) {
            Add-Content -Path $logFile -Value "[$timestamp] Daily backup push completed successfully."
        } else {
            Add-Content -Path $logFile -Value "[$timestamp] Warning: git push returned exit code $LASTEXITCODE"
        }
    } else {
        Add-Content -Path $logFile -Value "[$timestamp] No changes detected. Repository clean."
    }
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] Error during backup: $_"
}

Add-Content -Path $logFile -Value "[$timestamp] Finished Daily Git Backup"
Add-Content -Path $logFile -Value ""
