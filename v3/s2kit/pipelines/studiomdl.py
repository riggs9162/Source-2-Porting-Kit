"""
Running studiomdl on a generated QC.

The load-bearing detail: studiomdl's ``-game`` argument must be the folder that
*contains* `gameinfo.txt`. For StudioMDL CE that is `<install>/csgo`, not the
install root. Passing the root crashes the compiler with
EXCEPTION_ACCESS_VIOLATION rather than printing an error, so the game directory
is resolved and validated before the process is ever started.

This is the one ported module that is deliberately **not** a 1:1 copy of v2.
v2 used a bare `subprocess.run`, which cannot be cancelled and pops
a console window on Windows. It now goes through
:func:`s2kit.core.process.run_streamed`, which fixes both and strips the Source
SDK environment variables that make studiomdl pick a different game than the one
on the command line.

The success rule is preserved exactly: **exit code 0 AND "Completed" somewhere in
the output.** studiomdl returns 0 on several failures, so the string check is not
redundant.
"""

from __future__ import annotations

import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from s2kit.core.process import environment_without, run_streamed
from s2kit.pipelines.porting import LogFn

#: Source SDK variables that override `-game` if they are set in the
#: environment. A user with Hammer or the SDK launcher configured would
#: otherwise compile against whatever those point at, silently.
_SDK_VARS = ("VPROJECT", "SteamAppId", "SteamGameId")


# ----------------------------------------------------------------------
# Verifying what a compile left behind
# ----------------------------------------------------------------------

#: Byte offset of the checksum studiomdl stamps into each file it writes.
#: `studiohdr_t` and `vertexFileHeader_t` both put it third, after id and
#: version; the VTX header carries four fields ahead of it.
_CHECKSUM_OFFSET = {"mdl": 8, "vvd": 8, "vtx": 16, "phy": 12}

#: The VTX flavours studiomdl writes. Garry's Mod loads the dx90 one; the
#: others are checked when present because a stale one is the same defect.
VTX_SUFFIXES = (".dx90.vtx", ".dx80.vtx", ".sw.vtx")


@dataclass
class CompiledModel:
    """The files a compiled model is made of, and whether they agree.

    The engine refuses to load a model whose `.vvd`/`.vtx` checksum differs
    from the `.mdl`'s - ``Error Vertex File ... checksum X should be Y``,
    followed by ``Bad geometry``. That happens whenever one of them does not
    get rewritten: the compiler could not open it because the game had the
    model loaded, or a copy step dropped a file. Nothing warns at compile time,
    so the failure surfaces in a map compile instead.

    A stale `.phy` is quieter and worse. Collision is optional, so nothing is
    missing when there is none - but the engine loads `<model>.phy` whenever
    the file is *there*, checksum or no checksum. Recompile a model that used
    to have collision and no longer does, and studiomdl writes no `.phy` and
    deletes nothing: the model keeps the collision it had two compiles ago,
    silently, and no console message ever mentions it.
    """

    path: Path
    checksum: Optional[int] = None
    #: Sibling file -> its checksum, or None when it could not be read.
    siblings: Dict[Path, Optional[int]] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)

    @property
    def stale(self) -> List[Path]:
        """Siblings whose checksum disagrees with the .mdl's."""
        return [path for path, value in self.siblings.items()
                if value != self.checksum]

    @property
    def ok(self) -> bool:
        return (self.checksum is not None and not self.missing
                and not self.stale)

    def problems(self) -> List[str]:
        """One line per defect, in the words the engine would use."""
        if self.checksum is None:
            return [f"{self.path.name} is not a readable .mdl"]
        lines = [f"{self.path.stem}{suffix} is missing"
                 for suffix in self.missing]
        for path in self.stale:
            value = self.siblings[path]
            found = "unreadable" if value is None else str(value)
            if path.suffix.lower() == ".phy":
                lines.append(f"{path.name} checksum {found} should be "
                             f"{self.checksum} - it is left over from an "
                             f"earlier compile, and the engine loads it anyway, "
                             f"so the model still has that compile's collision")
            else:
                lines.append(f"{path.name} checksum {found} should be "
                             f"{self.checksum} - it is left over from an "
                             f"earlier compile")
        return lines


def _checksum_of(path: Path, offset: int) -> Optional[int]:
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(4)
        return struct.unpack("<i", data)[0] if len(data) == 4 else None
    except OSError:
        return None


