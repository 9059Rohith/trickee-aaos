param([string]$Tag = 'android-17.0.0_r1')
$ErrorActionPreference = 'Stop'
$root = Join-Path $PSScriptRoot 'upstream'
New-Item -ItemType Directory -Force $root | Out-Null
$repos = @{
  manifest = 'platform/manifest'
  Car = 'platform/packages/services/Car'
  'hardware-interfaces' = 'platform/hardware/interfaces'
}
foreach ($name in $repos.Keys) {
  $destination = Join-Path $root $name
  if (Test-Path $destination) { continue }
  if ($name -eq 'hardware-interfaces') {
    git clone --no-checkout --filter=blob:none --depth 1 --branch $Tag "https://android.googlesource.com/$($repos[$name])" $destination
    if ($LASTEXITCODE -ne 0) { throw "Failed to clone $name" }
    git -C $destination config core.longpaths true
    git -C $destination sparse-checkout init --cone
    git -C $destination sparse-checkout set automotive/vehicle
    git -C $destination checkout --force $Tag
  } else {
    git clone --filter=blob:none --depth 1 --branch $Tag "https://android.googlesource.com/$($repos[$name])" $destination
  }
  if ($LASTEXITCODE -ne 0) { throw "Failed to clone $name" }
}
