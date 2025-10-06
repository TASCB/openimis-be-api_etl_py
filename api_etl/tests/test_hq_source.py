# api_etl/tests/test_hq_source.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import io
import os
import zipfile
from unittest import TestCase
from unittest.mock import patch

from api_etl.sources.survey_solutions_export_source import SurveySolutionsExportSource as Source
from api_etl.apps import ApiEtlConfig as C


class _Resp:
    def __init__(self, status=200, json_obj=None, text="", content=b"", stream=False):
        self.status_code = status
        self._json = json_obj
        self.text = text
        self._content = content
        self._stream = stream
        self.reason = "OK"

    def json(self):
        if self._json is None:
            raise ValueError("No JSON")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}: {self.text[:200]}")

    def iter_content(self, chunk_size=1024 * 1024):
        assert self._stream, "iter_content called on non-stream response"
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]


def _build_zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        hh_headers = [
            "interview__key","interview__id","REGIONNAME","REGION_CODE",
            "DISTRICTNAME","DISTRICT_CODE","WARDNAME","WARD_CODE",
            "VILLAGENAME","VILLAGE_CODE","URBANORRURAL","TF4_NO",
        ]
        hh_rows = [{
            "interview__key":"KEY-001","interview__id":"IID-1","REGIONNAME":"Region A","REGION_CODE":"01",
            "DISTRICTNAME":"District X","DISTRICT_CODE":"0101","WARDNAME":"Ward P","WARD_CODE":"010101",
            "VILLAGENAME":"Village Z","VILLAGE_CODE":"01010101","URBANORRURAL":"Urban","TF4_NO":"TF4-100",
        }]
        hh_buf = io.StringIO()
        csv.DictWriter(hh_buf, fieldnames=hh_headers, delimiter="\t").writeheader()
        csv.DictWriter(hh_buf, fieldnames=hh_headers, delimiter="\t").writerows(hh_rows)
        zf.writestr("DODOSO_TESTING.tab", hh_buf.getvalue())

        roster_headers = [
            "interview__key","interview__id","hhroster__id","name","RELATIONSHIPTOHEAD",
            "memberlineno","SEX","DATEOFBIRTH","AGEYEARS","MEMBERIDTYPE","NIDA_NIN","MEMBER_ID_NO",
            "MARITALSTATUS","ATTENDED_SCHOOL","CURRENTINSCHOOL","GRADE",
            "SCHOOL_DISTRICT_CODE","SCHOOL_WARD_CODE","EDUFACILITYCODE_PS","EDUFACILITYCODE_SS",
        ]
        roster_rows = [
            {"interview__key":"KEY-001","interview__id":"IID-1","hhroster__id":"r1","name":"Alice",
             "RELATIONSHIPTOHEAD":"HEAD","memberlineno":"1","SEX":"2","DATEOFBIRTH":"1990-05-06","AGEYEARS":"35",
             "MEMBERIDTYPE":"NID","NIDA_NIN":"123456789","MEMBER_ID_NO":"MID-1","MARITALSTATUS":"S",
             "ATTENDED_SCHOOL":"1","CURRENTINSCHOOL":"0","GRADE":"FORM4",
             "SCHOOL_DISTRICT_CODE":"0101","SCHOOL_WARD_CODE":"010101","EDUFACILITYCODE_PS":"PS-01","EDUFACILITYCODE_SS":"SS-01"},
            {"interview__key":"KEY-001","interview__id":"IID-1","hhroster__id":"r2","name":"Bob",
             "RELATIONSHIPTOHEAD":"SPOUSE","memberlineno":"2","SEX":"1","DATEOFBIRTH":"1992-12-11","AGEYEARS":"32",
             "MEMBERIDTYPE":"NID","NIDA_NIN":"987654321","MEMBER_ID_NO":"MID-2","MARITALSTATUS":"M",
             "ATTENDED_SCHOOL":"1","CURRENTINSCHOOL":"0","GRADE":"FORM2",
             "SCHOOL_DISTRICT_CODE":"0101","SCHOOL_WARD_CODE":"010101","EDUFACILITYCODE_PS":"PS-01","EDUFACILITYCODE_SS":"SS-01"},
        ]
        r_buf = io.StringIO()
        w = csv.DictWriter(r_buf, fieldnames=roster_headers, delimiter="\t")
        w.writeheader(); w.writerows(roster_rows)
        zf.writestr("hhroster.tab", r_buf.getvalue())
    return buf.getvalue()


