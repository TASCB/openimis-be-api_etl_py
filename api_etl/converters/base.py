"""
api_etl/converters/base.py

Base converter pattern for transforming external data to openIMIS models.

PATTERN ORIGIN: Adapted from openimis-be-kobo_etl_py BaseKoboConverter
GUIDELINE COMPLIANCE:
- Code Reusability: Abstract base class enforces consistent interface
- Enhance, Don't Replace: Coexists with existing adapter pattern
- Keep Diff Minimal: New package, doesn't modify existing code

ARCHITECTURE:
The Converter pattern separates data transformation logic from ETL orchestration.
Each converter handles one specific data source structure (questionnaire type).

DIFFERENCE FROM ADAPTER PATTERN:
- Adapters: Row-by-row transformation (streaming)
- Converters: Batch transformation with validation

WHEN TO USE:
- Multiple questionnaire types with different structures
- Complex validation logic before transformation
- Need to filter/reject invalid records
- Batch processing with pre/post hooks

Example 1: Simple Converter
```python
class MyQuestionnaire Converter(BaseConverter):
    @classmethod
    def to_individual(cls, record, config):
        return Individual(
            first_name=record.get('name'),
            dob=record.get('birth_date'),
            # ... field mappings
        )
```

Example 2: Converter with Validation
```python
class TargetingConverter(BaseConverter):
    @classmethod
    def to_individual(cls, record, config):
        # Validation
        if not record.get('consent'):
            return None  # Skip non-consented

        # Complex transformations
        location_code = cls._build_location_code(record, config)

        return Individual(...)

    @classmethod
    def _build_location_code(cls, record, config):
        # Helper method
        pass
```

Example 3: Using in Service
```python
# In service
from api_etl.converters.targeting_converter import TargetingConverter

raw_rows = source.pull()
individuals = TargetingConverter.to_individuals(raw_rows, config)
sink.push(individuals)
```

Author: Adapted from Kobo ETL pattern
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import logging

LOG = logging.getLogger(__name__)


class BaseConverter(ABC):
    """
    Abstract base class for data converters.

    Converters transform raw external data into openIMIS model instances.
    Each converter handles one specific data structure (e.g., targeting questionnaire).

    Subclasses MUST implement:
    - to_individual(record, config) - Convert one record

    Subclasses CAN override:
    - to_individuals(records, config) - Convert multiple records
    - validate_record(record) - Pre-transformation validation
    - post_process(individual, record) - Post-transformation enrichment
    """

    @classmethod
    @abstractmethod
    def to_individual(
        cls,
        record: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None
    ) -> Optional[Any]:
        """
        Convert a single external record to an openIMIS Individual model instance.

        This method MUST be implemented by subclasses.

        Args:
            record: Raw record from external source (dict)
            config: Configuration dict from ModuleConfiguration

        Returns:
            Individual model instance, or None to skip this record

        Example:
        ```python
        @classmethod
        def to_individual(cls, record, config):
            if not record.get('consent'):
                return None  # Skip

            return Individual(
                first_name=record.get('firstname'),
                last_name=record.get('lastname'),
                dob=cls._normalize_date(record.get('dob')),
                gender=config.get('adapter_gender_map', {}).get(record.get('gender')),
                json_ext=cls._build_json_ext(record, config)
            )
        ```

        Raises:
            NotImplementedError: If not implemented by subclass
        """
        raise NotImplementedError(
            f"{cls.__name__} must implement to_individual(record, config)"
        )

    @classmethod
    def to_individuals(
        cls,
        records: List[Dict[str, Any]],
        config: Optional[Dict[str, Any]] = None,
        *,
        skip_invalid: bool = True,
        log_skipped: bool = True,
    ) -> List[Any]:
        """
        Convert multiple external records to openIMIS Individual instances.

        This method can be overridden to add batch-level processing,
        but the default implementation is usually sufficient.

        Args:
            records: List of raw records from external source
            config: Configuration dict from ModuleConfiguration
            skip_invalid: If True, skip records that return None
            log_skipped: If True, log skipped records

        Returns:
            List of Individual model instances (excludes None/invalid)

        Example:
        ```python
        # Default behavior (usually sufficient)
        individuals = TargetingConverter.to_individuals(raw_rows, config)

        # Custom batch processing
        @classmethod
        def to_individuals(cls, records, config, **kwargs):
            # Pre-processing
            records = cls._deduplicate(records)

            # Convert
            individuals = super().to_individuals(records, config, **kwargs)

            # Post-processing
            individuals = cls._enrich_locations(individuals)

            return individuals
        ```
        """
        if not records:
            return []

        individuals = []
        skipped_count = 0

        for idx, record in enumerate(records):
            try:
                # Optional pre-validation
                if not cls.validate_record(record):
                    if log_skipped:
                        LOG.debug(
                            f"Record {idx} failed validation, skipping: "
                            f"{record.get('id', 'unknown')}"
                        )
                    skipped_count += 1
                    continue

                # Convert record
                individual = cls.to_individual(record, config)

                # Skip if None returned (converter chose to skip)
                if individual is None:
                    if log_skipped:
                        LOG.debug(
                            f"Record {idx} returned None, skipping: "
                            f"{record.get('id', 'unknown')}"
                        )
                    skipped_count += 1
                    continue

                # Optional post-processing
                individual = cls.post_process(individual, record, config)

                individuals.append(individual)

            except Exception as e:
                LOG.error(
                    f"Failed to convert record {idx}: {e}",
                    exc_info=True,
                    extra={"record": record}
                )
                if not skip_invalid:
                    raise
                skipped_count += 1

        LOG.info(
            f"{cls.__name__}: Converted {len(individuals)} individuals, "
            f"skipped {skipped_count}/{len(records)}"
        )

        return individuals

    @classmethod
    def validate_record(cls, record: Dict[str, Any]) -> bool:
        """
        Validate a record before transformation.

        Override this method to add custom validation logic.
        Return False to skip this record.

        Args:
            record: Raw record to validate

        Returns:
            True if record is valid, False to skip

        Example:
        ```python
        @classmethod
        def validate_record(cls, record):
            # Must have consent
            if not record.get('consent'):
                return False

            # Must have required fields
            if not record.get('firstname') or not record.get('lastname'):
                return False

            return True
        ```
        """
        # Default: accept all records
        return True

    @classmethod
    def post_process(
        cls,
        individual: Any,
        record: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None
    ) -> Any:
        """
        Post-process an individual after conversion.

        Override this method to add enrichment, validation, or cleanup.

        Args:
            individual: Converted Individual instance
            record: Original raw record
            config: Configuration dict

        Returns:
            Modified individual instance

        Example:
        ```python
        @classmethod
        def post_process(cls, individual, record, config):
            # Add computed fields
            if individual.json_ext:
                individual.json_ext['source_record_id'] = record.get('_id')

            # Location lookup/enrichment
            if not individual.location and individual.json_ext.get('location_code'):
                individual.location = cls._lookup_location(
                    individual.json_ext['location_code']
                )

            return individual
        ```
        """
        # Default: no post-processing
        return individual


# Convenience type alias
Converter = BaseConverter
