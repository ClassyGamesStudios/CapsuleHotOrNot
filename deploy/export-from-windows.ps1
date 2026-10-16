# Export the dev database + uploaded media so they can be restored on Linux.
#
#   powershell -File deploy\export-from-windows.ps1
#
# Writes to .\chon-export\ :  game_profiles_db.sql  and  uploads.tar.gz
# Both are gitignored. Re-run whenever you want to refresh the copy.
param(
    [string]$DbUser = 'root',
    [string]$DbPassword = 'root',
    [string]$DbName = 'game_profiles_db',
    [string]$OutDir = (Join-Path $PSScriptRoot '..\chon-export')
)

$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..')

$mysqldump = Get-ChildItem 'C:\Program Files\MySQL\*\bin\mysqldump.exe' -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $mysqldump) { throw 'mysqldump.exe not found under C:\Program Files\MySQL' }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
# Keep this a plain string: the [string] parameter type strips PathInfo members.
$OutDir = (Get-Item $OutDir).FullName
$sql = Join-Path $OutDir 'game_profiles_db.sql'

Write-Host "Dumping $DbName ..."
# --set-gtid-purged=OFF keeps the dump importable into a fresh server.
& $mysqldump "--user=$DbUser" "--password=$DbPassword" --host=127.0.0.1 `
    --single-transaction --routines --events --default-character-set=utf8mb4 `
    --set-gtid-purged=OFF --databases $DbName --result-file="$sql"
if ($LASTEXITCODE -ne 0) { throw "mysqldump failed ($LASTEXITCODE)" }
Write-Host ("  {0} ({1:N1} MB)" -f $sql, ((Get-Item $sql).Length / 1MB))

$uploads = Join-Path $root 'static\uploads'
$tar = Join-Path $OutDir 'uploads.tar.gz'
if (Test-Path $uploads) {
    $count = (Get-ChildItem $uploads -File).Count
    Write-Host "Archiving $count uploaded files (this takes a minute) ..."
    # -C static so the archive contains uploads/... and restores in place.
    tar -czf "$tar" -C (Join-Path $root 'static') 'uploads'
    if ($LASTEXITCODE -ne 0) { throw "tar failed ($LASTEXITCODE)" }
    Write-Host ("  {0} ({1:N1} MB)" -f $tar, ((Get-Item $tar).Length / 1MB))
} else {
    Write-Warning 'static\uploads not found — skipping media archive'
}

$wslPath = '/mnt/' + $OutDir.Substring(0, 1).ToLower() + $OutDir.Substring(2).Replace('\', '/')
Write-Host ''
Write-Host 'Export complete. In Ubuntu run:' -ForegroundColor Cyan
Write-Host "  bash deploy/import-data.sh '$wslPath'"
