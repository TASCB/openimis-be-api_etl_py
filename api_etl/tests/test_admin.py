from __future__ import annotations
from django.test import SimpleTestCase, RequestFactory
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from unittest.mock import patch, MagicMock

# Import the real ModelAdmin class (safe; doesn't require DB)
from api_etl.admin import SurveySolutionsConfigAdmin
# We can still pass the real model class to ModelAdmin without creating tables
from api_etl.models import SurveySolutionsConfig


class _DummyConfig:
    """
    Lightweight stand-in for SurveySolutionsConfig rows.
    Behaves like a model instance for the admin actions we call,
    but doesn't touch the DB.
    """
    def __init__(self, **kw):
        self.name = kw.get("name", "HQ-OPENIMIS")
        self.hq_url = kw.get("hq_url", "http://hq.example:9700")
        self.username = kw.get("username", "u")
        self.password = kw.get("password", "p")
        self.questionnaire_id = kw.get(
            "questionnaire_id",
            "98bf9e3a-e9fd-47a2-998a-27ff7d61ee7e$1"
        )
        self.questionnaire_title = kw.get("questionnaire_title", "")
        self.is_active = kw.get("is_active", True)
        self._saved = False
        self._saved_fields = None

    def save(self, update_fields=None):
        self._saved = True
        self._saved_fields = list(update_fields or [])


class AdminActionNoDBTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite()
        # ModelAdmin needs the model class, not an instance
        self.admin = SurveySolutionsConfigAdmin(SurveySolutionsConfig, self.site)

    def _req_with_messages(self):
        req = self.factory.get("/")
        # minimal session/messages plumbing for message_user
        setattr(req, "session", "session")
        msgs = FallbackStorage(req)
        setattr(req, "_messages", msgs)
        return req, msgs

    @patch("api_etl.admin._get_source")
    def test_fetch_questionnaires_lists_titles_in_message(self, m_get_source):
        dummy = _DummyConfig()
        fake_src = MagicMock()
        fake_src.list_questionnaires.return_value = [
            {"Id": "A1", "Version": 1, "Title": "Form A"},
            {"Id": "B2", "Version": 2, "Title": "Form B"},
        ]
        m_get_source.return_value = fake_src

        req, msgs = self._req_with_messages()
        # Pass a simple iterable (admin iterates over queryset only)
        self.admin.fetch_questionnaires(req, [dummy])

        texts = [str(m.message) for m in msgs]
        # Expect a single INFO message containing our forms + normalized ids
        assert any("Form A" in t and "A1$1" in t for t in texts)
        assert any("Form B" in t and "B2$2" in t for t in texts)

    @patch("api_etl.admin._get_source")
    def test_refresh_titles_updates_cached_title(self, m_get_source):
        dummy = _DummyConfig(
            questionnaire_id="98bf9e3a-e9fd-47a2-998a-27ff7d61ee7e$1",
            questionnaire_title=""
        )
        fake_src = MagicMock()
        fake_src.list_questionnaires.return_value = [
            {
                "Id": "98bf9e3a-e9fd-47a2-998a-27ff7d61ee7e",
                "Version": 1,
                "Title": "DODOSO LA KAYA  PILOT",
            }
        ]
        m_get_source.return_value = fake_src

        req, msgs = self._req_with_messages()
        self.admin.refresh_titles(req, [dummy])

        assert dummy._saved is True
        assert "questionnaire_title" in (dummy._saved_fields or [])
        assert dummy.questionnaire_title == "DODOSO LA KAYA  PILOT (v1)"

    def test_modeladmin_constructs_without_db(self):
        # Just a smoke test that ModelAdmin is wired
        assert self.admin.model is SurveySolutionsConfig
