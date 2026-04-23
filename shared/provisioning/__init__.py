"""Tickles company provisioning — 9-step atomic executor + job tracking.

The Paperclip "Provision company workspace" checkbox (Phase 3) funnels into
``executor.run(company_id, slug, template_id, options)``, which runs the nine
steps listed in ``templates/companies/README.md`` and emits progress events
to Paperclip so the UI can show per-step status.

Public entrypoints:

* ``executor.run`` — async, full provisioning.
* ``executor.rollback`` — async, reverses previously completed steps.
* ``templates.load`` — load a template JSON by id.
* ``templates.list_available`` — discover all template files.
* ``jobs.emit`` — POST a progress event to Paperclip.
"""

from __future__ import annotations

from .templates import Template, list_available, load
from .jobs import JobEvent, emit
from .executor import run, rollback, StepResult

__all__ = [
    "Template",
    "list_available",
    "load",
    "JobEvent",
    "emit",
    "run",
    "rollback",
    "StepResult",
]
