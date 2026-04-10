# Legacy Code - VTF Library Notice

**IMPORTANT:** The legacy codebase (`legacy/`) uses the old VTFLibWrapper implementation for VTF file handling.

## Current State

- **Main application** (`main/`): Uses `sourcepp` (via Python bindings) for all VTF operations
- **Legacy tools** (`legacy/`): Still use `VTFLibWrapper` + native VTFLib DLLs

## Why Keep Legacy Stable?

The legacy code is preserved as-is to maintain backward compatibility with existing workflows and tool configurations. Migrating legacy tools to sourcepp is not currently planned to avoid disrupting users of these stable utilities.

## For Contributors

- When working on **main application** features: Use `sourcepp.vtfpp` exclusively
- When fixing bugs in **legacy tools**: Continue using `VTFLibWrapper` as needed
- Do not mix the two libraries within the same module

## Legacy VTFLib Requirements

Legacy tools require:
- VTFLib.x64.dll (64-bit) or VTFLib.x86.dll (32-bit) in `legacy/VTFLibWrapper/bin/`
- Python wrapper files in `legacy/VTFLibWrapper/`

These files are independent from the main application and are not removed during migration.
