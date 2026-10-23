# Agent access

Read [AGENT_ACCESS.md](AGENT_ACCESS.md) before operating Source Backport.
Prefer its MCP tools or `Backport.cmd` over desktop automation. Both use the same
backend, settings, add-ons, job queue, and outputs as the desktop application.

Discover capabilities with `Backport.cmd operations` and `Backport.cmd list`.
Describe and validate a tool before submitting unfamiliar inputs. Long jobs should
use `submit`, followed by `jobs get` or `jobs wait`; inspect artifacts and logs to
verify the result. A queued job is not proof of a successful conversion.

Build source changes with `Build.ps1` before running them. `Build.ps1 -AgentsOnly`
builds the headless entry points and add-ons without rebuilding the desktop.
Restart a running backend to load changed binaries; `server stop` refuses to stop
active jobs or close a desktop-owned backend. Do not bypass that check.

Only implemented V4 operations are exposed. Older Python versions are separate
applications. Do not claim their pipelines exist in V4 or silently invoke them.
Add processing features as add-on-owned `ITool` contributions with parameter
schemas; discovery automatically exposes them through both agent interfaces.

Keep validation harnesses and fixtures in ignored `tests/`. Do not commit them.
