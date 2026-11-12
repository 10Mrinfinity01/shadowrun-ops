# ShadowRun-Ops pre-flight check: run this before the judges arrive (Laptop A).
#   powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$fails = 0

function Check($name, $ok, $hint) {
    if ($ok) { Write-Host "[ OK ] $name" -ForegroundColor Green }
    else { Write-Host "[FAIL] $name  ->  $hint" -ForegroundColor Red; $script:fails++ }
}
function Http($url) {
    try { $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3; return $r.StatusCode -lt 400 } catch { return $false }
}

$dockerOk = $false
try { docker info *> $null; $dockerOk = ($LASTEXITCODE -eq 0) } catch {}
Check "Docker engine running" $dockerOk "open Docker Desktop and wait for 'Engine running'"

$names = @()
if ($dockerOk) { $names = docker ps --format "{{.Names}}" }
$needed = "demo-db","shop-cart","shop-payment","shop-storefront","docker-proxy-read","docker-proxy-exec",
          "shadow-db","shadow-cart","shadow-payment","shadow-storefront"
$missing = @($needed | Where-Object { $names -notcontains $_ })
Check "All 10 containers up" ($missing.Count -eq 0) ("missing: " + ($missing -join ", ") + "  (run start_all.ps1 or inject.py reset)")

Check "Shop        http://localhost:8080" (Http "http://localhost:8080/health") "cd shop; docker compose up -d"
Check "Shadow copy http://localhost:8090" (Http "http://localhost:8090/health") "cd shop; docker compose -f docker-compose.shadow.yml up -d"
Check "Agent       http://localhost:8000" (Http "http://localhost:8000/health") "start the AGENT window"
Check "Dashboard   http://localhost:5173" (Http "http://localhost:5173") "start the DASHBOARD window (npm run dev -- --host)"

$fresh = $false
if (Test-Path data\traffic_stats.json) {
    try { $t = Get-Content data\traffic_stats.json -Raw | ConvertFrom-Json
          $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$t.updated; $fresh = $age -lt 10 } catch {}
}
Check "Fake customers running" $fresh "start the FAKE CUSTOMERS window (traffic\traffic.py)"

Check "Ollama (offline LLM backup)" (Http "http://localhost:11434/api/tags") "start Ollama from the Start menu"
$net = $false
try { Resolve-DnsName api.groq.com -ErrorAction Stop | Out-Null; $net = $true } catch {}
Check "Internet for Groq/Gemini" $net "check the hotspot's mobile data (the demo still works via Ollama)"

$envText = if (Test-Path .env) { Get-Content .env -Raw } else { "" }
Check "GRANT_SECRET set in .env" ($envText -match "GRANT_SECRET=\S{16,}") "add a long random GRANT_SECRET to .env"
Check "Policy threshold is 0.7" ((Get-Content agent\safety\policy.yaml -Raw) -match "min_confidence_for_auto: 0\.7\s") "run demo_reset.ps1"

$tests = & .\venv\Scripts\python.exe -m unittest tests.test_phase1 tests.test_phase2 tests.test_phase2b tests.test_phase3 2>&1 | Out-String
Check "All logic tests pass" ($tests -match "\nOK") "run the tests to see which one fails"

Write-Host ""
if ($fails -eq 0) { Write-Host "READY FOR THE JUDGES." -ForegroundColor Green }
else { Write-Host "$fails problem(s) to fix first." -ForegroundColor Red }
