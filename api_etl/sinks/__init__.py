from .base import DataSink  

try:
    from .individual_import_sink import IndividualImportSink  # canonical sink
except Exception:
    try:
        from .survey_solutions_sink import IndividualImportSink  # back-compat shim
    except Exception:
        IndividualImportSink = None

__all__ = ["DataSink"]
if IndividualImportSink:
    __all__.append("IndividualImportSink")
