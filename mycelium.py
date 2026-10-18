"""Mycelium: pick folders to share on your LAN, see which PCs running this app are online,
browse their shared folders and download files or whole folders.

Run: py mycelium.py
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

VERSION = "1.0.0"
UPDATE_REPO = "Wizwo86/mycelium-releases"  # public repo whose GitHub releases hold the builds
PORT = 8765
DISCOVERY_PORT = 8766
NAME = socket.gethostname()
CONFIG = Path.home() / ".netshare.json"
OFFLINE_AFTER = 10  # seconds without an announcement before a PC counts as offline
peers = {}  # computer name -> (ip, port, last_seen)
shares = {}  # share name -> folder path


# ---------- auto update from GitHub releases ----------

FROZEN = getattr(sys, "frozen", False)  # True when running as the PyInstaller exe


def this_program():
    return Path(sys.executable if FROZEN else __file__).resolve()


def parse_version(tag):
    return tuple(int(x) for x in tag.strip().lstrip("v").split("."))


def check_update():
    """Return (version, download_url, size) if the latest release is newer than this one, else None."""
    with urllib.request.urlopen(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest", timeout=10) as r:
        release = json.load(r)
    if parse_version(release["tag_name"]) <= parse_version(VERSION):
        return None
    want = ("Mycelium.exe" if os.name == "nt" else "Mycelium") if FROZEN else "mycelium.py"
    for asset in release["assets"]:
        if asset["name"] == want:
            return release["tag_name"], asset["browser_download_url"], int(asset["size"])
    return None


def install_update(url, size):
    """Replace this program with the download. Returns the command that starts the new version."""
    current = this_program()
    new = current.with_name(current.name + ".new")
    old = current.with_name(current.name + ".old")
    try:
        with urllib.request.urlopen(url, timeout=30) as r, open(new, "wb") as f:
            shutil.copyfileobj(r, f)
        if new.stat().st_size != size:
            raise OSError("update download was incomplete")
        old.unlink(missing_ok=True)
        current.rename(old)  # Windows can't overwrite a running exe, but it can rename it
        try:
            new.rename(current)
        except OSError:
            old.rename(current)  # put the working version back
            raise
    finally:
        new.unlink(missing_ok=True)
    if os.name != "nt":
        current.chmod(0o755)
    return [str(current)] if FROZEN else [sys.executable, str(current)]


# ---------- shared folders ----------

def add_share(folder):
    if folder in shares.values():
        return
    base = Path(folder).name or "Drive"  # a drive root like D:\ has no name
    name, n = base, 2
    while name in shares:
        name, n = f"{base} ({n})", n + 1
    shares[name] = folder


def load_shares():
    try:
        folders = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return
    for f in folders:
        if Path(f).is_dir():
            add_share(f)


def save_shares():
    CONFIG.write_text(json.dumps(list(shares.values())))


def safe_name(name):
    """True if a file name from another PC is a plain name, not a path that could escape."""
    return isinstance(name, str) and name not in ("", ".", "..") and Path(name).name == name and "/" not in name


def resolve(share, path):
    """Map a request to a real path inside a shared folder, or None if it's outside."""
    root = shares.get(share)
    if root is None:
        return None
    root = Path(root).resolve()
    target = (root / path).resolve()
    return target if target.is_relative_to(root) else None


# ---------- discovery: UDP broadcast "I'm here" / "bye" ----------

