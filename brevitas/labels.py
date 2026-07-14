"""
Contextvar-based label propagation for tracking pipeline/agent/run_id.

Resolution order (highest to lowest priority):
1. Per-call _brevitas_meta override
2. Contextvar value (set by start_run/agent context managers)
3. Default (empty string)
"""
from contextvars import ContextVar
from contextlib import contextmanager
import os
from pathlib import Path
import secrets
from typing import Optional, Dict, Any


# Context variables for label propagation
_pipeline_var: ContextVar[str] = ContextVar("brevitas_pipeline", default="")
_agent_var: ContextVar[str] = ContextVar("brevitas_agent", default="")
_run_id_var: ContextVar[str] = ContextVar("brevitas_run_id", default="")


def start_run(pipeline: str = "", run_id: str = "") -> str:
    """
    Start a new run context, setting pipeline and run_id labels.
    Auto-generates run_id if not provided.
    Returns the run_id.
    """
    if not run_id:
        run_id = "run_" + secrets.token_urlsafe(12)

    _pipeline_var.set(pipeline)
    _run_id_var.set(run_id)
    return run_id


def get_pipeline() -> str:
    """Get the current pipeline label from contextvar."""
    return _pipeline_var.get("")


def get_agent() -> str:
    """Get the current agent label from contextvar."""
    return _agent_var.get("")


def get_run_id() -> str:
    """Get the current run_id label from contextvar."""
    return _run_id_var.get("")


@contextmanager
def agent(agent_name: str):
    """
    Context manager to set the agent label for all calls within the block.
    Example:
        with agent("copywriter"):
            client.messages.create(...)  # auto-tagged with agent="copywriter"
    """
    token = _agent_var.set(agent_name)
    try:
        yield
    finally:
        _agent_var.reset(token)


def resolve_labels(
    _brevitas_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """
    Resolve labels using the priority order:
    1. Per-call _brevitas_meta override (highest)
    2. Contextvar value (set by start_run/agent)
    3. Default empty string (lowest)

    Args:
        _brevitas_meta: Optional per-call override dict with 'pipeline', 'agent', 'run_id'

    Returns:
        Dict with 'pipeline', 'agent', 'run_id' keys
    """
    _brevitas_meta = _brevitas_meta or {}

    project = (_brevitas_meta.get("project") or _brevitas_meta.get("repo")
               or os.getenv("BREVITAS_PROJECT") or os.getenv("BREVITAS_REPO")
               or _git_root_name())
    source = (_brevitas_meta.get("source") or _brevitas_meta.get("client")
              or os.getenv("BREVITAS_SOURCE") or os.getenv("BREVITAS_CLIENT") or "sdk")
    return {
        "project": project, "repo": project,
        "environment": _brevitas_meta.get("environment") or os.getenv("BREVITAS_ENVIRONMENT", ""),
        "source": source, "client": source,
        "pipeline": _brevitas_meta.get("pipeline") or get_pipeline(),
        "agent": _brevitas_meta.get("agent") or get_agent(),
        "call_site_id": _brevitas_meta.get("call_site_id", ""),
        "framework": _brevitas_meta.get("framework", ""),
        "gateway": _brevitas_meta.get("gateway", ""),
        "run_id": _brevitas_meta.get("run_id") or get_run_id(),
    }


def _git_root_name() -> str:
    """Return only the local Git-root folder name; never a path or remote."""
    here = Path.cwd().resolve()
    for directory in (here, *here.parents):
        if (directory / ".git").exists():
            return directory.name
    return here.name
