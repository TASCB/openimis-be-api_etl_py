# api_etl/converters/__init__.py
"""
Converter pattern for batch data transformation.

Converters provide an alternative to row-by-row adapters for scenarios where:
- Multiple questionnaire types need different transformation logic
- Batch validation is required before transformation
- Invalid records need to be filtered out
- Pre/post processing hooks are needed

Use adapters for: Simple row-by-row streaming
Use converters for: Batch processing with validation
"""
