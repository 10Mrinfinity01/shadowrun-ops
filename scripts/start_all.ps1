# ShadowRun-Ops: start everything for the demo (Laptop A).
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
Write-Host "Starting the shop and its shadow copy in Docker..."
docker compose -f shop\docker-compose.yml up -d
docker compose -f shop\docker-compose.shadow.yml up -d

Write-Host "Opening the agent, dashboard and fake-customer windows..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root'; `$host.UI.RawUI.WindowTitle='1 AGENT'; .\venv\Scripts\python.exe -m uvicorn agent.main:app --host 0.0.0.0 --port 8000"
Start-Sleep -Seconds 4
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\dashboard'; `$host.UI.RawUI.WindowTitle='2 DASHBOARD'; npm run dev -- --host"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root'; `$host.UI.RawUI.WindowTitle='3 FAKE CUSTOMERS'; .\venv\Scripts\python.exe traffic\traffic.py"
Start-Sleep -Seconds 8
Start-Process "http://localhost:5173"
Write-Host ""
Write-Host "Done. Use THIS window for the injector, e.g.:"
Write-Host "  .\venv\Scripts\python.exe injector\inject.py bad_deploy"
Write-Host "Check everything with:  powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1"