def check_compiled_model(mdl_path: Any) -> CompiledModel:
    """Read the checksum out of a model's `.mdl`, `.vvd`, `.vtx` and `.phy`.

    A `.vtx` flavour that was never written is not reported - only dx90 is
    needed by Garry's Mod - and neither is an absent `.phy`, since collision is
    optional. A missing `.vvd` is, and so is any file that is *present* and
    carries a checksum from a different compile.
    """
    mdl_path = Path(mdl_path)
    model = CompiledModel(path=mdl_path,
                          checksum=_checksum_of(mdl_path, _CHECKSUM_OFFSET["mdl"]))
    if model.checksum is None:
        return model

    vvd = mdl_path.with_suffix(".vvd")
    if vvd.is_file():
        model.siblings[vvd] = _checksum_of(vvd, _CHECKSUM_OFFSET["vvd"])
    else:
        model.missing.append(".vvd")

    # Collision is optional, so a missing .phy is not a defect - but one that
    # is present and disagrees is the model quietly keeping old collision.
    phy = mdl_path.with_suffix(".phy")
    if phy.is_file():
        model.siblings[phy] = _checksum_of(phy, _CHECKSUM_OFFSET["phy"])

    found_vtx = False
    for suffix in VTX_SUFFIXES:
        vtx = mdl_path.with_name(mdl_path.stem + suffix)
        if vtx.is_file():
            found_vtx = True
            model.siblings[vtx] = _checksum_of(vtx, _CHECKSUM_OFFSET["vtx"])
    if not found_vtx:
        model.missing.append(".dx90.vtx")
    return model


def model_path_for(qc_path: Any, game: Any) -> Optional[Path]:
    """Where the QC's `$modelname` will land under `<game>/models/`."""
    try:
        text = Path(qc_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("$modelname"):
            continue
        name = stripped[len("$modelname"):].strip().strip('"').strip()
        if not name:
            return None
        target = Path(game) / "models" / name
        return target if target.suffix else target.with_suffix(".mdl")
    return None


def resolve_game_dir(game: Path) -> Optional[Path]:
    """The directory studiomdl's ``-game`` needs (the one with gameinfo.txt).

    Accepts either the game dir itself or a compiler install root whose `csgo`
    subfolder holds gameinfo.txt (the StudioMDL CE layout) - the subfolder is
    auto-appended. Returns None when neither matches.
    """
    game = Path(game)
    if (game / "gameinfo.txt").is_file():
        return game
    if (game / "csgo" / "gameinfo.txt").is_file():
        return game / "csgo"
    return None


def resolve_game_arg(value: Any) -> Optional[Path]:
    """Resolve a user-supplied `-game` value that may be a file or a folder.

    v3's Preferences store `gameinfo_path` as a path to `gameinfo.txt` itself,
    while v2's tool asked for the folder. Accept both: a file named
    `gameinfo.txt` resolves to its parent, anything else falls through to
    :func:`resolve_game_dir`.
    """
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text)
    if path.is_file():
        return path.parent if path.name.lower() == "gameinfo.txt" else None
    if path.is_dir():
        return resolve_game_dir(path)
    return None


def compile_qc(qc_path: Path, studiomdl: Path, game: Path,
               on_log: Optional[LogFn] = None,
               cancel: Optional[threading.Event] = None) -> bool:
    """Compile a QC. True only when studiomdl reports completion."""
    log = on_log or (lambda _message, _level="INFO": None)
    qc_path = Path(qc_path)

    resolved = resolve_game_arg(game) or resolve_game_dir(Path(game))
    if resolved is None:
        log(f"-game is invalid: {game} has no gameinfo.txt (and no csgo "
            "subfolder with one) - studiomdl would crash", "ERROR")
        return False
    if resolved != Path(game):
        log(f"-game resolved to {resolved} (contains gameinfo.txt)", "INFO")

    command = [str(studiomdl), "-game", str(resolved), "-nop4", str(qc_path)]
    log("compiling: " + " ".join(command), "INFO")

    output: list[str] = []

    def collect(line: str) -> None:
        output.append(line)
        # studiomdl is chatty; surface only what a user acts on. The full
        # transcript still reaches the job log at DEBUG through the tail below.
        lowered = line.lower()
        if "error" in lowered or "warning" in lowered:
            log(line, "ERROR" if "error" in lowered else "WARNING")

    try:
        code = run_streamed(command, collect, cancel=cancel,
                            cwd=qc_path.parent,
                            env=environment_without(*_SDK_VARS),
                            echo_command=False)
    except FileNotFoundError:
        log(f"studiomdl not found: {studiomdl}", "ERROR")
        return False
    except Exception as exc:
        log(f"studiomdl could not be run: {exc}", "ERROR")
        return False

    for line in output[-3:]:
        log(line, "INFO")

    compiled = code == 0 and any("Completed" in line for line in output)
    if compiled:
        # studiomdl says "Completed" whether or not every file it meant to
        # write actually got written. One it could not open - because the game
        # or a model viewer had the model loaded - is left at its previous
        # contents, and the mismatch only surfaces later, in a map compile.
        target = model_path_for(qc_path, resolved)
        if target is not None and target.is_file():
            model = check_compiled_model(target)
            for problem in model.problems():
                log(problem, "ERROR")
            if not model.ok:
                log("studiomdl reported success but left a file from an "
                    "earlier compile - the engine will refuse this model. "
                    "Close whatever has it open and compile again.", "ERROR")
                compiled = False

    log("compile " + ("succeeded" if compiled else "FAILED"),
        "SUCCESS" if compiled else "ERROR")
    return compiled
