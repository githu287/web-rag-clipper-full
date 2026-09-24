$ErrorActionPreference = "Stop"

$browserCandidates = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles(x86)\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles(x86)\Microsoft\Edge\Application\msedge.exe",
  "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe"
)
$browser = $browserCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $browser) {
  throw "Chrome or Edge was not found."
}

$fixturePath = (Resolve-Path -LiteralPath "$PSScriptRoot\extractor.browser.test.html").Path
$fixtureUrl = [System.Uri]::new($fixturePath).AbsoluteUri
$profilePath = Join-Path ([System.IO.Path]::GetTempPath()) ("web-rag-extractor-test-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $profilePath | Out-Null
try {
  $previousErrorActionPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  $output = & $browser --headless=new --disable-gpu --no-first-run --user-data-dir=$profilePath --dump-dom $fixtureUrl 2>&1 | Out-String
  $browserExitCode = $LASTEXITCODE
  $ErrorActionPreference = $previousErrorActionPreference
  if ($browserExitCode -ne 0) {
    throw "Headless browser exited with code $browserExitCode`n$output"
  }
  if ($output -notmatch "EXTRACTOR_BROWSER_TESTS_PASSED") {
    throw "Browser DOM regression tests failed:`n$output"
  }
  Write-Output "extractor browser tests passed"
} finally {
  if (Test-Path -LiteralPath $profilePath) {
    Remove-Item -LiteralPath $profilePath -Recurse -Force
  }
}
