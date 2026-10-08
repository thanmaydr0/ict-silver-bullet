#requires -Version 5.1
[CmdletBinding()]
param([switch]$StageOnly, [switch]$PublishOnly)
$ErrorActionPreference = 'Stop'
if ($StageOnly -and $PublishOnly) { throw 'Choose StageOnly or PublishOnly.' }
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$Frontend = Join-Path $RepoRoot 'frontend'
$Candidate = Join-Path $Frontend 'dist.next'
$Current = Join-Path $Frontend 'dist'
$Previous = Join-Path $Frontend 'dist.previous'
$Pending = Join-Path $RepoRoot 'logs\frontend.build-pending'
foreach ($BuildPath in @($Candidate, $Current, $Previous)) {
    $Resolved = [IO.Path]::GetFullPath($BuildPath)
    if ([IO.Path]::GetDirectoryName($Resolved) -ne $Frontend) { throw 'Build path escaped frontend directory.' }
    if ((Test-Path -LiteralPath $Resolved) -and ((Get-Item -LiteralPath $Resolved).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Build directories must not be junctions or symlinks.'
    }
}

function Assert-Build($Path) {
    if (-not (Test-Path -LiteralPath (Join-Path $Path 'index.html')) -or
        -not (Test-Path -LiteralPath (Join-Path $Path 'assets') -PathType Container)) { throw 'Frontend build incomplete.' }
    $Manifest = Get-Content -LiteralPath (Join-Path $Path 'build.json') -Raw | ConvertFrom-Json
    if ($Manifest.schema_version -ne 1 -or -not $Manifest.build_id -or -not $Manifest.source_hash) { throw 'Invalid frontend manifest.' }
    $Index = Get-Content -LiteralPath (Join-Path $Path 'index.html') -Raw
    foreach ($Match in [regex]::Matches($Index, '(?:src|href)="(/assets/[^"?]+)"')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Path $Match.Groups[1].Value.TrimStart('/')))) { throw 'Referenced asset missing.' }
    }
    return $Manifest
}
function Remove-BuildDirectory($Path) {
    # Validate each absolute recursive target before removal. No cross-shell deletes.
    $Absolute = [IO.Path]::GetFullPath($Path)
    if ($Absolute -ne $Candidate -and $Absolute -ne $Previous) { throw 'Unsafe build cleanup target.' }
    if (Test-Path -LiteralPath $Absolute) { Remove-Item -LiteralPath $Absolute -Recurse -Force }
}
Push-Location -LiteralPath $Frontend
try {
    if (-not $PublishOnly) {
        if (-not (Get-Command node -ErrorAction SilentlyContinue) -or -not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'Install Node.js 24 LTS (includes npm), then open a new PowerShell.' }
        & node -e 'const [a,b]=process.versions.node.split(".").map(Number); if(a<22||(a===22&&b<12))process.exit(1)'
        if ($LASTEXITCODE -ne 0) { throw 'Node.js 22.12+ required; use Node.js 24 LTS.' }
        $Hash = & node scripts/write-build.mjs --source-hash
        if ($LASTEXITCODE -ne 0) { throw 'Could not hash frontend sources.' }
        $ValidCurrent = $null
        try { $ValidCurrent = Assert-Build $Current } catch { }
        if ($ValidCurrent -and $ValidCurrent.source_hash -eq $Hash -and -not (Test-Path -LiteralPath $Pending)) {
            Write-Host "Frontend unchanged: $($ValidCurrent.build_id)"
            return
        }
        New-Item -ItemType Directory -Path (Join-Path $RepoRoot 'logs') -Force | Out-Null
        Set-Content -LiteralPath $Pending -Value 'Frontend build pending' -Encoding ASCII
        & npm.cmd ci --include=dev
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed; current build preserved. Retry update.' }
        $SavedBuildDir = $env:ICT_BUILD_DIR
        try {
            $env:ICT_BUILD_DIR = 'dist.next'
            & npm.cmd run build
            if ($LASTEXITCODE -ne 0) { throw 'Vite build failed; current build preserved. Retry update.' }
        } finally { $env:ICT_BUILD_DIR = $SavedBuildDir }
        $Built = Assert-Build $Candidate
        if ($Built.source_hash -ne $Hash) { throw 'Frontend sources changed during build.' }
        if ($StageOnly) { Write-Host "Frontend staged: $($Built.build_id)"; return }
    }
    if (-not (Test-Path -LiteralPath $Candidate)) {
        Assert-Build $Current | Out-Null
        return
    }
    $Built = Assert-Build $Candidate
    $Task = Get-ScheduledTask -TaskName 'ICT-Dashboard' -ErrorAction SilentlyContinue
    if ($Task -and $Task.State -eq 'Running') { throw 'Stop ICT-Dashboard before publishing the staged build.' }
    # NOTE: Retain earlier hashed assets for open browser tabs. A fresh index uses
    # only the new hashes. Remove old assets only during a planned offline cleanup.
    if (Test-Path -LiteralPath (Join-Path $Current 'assets')) {
        Copy-Item -Path (Join-Path $Current 'assets\*') -Destination (Join-Path $Candidate 'assets') -Recurse -Force
    }
    # Recover an interrupted promotion before replacing the previous known build.
    if (-not (Test-Path -LiteralPath $Current) -and (Test-Path -LiteralPath $Previous)) {
        Move-Item -LiteralPath $Previous -Destination $Current
    }
    Remove-BuildDirectory $Previous
    if (Test-Path -LiteralPath $Current) { Move-Item -LiteralPath $Current -Destination $Previous }
    try { Move-Item -LiteralPath $Candidate -Destination $Current }
    catch {
        if (Test-Path -LiteralPath $Previous) { Move-Item -LiteralPath $Previous -Destination $Current }
        throw
    }
    if (Test-Path -LiteralPath $Pending) { Remove-Item -LiteralPath $Pending }
    Write-Host "Frontend published: $($Built.build_id)"
} finally { Pop-Location }
