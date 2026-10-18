# Publish a new version: bump VERSION in mycelium.py, then run  .\release.ps1
# Builds the exe and posts it (plus mycelium.py for Linux) as a release in the public updates repo,
# which every running copy checks for updates.
$ErrorActionPreference = 'Stop'
$gh = 'C:\Program Files\GitHub CLI\gh.exe'
$version = [regex]::Match((Get-Content mycelium.py -Raw), 'VERSION = "([\d.]+)"').Groups[1].Value
$repo = [regex]::Match((Get-Content mycelium.py -Raw), 'UPDATE_REPO = "([^"]+)"').Groups[1].Value
if (-not $version) { throw 'VERSION not found in mycelium.py' }

py test_mycelium.py
if ($LASTEXITCODE) { throw 'tests failed' }
py -m PyInstaller --noconfirm --onefile --windowed --name Mycelium mycelium.py
if ($LASTEXITCODE) { throw 'build failed' }
# shortcut: v1.0.0 copies look for the old names, drop these two once every PC is on 1.0.1 or later
Copy-Item dist\Mycelium.exe dist\NetworkShare.exe
Copy-Item mycelium.py dist\share.py

git add -u  # tracked files only, so stray local files never get published
git commit -m "Release v$version"
git tag -a "v$version" -m "v$version"  # annotated, so --follow-tags pushes it
git push --follow-tags
& $gh release create "v$version" dist\Mycelium.exe mycelium.py dist\NetworkShare.exe dist\share.py --repo $repo --title "v$version" --notes "Mycelium $version"
