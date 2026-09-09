"""Local app launcher that opens JTrack Insight in the user's browser.

This keeps all analysis on the same machine while distributing the app as a
clickable local launcher.
"""

from __future__ import annotations

import multiprocessing
import platform
import socket
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser


DEFAULT_HOST = "127.0.0.1"
DEFAULT_TITLE = "JTrack Insight"
DEFAULT_STARTUP_TIMEOUT_SEC = 25.0
DEFAULT_PORT_CANDIDATES = (8000, 8001, 8002, 8003, 8004, 8005)


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run_osascript(script: str) -> subprocess.CompletedProcess[str] | None:
    if platform.system() != "Darwin":
        return None
    try:
        return subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None


def _show_launch_prompt(base_url: str) -> bool:
    if platform.system() != "Darwin":
        return True

    title = _escape_applescript(DEFAULT_TITLE)
    message = _escape_applescript(
        "Your local analysis app is ready.\n\n"
        "Click Open Browser to launch JTrack Insight.\n\n"
        f"URL: {base_url}"
    )
    script = (
        f'display dialog "{message}" with title "{title}" '
        'buttons {"Cancel", "Open Browser"} default button "Open Browser"'
    )
    completed = _run_osascript(script)
    if completed is None:
        return True
    return completed.returncode == 0 and "Open Browser" in completed.stdout


def _show_error_dialog(message: str) -> None:
    if platform.system() != "Darwin":
        sys.stderr.write(f"{DEFAULT_TITLE}: {message}\n")
        return

    title = _escape_applescript(DEFAULT_TITLE)
    text = _escape_applescript(message)
    script = (
        f'display dialog "{text}" with title "{title}" '
        'buttons {"OK"} default button "OK"'
    )
    _run_osascript(script)


def _port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) != 0


def _run_server_process(host: str, port: int, state: dict[str, str]) -> None:
    from trackautism_app.server.web import run_server

    try:
        run_server(host=host, port=port)
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        state["error"] = str(exc)


def _wait_for_health(base_url: str, timeout_sec: float) -> None:
    deadline = time.time() + timeout_sec
    health_url = f"{base_url}/health"
    last_error: str | None = None
    while time.time() < deadline:
        try:
            with urlopen(health_url, timeout=2.0) as response:
                if response.status == 200:
                    return
        except URLError as exc:
            last_error = str(exc)
        except Exception as exc:  # pragma: no cover - defensive startup guard
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"Local app server did not become ready in time. Last error: {last_error or 'unknown error'}")


def _open_local_url(base_url: str) -> None:
    if platform.system() == "Darwin":
        completed = subprocess.run(["open", base_url], check=False)
        if completed.returncode == 0:
            return
    if webbrowser.open(base_url, new=1, autoraise=True):
        return
    raise RuntimeError(f"Could not open the local app URL in a browser: {base_url}")


def launch_local_browser_app(
    host: str = DEFAULT_HOST,
    port: int | None = None,
    startup_timeout_sec: float = DEFAULT_STARTUP_TIMEOUT_SEC,
) -> None:
    candidate_ports = [port] if port is not None else list(DEFAULT_PORT_CANDIDATES)
    last_error: str | None = None

    for candidate_port in candidate_ports:
        if candidate_port is None:
            continue
        if not _port_is_available(host, candidate_port):
            last_error = f"Port {candidate_port} is already in use."
            continue

        base_url = f"http://{host}:{candidate_port}"
        server_state: dict[str, str] = {}
        server_thread = threading.Thread(
            target=_run_server_process,
            args=(host, candidate_port, server_state),
            daemon=True,
        )
        server_thread.start()

        try:
            _wait_for_health(base_url, startup_timeout_sec)
            if not _show_launch_prompt(base_url):
                return
            _open_local_url(base_url)
            while server_thread.is_alive():
                time.sleep(0.5)
            if server_state.get("error"):
                last_error = server_state["error"]
                continue
            return
        except Exception as exc:
            last_error = server_state.get("error") or str(exc)

    raise RuntimeError(last_error or "Could not start the local app server on any candidate localhost port.")


def main() -> None:
    multiprocessing.freeze_support()
    try:
        launch_local_browser_app()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:
        _show_error_dialog(str(exc))
        sys.exit(1)