def local_ips():
    """This PC's IPv4 addresses, one per network adapter."""
    ips = set()
    try:
        ips |= {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        pass
    try:  # Linux often lists only 127.0.1.1 above; the default-route address catches the real one
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent, this just picks the outgoing adapter
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return {ip for ip in ips if not ip.startswith(("127.", "169.254."))}


def send_broadcast(bye=False):
    msg = json.dumps({"name": NAME, "port": PORT, "bye": bye}).encode()
    # Windows sends a broadcast out of one adapter only, so send once from each adapter's address
    for ip in local_ips() or {""}:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((ip, 0))
                s.sendto(msg, ("255.255.255.255", DISCOVERY_PORT))
        except OSError:
            pass  # that adapter is down; the others still get it


def announce():
    while True:
        send_broadcast()
        time.sleep(3)


def parse_announcement(data):
    """Return (name, port, bye) from a peer's broadcast, or None if it's junk."""
    try:
        info = json.loads(data)
        port = int(info["port"])
        if not 0 < port < 65536:
            return None
        return str(info["name"])[:64], port, bool(info.get("bye"))
    except (ValueError, KeyError, TypeError):
        return None


def listen():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", DISCOVERY_PORT))
    while True:
        data, (ip, _) = s.recvfrom(1024)
        if parsed := parse_announcement(data):
            name, port, bye = parsed
            if bye:
                peers.pop(name, None)
                continue
            # a PC with several adapters is heard from several addresses; stick with the first while it's fresh
            old = peers.get(name)
            if old and old[0] != ip and time.time() - old[2] < OFFLINE_AFTER:
                ip = old[0]
            peers[name] = (ip, port, time.time())


# ---------- server: read-only HTTP API other PCs talk to ----------

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/api/shares":
            return self.send_json(sorted(shares))
        target = resolve(q.get("share", [""])[0], q.get("path", [""])[0])
        try:
            if target and url.path == "/api/list" and target.is_dir():
                entries = []
                for p in target.iterdir():
                    try:
                        entries.append({"name": p.name, "dir": p.is_dir(), "size": p.stat().st_size})
                    except OSError:
                        pass  # unreadable entry, skip it
                return self.send_json(entries)
            if target and url.path == "/api/file" and target.is_file():
                with open(target, "rb") as f:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
                    self.end_headers()
                    shutil.copyfileobj(f, self.wfile)
                return
        except PermissionError:
            return self.send_error(403)
        self.send_error(404)

    def send_json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # windowed app, no console to log to


# ---------- client ----------

def api(peer, endpoint, **params):
    ip, port = peer
    return urllib.request.urlopen(f"http://{ip}:{port}/api/{endpoint}?" + urllib.parse.urlencode(params), timeout=5)


def get_json(peer, endpoint, **params):
    with api(peer, endpoint, **params) as r:
        return json.load(r)


def unique(path):
    n = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


class Cancelled(Exception):
    pass


def folder_size(peer, share, parts):
    total = 0
    for e in get_json(peer, "list", share=share, path="/".join(parts)):
        if safe_name(e.get("name")):
            total += folder_size(peer, share, parts + [e["name"]]) if e.get("dir") else int(e.get("size") or 0)
    return total


def download(peer, share, parts, is_dir, dest, progress=lambda n: None):
    """Copy a remote file or folder (share + path parts) into the local folder dest.
    progress(n) is called after each n bytes written."""
    target = unique(dest / (parts[-1] if parts else share))
    path = "/".join(parts)
    if is_dir:
        target.mkdir()
        for e in get_json(peer, "list", share=share, path=path):
            if safe_name(e.get("name")):
                download(peer, share, parts + [e["name"]], bool(e.get("dir")), target, progress)
        return
    tmp = target.with_name(target.name + ".part")  # never leave a half file under the real name
    try:
        with api(peer, "file", share=share, path=path) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                progress(len(chunk))
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------- window ----------

class App:
    def __init__(self, root):
        self.root = root
        root.title(f"Mycelium {VERSION} — {NAME}")
        self.declined = None  # update version the user said no to, so we don't nag
        root.geometry("900x520")
        self.peer = None  # name of the computer being browsed
        self.peer_names = []
        self.share = None  # None = showing the peer's list of shares
        self.parts = []
        self.items = {}
        self.request = 0  # ignores replies to clicks the user has since moved past

        left = ttk.Frame(root, padding=8)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="My shared folders").pack(anchor="w")
        self.my_list = tk.Listbox(left, width=40, height=8, exportselection=False)
        self.my_list.pack(fill="x")
        row = ttk.Frame(left)
        row.pack(fill="x", pady=4)
        ttk.Button(row, text="Add folder…", command=self.add_folder).pack(side="left")
        ttk.Button(row, text="Remove", command=self.remove_folder).pack(side="left", padx=4)
        ttk.Label(left, text="Computers online").pack(anchor="w", pady=(12, 0))
        self.peer_list = tk.Listbox(left, width=40, exportselection=False)
        self.peer_list.pack(fill="both", expand=True)
        self.peer_list.bind("<<ListboxSelect>>", self.open_peer)

        right = ttk.Frame(root, padding=8)
        right.pack(side="left", fill="both", expand=True)
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        ttk.Button(bar, text="⬆ Up", command=self.up).pack(side="left")
        ttk.Button(bar, text="Download…", command=self.download_selected).pack(side="left", padx=4)
        self.speed = ttk.Label(bar, width=12, anchor="e")
        self.speed.pack(side="right")
        style = ttk.Style()
        # a progress bar layout with a text label drawn on top, for the "25%"
        style.layout("Text.Horizontal.TProgressbar", [
            ("Horizontal.Progressbar.trough", {"sticky": "nswe", "children": [
                ("Horizontal.Progressbar.pbar", {"side": "left", "sticky": "ns"})]}),
            ("Horizontal.Progressbar.label", {"sticky": ""})])
        style.configure("Text.Horizontal.TProgressbar", text="")
        self.progress = ttk.Progressbar(bar, length=240, maximum=100, style="Text.Horizontal.TProgressbar")
        self.progress.pack(side="right", padx=8)
        self.downloading = self.cancelled = False
        self.cancel_button = ttk.Button(bar, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left")
        self.where = ttk.Label(right, text="← Pick a computer")
        self.where.pack(anchor="w", pady=(6, 0))
        self.tree = ttk.Treeview(right, columns=("size",), selectmode="browse")
        self.tree.heading("#0", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.pack(fill="both", expand=True, pady=4)
        self.tree.bind("<Double-1>", self.enter)
        self.tree.bind("<Return>", self.enter)
        self.status = ttk.Label(right, text="")
        self.status.pack(anchor="w")

        self.refresh_my_list()
        self.tick()
        if FROZEN or not (this_program().parent / ".git").exists():  # never overwrite the dev copy
            self.check_for_update()

    # updates: checked at startup and every 6 hours
    def check_for_update(self):
        def work():
            try:
                found = check_update()
            except (OSError, ValueError, KeyError, TypeError):
                found = None  # offline, or no release published yet
            if found and found[0] != self.declined:
                self.root.after(0, self.offer_update, *found)

        threading.Thread(target=work, daemon=True).start()
        self.root.after(6 * 3600 * 1000, self.check_for_update)

    def offer_update(self, version, url, size):
        if not messagebox.askyesno("Update available", f"Version {version} is available (you have {VERSION}).\n\nUpdate and restart now?"):
            self.declined = version
            return
        self.set_status(f"Downloading update {version}…")

        def work():
            try:
                cmd = install_update(url, size)
            except OSError as err:
                return self.set_status(f"Update failed: {err}")
            # the reset flag makes a PyInstaller exe start fresh instead of thinking it's our child
            subprocess.Popen(cmd, env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
            self.root.after(0, self.quit)

        threading.Thread(target=work, daemon=True).start()

    def set_status(self, text):
        self.root.after(0, lambda: self.status.config(text=text))

    # my shares
    def refresh_my_list(self):
        self.my_list.delete(0, "end")
        for name, folder in shares.items():
            self.my_list.insert("end", f"{name}  —  {folder}")

    def add_folder(self):
        if folder := filedialog.askdirectory(title="Choose a folder to share"):
            add_share(os.path.normpath(folder))
            save_shares()
            self.refresh_my_list()

    def remove_folder(self):
        if sel := self.my_list.curselection():
            del shares[list(shares)[sel[0]]]
            save_shares()
            self.refresh_my_list()

    # computers list, refreshed every 2 s
    def tick(self):
        now = time.time()
        for name, (_, _, seen) in list(peers.items()):
            if now - seen > OFFLINE_AFTER:
                peers.pop(name, None)
        names = sorted(peers)
        labels = [f"{name}  ({peers[name][0]})" + ("  — this PC" if name == NAME else "") for name in names]
        if labels != list(self.peer_list.get(0, "end")):
            self.peer_list.delete(0, "end")
            for label in labels:
                self.peer_list.insert("end", label)
            self.peer_names = names
            if self.peer in names:
                self.peer_list.selection_set(names.index(self.peer))
            elif self.peer:
                self.peer = None
                self.tree.delete(*self.tree.get_children())
                self.where.config(text="That computer went offline")
        self.root.after(2000, self.tick)

    def open_peer(self, _event):
        if sel := self.peer_list.curselection():
            self.peer = self.peer_names[sel[0]]
            self.share, self.parts = None, []
            self.load()

    # browsing
    def load(self):
        if self.peer not in peers:
            return
        self.request += 1
        request, peer, share, path = self.request, peers[self.peer][:2], self.share, "/".join(self.parts)
        where = f"{self.peer} \\ " + (" \\ ".join([share] + self.parts) if share else "")
        self.where.config(text=where)
        self.set_status("Loading…")

        def work():
            try:
                if share is None:
                    entries = [{"name": s, "dir": True, "size": 0} for s in get_json(peer, "shares")]
                else:
                    entries = get_json(peer, "list", share=share, path=path)
            except (OSError, ValueError) as e:
                return self.set_status(f"Couldn't load: {e}")
            self.root.after(0, self.show, request, entries)

        threading.Thread(target=work, daemon=True).start()

    def show(self, request, entries):
        if request != self.request:
            return
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        entries = [e for e in entries if isinstance(e, dict) and safe_name(e.get("name"))]
        for e in sorted(entries, key=lambda e: (not e.get("dir"), e["name"].lower())):
            size = "" if e.get("dir") else human_size(int(e.get("size") or 0))
            iid = self.tree.insert("", "end", text=("📁 " if e.get("dir") else "📄 ") + e["name"], values=(size,))
            self.items[iid] = e
        self.set_status(f"{len(entries)} items" if entries else "Empty")

    def enter(self, _event):
        e = self.items.get(self.tree.focus())
        if e and e.get("dir"):
            if self.share is None:
                self.share = e["name"]
            else:
                self.parts.append(e["name"])
            self.load()

    def up(self):
        if self.parts:
            self.parts.pop()
        elif self.share is not None:
            self.share = None
        self.load()

    def download_selected(self):
        e = self.items.get(self.tree.focus())
        if not e or self.peer not in peers:
            return messagebox.showinfo("Download", "Select a file or folder first.")
        if self.downloading:
            return messagebox.showinfo("Download", "Wait for the current download to finish.")
        dest = filedialog.askdirectory(title="Download to…")
        if not dest:
            return
        peer, share = peers[self.peer][:2], self.share
        args = (e["name"], []) if share is None else (share, self.parts + [e["name"]])
        self.downloading, self.cancelled, self.done, self.total = True, False, 0, 0
        self.cancel_button.config(state="normal")

        def work():
            self.set_status(f"Downloading {e['name']}…")
            try:
                self.total = folder_size(peer, *args) if e.get("dir") else int(e.get("size") or 0)
                self.started = time.time()
                self.root.after(0, self.show_progress)
                download(peer, *args, bool(e.get("dir")), Path(dest), self.add_progress)
                self.set_status(f"Downloaded {e['name']} to {dest}")
            except Cancelled:
                self.set_status(f"Cancelled {e['name']} (files already finished were kept)")
            except (OSError, ValueError) as err:
                self.set_status(f"Download failed: {err}")
            finally:
                self.downloading = False
                self.root.after(0, lambda: self.cancel_button.config(state="disabled"))

        threading.Thread(target=work, daemon=True).start()

    def cancel(self):
        self.cancelled = True

    def add_progress(self, n):
        if self.cancelled:
            raise Cancelled  # download() deletes the half-written .part file on the way out
        self.done += n  # only the one download thread writes this

    def show_progress(self):
        pct = min(100, self.done * 100 / self.total) if self.total else 100
        self.progress["value"] = pct
        ttk.Style().configure("Text.Horizontal.TProgressbar", text=f"{pct:.0f}%")
        self.speed.config(text=f"{human_size(self.done / max(time.time() - self.started, 0.001))}/s")
        if self.downloading:
            self.root.after(250, self.show_progress)

    def quit(self):
        try:
            send_broadcast(bye=True)  # lets other PCs drop us right away
        except OSError:
            pass
        self.root.destroy()


def main():
    load_shares()
    try:
        this_program().with_name(this_program().name + ".old").unlink(missing_ok=True)  # left by an update
    except OSError:
        pass  # the old version is still closing; it goes next start
    root = tk.Tk()
    for attempt in range(10):  # after an update the old version may still hold the port for a moment
        try:
            server = http.server.ThreadingHTTPServer(("", PORT), Handler)
            break
        except OSError:
            if attempt == 9:
                root.withdraw()
                messagebox.showerror("Mycelium", f"Port {PORT} is in use. Is Mycelium already running?")
                return
            time.sleep(0.5)
    for target in (server.serve_forever, announce, listen):
        threading.Thread(target=target, daemon=True).start()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.quit)
    root.mainloop()


if __name__ == "__main__":
    main()
