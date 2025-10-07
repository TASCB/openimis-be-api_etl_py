# Survey Solutions to openIMIS ETL System - High-Level Overview

## System Purpose

This ETL (Extract, Transform, Load) system integrates household survey data from Survey Solutions HQ (World Bank's CAPI platform) into openIMIS.
It automates the complete data pipeline from survey collection to beneficiary enrollment.

---

## File-by-File Overview

### **Core Application Files**

#### `apps.py` - Configuration Hub

**What it does:** Central configuration management for the entire ETL system

- **178 configuration parameters** covering authentication, field mappings, API settings
- **Dynamic configuration loading** from database with fallback defaults
- **Runtime reconfiguration** without code changes
- **Environment-specific settings** (dev/staging/production)

**Key configurations:**

- Survey Solutions HQ connection details
- 70+ field mappings between survey and openIMIS formats
- Workflow settings and timeout configurations
- Authentication methods (basic, bearer, none)

#### `models.py` - Data Storage & Audit

**What it does:** Database models for configuration storage and comprehensive audit trails

**SurveySolutionsConfig Model:**

- Stores Survey Solutions HQ connection settings
- Supports multiple HQ instances
- Questionnaire version tracking

**PulledHistory Model:**

- **Complete ETL execution audit trail**
- PAA (Planning Area Administrative) information
- Import statistics (households/individuals inserted/updated)
- Error tracking and status management
- JSON metadata storage for forensic analysis

#### `utils.py` - Core Utilities

**What it does:** Essential support functions for the ETL pipeline

**Key functions:**

- **Batch identification:** Timestamped unique IDs for ETL runs
- **CSV file generation:** In-memory CSV creation for bulk imports
- **Tab file processing:** Survey Solutions ZIP file parsing
- **Date normalization:** Flexible date format conversion
- **Class discovery:** Dynamic ETL service loading

---

### **ETL Pipeline Components**

#### `services/base.py` - ETL Orchestrator

**What it does:** Abstract base class defining the ETL contract

- **Source → Adapter → Sink pattern** implementation
- **Streaming data processing** with generator-based pipeline
- **Error handling** and logging framework
- **Batch processing** coordination

#### `services/survey_solution_service.py` - Main ETL Service

**What it does:** Complete ETL pipeline orchestration with advanced features

**Core capabilities:**

- **PAA questionnaire matching** with intelligent scoring algorithms
- **Multi-questionnaire discovery** and automatic selection
- **PMT integration pipeline** for poverty scoring
- **Comprehensive error recovery** with multiple fallback strategies
- **ETL execution history tracking** with detailed audit trails

**Questionnaire matching strategies:**

- Exact district code matching (score: 100)
- Exact district name matching (score: 90)
- Fuzzy name matching (score: 80)
- Region-based matching (score: 70-60)
- Pattern-based matching (score: 50)

---

### **Source Layer (Data Extraction)**

#### `sources/base.py` - Source Interface

**What it does:** Abstract interface for data sources

- Defines standard **pull()** method for data extraction
- **Generator-based streaming** to handle large datasets
- **Batch identification** for tracking

#### `sources/survey_solutions_export_source.py` - Survey Solutions Connector

**What it does:** Extracts data from Survey Solutions HQ via REST API

**Complete export lifecycle:**

1. **Questionnaire discovery** - Lists available questionnaires
2. **Export job creation** - Submits asynchronous export requests
3. **Status monitoring** - Polls job completion with configurable timeouts
4. **File download** - Retrieves ZIP files with survey data
5. **Data parsing** - Processes .tab files within ZIP archives

**Advanced features:**

- **Retry logic** with exponential backoff
- **Fallback mechanisms** to reuse existing completed exports
- **Tab filtering** to include/exclude specific data tables
- **Authentication support** (basic, bearer, no-auth)
- **BOM-safe processing** for international character support

---

### **Adapter Layer (Data Transformation)**

#### `adapters/survey_solutions_targeting_adapter.py` - Data Transformer

**What it does:** Transforms Survey Solutions data into openIMIS Individual format

**Comprehensive transformation:**

- **70+ configurable field mappings** (names, demographics, location, health, education, assets)
- **Zero-data-loss design** - all original data preserved in json_ext
- **Robust field extraction** with BOM-safe, case-insensitive processing
- **Location code composition** from hierarchical geographic data
- **Household role mapping** with gender-aware logic
- **Group code generation** for household clustering

**Key transformations:**

- Gender normalization (various formats → M/F)
- Date standardization (multiple formats → YYYY-MM-DD)
- Location hierarchy composition (Region-District-Ward-Village codes)
- Household relationship mapping (numeric codes → descriptive roles)
- Name processing with multiple fallback strategies

---

### **Sink Layer (Data Loading)**

#### `sinks/individual_import_sink.py` - openIMIS Data Loader

**What it does:** Loads transformed data into openIMIS with workflow integration

**Import capabilities:**

- **Automatic mode detection** (single vs bulk import)
- **CSV generation** for bulk operations with smart field selection
- **Workflow integration** with 6 different resolution strategies
- **Auto-trigger workflows** for immediate approval processing
- **Field padding** and normalization for openIMIS compatibility

### **Workflow System**

#### `workflows/targeting.py` - PMT Enrichment

**What it does:** Post-ETL processing for Proxy Means Test (poverty scoring)

**Workflow steps:**

1. **Data loading** from uploaded CSV files
2. **PMT enrichment** using external poverty scoring algorithms
3. **Data projection** for final import (removes json_ext duplication)
4. **Validation workflow triggering** for approval processes

**Features:**

- **CSV parsing** with json_ext field expansion
- **Code padding** for standardized field formats
- **PMT field promotion** to top-level columns
- **Automatic workflow chaining** for complete processing

---

## **Data Flow Summary**

```
1. EXTRACT (Source)
   Survey Solutions HQ → REST API → Export Job → ZIP Download → Tab Parsing

2. TRANSFORM (Adapter)
   Raw Survey Data → Field Mapping → Normalization → openIMIS Individual Format

3. LOAD (Sink)
   Individual Objects → CSV Generation → Bulk Import → Workflow Trigger → Database Storage
```

## **System Integration Points**

### **External Systems**

- **Survey Solutions HQ:** Data source via REST API and Graphql
- **openIMIS Database:** Target system for individual/household data
- **PMT Service:** Poverty scoring integration
- **Workflow Engine:** Approval process automation

### **Internal Dependencies**

- **Django Framework:** Web framework and ORM
- **PostgreSQL:** Database storage
- **Python Libraries:** requests, zipfile, csv, json processing

---

## **Use Cases**

### **Primary Use Case: Household Survey Integration**

1. Survey data collected via Survey Solutions mobile app
2. Data exported from Survey Solutions HQ
3. ETL system transforms and loads data into openIMIS
4. PMT enrichment adds poverty classifications
5. Workflow approval processes validate data
6. Beneficiaries become available for program enrollment