class SurveySolutionsHQSourceTests(TestCase):
    def setUp(self):
        base = os.environ.get("DJANGO_TEST_TMPDIR") or os.environ.get("TMPDIR") or "/tmp"
        self.tmp = os.path.join(base, "ss_exports_test")
        os.makedirs(self.tmp, exist_ok=True)

        # Neutralize any Admin/DB config that would filter tabs
        C.export_tab_name_contains = None
        # Keep ZIP cleanup predictable in tests
        C.export_tmp_dir = self.tmp
        C.export_keep_zip = False

    @patch("api_etl.sources.survey_solutions_export_source.requests.get")
    def test_list_questionnaires_success(self, m_get):
        m_get.return_value = _Resp(
            200,
            json_obj=[
                {"Id":"98bf9e3a-e9fd-47a2-998a-27ff7d61ee7e","Version":1,"Title":"DODOSO LA KAYA  PILOT"},
                {"Id":"f4c32e8c-6d61-4141-b665-263578c5a211","Version":1,"Title":"UHAKIKI R3 - SHINYANGA MC"},
            ],
        )
        src = Source()
        out = src.list_questionnaires(
            base_url="http://hq.example:9700",
            workspace="openimis",
            api_prefix="/api/v1",
            auth_type="basic",
            username="u",
            password="p",
        )
        self.assertEqual(out[0]["Identity"], "98bf9e3a-e9fd-47a2-998a-27ff7d61ee7e$1")
        self.assertEqual(out[1]["Identity"], "f4c32e8c-6d61-4141-b665-263578c5a211$1")

    @patch("api_etl.sources.survey_solutions_export_source.requests.get")
    @patch("api_etl.sources.survey_solutions_export_source.requests.post")
    def test_rows_single_qid_with_meta_and_zip(self, m_post, m_get):
        zip_bytes = _build_zip_bytes()

        def _get_side_effect(url, *a, **kw):
            if url.endswith("/export/123"):
                return _Resp(200, json_obj={"ExportStatus": "Completed"})
            if url.endswith("/export/123/file"):
                return _Resp(200, json_obj=None, content=zip_bytes, stream=True)
            return _Resp(404, text=f"Unhandled GET url {url}")

        def _post_side_effect(url, *a, **kw):
            if url.endswith("/export"):
                return _Resp(200, json_obj={"JobId": 123})
            return _Resp(404, text=f"Unhandled POST url {url}")

        m_get.side_effect = _get_side_effect
        m_post.side_effect = _post_side_effect

        src = Source()
        it = src.rows(
            questionnaire_id="98bf9e3ae9fd47a2998a27ff7d61ee7e$1",
            workspace="openimis",
            base_url="http://hq.example:9700",
            api_prefix="/api/v2",
            auth_type="basic",
            username="u",
            password="p",
            tab_name_contains=None,     # explicit: do not filter out any tabs
            yield_source_meta=True,
        )
        rows = list(it)

        self.assertGreaterEqual(len(rows), 3, f"expected at least 3 rows, got {len(rows)}")
        meta = rows[0].get("_source")
        self.assertIsInstance(meta, dict)
        self.assertEqual(meta.get("workspace"), "openimis")
        self.assertEqual(meta.get("questionnaire_id"), "98bf9e3ae9fd47a2998a27ff7d61ee7e$1")
        self.assertTrue(meta.get("endpoint").endswith("/openimis/api/v2"))

        any_roster = next((r for r in rows if "hhroster__id" in r), None)
        self.assertIsNotNone(any_roster)
        self.assertIn("RELATIONSHIPTOHEAD", any_roster)
        self.assertIn("DATEOFBIRTH", any_roster)
        self.assertEqual(any_roster.get("interview__key"), "KEY-001")
