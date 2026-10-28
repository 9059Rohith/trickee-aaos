param(
    [switch]$Offline
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$androidRoot = Join-Path $projectRoot 'android-app'
$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else {
    Join-Path $env:LOCALAPPDATA 'Android\Sdk'
}
if (-not (Test-Path -LiteralPath (Join-Path $sdk 'platforms'))) {
    throw "Android SDK not found. Set ANDROID_HOME to the installed SDK."
}

# Some Windows JDKs cannot start Gradle's daemon when java.io.tmpdir uses a
# long or 8.3 temp path. Keep the workaround local to this build process.
$scratch = Join-Path $env:SystemDrive 'trickee-gradle-tmp'
New-Item -ItemType Directory -Force -Path $scratch | Out-Null
$env:ANDROID_HOME = $sdk
$env:TEMP = $scratch
$env:TMP = $scratch
$env:JAVA_TOOL_OPTIONS = (($env:JAVA_TOOL_OPTIONS + " -Djava.io.tmpdir=$scratch").Trim())

$tasks = @()
foreach ($module in @('app', 'driver', 'navigation')) {
    $tasks += ":${module}:testDebugUnitTest"
    $tasks += ":${module}:lintDebug"
    $tasks += ":${module}:assembleDebug"
    $tasks += ":${module}:lintRelease"
    $tasks += ":${module}:assembleRelease"
}
$options = @('--no-daemon', '--max-workers=1')
if ($Offline) { $options += '--offline' }
Push-Location $androidRoot
try {
    & (Join-Path $androidRoot 'gradlew.bat') @tasks @options
    if ($LASTEXITCODE -ne 0) { throw "Android verification failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
