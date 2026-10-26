# Windows equivalent of the Makefile (no `make` on Windows).
# Usage, from the project folder in PowerShell:
#   .\scripts\windows\dev.ps1 setup    # first time: database container, Python venv, npm packages, migrate, seed
#   .\scripts\windows\dev.ps1 start    # database + API (http://127.0.0.1:8000) + web app (http://localhost:5173)
#   .\scripts\windows\dev.ps1 stop     # stop API and web app (the database container keeps running)
#   .\scripts\windows\dev.ps1 status
#   .\scripts\windows\dev.ps1 test     # backend tests (uses the skincare_test database)
# If PowerShell refuses to run scripts:  powershell -ExecutionPolicy Bypass -File .\scripts\windows\dev.ps1 start
param([ValidateSet("setup", "start", "stop", "status", "test", "db", "migrate")][string]$cmd = "start")

$ErrorActionPreference = "Continue"  # native tools (alembic, uvicorn) log to stderr
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$py = Join-Path $backend ".venv-win\Scripts\python.exe"
$logs = Join-Path $root "var\logs"
New-Item -ItemType Directory -Force $logs | Out-Null

function Step($n, $msg) { Write-Host "[$n] $msg" -ForegroundColor Cyan }

function Db {
    Step "db" "PostgreSQL 16 in Docker on 127.0.0.1:5433 (container skincare-db)"
    $exists = docker ps -a --filter "name=^skincare-db$" --format "{{.Names}}"
    if (-not $exists) {
        docker run -d --name skincare-db -p 127.0.0.1:5433:5432 -e POSTGRES_USER=skincare -e POSTGRES_PASSWORD=skincare `
            -e POSTGRES_DB=skincare -v skincare-pgdata:/var/lib/postgresql/data --restart unless-stopped postgres:16-alpine | Out-Null
    } else {
        docker start skincare-db | Out-Null
    }
    for ($i = 0; $i -lt 30; $i++) {
        docker exec skincare-db pg_isready -U skincare -d skincare *> $null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep 1
    }
    $has = docker exec skincare-db psql -U skincare -d skincare -tAc "SELECT 1 FROM pg_database WHERE datname='skincare_test'"
    if ("$has".Trim() -ne "1") { docker exec skincare-db psql -U skincare -d skincare -qc "CREATE DATABASE skincare_test" | Out-Null }
    Write-Host "    database ready"
}

function Migrate {
    Step "migrate" "alembic upgrade head + seed (catalogue, rooms, machines, demo users, doctor hours)"
    Push-Location $backend
    try {
        & $py -m alembic upgrade head
        & $py -m app.seed --demo-users
    } finally { Pop-Location }
}

function PortPid($port) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.OwningProcess } else { return $null }
}

switch ($cmd) {
    "setup" {
        Db
        Step "1/3" "Python venv (backend\.venv-win) and packages"
        if (-not (Test-Path $py)) { python -m venv (Join-Path $backend ".venv-win") }
        & $py -m pip install --upgrade pip
        Push-Location $backend; & $py -m pip install -e ".[dev]"; Pop-Location
        Step "2/3" "npm packages"
        Push-Location $frontend; npm ci --no-audit --no-fund; Pop-Location
        Step "3/3" "database schema and seed data"
        Migrate
        Write-Host "Setup done. Run: .\scripts\windows\dev.ps1 start" -ForegroundColor Green
    }
    "db" { Db }
    "migrate" { Db; Migrate }
    "start" {
        Db
        if (PortPid 8000) { Write-Host "[api] already running on :8000" } else {
            Step "api" "FastAPI on http://127.0.0.1:8000 (log: var\logs\api.log)"
            Start-Process -WindowStyle Hidden -WorkingDirectory $backend -FilePath $py `
                -ArgumentList "-m", "uvicorn", "app.main:app", "--reload", "--host", "127.0.0.1", "--port", "8000" `
                -RedirectStandardOutput (Join-Path $logs "api.out.log") -RedirectStandardError (Join-Path $logs "api.log")
        }
        if (PortPid 5173) { Write-Host "[web] already running on :5173" } else {
            Step "web" "Vite on http://localhost:5173 (log: var\logs\web.log)"
            Start-Process -WindowStyle Hidden -WorkingDirectory $frontend -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev" `
                -RedirectStandardOutput (Join-Path $logs "web.log") -RedirectStandardError (Join-Path $logs "web.err.log")
        }
        for ($i = 0; $i -lt 40; $i++) {
            if ((PortPid 8000) -and (PortPid 5173)) { break }
            Start-Sleep 1
        }
        try { $h = Invoke-RestMethod http://127.0.0.1:8000/api/health -TimeoutSec 5; Write-Host "    API health: $($h.status), AI: $($h.llm)" } catch { Write-Host "    API not answering yet - see var\logs\api.log" -ForegroundColor Yellow }
        Write-Host "Open http://localhost:5173  (demo password Demo@12345: operator@ / doctor@ / admin@clinic.local; OTP 123456)" -ForegroundColor Green
    }
    "stop" {
        # uvicorn --reload serves from a child process that keeps the port open if only the parent is stopped, so stop
        # whole process trees: the reloader, its workers, and whatever still listens on the ports.
        $uv = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*uvicorn*app.main:app*" })
        foreach ($u in $uv) { taskkill /PID $u.ProcessId /T /F *> $null }
        foreach ($p in 8000, 5173) {
            $id = PortPid $p
            if ($id -and (Get-Process -Id $id -ErrorAction SilentlyContinue)) { taskkill /PID $id /T /F *> $null }
            elseif ($id) {
                # The listed owner is gone: the socket is held by a worker it spawned (spawn_main(parent_pid=<owner>)).
                Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*spawn_main(parent_pid=$id,*" } |
                    ForEach-Object { taskkill /PID $_.ProcessId /T /F *> $null }
            }
            Start-Sleep -Milliseconds 300
            if (PortPid $p) { Write-Host ":$p still in use" -ForegroundColor Yellow } else { Write-Host "stopped :$p" }
        }
        Write-Host "The database container keeps running (docker stop skincare-db to stop it)."
    }
    "status" {
        foreach ($p in 5433, 8000, 5173) { $id = PortPid $p; Write-Host (":$p " + $(if ($id) { "running (pid $id)" } else { "stopped" })) }
    }
    "test" {
        Db
        Push-Location $backend
        try { & $py -m pytest -q -p no:cacheprovider } finally { Pop-Location }
    }
}
