# Publish a new version: bump VERSION in share.py, then run  .\release.ps1
# Builds the exe and posts it (plus share.py for Linux) as a release in the public updates repo,
# which every running copy checks for updates.
$ErrorActionPreference = 'Stop'
$gh = 'C:\Program Files\GitHub CLI\gh.exe'
$version = [regex]::Match((Get-Content share.py -Raw), 'VERSION = "([\d.]+)"').Groups[1].Value
$repo = [regex]::Match((Get-Content share.py -Raw), 'UPDATE_REPO = "([^"]+)"').Groups[1].Value
if (-not $version) { throw 'VERSION not found in share.py' }

py test_share.py
if ($LASTEXITCODE) { throw 'tests failed' }
py -m PyInstaller --noconfirm --onefile --windowed --name NetworkShare share.py
if ($LASTEXITCODE) { throw 'build failed' }

git add -A
git commit -m "Release v$version"
git tag "v$version"
git push --follow-tags
& $gh release create "v$version" dist\NetworkShare.exe share.py --repo $repo --title "v$version" --notes "Network Share $version"
