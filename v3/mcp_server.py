"""Local stdio MCP server exposing the kit's shared nonvisual tool catalog."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from s2kit.core.agent_catalog import catalog, describe, doctor, prepare
from s2kit.core.agent_jobs import AgentRuntime
from s2kit.core.agent_schema import RequestError


def object_schema(properties: dict, required=()) -> dict:
    """Build strict helper-operation schemas."""
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


async def serve(args) -> None:
    """Serve asynchronous job handles while the event loop remains available."""
    import jsonschema
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    runtime = AgentRuntime(args.state_dir, args.settings)
    operations = {}
    tools = []
    for entry in catalog():
        details = describe(entry["id"])
        name = "s2kit_" + entry["id"].replace("-", "_")
        tool_schema = object_schema({"params": details["parameters"], "preferences": details["preferences"]}, ("params",))
        tools.append(types.Tool(name=name, title=entry["title"],
                                description=entry["description"] + " Starts a background job; poll s2kit_status for its result.",
                                inputSchema=tool_schema,
                                annotations=types.ToolAnnotations(readOnlyHint=not entry["writes_files"])))
        operations[name] = (lambda arguments, tool_id=entry["id"]:
                            runtime.start(tool_id, arguments["params"], arguments.get("preferences")))
    string = {"type": "string"}
    cursor = {"type": "integer", "minimum": 0, "default": 0}
    limit = {"type": "integer", "minimum": 1, "maximum": 200, "default": 100}
    helpers = [
        ("catalog", "List nonvisual capabilities", {}, (), lambda a: {"tools": catalog()}),
        ("describe", "Read a tool's schema and help", {"tool_id": string}, ("tool_id",), lambda a: describe(a["tool_id"])),
        ("doctor", "Inspect local dependencies without installing anything", {}, (), lambda a: doctor(args.settings)),
        ("validate", "Validate parameters without running a tool",
         {"tool_id": string, "params": {"type": "object"}, "preferences": {"type": "object"}}, ("tool_id", "params"),
         lambda a: validate(a, args.settings)),
        ("jobs", "List recent jobs across server sessions", {"limit": limit}, (),
         lambda a: {"jobs": runtime.store.list(a.get("limit", 50))}),
        ("status", "Read job status, result tables, metrics and artifacts", {"job_id": string}, ("job_id",),
         lambda a: runtime.store.get(a["job_id"])),
        ("events", "Read paginated events using the previous next_cursor",
         {"job_id": string, "cursor": cursor, "limit": limit}, ("job_id",),
         lambda a: runtime.store.events(a["job_id"], a.get("cursor", 0), a.get("limit", 100))),
        ("cancel", "Request cooperative cancellation and preserve checkpoints", {"job_id": string}, ("job_id",),
         lambda a: runtime.store.cancel(a["job_id"])),
        ("read_artifact", "Read registered text/report artifacts; binary files remain local paths",
         {"job_id": string, "path": string, "cursor": cursor,
          "limit": {"type": "integer", "minimum": 1, "maximum": 65536}}, ("job_id", "path"),
         lambda a: runtime.store.read_artifact(a["job_id"], a["path"], a.get("cursor", 0), a.get("limit", 16384))),
    ]
    for name, description, properties, required, function in helpers:
        name = "s2kit_" + name
        tools.append(types.Tool(name=name, description=description, inputSchema=object_schema(properties, required)))
        operations[name] = function
    schemas = {tool.name: tool.input_schema for tool in tools}
    validators = {name: jsonschema.Draft202012Validator(value) for name, value in schemas.items()}

    async def list_tools(context, params):
        return types.ListToolsResult(tools=tools)

    async def call_tool(context, params):
        try:
            if params.name not in operations:
                raise RequestError({"tool": "Unknown tool"})
            arguments = params.arguments if params.arguments is not None else {}
            validators[params.name].validate(arguments)
            result = await asyncio.to_thread(operations[params.name], arguments)
            return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                        structuredContent=result)
        except (RequestError, jsonschema.ValidationError) as exc:
            result = {"status": "invalid", "errors": exc.errors if isinstance(exc, RequestError) else {"schema": exc.message}}
        except Exception as exc:
            logging.exception("MCP operation failed")
            result = {"status": "failed", "errors": {"runtime": str(exc)}}
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result))],
                                    structuredContent=result, isError=True)

    server = Server("Source 2 Porting Kit", version="3", on_list_tools=list_tools, on_call_tool=call_tool,
                    get_tool_input_schema=schemas.get,
                    instructions="Use the catalog and schemas to select a tool. Start one job, poll status/events, and read its reports. "
                                 "Resume port-workflow using the same project and parameters. Installation defaults off.")
    try:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        await asyncio.to_thread(runtime.close)


def validate(arguments: dict, settings: Path | None) -> dict:
    """Return the exact normalized values a subsequent execution would use."""
    params, preferences = prepare(arguments["tool_id"], arguments["params"], arguments.get("preferences"), settings)
    return {"valid": True, "params": params, "preferences": preferences}


def main() -> int:
    """Start a local server with SDK-managed protocol-only stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--settings", type=Path)
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        print("MCP requires Python 3.11+; the CLI supports Python 3.10+.", file=sys.stderr)
        return 2
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        asyncio.run(serve(args))
        return 0
    except ImportError as exc:
        print(f"MCP dependency unavailable: {exc}. Install v3/requirements-mcp.txt.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
