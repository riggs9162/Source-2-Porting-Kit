# Utilities

`utilities/environment` records .NET/OS information, this host process's working
set and managed heap, cache usage, configured native tools, and provider IDs.
Those numbers describe this host at the instant of the report, not future map
or conversion performance.

`utilities/inventory` streams a JSON Lines file inventory with an optional SHA-256
checksum per file. It skips reparse points, limits the number of files, and checks
cancellation between files and during hashing. Source files are read-only.
