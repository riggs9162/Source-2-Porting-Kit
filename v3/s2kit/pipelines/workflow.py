"""Persistent, serial port workflows built from the existing conversion stages."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

from s2kit.pipelines.porting import PortCancelled, check_cancelled


@dataclass(frozen=True)
class WorkflowOptions:
    """The immutable settings recorded with a resumable project."""

    source: str
    project: str
    target: str = "sources"
    vpk_filter: str = ""
    material_source: str = ""
    namespace: str = "ports"
    vrf_executable: str = ""
    compiler: str = ""
    game: str = ""
    collision: bool = True
    emit_normal: bool = True
    model_mode: str = "generic"
    source_urls: str = ""
    include_source_animations: bool = False
    create_playermodels: bool = False
    install: bool = False
    gmod_addons: str = ""
    animation_cache: str = ""

    def validate(self) -> None:
        """Reject ambiguous targets and overlapping source/output trees."""
        if self.model_mode == "ragdoll":
            from s2kit.pipelines.ragdoll_workflow import validate_options

            validate_options(self)
            return
        source, project = Path(self.source).resolve(), Path(self.project).resolve()
        if not source.exists():
            raise ValueError(f"Source does not exist: {source}")
        if self.target not in {"sources", "compiled", "materials"}:
            raise ValueError("Choose model sources, compiled models, or materials")
        if source.is_file() and source.suffix.lower() not in {".glb", ".gltf", ".vpk", ".vmdl_c", ".vmat", ".vmat_c"}:
            raise ValueError("Choose a glTF/GLB, VMAT, compiled Source 2 model/material, VPK, or exported folder")
        if source.suffix.lower() in {".vmat", ".vmat_c"} and self.target != "materials":
            raise ValueError("VMAT inputs require the Materials target")
        if not re.fullmatch(r"[a-z0-9_]+(?:/[a-z0-9_]+)*", self.namespace):
            raise ValueError("Output namespace must use lowercase letters, digits, underscores and folder separators")
        roots = [source if source.is_dir() else source.parent]
        if self.material_source:
            material = Path(self.material_source).resolve()
            if not material.is_dir():
                raise ValueError(f"Material source is not a folder: {material}")
            roots.append(material)
        if any(project == root or project.is_relative_to(root) or root.is_relative_to(project) for root in roots):
            raise ValueError("Use a separate project folder outside the input tree")
        if source.suffix.lower() == ".vpk" and not self.vpk_filter.strip():
            raise ValueError("Specify paths inside the VPK; whole-game exports are not queued implicitly")


@dataclass
class Finding:
    """A measured preflight result, with scope and severity preserved in reports."""

    severity: str
    asset: str
    message: str


@dataclass
class Task:
    """One checkpointable item; dependencies refer to other task IDs."""

    id: str
    kind: str
    source: str
    root: str
    dependencies: list[str] = field(default_factory=list)


def file_digest(path: Path) -> str:
    """Hash file content without loading large assets into memory."""
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    """Replace a checkpoint only after its full contents reach disk."""
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def project_lock(project: Path):
    """Hold an OS lock that is released even if the process exits unexpectedly."""
    project.mkdir(parents=True, exist_ok=True)
    with (project / ".workflow.lock").open("a+b") as stream:
        stream.seek(0)
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("This workflow project is already running in another session") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def source_stamp(options: WorkflowOptions) -> str:
    """Detect changed inputs/settings before reusing a checkpoint."""
    source = Path(options.source).resolve()
    roots = [source]
    if options.material_source:
        roots.append(Path(options.material_source).resolve())
    files = sorted({p for root in roots for p in (root.rglob("*") if root.is_dir() else [root]) if p.is_file()})
    if source.suffix.lower() == ".vpk":
        prefix = source.stem.removesuffix("_dir")
        files.extend(sorted(source.parent.glob(prefix + "_[0-9]*.vpk")))
    digest = hashlib.sha256(json.dumps(asdict(options), sort_keys=True).encode())
    for path in files:
        stat = path.stat()
        digest.update(f"{path}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def inspect_model(path: Path) -> list[Finding]:
    """Check external resources and skin structure/weights, not animation fidelity."""
    from s2kit.pipelines import glb_skin, gltf_animation
    import numpy as np

    found: list[Finding] = []
    add = lambda severity, message: found.append(Finding(severity, str(path), message))
    try:
        if path.suffix.lower() == ".glb":
            document, buffer = glb_skin.read_glb(path)
        else:
            document = json.loads(path.read_text(encoding="utf-8"))
            buffer = gltf_animation.load_buffer_bytes(document, path.parent)
        if not document.get("meshes"):
            add("error", "No render meshes")
        for kind in ("buffers", "images"):
            for resource in document.get(kind, []):
                uri = resource.get("uri", "")
                if uri and not uri.startswith("data:") and not (path.parent / unquote(uri)).is_file():
                    add("error" if kind == "buffers" else "warning", f"Missing {kind} resource: {uri}")
        if len(document.get("buffers", [])) > 1:
            add("error", "Multiple glTF buffers are not supported by the current skin reader")
        nodes = document.get("nodes", [])
        parents = {}
        for index, node in enumerate(nodes):
            for child in node.get("children", []):
                if not isinstance(child, int) or not 0 <= child < len(nodes):
                    add("error", f"Node {index} has an invalid child index")
                    continue
                if child in parents:
                    add("error", f"Node {child} has multiple parents")
                parents[child] = index
        for start in range(len(nodes)):
            seen, current = set(), start
            while current in parents:
                if current in seen:
                    add("error", "Cycle in node hierarchy")
                    break
                seen.add(current)
                current = parents[current]
        skins = document.get("skins", [])
        for index, skin in enumerate(skins):
            joints = skin.get("joints", [])
            if not joints or any(not isinstance(j, int) or not 0 <= j < len(nodes) for j in joints):
                add("error", f"Skin {index} has missing or invalid joints")
                continue
            names = [nodes[j].get("name", f"joint_{j}").casefold() for j in joints]
            if len(set(joints)) != len(joints) or len(set(names)) != len(names):
                add("error", f"Skin {index} contains duplicate joints or bone names")
            if len(joints) > 256:
                add("error", f"Skin {index} has {len(joints)} joints, above the GMod 256-bone limit")
        for node in nodes:
            if "skin" not in node or "mesh" not in node:
                continue
            skin_index, mesh_index = node["skin"], node["mesh"]
            if not 0 <= skin_index < len(skins) or not 0 <= mesh_index < len(document.get("meshes", [])):
                add("error", "Invalid skin or mesh index on a node")
                continue
            for primitive in document["meshes"][mesh_index].get("primitives", []):
                attributes = primitive.get("attributes", {})
                if not {"JOINTS_0", "WEIGHTS_0"} <= attributes.keys() or buffer is None:
                    add("error", "Skinned primitive is missing readable joints or weights")
                    continue
                joints = gltf_animation.read_accessor(document, buffer, attributes["JOINTS_0"])
                weights = gltf_animation.read_accessor(document, buffer, attributes["WEIGHTS_0"])
                if joints.shape != weights.shape or not np.isfinite(weights).all() or (weights < 0).any():
                    add("error", "Invalid skin weight values or shape")
                elif (joints[weights > 0] >= len(skins[skin_index]["joints"])).any():
                    add("error", "Vertex references a joint outside its skin")
                elif (weights.sum(axis=1) <= 0).any():
                    add("error", "Skinned vertices with no positive bone weight")
                elif not np.allclose(weights.sum(axis=1), 1, atol=0.01):
                    add("warning", "Some vertex weights are not normalized")
        add("info", f"Inspected {len(skins)} skin(s); animation alignment and in-game deformation not tested")
        if not document.get("images"):
            add("warning", "No image references in glTF; material completeness cannot be established from this file")
    except Exception as exc:
        add("error", f"Model could not be inspected: {exc}")
    return found


def needs_export(options: WorkflowOptions) -> bool:
    return Path(options.source).suffix.lower() in {".vpk", ".vmdl_c", ".vmat_c"}


def discover_tasks(options: WorkflowOptions, *, exported: bool = False) -> list[Task]:
    """Suggest only stages supported by the input; preserve source subfolders."""
    from s2kit.pipelines.gltf_smd import ModelSetScanner

    source = Path(options.source).resolve()
    project = Path(options.project).resolve()
    if needs_export(options) and not exported:
        return [Task("export", "export", str(source), str(source.parent))]
    root = project / "extracted" if exported else source if source.is_dir() else source.parent
    model_root = root / "modelsrc" if (root / "modelsrc").is_dir() else root
    material_root = Path(options.material_source).resolve() if options.material_source else root / "materialsrc" if (root / "materialsrc").is_dir() else root
    dependencies = ["export"] if exported else []
    tasks = []
    if options.target != "materials":
        for model in sorted(ModelSetScanner(model_root, recursive=True).find_sets(), key=lambda item: str(item.render_path)):
            if source.is_file() and not exported and model.render_path.resolve() != source:
                continue
            key = "model:" + model.render_path.relative_to(model_root).as_posix()
            tasks.append(Task(key, "model", str(model.render_path), str(model_root), dependencies))
            if options.target == "compiled":
                tasks.append(Task("compile:" + key, "compile", str(model.render_path), str(model_root), [key]))
    vmats = sorted(material_root.rglob("*.vmat"))
    if source.is_file() and source.suffix.lower() == ".vmat" and not exported:
        vmats = [source]
    for path in vmats:
        tasks.append(Task("material:" + path.relative_to(material_root).as_posix(), "material", str(path), str(material_root), dependencies))
    return tasks


def preflight(options: WorkflowOptions, tasks: list[Task]) -> list[Finding]:
    """Inspect inputs and external setup without executing converters or compilers."""
    from s2kit.pipelines import studiomdl, vmat, vrf
    from s2kit.bootstrap import module_available

    found = []
    if not tasks:
        found.append(Finding("error", options.source, "No supported input assets found"))
    if needs_export(options):
        try:
            vrf.resolve_vrf_executable(explicit=options.vrf_executable or None)
        except Exception as exc:
            found.append(Finding("error", "export", str(exc)))
        if not any(task.kind != "export" for task in tasks):
            found.append(Finding("info", "export", "Texture and skeleton checks are deferred until extraction finishes"))
    if options.target == "compiled":
        if not options.compiler or not Path(options.compiler).is_file():
            found.append(Finding("error", "compiler", "Set an existing studiomdl executable"))
        if not studiomdl.resolve_game_arg(options.game):
            found.append(Finding("error", "compiler", "Set a game folder containing gameinfo.txt"))
        found.append(Finding("info", "compiler", "Checks setup paths only; successful compilation is checked during Run"))
    if any(task.kind == "material" for task in tasks) and not module_available("sourcepp"):
        found.append(Finding("error", "materials", "VTF output requires sourcepp"))
    for task in tasks:
        if task.kind == "model":
            found.extend(inspect_model(Path(task.source)))
        elif task.kind == "material":
            try:
                entry = vmat.VmatParser(texture_root=Path(task.root)).parse(task.source, root=Path(task.root))
                if not entry.buildable:
                    found.append(Finding("error", task.source, "No resolved color texture or literal color"))
                found.extend(Finding("warning", task.source, message) for message in entry.warnings)
            except Exception as exc:
                found.append(Finding("error", task.source, f"Material cannot be read: {exc}"))
    return found


def execute_task(task: Task, options: WorkflowOptions, records: dict,
                 log: Callable, cancel: threading.Event) -> list[Path]:
    """Delegate each stage to its existing pipeline implementation."""
    from s2kit.pipelines import gltf_smd, materials, studiomdl, vmat, vrf

    source, root, project = Path(task.source), Path(task.root), Path(options.project)
    check_cancelled(cancel)
    if task.kind == "export":
        destination = project / "extracted"
        if destination.exists():
            destination.rename(project / f"extracted-previous-{time.time_ns()}")
        executable = vrf.resolve_vrf_executable(explicit=options.vrf_executable or None)
        code = vrf.run_vrf_export(executable, source, destination,
                                 mode="materials" if options.target == "materials" else "models",
                                 vpk_filepath=options.vpk_filter or None,
                                 threads=1, on_log=log, cancel=cancel)
        check_cancelled(cancel)
        if code:
            raise ValueError(f"Source 2 Viewer exited with {code}")
        return sorted(path for path in destination.rglob("*") if path.is_file())
    if task.kind == "model":
        models = gltf_smd.ModelSetScanner(source.parent, recursive=False).find_sets()
        model = next(item for item in models if item.render_path.resolve() == source.resolve())
        settings = gltf_smd.ConversionOptions(
            modelname_prefix=(Path(options.namespace) / model.base_dir.relative_to(root)).as_posix(),
            cdmaterials=f"models/{options.namespace}/",
            export_physics=options.collision, replace_existing=True)
        result = gltf_smd.convert_model_set(model, settings, input_root=root,
                                           output_root=project / "modelsrc" / options.namespace,
                                           on_log=log, cancel=cancel)
        if result.status != "converted":
            raise ValueError(result.reason or f"Conversion returned {result.status}")
        for warning in result.warnings:
            log(warning, "WARNING")
        return result.files
    if task.kind == "material":
        entry = vmat.VmatParser(texture_root=root).parse(source, root=root)
        material_path = (Path("models") / options.namespace / entry.rel_dir).as_posix()
        settings = materials.MaterialOptions(emit_normal=options.emit_normal,
                                              translucent=entry.translucent, alphatest=entry.alphatest,
                                              tintable=entry.tintable)
        result = materials.build_material(entry.to_source(), project / "materials" / material_path,
                                          entry.name, material_path, settings, log=log, cancel=cancel)
        for note in result.notes:
            log(note, "WARNING")
        return result.written
    if task.kind == "compile":
        qcs = [Path(path) for path in records[task.dependencies[0]]["outputs"] if Path(path).suffix.lower() == ".qc"]
        outputs = []
        for qc in qcs:
            if not studiomdl.compile_qc(qc, Path(options.compiler), Path(options.game), on_log=log, cancel=cancel):
                check_cancelled(cancel)
                raise ValueError(f"Compiler failed: {qc}")
            mdl = studiomdl.model_path_for(qc, studiomdl.resolve_game_arg(options.game))
            if mdl is None or not mdl.is_file():
                raise ValueError("Compiler reported completion without producing the expected MDL")
            result = studiomdl.check_compiled_model(mdl)
            if not result.ok:
                raise ValueError("; ".join(result.problems()))
            outputs.extend([mdl, *result.siblings.keys()])
        return outputs
    raise ValueError(f"Unsupported workflow step: {task.kind}")


def write_report(project: Path, state: dict) -> Path:
    """Persist factual outcomes, remaining failures and unperformed validation."""
    atomic_json(project / "port-report.json", state)
    lines = ["# Port report", "", f"Status: {state.get('status', 'planned')}", "",
             "## Preflight", ""]
    lines.extend(f"- {item['severity'].upper()}: {item['asset']} - {item['message']}" for item in state.get("findings", []))
    lines.extend(["", "## Queue", ""])
    for key, record in state.get("tasks", {}).items():
        lines.append(f"- {key}: {record['status']}" + (f" - {record['error']}" if record.get("error") else ""))
        lines.extend(f"  - {level}: {message}" for level, message in record.get("messages", []) if level in {"WARNING", "ERROR"})
        lines.extend(f"  - Output: {path}" for path in record.get("outputs", {}))
    if state.get("options", {}).get("model_mode") == "ragdoll":
        lines.extend(["", "## Conversion audits", ""])
        for record in state.get("tasks", {}).values():
            for name in record.get("outputs", {}):
                path = Path(name)
                if path.name in {"ragdoll-report.json", "playermodel_report.json", "playermodel-report.json",
                                 "compiled-report.json", "material-report.json"}:
                    lines.append(f"- [{path.name}](<{path.as_posix()}>)")
        lines.extend(["", "## Validation", "",
                      "- Compiled outputs are packaged only after file-consistency checks and available physics audits.",
                      "- Source primitives, joint envelopes, shader conversion and generated physics retain their individual adaptation reports.",
                      "- In-game appearance, movement and physical behavior require an explicit engine validation record.",
                      "- Editable sources and reports remain outside the addon; installation is optional."])
        report = project / "port-report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report
    lines.extend(["", "## Still to verify", "",
                  "- Material names and paths against the model's SMD/QC references; material output is staged, not installed in the game.",
                  "- In-game appearance, animation alignment, collision behavior and performance have not been tested.",
                  "- Model-source targets have not been compiled. Compiled targets check produced file checksums, not engine behavior.",
                  "- Resume operates between items. An interrupted item runs again and can overwrite its partial outputs."])
    report = project / "port-report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def run_workflow(options: WorkflowOptions, *, inspect_only: bool = False,
                 log: Callable = lambda *_args: None,
                 progress: Callable = lambda *_args: None,
                 cancel: threading.Event | None = None,
                 executor: Callable = execute_task) -> dict:
    """Run serially with atomic checkpoints, output verification and retryable failures."""
    if options.model_mode == "ragdoll":
        from s2kit.pipelines.ragdoll_workflow import run_ragdoll_workflow

        return run_ragdoll_workflow(options, inspect_only=inspect_only, log=log,
                                    progress=progress, cancel=cancel)
    options.validate()
    cancel = cancel or threading.Event()
    project = Path(options.project).resolve()
    with project_lock(project):
        checkpoint = project / "workflow-queue.json"
        stamp = source_stamp(options)
        state = {"version": 1, "signature": stamp, "options": asdict(options), "tasks": {}, "findings": [], "status": "planned"}
        if checkpoint.exists() and not inspect_only:
            state = json.loads(checkpoint.read_text(encoding="utf-8"))
            saved_options = state.get("options", {})
            normalized = asdict(WorkflowOptions(**saved_options))
            if state.get("version") != 1 or normalized != asdict(options):
                raise ValueError("Settings changed since this queue was created. Use a new project folder.")
            if state.get("signature") != stamp:
                log("Inputs changed; all queued items will be checked and rebuilt", "WARNING")
                state.update(signature=stamp, tasks={})
        tasks = discover_tasks(options)
        findings = preflight(options, tasks)
        state["findings"] = [asdict(item) for item in findings]
        if inspect_only:
            state["tasks"] = {task.id: {**asdict(task), "status": "planned"} for task in tasks}
            write_report(project, state)
            return state
        state["status"] = "running"
        try:
            index = 0
            while index < len(tasks):
                task = tasks[index]
                check_cancelled(cancel)
                record = state["tasks"].setdefault(task.id, {**asdict(task), "status": "pending", "outputs": {}, "messages": []})
                reusable = record["status"] == "success" and bool(record["outputs"])
                reusable = reusable and all(Path(path).is_file() and file_digest(Path(path)) == digest for path, digest in record["outputs"].items())
                if reusable and not any(state["tasks"].get(dep, {}).get("rerun", False) for dep in task.dependencies):
                    record["rerun"] = False
                else:
                    record.update(status="pending", error="", messages=[], rerun=True)
                    dependencies_ok = all(state["tasks"].get(dep, {}).get("status") == "success" for dep in task.dependencies)
                    errors = [item.message for item in findings if item.severity == "error" and item.asset in {task.source, task.kind, "compiler" if task.kind == "compile" else task.source, "materials" if task.kind == "material" else task.source}]
                    if not dependencies_ok or errors:
                        record.update(status="blocked", error="; ".join(errors) if errors else "A prerequisite failed")
                    else:
                        record["status"] = "running"
                        atomic_json(checkpoint, state)

                        def task_log(message, level="INFO"):
                            record["messages"].append([level, str(message)])
                            record["messages"] = record["messages"][-1000:]
                            log(f"{task.id}: {message}", level)

                        try:
                            outputs = executor(task, options, state["tasks"], task_log, cancel)
                            check_cancelled(cancel)
                            if not outputs or any(not Path(path).is_file() for path in outputs):
                                raise ValueError("Stage did not produce all expected output files")
                            record["outputs"] = {str(Path(path).resolve()): file_digest(Path(path)) for path in outputs}
                            record["status"] = "success"
                        except Exception as exc:
                            if cancel.is_set() or isinstance(exc, PortCancelled):
                                record.update(status="interrupted", error="Cancelled; resume reruns this item")
                                raise PortCancelled() from exc
                            record.update(status="failed", error=str(exc))
                            log(f"{task.id}: {exc}", "ERROR")
                if task.kind == "export" and record["status"] == "success":
                    downstream = discover_tasks(options, exported=True)
                    tasks.extend(downstream)
                    active_ids = {item.id for item in tasks}
                    state["tasks"] = {key: value for key, value in state["tasks"].items() if key in active_ids}
                    findings.extend(preflight(options, downstream))
                    state["findings"] = [asdict(item) for item in findings]
                    if not downstream:
                        record.update(status="failed", error="Export produced no supported assets")
                atomic_json(checkpoint, state)
                write_report(project, state)
                index += 1
                progress(index, len(tasks))
            state["status"] = "success" if tasks and all(record["status"] == "success" for record in state["tasks"].values()) else "incomplete"
        except PortCancelled:
            state["status"] = "interrupted"
        finally:
            state["updated_at"] = time.time()
            atomic_json(checkpoint, state)
            write_report(project, state)
        return state
