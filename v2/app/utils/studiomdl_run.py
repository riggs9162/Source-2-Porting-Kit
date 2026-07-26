"""StudioMDL invocation helpers shared by the porter pipelines. Qt-free.

The load-bearing detail: studiomdl's ``-game`` argument must be the folder
that CONTAINS gameinfo.txt (for StudioMDL CE that is ``<install>/csgo``, not
the install root). Passing the root crashes the compiler with
EXCEPTION_ACCESS_VIOLATION, so the game dir is validated/resolved up front.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

LogFn = Callable[[str, str], None]


def resolve_game_dir(game: Path) -> Optional[Path]:
    """The directory studiomdl's ``-game`` needs (contains gameinfo.txt).

    Accepts either the game dir itself or a compiler install root whose
    ``csgo`` subfolder holds gameinfo.txt (the StudioMDL CE layout) — the
    subfolder is auto-appended. Returns None when neither matches.
    """
    game = Path(game)
    if (game / "gameinfo.txt").is_file():
        return game
    if (game / "csgo" / "gameinfo.txt").is_file():
        return game / "csgo"
    return None


def compile_qc(qc_path: Path, studiomdl: Path, game: Path,
               on_log: Optional[LogFn] = None) -> bool:
    """Compile a QC; True only when studiomdl reports completion."""
    log = on_log or (lambda _m, _l="INFO": None)
    qc_path = Path(qc_path)
    resolved = resolve_game_dir(game)
    if resolved is None:
        log(f"-game folder invalid: {game} has no gameinfo.txt (and no "
            "csgo subfolder with one) - studiomdl would crash", "ERROR")
        return False
    if resolved != Path(game):
        log(f"-game resolved to {resolved} (contains gameinfo.txt)", "INFO")
    cmd = [str(studiomdl), "-game", str(resolved), "-nop4", str(qc_path)]
    log("compiling: " + " ".join(cmd), "INFO")
    pr = subprocess.run(cmd, capture_output=True, text=True,
                        cwd=str(qc_path.parent))
    tail = (pr.stdout or "").strip().splitlines()[-3:]
    for t in tail:
        log(t, "INFO")
    compiled = pr.returncode == 0 and "Completed" in (pr.stdout or "")
    log("compile " + ("succeeded" if compiled else "FAILED"),
        "SUCCESS" if compiled else "ERROR")
    return compiled
