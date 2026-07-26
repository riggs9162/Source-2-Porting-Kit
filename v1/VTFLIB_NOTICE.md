# v1 (Legacy) Code - VTF Library Notice

**IMPORTANT:** The v1 codebase (`v1/`) uses the old VTFLibWrapper implementation for VTF file handling.

## Current State

- **v2 application** (`v2/`): Uses `sourcepp` (via Python bindings) for all VTF operations
- **v1 tools** (`v1/`): Still use `VTFLibWrapper` + native VTFLib DLLs

## Why Keep v1 Stable?

The v1 code is preserved as-is to maintain backward compatibility with existing workflows and tool configurations. Migrating v1 tools to sourcepp is not currently planned to avoid disrupting users of these stable utilities.

## For Contributors

- When working on **v2 application** features: Use `sourcepp.vtfpp` exclusively
- When fixing bugs in **v1 tools**: Continue using `VTFLibWrapper` as needed
- Do not mix the two libraries within the same module

## v1 VTFLib Requirements

v1 tools require:

- VTFLib.x64.dll (64-bit) or VTFLib.x86.dll (32-bit) in `v1/VTFLibWrapper/bin/`
- Python wrapper files in `v1/VTFLibWrapper/`

These files are independent from the v2 application and are not removed during migration.
