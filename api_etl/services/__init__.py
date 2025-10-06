# api_etl/services/__init__.py
"""
Expose ETL service classes at the package level so helpers like:
    get_class_by_name("api_etl.services", "SurveySolutionService")
can resolve them.

- Eagerly imports known services (fast path).
- Auto-discovers any other *Service classes in sibling modules.
- Stays resilient: failures are logged but don't crash imports.
"""

from importlib import import_module
import inspect
import logging
import pkgutil
import sys

logger = logging.getLogger(__name__)

# Base type is always present
from .base import ETLService  # noqa: F401

__all__ = ["ETLService"]
_EXPORTS = {}


def _export(cls):
    """Export a service class under its class name."""
    name = cls.__name__
    globals()[name] = cls
    if name not in __all__:
        __all__.append(name)
    _EXPORTS[name] = cls


def _try_import(modname: str, clsname: str | None = None):
    """Import a sibling module and export requested class (or all *Service classes)."""
    try:
        m = import_module(f".{modname}", package=__name__)
    except Exception as e:
        logger.debug("api_etl.services: failed to import %s: %s", modname, e)
        return

    if clsname and hasattr(m, clsname):
        _export(getattr(m, clsname))
        return

    # Export all concrete *Service subclasses defined in this module
    for name, obj in inspect.getmembers(m, inspect.isclass):
        if (
            name.endswith("Service")
            and obj.__module__ == m.__name__
            and ETLService in getattr(obj, "__mro__", ())
        ):
            _export(obj)


# --- Fast path: known modules (keep names in sync with filenames) ---
_try_import("survey_solution_service", "SurveySolutionService")
_try_import("exampleIndividualETLService", "ExampleIndividualETLService")  # optional


# --- Discovery: scan other modules in this package for *Service classes ---
def _discover():
    pkg = sys.modules[__name__]
    if not hasattr(pkg, "__path__"):
        return
    for _, modname, ispkg in pkgutil.iter_modules(pkg.__path__):
        if ispkg or modname.startswith("_"):
            continue
        # Skip ones we already handled explicitly; harmless if repeated
        _try_import(modname)


_discover()


def __getattr__(name: str):
    """
    Lazy lookup: if someone asks for FooService, try another discovery pass.
    """
    if name in _EXPORTS:
        return _EXPORTS[name]
    if name.endswith("Service"):
        _discover()
        if name in _EXPORTS:
            return _EXPORTS[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
