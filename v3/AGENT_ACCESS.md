# Agent access

The CLI and local stdio MCP server expose all 19 nonvisual tools. They use the
same schemas, preferences and executors as the desktop/browser app. Hotspot
Editor remains interactive. Neither entry point launches Flet or installs
dependencies automatically.

## Setup

Use Python 3.10+ for the CLI, or Python 3.11+ for MCP. Install into your preferred
virtual environment:

```console
python -m pip install -r v3/requirements-headless.txt
python -m pip install -r v3/requirements-mcp.txt
python v3/cli.py doctor --json
```

The second requirements file includes the first. VRF extraction requires
Source2Viewer-CLI; compilation requires StudioMDL and a game folder containing
`gameinfo.txt`; audio conversion requires FFmpeg. `doctor` reports availability
and configured executable paths. Dependencies are checked for the selected
operation rather than requiring every external program to run every tool.

## CLI

Run commands from the repository root, or use an absolute script path from any
working directory. Relative paths inside requests resolve against that working
directory.

```console
python v3/cli.py list --json
python v3/cli.py describe port-workflow --json
python v3/cli.py validate port-workflow --params port.json --json
python v3/cli.py run port-workflow --params port.json --json
python v3/cli.py jobs list --json
python v3/cli.py jobs status JOB_ID --json
python v3/cli.py jobs events JOB_ID --cursor 0 --limit 100 --json
python v3/cli.py jobs cancel JOB_ID --json
python v3/cli.py jobs artifact JOB_ID "A:/ports/example/port-report.json" --json
```

Replace `JOB_ID` with the returned 12-character ID. `run` waits for completion;
MCP starts the same work asynchronously. Progress goes to stderr. With `--json`,
stdout contains one JSON result. `--params -` reads the parameter object from
stdin. `--preferences overrides.json` supplies request-only preference overrides;
it also accepts `-`, but both inputs cannot consume stdin simultaneously.

Exit codes are **0** success, **1** failed or partial execution, **2** invalid
input or unavailable dependencies, and **130** cancellation. Ctrl+C or `jobs
cancel` cooperatively stops a CLI run and retains completed workflow stages.

This example `port.json` builds a model addon with installation off. `model_type`
is `auto` (the default), `prop` or `ragdoll`:

```json
{
  "model_mode": "ragdoll",
  "model_type": "auto",
  "input_kind": "urls",
  "source_urls": "vpk:S:/SteamLibrary/steamapps/common/Half-Life Alyx/game/hlvr/pak01_dir.vpk:models/creatures/headcrab_classic/headcrab_classic.vmdl_c",
  "project": "A:/ports/headcrab",
  "namespace": "my_ports/headcrab",
  "compiler": "A:/Tools/StudioMDL CE/bin/studiomdl.exe",
  "game": "A:/Tools/StudioMDL CE/csgo",
  "install": false
}
```

Set `action` to `inspect` to prepare the workflow plan and reports without
conversion. Planning writes project reports; `validate` only validates the
request. To resume, rerun the same request against the same project. Existing
workflow locks, converter signatures and output hashes still determine reuse.

Soundscapes use explicit inputs instead of picker state:

```json
{"action": "list", "source": "A:/exports/alyx"}
```

```json
{
  "action": "port",
  "source": "A:/exports/alyx",
  "vpk": "S:/SteamLibrary/steamapps/common/Half-Life Alyx/game/hlvr/pak01_dir.vpk",
  "soundscapes": "a2_pistol_basement\nother_exact_soundscape_name",
  "output": "A:/ports/alyx_ambience",
  "namespace": "alyx_ambience"
}
```

Use one exact soundscape name per line from the listing. Set `extract_audio` to
false when only converting scripts from an exported folder. The UI picker
updates these same explicit names.

## MCP

Launch `python v3/mcp_server.py` through a client's stdio configuration. For
clients using `mcpServers`, an example with absolute Windows paths is:

