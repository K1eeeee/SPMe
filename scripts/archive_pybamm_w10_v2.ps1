[CmdletBinding()]
param(
    [switch]$Execute,
    [string]$Workspace = 'E:\SPMe'
)

$ErrorActionPreference = 'Stop'
$workspaceRoot = (Resolve-Path -LiteralPath $Workspace).Path
$outputsRoot = Join-Path $workspaceRoot 'outputs'
$source = Join-Path $outputsRoot 'pybamm_spme\w10-soh-comparison-v1'
$target = Join-Path $outputsRoot 'archive\v2\w10-soh-comparison-v1'
$auditPath = Join-Path $outputsRoot 'archive\v2_to_v3_migration.json'

function Assert-WithinOutputs([string]$Path, [string]$Label) {
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetFullPath($outputsRoot)
    if (-not $full.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label is outside the workspace outputs directory: $full"
    }
}

function Get-Inventory([string]$Root) {
    @(Get-ChildItem -LiteralPath $Root -File -Recurse | Sort-Object FullName | ForEach-Object {
        [pscustomobject]@{
            relative_path = $_.FullName.Substring($Root.Length).TrimStart('\').Replace('\', '/')
            bytes = $_.Length
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
}

Assert-WithinOutputs $source 'source'
Assert-WithinOutputs $target 'target'
if (-not (Test-Path -LiteralPath $source -PathType Container)) { throw "v2 source does not exist: $source" }
if (Test-Path -LiteralPath $target) { throw "archive target already exists: $target" }
$lockPath = Join-Path $source '.run.lock'
if (Test-Path -LiteralPath $lockPath) {
    try {
        $lock = Get-Content -Raw -LiteralPath $lockPath | ConvertFrom-Json
    } catch {
        throw 'v2 source lock metadata is unreadable; archive is blocked'
    }
    if ($null -eq $lock.released_at_utc) { throw 'v2 source run lock is still held; archive is blocked' }
}

$inventory = Get-Inventory $source
$plan = [ordered]@{
    action = 'archive_v2_to_v3'
    execute = [bool]$Execute
    source = $source
    target = $target
    source_file_count = $inventory.Count
    source_inventory_sha256 = ((ConvertTo-Json $inventory -Compress | %{ [Text.Encoding]::UTF8.GetBytes($_) } | %{ [Security.Cryptography.SHA256]::HashData($_) } | %{ [Convert]::ToHexString($_).ToLowerInvariant() }))
}
if (-not $Execute) {
    $plan | ConvertTo-Json -Depth 4
    exit 0
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
Move-Item -LiteralPath $source -Destination $target -ErrorAction Stop
$archived = Get-Inventory $target
if ((ConvertTo-Json $inventory -Compress) -ne (ConvertTo-Json $archived -Compress)) {
    throw 'archive verification failed: file inventory differs after move'
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $auditPath) | Out-Null
@{
    migration_schema_version = 1
    archived_at_utc = [DateTime]::UtcNow.ToString('o')
    source = $source
    target = $target
    old_output_schema_version = 2
    reason = 'schema_3 charge-efficiency output requires a clean v3 run; v2 was not converted'
    file_count = $archived.Count
    inventory = $archived
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $auditPath -Encoding UTF8
"Archived $($archived.Count) v2 files to $target"
