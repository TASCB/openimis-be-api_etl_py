from .base import DataAdapter  # always present

# Optional example adapter; don't hard-fail if missing
try:
    from .exampleIndividialAdapter import ExampleIndividualAdapter  # noqa: F401
except Exception:
    ExampleIndividualAdapter = None

# Our Survey Solutions targeting adapter
try:
    from .survey_solutions_targeting_adapter import SurveySolutionsTargetingAdapter  # noqa: F401
    # Backward-compat alias if you previously used the misspelled name:
    SurveySolutionsTargetinglAdapter = SurveySolutionsTargetingAdapter  # noqa: N816
except Exception:
    SurveySolutionsTargetingAdapter = None
    SurveySolutionsTargetinglAdapter = None

__all__ = ["DataAdapter"]
if ExampleIndividualAdapter:
    __all__.append("ExampleIndividualAdapter")
if SurveySolutionsTargetingAdapter:
    __all__.append("SurveySolutionsTargetingAdapter")
if SurveySolutionsTargetinglAdapter:
    __all__.append("SurveySolutionsTargetinglAdapter")
