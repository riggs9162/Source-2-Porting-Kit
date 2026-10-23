# VRF integration

`vrf/export` invokes Source2Viewer-CLI with explicit arguments and a cancellable
process tree. VPK inputs need a nonempty path filter. glTF is the default model
format; source mode performs ordinary decompilation. Animations are opt-in and
worker count defaults to one. Export success also requires at least one output
file. Results use the original VRF directory layout.

`vrf/inspect` saves DATA, RERL, PHYS, REDI, or NTRO text from a loose compiled
resource. It rejects archive/folder inputs. Both tools take an executable override
or use the global Source2Viewer-CLI path. They never launch the GUI executable.

Direct .NET VRF renderer hosting, preview memory management, dependency-aware
material extraction, and the V3 source-layout pipeline are separate future work.
