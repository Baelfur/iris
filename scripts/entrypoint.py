#!/usr/bin/env python3
"""Container entrypoint.

Builds the uvicorn invocation, optionally adding TLS flags when
TLS_CERT_FILE and TLS_KEY_FILE are both set. Without them, the service
listens HTTP on port 8000 — same default as before.

These two env vars are read at launch time, not by the Python app
(Settings/dotenv loading happens inside the app, which is too late to
influence the uvicorn process flags). Set them in the container
environment (k8s Secret/ConfigMap, docker run -e), not in .env.

Test hook: DRY_RUN=1 prints the command instead of execing it.

Python instead of /bin/sh so the entrypoint runs on shell-less hardened
base images (e.g. distroless). Python is present in every base image
the service supports.
"""

import os
import sys


def main() -> None:
    # `python -m uvicorn` instead of bare `uvicorn` because the runtime
    # image's two-stage `pip install --target=/install` doesn't generate
    # the `bin/uvicorn` console-script entry. Module mode bypasses bin/
    # entirely and works regardless of pip --target's entry-point quirk. (#221)
    cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

    cert = os.environ.get("TLS_CERT_FILE", "")
    key = os.environ.get("TLS_KEY_FILE", "")
    if cert and key:
        cmd.extend([f"--ssl-certfile={cert}", f"--ssl-keyfile={key}"])
    elif cert or key:
        sys.stderr.write(
            "entrypoint: TLS_CERT_FILE and TLS_KEY_FILE must both be set, "
            "or both unset.\n"
        )
        sys.exit(64)

    if os.environ.get("DRY_RUN") == "1":
        print(" ".join(cmd))
        sys.exit(0)

    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
