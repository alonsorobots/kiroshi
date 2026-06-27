<#
.SYNOPSIS
  Remove a Kiroshi Windows service. Thin elevation shim over `kiroshi service uninstall`.

.EXAMPLE
  .\uninstall_service.ps1 -Role fixer
  .\uninstall_service.ps1 -Name kiroshi-runner
#>
[CmdletBinding()]
param(
  [ValidateSet("fixer","runner")][string]$Role = "fixer",
  [string]$Name,
  [string]$Python
)

$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
  Write-Host "Re-launching elevated..."
  $bound = ($PSBoundParameters.GetEnumerator() | ForEach-Object {
    "-$($_.Key)", "`"$($_.Value)`""
  }) -join ' '
  Start-Process powershell -Verb RunAs -ArgumentList @(
    "-NoProfile","-ExecutionPolicy","Bypass","-File","`"$PSCommandPath`"",$bound)
  return
}

if (-not $Python) {
  $repo = Split-Path -Parent $PSScriptRoot
  $venv = Join-Path $repo ".venv\Scripts\python.exe"
  if (Test-Path $venv) { $Python = $venv } else { $Python = "python" }
}

$svcArgs = @("-m","kiroshi","service","uninstall","--role",$Role)
if ($Name) { $svcArgs += @("--name",$Name) }

Write-Host "Running: $Python $($svcArgs -join ' ')"
& $Python @svcArgs
exit $LASTEXITCODE
