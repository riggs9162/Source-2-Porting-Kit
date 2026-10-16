"""
Porting pipelines: the actual asset work, with no UI attached.

Each module here is framework-free (no Flet, no Qt) and takes an `on_log`
callback plus an optional `threading.Event` for cancellation, so the same code
backs a tool page, a headless CLI and a test. Tool pages in
:mod:`s2kit.tools` are thin: collect parameters, call in here, report what
came back.
"""
