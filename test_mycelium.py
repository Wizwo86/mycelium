import http.server
import tempfile
import threading
import urllib.error
from pathlib import Path

import mycelium

assert mycelium.parse_announcement(b'{"name": "pc1", "port": 8765}') == ("pc1", 8765, False)
assert mycelium.parse_announcement(b'{"name": "pc1", "port": 8765, "bye": true}') == ("pc1", 8765, True)
assert mycelium.parse_announcement(b"garbage") is None
assert mycelium.parse_announcement(b'{"name": "x", "port": 99999}') is None
assert mycelium.parse_version("v1.10.0") > mycelium.parse_version("1.9.3")
assert mycelium.local_ips() and not any(ip.startswith("127.") for ip in mycelium.local_ips())
assert mycelium.safe_name("a.txt") and not mycelium.safe_name("..") and not mycelium.safe_name("a/b") and not mycelium.safe_name("C:\\x")

with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
    (Path(src) / "sub").mkdir()
    (Path(src) / "sub" / "deep.txt").write_text("deep")
    (Path(src) / "hello.txt").write_text("hi")
    mycelium.CONFIG = Path(dst) / "config.json"
    mycelium.add_share(src)
    mycelium.add_share(src)  # duplicate ignored
    name = Path(src).name
    assert list(mycelium.shares) == [name]
    assert mycelium.resolve(name, "../") is None and mycelium.resolve(name, "C:\\Windows") is None

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), mycelium.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    peer = ("127.0.0.1", srv.server_port)

    assert mycelium.get_json(peer, "shares") == [name]
    assert {e["name"] for e in mycelium.get_json(peer, "list", share=name, path="")} == {"sub", "hello.txt"}
    try:
        mycelium.get_json(peer, "file", share=name, path="../../etc")
        raise AssertionError("escape should 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404

    assert mycelium.folder_size(peer, name, []) == 6
    got = []
    mycelium.download(peer, name, [], True, Path(dst), got.append)
    assert sum(got) == 6
    assert (Path(dst) / name / "sub" / "deep.txt").read_text() == "deep"
    mycelium.download(peer, name, ["hello.txt"], False, Path(dst))
    mycelium.download(peer, name, ["hello.txt"], False, Path(dst))
    assert (Path(dst) / "hello (2).txt").read_text() == "hi"  # no overwrite

    def cancel(n):
        raise mycelium.Cancelled
    try:
        mycelium.download(peer, name, ["hello.txt"], False, Path(dst), cancel)
        raise AssertionError("should cancel")
    except mycelium.Cancelled:
        assert not list(Path(dst).glob("*.part"))  # half file cleaned up

    # updater swaps the program file, keeping the old one aside
    prog = Path(dst) / "prog.py"
    prog.write_text("old")
    mycelium.this_program = lambda: prog
    url = f"http://127.0.0.1:{srv.server_port}/api/file?share={name}&path=hello.txt"
    try:
        mycelium.install_update(url, 999)
        raise AssertionError("size mismatch should fail")
    except OSError:
        assert prog.read_text() == "old" and not (Path(dst) / "prog.py.new").exists()
    mycelium.install_update(url, 2)
    assert prog.read_text() == "hi" and (Path(dst) / "prog.py.old").read_text() == "old"
    srv.shutdown()

print("ok")