```json
{
  "mcpServers": {
    "source2-porting-kit": {
      "command": "A:/venvs/s2kit/Scripts/python.exe",
      "args": ["A:/GameMods/Source2/Source 2 Porting Kit/v3/mcp_server.py"]
    }
  }
}
```

Point `command` at the interpreter where you installed the MCP requirements.
There is no listening network port. Each server permits one active conversion;
another start receives a busy error while inspection and cancellation remain
available. Different server processes remain independent; workflow projects
retain their existing cross-process locks.

Tool names have a `s2kit_` prefix and underscores, for example
`s2kit_port_workflow` or `s2kit_material_builder`. Arguments are
`{"params": {...}, "preferences": {...}}`; preferences are optional. The MCP
input schemas describe each tool's real parameters. Starts return a job ID.

| Operation | Purpose |
| --- | --- |
| `s2kit_catalog`, `s2kit_describe` | Discover tools and their schemas/help |
| `s2kit_validate` | Validate a tool's parameters and preference overrides |
| `s2kit_doctor` | Inspect the headless environment |
| `s2kit_jobs`, `s2kit_status` | List jobs or read a result |
| `s2kit_events` | Read events with `cursor` and `limit` |
| `s2kit_cancel` | Request cancellation |
| `s2kit_read_artifact` | Read a registered text/report artifact |

Poll status until it leaves `running`. Results preserve `success`, `partial`,
`failed`, `cancelled` and interrupted-process outcomes. Logs reporting errors
prevent an otherwise returned job from being labelled successful.

## Results and persistence

Both interfaces read global preferences, then tool preferences, then request
overrides. Schema defaults fill missing values. Last-used GUI inputs are never
replayed implicitly, and requests never save form values or preference changes.
Unknown fields, invalid choices, invalid numeric ranges and incorrect JSON
types are rejected before coercion.

Jobs live under `v3/config/agent-jobs/`, separate from GUI history. Both entry
points accept `--state-dir` and `--settings` to choose another job store or a
read-only settings file. Records include normalized inputs, effective
preferences, status, metrics, tables, artifacts, error messages and timestamps.
Closing the server requests cancellation and drains its events. After an abrupt
process exit, records are reported as interrupted; restart the workflow explicitly
to resume from its existing verified checkpoints.

The event queue holds at most 256 events. Logs stream to JSONL; event reads return
at most 200 entries and an opaque `next_cursor`. Tables contain a 50-row preview,
their full row count and a `data_path` for the complete JSON table. Artifact reads
are restricted to files registered by that job and capped at 64 KiB per call.
Binary outputs are returned as local filesystem paths. Runtime records and local
validation files stay ignored by Git.

Batch execution keeps at most twice the worker count submitted and preserves
input-order results. Workflow converter fingerprints are reused only within one
invocation; the next invocation recomputes them. Texture file export bypasses the
preview renderer, preserving native resolution and alpha unless an explicit
clamp or channel selection changes them.

## Local verification snapshot

Measured on the development PC on 2026-10-19; these describe the exercised
operations rather than a promise about complete porting time:

| Check | Before | After |
| --- | --- | --- |
| Catalog discovery, median of five fresh Python processes | 1.364 s; 18 UI modules | 0.056 s; no UI or NumPy imports |
| Submitted items while three workers are blocked in a 200-item batch | 200 | 6 |
| Source reads for ten native-stage fingerprint lookups in one invocation | 210 | 22, including the shared defaults |

Local validation covered all 19 backend imports with UI imports blocked, strict
requests, bounded event storage, partial failures, cancellation and MCP shutdown/
restart. A material conversion matched all five pre-change output files byte for
byte. A real Source 2 girder export compiled through the CLI workflow with matching
MDL/VVD/VTX checksums; its parsed model audit matched the pre-change build apart
from the compiler-generated checksum. Repeating that workflow reused its outputs
without rewriting model sources or compiled files.
