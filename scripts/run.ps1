# NETVISTA one-command launcher for Windows (WSL2).
#   powershell -ExecutionPolicy Bypass -File scripts\run.ps1            # build UI if needed, start, open browser
#   powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Rebuild   # force a fresh UI build
#   powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Topology topologies\small.json
# Stop with Ctrl+C. The backend runs as root inside WSL because Mininet needs it.
# Copilot: put GEMINI_API_KEY=... (and GROQ_API_KEY=... as backup) or ANTHROPIC_API_KEY=... in .env
# at the repo root (see .env.example), or run Ollama.
param(
    [switch]$Rebuild,
    [switch]$NoBrowser,
    [string]$Distro = "Ubuntu-24.04",
    [string]$Topology = "",
    [int]$Port = 8000
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# 1. WSL distro present?
$distros = (wsl.exe --list --quiet) -replace "`0", "" | Where-Object { $_.Trim() -ne "" } | ForEach-Object { $_.Trim() }
if (-not ($distros -contains $Distro)) {
    Write-Host "WSL distribution '$Distro' is not installed. Install it once with:" -ForegroundColor Yellow
    Write-Host "    wsl --install -d $Distro"
    Write-Host "then run this script again (first start installs Mininet, Open vSwitch and iperf3 inside it)."
    exit 1
}

# 2. Build the UI (Node 18+ on Windows) if needed
$dist = Join-Path $Root "frontend\dist\index.html"
if ($Rebuild -or -not (Test-Path $dist)) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Host "npm not found. Install Node.js 18+ from https://nodejs.org and re-run." -ForegroundColor Yellow
        exit 1
    }
    Say "Building the dashboard (frontend/dist)"
    Push-Location (Join-Path $Root "frontend")
    try {
        if (-not (Test-Path "node_modules")) { npm ci; if ($LASTEXITCODE -ne 0) { npm install } }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "frontend build failed" }
    } finally { Pop-Location }
}

# 3. Path of the repo as WSL sees it
$wslRoot = (wsl.exe -d $Distro -e wslpath -a ($Root -replace '\\', '/')) -replace "`0", ""
$wslRoot = $wslRoot.Trim()
$extra = @()
if ($Topology -ne "") {
    $t = (wsl.exe -d $Distro -e wslpath -a ((Resolve-Path $Topology).Path -replace '\\', '/')) -replace "`0", ""
    $extra += @("--topology", $t.Trim())
}

# 4. Open the browser once the API answers (in the background)
$url = "http://127.0.0.1:$Port"
if (-not $NoBrowser) { Start-Job -ScriptBlock {
    param($u)
    for ($i = 0; $i -lt 120; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "$u/api/health"
            if ($r.StatusCode -eq 200) { Start-Process $u; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
} -ArgumentList $url | Out-Null }

# 5. Run the backend (foreground) as root inside WSL
Say "Starting NETVISTA in WSL ($Distro) on $url  - Ctrl+C to stop"
$env:NETVISTA_PORT = "$Port"
# forward the copilot settings if they are set on the Windows side (a repo-root .env works too)
$fwd = @("NETVISTA_PORT")
foreach ($v in @("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "NETVISTA_AI_PROVIDER", "NETVISTA_AI_BACKUP", "NETVISTA_AI_MODEL",
                  "NETVISTA_GEMINI_MODEL", "NETVISTA_GROQ_MODEL", "NETVISTA_OLLAMA_URL", "NETVISTA_OLLAMA_NUM_CTX")) {
    if ([Environment]::GetEnvironmentVariable($v)) { $fwd += $v }
}
$env:WSLENV = ($fwd | ForEach-Object { "$_/u" }) -join ":"
wsl.exe -d $Distro -u root -- bash "$wslRoot/scripts/run.sh" @extra
Say "Stopping and cleaning up Mininet"
wsl.exe -d $Distro -u root -- bash "$wslRoot/scripts/stop.sh" | Out-Null
