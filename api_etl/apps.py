from django.apps import AppConfig

MODULE_NAME = "api_etl"

DEFAULT_CONFIG = {
    # --- Auth ---
    "auth_type": "basic",  # noauth | basic | bearer
    "auth_basic_username": "",
    "auth_basic_password": "",
    "auth_bearer_token": "",
    # --- Export API (Survey Solutions HQ) ---
    "source_mode": "export_api",
    "export_base_url": "",  # e.g. http://192.xxx.x.15:9700 or http://192.xxx.x.15:9700/workspace
    "export_api_prefix": "/api/v1",
    "export_format": "Tabular",
    "export_interview_status": "All",
    "export_include_meta": False,
    "export_tab_name_contains": "",  # e.g. "individual_test"
    "export_tmp_dir": "/tmp/ss_exports",
    "export_poll_interval_seconds": 3,
    "export_timeout_seconds": 3600,
    "export_reuse_latest_on_5xx": True,
    "export_keep_zip": False,
    # --- PAA ETL execution ---
    # When enabled, GraphQL returns immediately and Celery/RabbitMQ runs the import.
    "paa_etl_async_enabled": True,
    # Keep local/dev usable if the broker is unavailable.
    "paa_etl_fallback_to_sync_on_queue_error": True,
    # Prevent duplicate imports for the same district while a previous one is running.
    "paa_etl_prevent_duplicate_active": True,
    # Optional Celery queue name. Empty string uses the worker default queue.
    "paa_etl_task_queue": "",
    # Mark imports still running after this many hours as failed when history is read
    # or before duplicate-running checks are applied. Counts already saved remain visible.
    "paa_etl_running_timeout_hours": 12,
    # --- Questionnaire default(s) ---
    "questionnaire_id": "",  # single
    "export_questionnaire_ids": [],  # or multiple
    # --- Questionnaire matching configuration ---
    "questionnaire_title_prefix": "DODOSO LA KAYA - RM4-",
    "questionnaire_title_prefixes": [
        "DODOSO LA KAYA - RM4-",
        "DODOSO LA KAYA-RM4-",
        "DODOSO LA KAYA - RM4-",
        "DODOSO LA KAYA -RM4-",
    ],
    "questionnaire_list_cache_seconds": 500,
    "district_name_suffixes": ["DC", "TC", "MC"],
    "questionnaire_district_aliases": {
        "PEMBA": ["PEMBA", "KASKAZINI PEMBA", "KUSINI PEMBA"],
        "UNGUJA": ["UNGUJA", "KASKAZINI UNGUJA", "KUSINI UNGUJA", "MJINI MAGHARIBI"],
    },
    "paa_aliases": {
        "PEMBA": {
            "codes": ["54", "55"],
            "names": ["PEMBA", "KASKAZINI PEMBA", "KUSINI PEMBA"],
        },
        "UNGUJA": {
            "codes": ["51", "52", "53"],
            "names": ["UNGUJA", "KASKAZINI UNGUJA", "KUSINI UNGUJA", "MJINI MAGHARIBI"],
        },
    },
    # --- Adapter field mapping (core) ---
    "adapter_first_name_field": "firstname",
    "adapter_last_name_field": "lastname",
    "adapter_phone_field": "phoneNumber",
    "adapter_gender_field": "gender",
    "adapter_email_field": "email",
    "adapter_location_name_field": "locationName",
    "adapter_location_code_field": "locationCode",
    "adapter_external_id_field": "interview__key",
    "adapter_interview_key_field": "interview_key",
    "adapter_dob_field": "dob",
    "adapter_gender_map": {"1": "M", "2": "F"},
    # --- Adapter field mapping (education, health, WASH, etc.) ---
    "adapter_recipient_info": "recipient_info",
    "adapter_relationship_to_head_field": "relationship_to_head",
    "adapter_id_type_field": "id_type",
    "adapter_national_id_no_field": "national_id_no",
    "adapter_other_id_no_field": "other_id_no",
    "adapter_marital_status_field": "marital_status",
    "adapter_ever_attended_school_field": "ever_attended_school",
    "adapter_currently_in_school_field": "currently_in_school",
    "adapter_current_grade_field": "current_grade",
    "adapter_highest_grade_completed_field": "highest_grade_completed",
    "adapter_literate_field": "literate",
    "adapter_school_district_code_field": "school_district_code",
    "adapter_school_ward_code_field": "school_ward_code",
    "adapter_school_facility_code_ps_field": "school_facility_code_ps",
    "adapter_school_facility_code_ss_field": "school_facility_code_ss",
    "adapter_school_ownership_field": "school_ownership",
    "adapter_schooling_payer_field": "schooling_payer",
    "adapter_school_transport_mode_field": "school_transport_mode",
    "adapter_reason_never_attended_field": "reason_never_attended",
    "adapter_reason_not_in_school_field": "reason_not_in_school",
    "adapter_had_illness_2w_field": "had_illness_2w",
    "adapter_illness_type_multi_field": "illness_type_multi",
    "adapter_health_insurance_type_field": "health_insurance_type",
    "adapter_mvc_benefits_field": "mvc_benefits",
    "adapter_attends_health_facility_field": "attends_health_facility",
    "adapter_hfac_district_code_field": "hfac_district_code",
    "adapter_hfac_ward_code_field": "hfac_ward_code",
    "adapter_health_facility_code_field": "health_facility_code",
    "adapter_health_facility_code_other_field": "health_facility_code_other",
    "adapter_health_facility_name_other_field": "health_facility_name_other",
    "adapter_wg_visual_field": "wg_visual",
    "adapter_wg_hearing_field": "wg_hearing",
    "adapter_wg_mobility_field": "wg_mobility",
    "adapter_wg_memory_field": "wg_memory",
    "adapter_wg_selfcare_field": "wg_selfcare",
    "adapter_wg_communication_field": "wg_communication",
    "adapter_albino_field": "albino",
    "adapter_employment_status_field": "employment_status",
    "adapter_region_name_field": "region_name",
    "adapter_region_code_field": "region_code",
    "adapter_district_name_field": "district_name",
    "adapter_district_code_field": "district_code",
    "adapter_ward_name_field": "ward_name",
    "adapter_ward_code_field": "ward_code",
    "adapter_village_name_field": "village_name",
    "adapter_village_code_field": "village_code",
    "adapter_settlement_type_field": "settlement_type",
    "adapter_tf4_no_field": "tf4_no",
    "adapter_hh_serial_field": "hh_serial",
    "adapter_veo_name_field": "veo_name",
    "adapter_veo_phone_field": "veo_phone",
    "adapter_supervisor_field": "supervisor",
    "adapter_hh_status_field": "hh_status",
    # GPS (if exported as numeric columns)
    # "adapter_gps_lat_field": "gps__lat",
    # "adapter_gps_lon_field": "gps__lng",
    # "adapter_gps_alt_field": "gps__alt",
    # "adapter_gps_acc_field": "gps__acc",
    "adapter_consent_field": "consent",
    "adapter_pssn_wave_field": "pssn_wave",
    "adapter_interview_date_field": "interview_date",
    "adapter_household_size_field": "household_size",
    "adapter_household_photo_url_field": "household_photo_url",
    "adapter_household_representative_field": "household_representative",
    "adapter_assets_owned_field": "assets_owned",
    "adapter_primary_saving_channel_field": "primary_saving_channel",
    "adapter_preferred_payment_mode_field": "preferred_payment_mode",
    "adapter_bank_code_field": "bank_code",
    "adapter_bank_account_no_field": "bank_account_no",
    "adapter_mobile_network_field": "mobile_network",
    "adapter_mobile_msisdn_field": "mobile_msisdn",
    "adapter_floor_material_field": "floor_material",
    "adapter_wall_material_field": "wall_material",
    "adapter_roof_material_field": "roof_material",
    "adapter_rooms_total_field": "rooms_total",
    "adapter_rooms_sleeping_field": "rooms_sleeping",
    "adapter_has_kitchen_field": "has_kitchen",
    "adapter_grid_connected_field": "grid_connected",
    "adapter_lighting_fuel_field": "lighting_fuel",
    "adapter_cooking_fuel_field": "cooking_fuel",
    "adapter_tenure_status_field": "tenure_status",
    "adapter_income_support_sources_field": "income_support_sources",
    "adapter_food_support_sources_field": "food_support_sources",
    "adapter_toilet_type_field": "toilet_type",
    "adapter_toilet_shared_with_field": "toilet_shared_with",
    "adapter_handwashing_place_field": "handwashing_place",
    "adapter_drinking_water_source_field": "drinking_water_source",
    "adapter_water_treatment_field": "water_treatment",
    "adapter_interview_result_field": "interview_result",
    # --- Derived/household grouping (used by adapter logic) ---
    "group_code_prefix": "P3",
    "group_code_midfix": "000",
    # --- Sink (import into openIMIS Individuals) ---
    # Upsert by json_ext.external_id (stable interview key)
    "sink_model_lookup_field": "json_ext__external_id",
    "sink_update_existing": True,
    # Workflow handler to run after upload (short name or dotted class)
    "sink_workflow": "api_etl.workflows.targeting.TargetingWorkflow",
    # Grouping column for bulk importer (fallback handled in sink)
    "sink_group_aggregation_column": "location_code",
    # CSV columns passed to bulk importer.
    # Includes group_code, individual_role, individual_role_code, and hhrep so
    # the role/recipient mapping at group-link time can see them in
    # Individual.Json_ext, plus external_id + json_ext for idempotency +
    # no-loss payload.
    "sink_csv_fields": [
        "first_name",
        "last_name",
        "dob",
        "gender",
        "location_name",
        "location_code",
        "phone",
        "email",
        "interview_key",
        "group_code",
        "individual_role",
        "individual_role_code",
        "hhrep",
        "external_id",
        "json_ext",
    ],
    # Optional: auto-trigger the workflow after upload
    "sink_trigger_workflow_after_upload": False,
    # Maker-checker for ETL imports. When True the sink stages the data and
    # creates a tasks_management approval task instead of importing immediately;
    # the actual insert/update runs only after a checker approves the task in the
    # Tasks UI. When False (default) the import runs straight through.
    "sink_enable_maker_checker": False,
    # --- Real-time Survey Monitoring Dashboard ---
    "dashboard_enabled": True,
    # How "fresh" the cache must be before a dashboard query self-heals (seconds).
    "dashboard_poll_interval_seconds": 60,
    # Page size when paging the HQ /api/v1/interviews endpoint.
    "dashboard_interview_page_size": 200,
    # Safety cap so a runaway HQ doesn't blow up memory.
    "dashboard_max_interviews": 50000,
    # Planned target total for the cumulative completion (S-curve) chart; 0 = auto.
    "dashboard_target_total": 0,
    # An enumerator counts as "active" if they synced within this many hours.
    "dashboard_active_window_hours": 24,
    # Number of recent interview cards to show in the live feed.
    "dashboard_feed_size": 50,
    # Headline KPIs come from cheap per-status TotalCount queries; the live feed /
    # leaderboard / heatmap come from a *bounded sample* of interview briefs (HQ
    # offers no recency sort, so we sample a few pages per "interesting" status).
    "dashboard_sample_size": 350,
    # Cap on cached rows scanned in Python when building the leaderboard/heatmap.
    "dashboard_metrics_row_cap": 5000,
    # Optional questionnaire question identifier/variable for a numeric household-size metric.
    "dashboard_household_size_question_key": "",
    "dashboard_household_size_variable": "hh_size",
    # Cache TTLs for optional HQ enrichments.
    "dashboard_roster_cache_seconds": 600,
    "dashboard_question_stats_cache_seconds": 600,
    # HQ HTTP timeouts (seconds): short connect so an unreachable HQ fails fast.
    "dashboard_hq_connect_timeout": 5,
    "dashboard_hq_read_timeout": 60,
    # After a failed poll, wait this long before the dashboard self-heal re-polls
    # (keeps an unreachable HQ from stalling every page load on the connect timeout).
    "dashboard_poll_fail_backoff_seconds": 300,
    # --- GraphQL perms ---
    "gql_query_api_etl_rule_perms": ["953001"],
    "gql_mutation_execute_api_etl_rule_perms": ["953002"],
    # --- Misc ---
    "skip_integration_test": False,
}


class ApiEtlConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = MODULE_NAME
    label = MODULE_NAME
    verbose_name = "API ETL"

    # Keep a copy of merged config here
    config = DEFAULT_CONFIG.copy()

    # Convenience: computed endpoint base (export_base_url + export_api_prefix)
    export_endpoint_base = None

    @classmethod
    def _load_config(cls, cfg: dict):
        """
        Merge DB config onto class:
        - set every key as an attribute (even if not predeclared)
        - maintain a full dict copy in `config`
        - compute `export_endpoint_base`
        """
        if not isinstance(cfg, dict):
            cfg = {}

        # Keep full copy
        cls.config = {**DEFAULT_CONFIG, **cfg}

        # Set every key dynamically
        for k, v in cls.config.items():
            setattr(cls, k, v)

        # Compute endpoint base once for convenience: {export_base_url}{export_api_prefix}
        base = (cls.export_base_url or "").rstrip("/")
        prefix = (cls.export_api_prefix or "").strip()
        if prefix and not prefix.startswith("/"):
            prefix = "/" + prefix
        cls.export_endpoint_base = (base + prefix) if base else prefix or None

    def ready(self):
        # Load or create the ModuleConfiguration record and apply it.
        from core.models import ModuleConfiguration

        cfg = ModuleConfiguration.get_or_default(self.name, DEFAULT_CONFIG.copy())
        self._load_config(cfg)
