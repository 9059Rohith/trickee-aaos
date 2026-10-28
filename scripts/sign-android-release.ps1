param(
    [string]$UnsignedApk = (Join-Path $PSScriptRoot '../dist/trickee-aaos-release-unsigned.apk'),
    [string]$SignedApk = (Join-Path $PSScriptRoot '../dist/trickee-aaos-release-signed.apk')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

foreach ($name in @('TRICKEE_SIGNING_KEYSTORE', 'TRICKEE_SIGNING_ALIAS', 'TRICKEE_SIGNING_STORE_PASSWORD', 'TRICKEE_SIGNING_KEY_PASSWORD')) {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        throw "$name must be supplied through the environment"
    }
}

$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { Join-Path $env:LOCALAPPDATA 'Android/Sdk' }
$toolsVersion = if ($env:TRICKEE_ANDROID_BUILD_TOOLS) { $env:TRICKEE_ANDROID_BUILD_TOOLS } else { '36.0.0' }
$buildTools = Join-Path $sdk "build-tools/$toolsVersion"
$zipalign = Join-Path $buildTools 'zipalign.exe'
$apksigner = Join-Path $buildTools 'apksigner.bat'
foreach ($tool in @($zipalign, $apksigner)) {
    if (-not (Test-Path -LiteralPath $tool)) { throw "Android build tool missing: $tool" }
}

$source = (Resolve-Path -LiteralPath $UnsignedApk).Path
$keystore = (Resolve-Path -LiteralPath $env:TRICKEE_SIGNING_KEYSTORE).Path
$destination = [System.IO.Path]::GetFullPath($SignedApk)
if ($source -eq $destination) { throw 'signed output must differ from unsigned input' }
$parent = Split-Path -Parent $destination
New-Item -ItemType Directory -Path $parent -Force | Out-Null
$aligned = Join-Path $parent ([System.IO.Path]::GetRandomFileName() + '.apk')

try {
    & $zipalign -p -f 4 $source $aligned
    if ($LASTEXITCODE -ne 0) { throw 'zipalign failed' }

    & $apksigner sign --ks $keystore --ks-key-alias $env:TRICKEE_SIGNING_ALIAS `
        --ks-pass env:TRICKEE_SIGNING_STORE_PASSWORD `
        --key-pass env:TRICKEE_SIGNING_KEY_PASSWORD `
        --out $destination $aligned
    if ($LASTEXITCODE -ne 0) { throw 'APK signing failed' }

    & $apksigner verify --verbose --print-certs $destination
    if ($LASTEXITCODE -ne 0) { throw 'APK signature verification failed' }
    $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $destination).Hash.ToLowerInvariant()
    Write-Output "Signed APK: $destination"
    Write-Output "SHA-256: $digest"
} finally {
    Remove-Item -LiteralPath $aligned -ErrorAction SilentlyContinue
}
