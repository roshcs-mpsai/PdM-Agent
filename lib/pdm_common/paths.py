"""Where the repository's data folders are (schemas/, corpus/, skills/).

pdm_common is installed two ways. From a checkout (``pip install -e lib``, or tests with lib/ on
the path) the package sits at <root>/lib/pdm_common, so the root is two folders up. In the Docker
image it is installed with ``pip install ./lib`` into site-packages, where two folders up is
/usr/local/lib/python3.11 and holds none of the data. So the root is, in order:

1. ``$PDM_ROOT`` when set (the Dockerfile sets it to /app);
2. two folders above this file, when schemas/ is there (a checkout);
3. the working directory, when schemas/ is there (every service starts in the repository root).

If none has schemas/, the checkout guess is returned, so the error a caller raises names the same
folder it always did.
"""
from __future__ import annotations

import os
from pathlib import Path

MARKER = "schemas"


def repo_root() -> Path:
    env = os.environ.get("PDM_ROOT")
    if env:
        return Path(env)
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / MARKER).is_dir():
        return checkout
    cwd = Path.cwd()
    if (cwd / MARKER).is_dir():
        return cwd
    return checkout
