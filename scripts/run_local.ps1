<#
.SYNOPSIS
  One entry point for running the O-RAN model adaptation framework locally on Windows.

.DESCRIPTION
  Every step runs one process at a time (the project is developed on a 16 GB, CPU-only laptop).
  Nothing here deletes files, changes system settings or runs anything elevated.

    setup      create .venv and install the project with its dev and security extras
    migrate    apply the database migrations (DATABASE_URL, default SQLite under .\data)
    newkey     make an API key and the API_KEYS entry that grants it a role (-Role, default ADMIN)
    quicktest  the fast unit tests (no worker-process pipeline runs; about 1-2 minutes)
    test       the full test suite (about 15-20 minutes on a small laptop)
    lint       ruff over src and tests
    security   bandit, pip-audit, mypy ratchet and SBOM into .\reports (scripts\security_check.py)
    api        serve the API on http://127.0.0.1:8000 (Ctrl+C stops it)
    demo       the self-contained end-to-end demo (scripts\demo.py)
    docker     build and start the full stack with Docker Compose (needs Docker)
    docker-down stop the Docker Compose stack (volumes are kept)

.EXAMPLE
  .\scripts\run_local.ps1 setup
  .\scripts\run_local.ps1 migrate
  .\scripts\run_local.ps1 api
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("setup", "migrate", "newkey", "quicktest", "test", "lint", "security", "api",
        "demo", "docker", "docker-down")]
    [string]$Command,
    [ValidateSet("ADMIN", "OPERATOR", "ML_ENGINEER", "READ_ONLY")]
    [string]$Role = "ADMIN"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"

function Require-Venv {
    if (-not (Test-Path $Py)) {
        throw "No virtual environment at .venv - run '.\scripts\run_local.ps1 setup' first."
    }
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "'$Exe $($Arguments -join ' ')' failed with exit code $LASTEXITCODE" }
}

# The fast tests: everything except the files that run whole adaptation jobs in worker processes.
$SlowTests = @(
    "tests/unit/test_phase9_orchestrator.py", "tests/unit/test_phase10_hardening.py",
    "tests/unit/test_phase14_member1.py", "tests/unit/test_phase14_stage_b.py",
    "tests/unit/test_phase14_stage_d.py", "tests/unit/test_phase15_api_decision.py",
    "tests/unit/test_phase13_versioning.py"
)

switch ($Command) {
    "setup" {
        if (-not (Test-Path $Py)) { Invoke-Checked "python" @("-m", "venv", ".venv") }
        Invoke-Checked $Py @("-m", "pip", "install", "-e", ".[dev,security]")
        if (-not (Test-Path ".env")) {
            Copy-Item ".env.example" ".env"
            Write-Host "Created .env from .env.example - review it before running the API."
        }
    }
    "migrate" { Require-Venv; Invoke-Checked $Py @("-m", "oran_adapt.cli", "db", "upgrade") }
    "newkey" {
        Require-Venv
        Invoke-Checked $Py @("-m", "oran_adapt.cli", "auth", "new-key", "--role", $Role)
        Write-Host "Put the api_keys_entry into API_KEYS in .env; keep api_key secret (it is not stored)."
    }
    "quicktest" {
        Require-Venv
        $ignore = $SlowTests | ForEach-Object { "--ignore=$_" }
        Invoke-Checked $Py (@("-m", "pytest", "-q", "-p", "no:cacheprovider", "tests") + $ignore)
    }
    "test" { Require-Venv; Invoke-Checked $Py @("-m", "pytest", "-q", "-p", "no:cacheprovider", "tests") }
    "lint" { Require-Venv; Invoke-Checked $Py @("-m", "ruff", "check", "src", "tests") }
    "security" { Require-Venv; Invoke-Checked $Py @("scripts/security_check.py") }
    "api" {
        Require-Venv
        Invoke-Checked $Py @("-m", "uvicorn", "oran_adapt.api.main:app", "--host", "127.0.0.1", "--port", "8000")
    }
    "demo" { Require-Venv; Invoke-Checked $Py @("scripts/demo.py") }
    "docker" {
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            throw "Docker is not installed - see RUN.md, section 'Docker stack'."
        }
        Invoke-Checked "docker" @("build", "-t", "oran-adapt-sandbox:latest", "-f", "docker/sandbox/Dockerfile", "docker/sandbox")
        Invoke-Checked "docker" @("compose", "up", "--build", "-d")
        Invoke-Checked "docker" @("compose", "ps")
    }
    "docker-down" { Invoke-Checked "docker" @("compose", "down") }
}
