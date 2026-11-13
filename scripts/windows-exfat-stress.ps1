<#
.SYNOPSIS
The Windows stress test on an exFAT volume, which has no POSIX delete semantics (plan 25, step 4).

.DESCRIPTION
Creates a VHDX, formats it exFAT, runs tests.unit.windows_stress with its root on it, then detaches
and deletes the VHDX. diskpart needs an elevated PowerShell. Run from the repo root:

    pwsh -NoProfile -File scripts\windows-exfat-stress.ps1 [-Seconds 600] [-SizeGB 8] [-Python venv]

Exits with the stress test's code (0 passed, 1 failed); the summary is printed either way, and the
stress root on the volume is copied to the scratch folder the script names if it failed.
#>
param(
    [double]$Seconds = 600,
    [int]$SizeGB = 8,
    [string]$Python = 'venv'
)
$ErrorActionPreference = 'Stop'

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'diskpart needs an elevated PowerShell (Run as administrator).'
}

$work = Join-Path $env:TEMP "dco-exfat-$PID"
New-Item -ItemType Directory -Force $work | Out-Null
$vhd = Join-Path $work 'exfat.vhdx'
$label = 'DCOEXFAT'

function Invoke-DiskPart([string[]]$Lines) {
    $script = Join-Path $work 'diskpart.txt'
    Set-Content -Path $script -Value $Lines -Encoding ascii
    diskpart /s $script | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "diskpart failed ($LASTEXITCODE)" }
}

Invoke-DiskPart @(
    "create vdisk file=`"$vhd`" maximum=$($SizeGB * 1024) type=expandable",
    "select vdisk file=`"$vhd`"",
    'attach vdisk',
    'create partition primary',
    "format fs=exfat label=$label quick",
    'assign'
)
$code = 1
try {
    $volume = Get-Volume -FileSystemLabel $label
    if ($volume.FileSystemType -ne 'exFAT') { throw "the volume is $($volume.FileSystemType), not exFAT" }
    $root = "$($volume.DriveLetter):\stress"
    Write-Host "exFAT volume at $($volume.DriveLetter):, stress root $root"
    # exFAT has no hard links, so a FanoutCache must be refused with the error that says so (which
    # also checks os_error's guess at the code exFAT's CreateHardLinkW gives), and leave nothing.
    $refused = @'
import sys
from pathlib import Path
from diskcache_oxide import FanoutCache
root = Path(sys.argv[1])
try:
    FanoutCache(root)
except OSError as e:
    assert "doesn't support hard links" in str(e), e
    assert not (root / 'diskcache-oxide-fanout').exists(), 'a manifest was left behind'
    print('FanoutCache refused on exFAT, as it should be:', e)
else:
    raise SystemExit('a FanoutCache opened on exFAT, which has no hard links')
'@
    & "$Python\Scripts\python.exe" -c $refused "$($volume.DriveLetter):\fanout"
    if ($LASTEXITCODE -ne 0) { throw 'the FanoutCache check on exFAT failed' }
    & "$Python\Scripts\python.exe" -m tests.unit.windows_stress --seconds $Seconds --root $root --no-fanout
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        $kept = Join-Path $work 'stress-root'
        Copy-Item -Recurse $root $kept -ErrorAction SilentlyContinue
        Write-Host "failed: the stress root is copied to $kept"
    }
} finally {
    Invoke-DiskPart @("select vdisk file=`"$vhd`"", 'detach vdisk')
    Remove-Item $vhd -Force
}
exit $code
