# Installs the Hornets dashboard as a Windows service (NSSM) that runs under a domain service account and
# talks to SQL Server with Windows authentication. Run once from an ELEVATED PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\install_service.ps1
# You are asked for the service account's password; NSSM stores it in the service configuration.
# To remove the service later:
#   powershell -ExecutionPolicy Bypass -File .\install_service.ps1 -Uninstall
param(
    [switch]$Uninstall,
    [string]$ServiceName = "HornetsDashboard",
    [int]$Port = 8080,
    [string]$Account = "DAVIDSIDOL\pyapp",
    [string]$SqlServer = "IDOLAPP01\IDOLMSSQL",
    [string]$Database = "HornetsDB",
    [string]$Nssm = "C:\Tools\nssm\nssm.exe"
)

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Error "Run this from an elevated (Run as administrator) PowerShell."; exit 1 }
if (-not (Test-Path $Nssm)) { Write-Error "nssm.exe not found at $Nssm"; exit 1 }

if ($Uninstall) {
    & $Nssm stop $ServiceName | Out-Null
    & $Nssm remove $ServiceName confirm
    exit 0
}

$Dir = $PSScriptRoot
$Python = Join-Path $Dir "venv\Scripts\python.exe"
$Logs = Join-Path $Dir "logs"
if (-not (Test-Path $Python)) { Write-Error "No venv at $Python. Create it first (see README)."; exit 1 }
New-Item -ItemType Directory -Force $Logs | Out-Null

# The service account reads the app (and the Python the venv points at) and writes only its logs.
$base = (Get-Content (Join-Path $Dir "venv\pyvenv.cfg") | Where-Object { $_ -match '^home\s*=' }) -replace '^home\s*=\s*', ''
foreach ($p in @($Dir, $base)) { icacls $p /grant "${Account}:(OI)(CI)RX" /T /C /Q | Out-Null }
icacls $Logs /grant "${Account}:(OI)(CI)M" /T /C /Q | Out-Null

# Database user for the service account: read, write and create its own tables.
sqlcmd -S $SqlServer -E -C -b -d $Database -Q @"
IF USER_ID(N'$Account') IS NULL CREATE USER [$Account] FOR LOGIN [$Account];
ALTER ROLE db_datareader ADD MEMBER [$Account];
ALTER ROLE db_datawriter ADD MEMBER [$Account];
ALTER ROLE db_ddladmin ADD MEMBER [$Account];
"@
if ($LASTEXITCODE -ne 0) { Write-Error "Could not grant $Account access to $SqlServer/$Database."; exit 1 }

$cred = Get-Credential -UserName $Account -Message "Password for the $ServiceName service account"
if (-not $cred) { exit 1 }

& $Nssm install $ServiceName $Python serve.py
& $Nssm set $ServiceName AppDirectory $Dir
& $Nssm set $ServiceName DisplayName "Charlotte Hornets Dashboard"
& $Nssm set $ServiceName Description "Charlotte Hornets ML dashboard (Waitress) on port $Port, data in $SqlServer/$Database"
& $Nssm set $ServiceName AppEnvironmentExtra "PORT=$Port" "DB_BACKEND=mssql" "MSSQL_SERVER=$SqlServer" `
    "MSSQL_DATABASE=$Database" "LOG_DIR=$Logs" "PYTHONUNBUFFERED=1"
& $Nssm set $ServiceName AppStdout (Join-Path $Logs "service.out.log")
& $Nssm set $ServiceName AppStderr (Join-Path $Logs "service.err.log")
& $Nssm set $ServiceName AppRotateFiles 1
& $Nssm set $ServiceName AppRotateBytes 10485760
& $Nssm set $ServiceName AppExit Default Restart
& $Nssm set $ServiceName AppRestartDelay 10000
& $Nssm set $ServiceName Start SERVICE_DELAYED_AUTO_START
$sqlService = "MSSQL`$" + ($SqlServer -split '\\')[-1]          # wait for a SQL instance on this same box
if ($SqlServer -match '\\' -and (Get-Service $sqlService -ErrorAction SilentlyContinue)) {
    & $Nssm set $ServiceName DependOnService $sqlService
}
& $Nssm set $ServiceName ObjectName $Account $cred.GetNetworkCredential().Password   # also grants Log on as a service

& $Nssm start $ServiceName
Start-Sleep -Seconds 10
try {
    $s = Invoke-RestMethod "http://localhost:$Port/api/status" -TimeoutSec 15
    Write-Host "Dashboard is up: season $($s.current_season), $($s.current_season_games) games this season. http://localhost:$Port"
} catch {
    Write-Warning "Service started but the dashboard did not answer yet. Check $Logs\hornets.log and service.err.log."
}
