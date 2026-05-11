from __future__ import annotations

import subprocess
from pathlib import Path


CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1350

_BROWSER_CANDIDATES = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
)


def export_editable_html_to_png(
    html_path: Path | str,
    output_path: Path | str,
    *,
    canvas_width: int = CANVAS_WIDTH,
    canvas_height: int = CANVAS_HEIGHT,
) -> Path:
    resolved_html_path = Path(html_path).expanduser().resolve()
    resolved_output_path = Path(output_path).expanduser().resolve()
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)

    browser_path = _resolve_browser_path()
    export_url = f"{resolved_html_path.as_uri()}?export=1"
    command = [
        str(browser_path),
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=2000",
        f"--window-size={canvas_width},{canvas_height}",
        f"--screenshot={resolved_output_path}",
        export_url,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "unknown browser export error"
        raise RuntimeError(f"Browser screenshot export failed: {stderr}")
    if not resolved_output_path.exists():
        raise RuntimeError(f"Browser screenshot export did not create output: {resolved_output_path}")
    return resolved_output_path


def _resolve_browser_path() -> Path:
    for candidate in _BROWSER_CANDIDATES:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "No supported browser executable found. Install Google Chrome, Chromium, Edge, or Brave."
    )
