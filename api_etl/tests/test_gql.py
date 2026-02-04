# api_etl/tests/test_gql.py
from __future__ import annotations
import unittest
from unittest.mock import patch
import graphene

from api_etl.schema import Query, Mutation
from api_etl.gql_mutations import ETLServiceMutation


class _User:
    id = 1
    def has_perms(self, perms):  # allow both query & mutation perms
        return True


class GraphQLTests(unittest.TestCase):
    def setUp(self):
        self.schema = graphene.Schema(query=Query, mutation=Mutation)

    @patch("api_etl.schema.get_classes_in_module", return_value=["SurveySolutionService", "OtherService"])
    def test_query_list_all_services(self, _mock_list):
        q = """
        query {
          etlServicesByServiceName {
            etlServices { nameOfService }
          }
        }
        """
        # context only needs a 'user' with the right perms
        class Ctx: user = _User()
        res = self.schema.execute(q, context_value=Ctx())
        self.assertIsNone(res.errors, msg=res.errors)
        names = [n["nameOfService"] for n in res.data["etlServicesByServiceName"]["etlServices"]]
        self.assertIn("SurveySolutionService", names)
        self.assertIn("OtherService", names)

    @patch("api_etl.schema.get_class_by_name", return_value=type("SurveySolutionService", (), {}) )
    def test_query_single_service(self, _mock_get):
        q = """
        query {
          etlServicesByServiceName(nameOfService:"SurveySolutionService") {
            etlServices { nameOfService }
          }
        }
        """
        class Ctx: user = _User()
        res = self.schema.execute(q, context_value=Ctx())
        self.assertIsNone(res.errors, msg=res.errors)
        names = [n["nameOfService"] for n in res.data["etlServicesByServiceName"]["etlServices"]]
        self.assertEqual(names, ["SurveySolutionService"])

    @patch("api_etl.gql_mutations.get_class_by_name")
    def test_mutation_execute_service_success(self, m_get):
        # Bypass OpenIMISMutation pipeline & DB; test our mutation logic directly.
        class _Svc:
            def __init__(self, user): self.user = user
            def execute(self): return {"success": True}
        m_get.return_value = _Svc

        errs = ETLServiceMutation._mutate(_User(), name_of_service="SurveySolutionService")
        self.assertIsNone(errs, msg=errs)
