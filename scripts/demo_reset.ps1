# ShadowRun-Ops: put everything back to a clean, healthy state between demos (Laptop A).
#   powershell -ExecutionPolicy Bypass -File scripts\demo_reset.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\demo_reset.ps1 -ClearHistory   (then restart the AGENT window)
#   powershell -ExecutionPolicy Bypass -File scripts\demo_reset.ps1 -ClearMemory    (the agent forgets solved problems,
#                                                                                    so the next one uses the AI again)
param([switch]$ClearHistory, [switch]$ClearMemory)
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "Resetting releases/configs and starting all shop containers..."
.\venv\Scripts\python.exe injector\inject.py reset
docker compose -f shop\docker-compose.shadow.yml up -d | Out-Null

# Undo a forgotten "show the Approve buttons" tweak.
$policy = "agent\safety\policy.yaml"
$text = Get-Content $policy -Raw
if ($text -notmatch "min_confidence_for_auto: 0\.7\s") {
    $text = $text -replace "min_confidence_for_auto: [0-9.]+", "min_confidence_for_auto: 0.7"
    Set-Content -Path $policy -Value $text -NoNewline
    Write-Host "Policy threshold put back to 0.7 (restart the AGENT window to apply)."
}

if ($ClearHistory) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    if (Test-Path data\history.json) { Move-Item data\history.json "data\history-$stamp.json" }
    Write-Host "History archived to data\history-$stamp.json. Restart the AGENT window for a clean dashboard."
}

if ($ClearMemory) {
    try {
        Invoke-RestMethod -Method Post -Uri "http://localhost:8000/memory/clear" -TimeoutSec 3 | Out-Null
        Write-Host "Incident memory cleared: the next problem will be investigated by the AI again."
    } catch {
        if (Test-Path data\memory.json) { Remove-Item data\memory.json }
        Write-Host "Incident memory file removed (agent not running)."
    }
}

Write-Host "Waiting for the shop to be healthy..."
for ($i = 0; $i -lt 40; $i++) {
    try {
        $s = Invoke-RestMethod -Uri "http://localhost:8000/state" -TimeoutSec 3
        $bad = @($s.services.PSObject.Properties | Where-Object { $_.Value.health -ne "ok" })
        if ($bad.Count -eq 0 -and $s.probe.result -eq "ok" -and -not $s.active) {
            Write-Host "[OK] All green. Wait about 60 s after the last fix before the next scenario (cooldown rule)."
            exit 0
        }
    } catch {}
    Start-Sleep -Seconds 3
}
Write-Host "[!] Not healthy after 2 minutes. Run preflight.ps1 to see what is wrong."
