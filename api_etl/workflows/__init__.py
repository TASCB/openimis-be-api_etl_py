# api_etl/workflows/__init__.py
try:
    from .targeting import TargetingWorkflow  # noqa: F401
except Exception:
    TargetingWorkflow = None  #

__all__ = []
if TargetingWorkflow:
    __all__.append("TargetingWorkflow")
