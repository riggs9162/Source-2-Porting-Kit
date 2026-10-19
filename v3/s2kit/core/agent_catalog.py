"""Agent catalog, effective preferences and dependency diagnostics."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

from s2kit.core import preferences, registry
from s2kit.core.agent_schema import RequestError, schema, strict_values
from s2kit.core.settings import Settings
from s2kit.tools import discover


def get_tool(tool_id: str) -> registry.ToolSpec:
    """Resolve a nonvisual declaration, failing explicitly for interactive tools."""
    discover()
    spec = registry.get(tool_id)
    if not spec or not spec.definition or not spec.definition.HEADLESS:
        raise RequestError({"tool_id": f"Unknown or interactive-only tool: {tool_id}"})
    return spec


def catalog() -> list[dict]:
    """List agent capabilities without importing conversion libraries."""
    discover()
    return [{"id": s.id, "title": s.title, "category": s.category,
             "description": s.description, "writes_files": s.definition.WRITES_FILES}
            for s in registry.all_tools() if s.definition and s.definition.HEADLESS]


def preference_params(definition) -> list:
    """Flatten global and per-tool declarations using the runtime's precedence."""
    values = {param.key: param for param in preferences.global_params()}
    values.update({param.key: param for param in definition.PREFERENCES})
    return list(values.values())


def describe(tool_id: str) -> dict:
    """Return shared schemas, help and tool identity."""
    spec = get_tool(tool_id)
    return {"id": spec.id, "title": spec.title, "description": spec.description,
            "parameters": schema(spec.definition.PARAMS),
            "preferences": schema(preference_params(spec.definition)), "help": spec.definition.HELP}


def prepare(tool_id: str, params: dict, overrides: dict | None = None,
            settings_path: Path | None = None) -> tuple[dict, dict]:
    """Validate independent request values without persisting form state or overrides."""
    spec = get_tool(tool_id)
    values = strict_values(spec.definition.PARAMS, params)
    pref_params = preference_params(spec.definition)
    overrides = {} if overrides is None else overrides
    strict_values(pref_params, overrides, validate_paths=False)
    settings = Settings(settings_path)
    prefs = settings.as_dict()
    prefs.update(settings.tool_prefs(tool_id))
    prefs.update(overrides or {})
    prefs = strict_values(pref_params, {p.key: prefs[p.key] for p in pref_params if p.key in prefs},
                          validate_paths=False)
    try:
        errors = spec.definition().validate(values)
    except (ImportError, ModuleNotFoundError) as exc:
        raise RequestError({"dependencies": str(exc)}) from exc
    if errors:
        raise RequestError(errors)
    check_dependencies(tool_id, values, prefs)
    return values, prefs


def check_dependencies(tool_id: str, values: dict, prefs: dict) -> None:
    """Check selected operations rather than requiring every optional tool dependency."""
    modules = set()
    material_tools = {"vmat-convert", "texture-set-convert", "material-builder", "material-extract", "vtf-resize"}
    if tool_id in material_tools | {"direct-port", "gltf-smd", "port-workflow", "alpha-mask", "texture-convert"}:
        modules.update(("numpy", "PIL"))
    if tool_id in material_tools and values.get("generate_vtf", True):
        modules.add("sourcepp")
    if tool_id in {"direct-port", "gltf-smd", "port-workflow"}:
        modules.update(("trimesh", "scipy"))
    if tool_id == "port-workflow" and values.get("action") == "run":
        modules.add("sourcepp")
    missing = sorted(name for name in modules if importlib.util.find_spec(name) is None)
    if missing:
        raise RequestError({"dependencies": "Missing Python packages: " + ", ".join(missing)})
    if tool_id in {"sound-convert", "quad-to-stereo", "sound-optimizer"} or (
            tool_id == "soundscape-port" and values.get("action") != "list" and values.get("extract_audio")):
        from s2kit.pipelines.audio_ops import ffmpeg_binary

        try:
            ffmpeg_binary(str(prefs.get("ffmpeg_path") or ""))
        except RuntimeError as exc:
            raise RequestError({"dependencies": str(exc)}) from exc
    needs_vrf = tool_id == "vrf-export" or tool_id == "soundscape-port" and bool(values.get("vpk"))
    if tool_id == "port-workflow":
        needs_vrf = values.get("input_kind") == "urls" or str(values.get("source_file", "")).lower().endswith((".vpk", ".vmdl_c", ".vmat_c"))
    if needs_vrf:
        from s2kit.pipelines.vrf import resolve_vrf_executable

        try:
            resolve_vrf_executable(settings_path=str(prefs.get("vrf_cli_path") or ""))
        except RuntimeError as exc:
            raise RequestError({"dependencies": str(exc)}) from exc
    compile_model = tool_id == "direct-port" and values.get("compile") or (
        tool_id == "port-workflow" and values.get("action") == "run" and
        (values.get("model_mode") == "ragdoll" or values.get("target") == "compiled"))
    if compile_model:
        from s2kit.pipelines.studiomdl import resolve_game_arg

        compiler = str(values.get("compiler") or values.get("studiomdl") or prefs.get("studiomdl_path") or "")
        game = str(values.get("game") or values.get("gameinfo") or prefs.get("gameinfo_path") or "")
        if not compiler or not Path(compiler).is_file():
            raise RequestError({"dependencies": "StudioMDL executable not found"})
        if resolve_game_arg(game) is None:
            raise RequestError({"dependencies": "Compile game must contain gameinfo.txt"})


def doctor(settings_path: Path | None = None) -> dict:
    """Report headless dependencies and executable paths without installing anything."""
    settings = Settings(settings_path)
    modules = {name: importlib.util.find_spec(name) is not None
               for name in ("numpy", "PIL", "sourcepp", "trimesh", "scipy", "srctools", "mcp")}
    external = {}
    for name, key in (("ffmpeg", "ffmpeg_path"), ("ffprobe", "ffprobe_path"),
                      ("studiomdl", "studiomdl_path"), ("Source2Viewer-CLI", "vrf_cli_path")):
        configured = str(settings.get(key, "") or "")
        found = configured if configured and Path(configured).is_file() else shutil.which(name)
        external[name] = {"configured": configured, "resolved": found}
    return {"python": sys.version.split()[0], "executable": sys.executable,
            "modules": modules, "external_programs": external, "tools": len(catalog()),
            "mcp_python_supported": sys.version_info >= (3, 11)}
