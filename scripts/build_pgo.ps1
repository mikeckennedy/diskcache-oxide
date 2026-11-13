<#
.SYNOPSIS
Profile-guided release wheels for Windows (win_amd64), into dist\. The port of build_pgo.sh (plan 25, step 6).

.DESCRIPTION
For each Python (default: 3.14 3.14t 3.15 3.15t): build instrumented, train on bench.baseline's workloads plus
bench.pgo_train in a throwaway venv, merge the profiles, rebuild with them, and put the wheel in dist\. Each
wheel is then installed into a fresh venv and smoke-tested. The GIL and free-threaded builds compile different
code, so each gets its own profile. As on Unix this optimizes the Rust code only; the bundled SQLite is C,
built by MSVC as in a normal release. Child processes run at BelowNormal priority (Windows has no nice).

    pwsh -NoProfile -File scripts\build_pgo.ps1 [3.14 3.14t 3.15 3.15t]
#>
param([Parameter(ValueFromRemainingArguments)][string[]]$Pythons)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot)
if (-not $Pythons) { $Pythons = @('3.14', '3.14t', '3.15', '3.15t') }

# The whole tree runs below normal priority, and children inherit it.
(Get-Process -Id $PID).PriorityClass = 'BelowNormal'

function Step([string]$What) { Write-Host "`n==> $What" }
function Check([string]$What) { if ($LASTEXITCODE -ne 0) { throw "$What failed ($LASTEXITCODE)" } }
function Maturin { uvx --from 'maturin>=1.15,<2' maturin @args; Check 'maturin' }

$toolchain = (rustup show active-toolchain).Split(' ')[0]
rustup component add llvm-tools --toolchain $toolchain *> $null
$profdata = Get-ChildItem (Join-Path (rustc --print sysroot) 'lib\rustlib') -Recurse -Filter 'llvm-profdata.exe' |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $profdata) { throw "llvm-profdata not found in $toolchain" }

$work = Join-Path $PWD "target\pgo\Windows-$([Runtime.InteropServices.RuntimeInformation]::OSArchitecture)"
New-Item -ItemType Directory -Force dist | Out-Null

# The same checks as check.sh's smoke test, plus a round trip through each part of the API a wheel could
# break: spilled values, tags, eviction, FanoutCache, the queue; and SQLite's own mutexes (decision 1).
$smoke = @'
import sys, tempfile
import diskcache_oxide as d
assert d.sqlite_version() == d.BUNDLED_SQLITE_VERSION, "linked the system SQLite, not the bundled one"
assert issubclass(d.Timeout, TimeoutError) and issubclass(d.UnsafeValueError, TypeError)
opts = d.sqlite_compile_options()
assert "ENABLE_STAT4" not in opts and "DEFAULT_MEMSTATUS=0" in opts, f"built without .cargo/config.toml's SQLite flags: {opts}"
assert "MUTEX_W32" in opts and d._core._sqlite_mutex_status() == "native", d._core._sqlite_mutex_status()
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    c = d.Cache(tmp + "/c")
    c.set("small", 1, tag="t"); c.set("big", b"x" * 200_000)
    assert c.get("small") == 1 and c.get("big") == b"x" * 200_000
    assert c.evict("t") == 1 and "small" not in c
    c.push("job"); assert c.pull()[1] == "job"
    assert c.check() == []
    c.close()
    f = d.FanoutCache(tmp + "/f", shards=4)
    for i in range(50):
        f.set(i, b"y" * 70_000)
    assert len(f) == 50 and f.get(7) == b"y" * 70_000
    f.close()
build = "free-threaded" if not sys._is_gil_enabled() else "GIL"
print(f"smoke ok: diskcache_oxide {d.__version__} on {sys.version.split()[0]} ({build}), SQLite {d.sqlite_version()}")
'@

$built = @()
foreach ($python in $Pythons) {
    $interpreter = uv python find --managed-python --no-project --system $python; Check "uv python find $python"
    $root = Join-Path $work $python
    if (Test-Path $root) { Remove-Item -Recurse -Force $root }
    New-Item -ItemType Directory -Force "$root\profiles", "$root\wheel", "$root\dist" | Out-Null

    Step "${python}: instrumented build"
    $env:CARGO_TARGET_DIR = "$work\target-generate"
    $env:RUSTFLAGS = "-Cprofile-generate=$root\profiles"
    Maturin build --release --quiet --interpreter $interpreter --out "$root\wheel"

    Step "${python}: training"
    uv venv --quiet --managed-python --python $python "$root\venv"; Check 'uv venv'
    $venvPython = "$root\venv\Scripts\python.exe"
    $wheel = Get-ChildItem "$root\wheel\*.whl" | Select-Object -First 1 -ExpandProperty FullName
    uv pip install --quiet --python $venvPython $wheel 'diskcache==5.6.3' setproctitle pytest; Check 'uv pip install'
    $env:LLVM_PROFILE_FILE = "$root\profiles\train_%m_%p.profraw"
    & $venvPython -m bench.baseline --backend oxide --quick --out "$root\baseline.json" > $null; Check 'bench.baseline'
    & $venvPython -m bench.pgo_train; Check 'bench.pgo_train'
    Remove-Item Env:LLVM_PROFILE_FILE
    # Only the training runs: the instrumented build's build scripts and proc-macros drop default_*.profraw too.
    $profiles = Get-ChildItem "$root\profiles\train_*.profraw"
    if (-not $profiles) { throw "${python}: training wrote no profiles" }
    & $profdata merge -o "$root\merged.profdata" $profiles.FullName; Check 'llvm-profdata merge'
    Write-Host "merged $($profiles.Count) profiles"

    Step "${python}: optimized build"
    $env:CARGO_TARGET_DIR = "$work\target-use"
    $env:RUSTFLAGS = "-Cprofile-use=$root\merged.profdata -Cllvm-args=-pgo-warn-missing-function=false"
    Maturin build --release --quiet --interpreter $interpreter --out "$root\dist"
    Remove-Item Env:RUSTFLAGS, Env:CARGO_TARGET_DIR
    $new = Get-ChildItem "$root\dist\*.whl" | Select-Object -First 1
    Copy-Item $new.FullName dist\ -Force
    $new = Get-Item (Join-Path dist $new.Name)

    Step "${python}: smoke test of $($new.Name) in a fresh venv"
    uv venv --quiet --managed-python --python $python "$root\smoke"; Check 'uv venv (smoke)'
    uv pip install --quiet --python "$root\smoke\Scripts\python.exe" $new.FullName; Check 'uv pip install (smoke)'
    Push-Location $env:TEMP  # so the repo's python\ package can't shadow the installed wheel
    try { & "$root\smoke\Scripts\python.exe" -I -c $smoke; Check "smoke test ($python)" } finally { Pop-Location }
    $built += $new.Name
}

Step 'dist\'
$built | ForEach-Object { Get-Item "dist\$_" } | Format-Table Name, Length, LastWriteTime -AutoSize
