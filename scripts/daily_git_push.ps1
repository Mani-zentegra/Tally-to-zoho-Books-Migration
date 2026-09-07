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
        git add -A 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8
        
        $commitMsg = "chore: automated daily backup $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
        git commit -m $commitMsg 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8
        
        Add-Content -Path $logFile -Value "[$timestamp] Pushing to origin main..."
        git push origin main 2>&1 | Out-File -FilePath $logFile -Append -Encoding utf8
        
        Add-Content -Path $logFile -Value "[$timestamp] Daily backup push completed successfully."
    } else {
        Add-Content -Path $logFile -Value "[$timestamp] No changes detected. Repository clean."
    }
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] Error during backup: $_"
}

Add-Content -Path $logFile -Value "[$timestamp] Finished Daily Git Backup"
Add-Content -Path $logFile -Value ""
