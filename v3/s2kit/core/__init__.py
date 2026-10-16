"""Framework-free application core: state, jobs, history, tool registry.

Nothing in this package imports Flet. Everything here has to stay usable from
a headless CLI or a test, which is what keeps the pipelines testable.
"""
