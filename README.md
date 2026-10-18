# Mycelium (Network Share)

Share folders between PCs on your local network. Each PC running the app shows up in the others' list; click one to browse its shared folders and download files or whole folders.

## Run

- **Windows:** run `NetworkShare.exe` from the latest release. Allow it through the firewall on **Private** networks.
- **Linux:** `sudo apt install python3-tk`, then `python3 share.py`. Open ports 8765/tcp and 8766/udp if a firewall is on.

Anyone on your network running the app can download from your shared folders. There is no password, so use it on trusted networks only.

## Updates

The app checks the public releases repo at startup and every 6 hours, and offers to download and restart into a newer version.

To publish one: bump `VERSION` in `share.py`, then run `.\release.ps1`.

## Develop

`py share.py` runs it from source. `py test_share.py` runs the tests. The updater stays off when running from this git checkout, so it never overwrites your working copy.
