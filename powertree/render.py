"""Render DOT text to image files with Graphviz, on Windows, macOS and Linux."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ENV_VAR = "POWERTREE_DOT"
RASTER = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"}
FORMATS = RASTER | {"svg", "pdf", "eps", "ps"}



class RenderError(RuntimeError):
    pass


def install_hint() -> str:
    if sys.platform.startswith("win"):
        return "install Graphviz with 'winget install graphviz' (or from graphviz.org/download)"
    if sys.platform == "darwin":
        return "install Graphviz with 'brew install graphviz'"
    return "install Graphviz with your package manager, e.g. 'sudo apt install graphviz'"


def find_dot(explicit: str | None = None) -> str:
    """--dot, then $POWERTREE_DOT, then PATH."""
    for cand, origin in ((explicit, "--dot"), (os.environ.get(ENV_VAR), f"${ENV_VAR}")):
        if cand:
            if os.path.isfile(cand):
                return cand
            raise RenderError(f"Graphviz 'dot' not found at {cand} (from {origin})")
    found = shutil.which("dot")
    if found:
        return found
    raise RenderError(f"Graphviz 'dot' was not found on PATH; {install_hint()}, "
                      f"or point --dot / ${ENV_VAR} at dot(.exe). A .dot file can be written without it.")


def render(dot_text: str, output: str, fmt: str, dpi: int | None = None, dot: str | None = None) -> None:
    fmt = fmt.lower()
    if fmt not in FORMATS:
        raise RenderError(f"unsupported output format '{fmt}'; use one of: dot, "
                          f"{', '.join(sorted(FORMATS))}")
    exe = find_dot(dot)
    cmd = [exe, f"-T{fmt}", "-o", output]
    if dpi and fmt in RASTER:
        cmd.insert(1, f"-Gdpi={dpi}")
    try:
        proc = subprocess.run(cmd, input=dot_text.encode("utf-8"), capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RenderError(f"running {exe} failed: {e}") from None
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        raise RenderError(f"Graphviz failed (exit {proc.returncode}): {msg}")
