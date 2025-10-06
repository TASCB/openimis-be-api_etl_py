from api_etl.sources.base import DataSource

try:
    from api_etl.sources.exampleIndividualSource import ExampleIndividualSource
except Exception:
    ExampleIndividualSource = None

try:
    from api_etl.sources.survey_solutions_export_source import SurveySolutionsExportSource
except Exception:
    SurveySolutionsExportSource = None

__all__ = ["DataSource"]

if ExampleIndividualSource is not None:
    __all__.append("ExampleIndividualSource")

if SurveySolutionsExportSource is not None:
    __all__.append("SurveySolutionsExportSource")
