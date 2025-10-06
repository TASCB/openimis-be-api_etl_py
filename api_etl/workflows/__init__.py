# api_etl/workflows/__init__.py
try:
    from .targeting import TargetingWorkflow  # noqa: F401
except Exception:
    TargetingWorkflow = None  # allows importing this package without failing

__all__ = []
if TargetingWorkflow:
    __all__.append("TargetingWorkflow")
