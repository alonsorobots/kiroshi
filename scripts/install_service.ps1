<#
.SYNOPSIS
  Install a Kiroshi Fixer or Runner as an auto-starting Windows service (NSSM).

.DESCRIPTION
  Thin elevation shim. The real logic lives in `kiroshi service install` (Python),
  so this script just self-elevates and forwards your arguments to it. That keeps
  one source of truth and avoids PowerShell/NSSM quoting drift.

  NAS note: a Runner that reads/writes a NAS over SMB MUST run under a real user
  account whose Credential Manager holds the NAS login (LocalSystem can't see it).
  Pass -Account '.\<user>' -Password '<pw>' for such a Runner.

.EXAMPLE
  # Fixer (LocalSystem is fine; no NAS):
  .\install_service.ps1 -Role fixer -Db C:\kiroshi\jobs.db -Port 8787

.EXAMPLE
  # Runner that hits the NAS, running as the user that holds the NAS creds:
  .\install_service.ps1 -Role runner -Task mesh.tasks.motion_dat:run `
     -Fixer auto -Workers 8 -Account '.\me' -Password '<password>' `
     -ReadRoot '\\nas\share_direct' -WriteRoot '\\nas\share' -Token <mesh-token>
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][ValidateSet("fixer","runner")][string]$Role,
  [string]$Name,
  [string]$Python,
  [string]$Account,
  [string]$Password,
  [switch]$Force,
  # fixer
  [string]$Db = "kiroshi.db",
  [string]$BindHost = "0.0.0.0",
  [int]$Port = 8787,
  [string]$PagesDir,
  # runner
  [string]$Fixer = "auto",
  [string]$Task,
  [int]$Workers = 0,
  [string[]]$SysPath,
  [string]$ReadRoot,
  [string]$WriteRoot,
  # shared
  [string]$Token,
  [string[]]$Env
)

$ErrorActionPreference = "Stop"

# --- self-elevate if not running as Administrator ---
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
  Write-Host "Re-launching elevated..."
  $argLine = $MyInvocation.UnboundArguments -join ' '
  $bound = ($PSBoundParameters.GetEnumerator() | ForEach-Object {
    if ($_.Value -is [switch]) { if ($_.Value) { "-$($_.Key)" } }
    elseif ($_.Value -is [array]) { $_.Value | ForEach-Object { "-$($_.Key)", "`"$_`"" } }
    else { "-$($_.Key)", "`"$($_.Value)`"" }
  }) -join ' '
  Start-Process powershell -Verb RunAs -ArgumentList @(
    "-NoProfile","-ExecutionPolicy","Bypass","-File","`"$PSCommandPath`"",$bound)
  return
}

# --- resolve python (prefer a sibling .venv) ---
if (-not $Python) {
  $repo = Split-Path -Parent $PSScriptRoot
  $venv = Join-Path $repo ".venv\Scripts\python.exe"
  if (Test-Path $venv) { $Python = $venv } else { $Python = "python" }
}

# --- build `kiroshi service install` argument list ---
$svcArgs = @("-m","kiroshi","service","install","--role",$Role,"--python",$Python)
if ($Name)      { $svcArgs += @("--name",$Name) }
if ($Account)   { $svcArgs += @("--account",$Account) }
if ($Password)  { $svcArgs += @("--password",$Password) }
if ($Force)     { $svcArgs += @("--force") }
if ($Token)     { $svcArgs += @("--token",$Token) }
if ($Env)       { foreach ($e in $Env) { $svcArgs += @("--env",$e) } }

if ($Role -eq "fixer") {
  $svcArgs += @("--db",$Db,"--host",$BindHost,"--port",$Port)
  if ($PagesDir) { $svcArgs += @("--pages-dir",$PagesDir) }
} else {
  if (-not $Task) { throw "Runner install requires -Task module:function" }
  $svcArgs += @("--fixer",$Fixer,"--task",$Task)
  if ($Workers -gt 0) { $svcArgs += @("--workers",$Workers) }
  foreach ($sp in $SysPath) { $svcArgs += @("--syspath",$sp) }
  if ($ReadRoot)  { $svcArgs += @("--read-root",$ReadRoot) }
  if ($WriteRoot) { $svcArgs += @("--write-root",$WriteRoot) }
}

Write-Host "Running: $Python $($svcArgs -join ' ')"
& $Python @svcArgs
exit $LASTEXITCODE
