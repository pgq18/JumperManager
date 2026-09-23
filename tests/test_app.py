"""Exercise ownership and shutdown using an isolated installation and fake engine."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import ProxyHandler, Request, build_opener
from support import temp_directory


ROOT = Path(__file__).resolve().parents[1]
OPENER = build_opener(ProxyHandler({}))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class AppLifecycleTests(unittest.TestCase):
    def test_second_port_cannot_take_over_and_shutdown_cleans_manager(self):
        with temp_directory() as temp:
            root = Path(temp)
            (root / "jumper_manager").mkdir()
            (root / "web").mkdir()
            (root / "web" / "index.html").write_text("test", encoding="utf-8")
            shutil.copyfile(ROOT / "app.py", root / "app.py")
            shutil.copyfile(ROOT / "jumper_manager" / "server.py", root / "jumper_manager" / "server.py")
            (root / "jumper_manager" / "__init__.py").write_text("__version__ = 'test'\n", encoding="utf-8")
            (root / "jumper_manager" / "engine.py").write_text(
                "class Manager:\n"
                "    def __init__(self, root, config_path=None):\n"
                "        self.root = root\n"
                "        (root/'constructed').write_text('yes')\n"
                "    def state(self): return {'mappings': [], 'hosts': []}\n"
                "    def close(self): (self.root/'closed').write_text('yes')\n",
                encoding="utf-8",
            )
            port = free_port()
            process = subprocess.Popen(
                [sys.executable, str(root / "app.py"), "--serve", "--port", str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            base = f"http://127.0.0.1:{port}"
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if (root / "data" / "server.json").exists():
                        break
                    if process.poll() is not None:
                        self.fail("Isolated app exited before becoming ready")
                    time.sleep(0.1)
                self.assertTrue((root / "data" / "server.json").exists())
                self.assertTrue((root / "constructed").exists())

                other = subprocess.run(
                    [sys.executable, str(root / "app.py"), "--serve", "--port", str(free_port())],
                    capture_output=True, timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.assertNotEqual(other.returncode, 0)
                self.assertIsNone(process.poll())
                self.assertFalse((root / "closed").exists())
                with OPENER.open(base + "/api/session", timeout=4) as response:
                    token = json.load(response)["token"]
                request = Request(base + "/api/shutdown", data=b"{}", headers={"X-Jumper-Token": token, "Content-Type": "application/json"})
                with OPENER.open(request, timeout=4) as response:
                    self.assertTrue(json.load(response)["ok"])
                self.assertEqual(process.wait(timeout=8), 0)
                self.assertTrue((root / "closed").exists())
                self.assertFalse((root / "data" / "server.json").exists())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()

