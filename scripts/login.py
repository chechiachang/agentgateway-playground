#!/usr/bin/env python3
"""Run Vault OIDC login and open its URL in the host browser, keeping stdout clean."""
import shutil
import subprocess
import sys
from urllib.parse import urlsplit


def login():
    command = [
        "docker", "compose", "exec", "-T", "vault", "vault", "login",
        "-no-print", "-method=oidc", "-path=oidc", "role=agentgateway",
        "skip_browser=true", "listenaddress=0.0.0.0", "callbackhost=localhost",
    ]
    opened = False
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, bufsize=1) as process:
        for line in process.stdout:
            print(line, end="", file=sys.stderr, flush=True)
            url = line.strip()
            parsed = urlsplit(url)
            if opened or parsed.scheme != "http" or parsed.netloc != "localhost:5556" or parsed.path != "/dex/auth":
                continue
            opened = True
            opener = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
            if not opener:
                print("No browser opener found; open the link above manually.", file=sys.stderr)
                continue
            try:
                result = subprocess.run([opener, url], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=10)
                if result.returncode:
                    print("Browser could not open; open the link above manually.", file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired):
                print("Browser could not open; open the link above manually.", file=sys.stderr)
        return process.wait()


if __name__ == "__main__":
    sys.exit(login())
