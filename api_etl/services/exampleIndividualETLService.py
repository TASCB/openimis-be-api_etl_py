import logging
from core.models import User

from api_etl.services.base import ETLService
from api_etl.sources.exampleIndividualSource import ExampleIndividualSource
from api_etl.adapters.exampleIndividualAdapter import ExampleIndividualAdapter

# FIXED imports ↓
from api_etl.sinks.base import DataSink
from api_etl.sinks.individual_import_sink import IndividualImportSink

logger = logging.getLogger(__name__)


class ExampleIndividualETLService(ETLService):
    """
    Example ETL Pipeline:
    Source → Adapter → Sink
    """

    def __init__(self, user: User, source=None, adapter=None, sink=None):
        super().__init__(
            source=source or ExampleIndividualSource(),
            adapter=adapter or ExampleIndividualAdapter(),
            sink=sink or IndividualImportSink(user),
        )
