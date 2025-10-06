from django.core.management.base import BaseCommand
from django.db import transaction, connection
from individual.models import Individual
from api_etl.workflows.pmt import compute_household_pmt_score as compute
from api_etl.apps import ApiEtlConfig as C

def classify(score: float, cutoff: float) -> str:
    return "Poor" if score < cutoff else "Non-Poor"

class Command(BaseCommand):
    help = "Recompute PMT per household and store pmt_score/pmt_class in individual_individual.json_ext"

    def add_arguments(self, parser):
        parser.add_argument("--cutoff", type=float, default=None, help="PMT cutoff; overrides admin config")
        parser.add_argument("--limit", type=int, default=None, help="Limit affected households")

    def handle(self, *args, **opts):
        cutoff = opts["cutoff"] if opts["cutoff"] is not None else float(getattr(C, "pmt_cutoff", 0.5))

        # 1) Load all individuals with interview_key present in json_ext
        qs = Individual.objects.filter(json_ext__has_key="interview_key").only("uuid","dob","json_ext")
        # Build in-memory groups by interview_key
        groups = {}
        for ind in qs.iterator():
            ik = ind.json_ext.get("interview_key")
            if not ik:
                continue
            groups.setdefault(ik, []).append(ind)

        # 2) Compute PMT per household using hh row (first member) and all members
        updates = []  # (uuid, score, klass)
        for i, (ik, members) in enumerate(groups.items(), 1):
            hh = members[0].json_ext  # hh-level fields are repeated
            # Map to the dicts your compute() expects
            hh_payload = {
                "household_size": hh.get("household_size"),
                "assets_owned": hh.get("assets_owned"),
                "settlement_type": hh.get("settlement_type"),
            }
            member_payloads = [{"dob": m.dob.isoformat() if m.dob else m.json_ext.get("dob")} for m in members]
            score = compute(hh_payload, member_payloads)
            klass = classify(score, cutoff)
            for m in members:
                updates.append((str(m.uuid), score, klass))
            if opts["limit"] and i >= opts["limit"]:
                break

        # 3) Persist with a single SQL statement per individual (safe JSONB merge)
        with transaction.atomic():
            with connection.cursor() as cur:
                for uuid, score, klass in updates:
                    cur.execute(
                        """
                        UPDATE individual_individual
                        SET "Json_ext" = coalesce("Json_ext",'{}'::jsonb)
                            || jsonb_build_object('pmt_score', %s, 'pmt_class', %s)
                        WHERE "UUID" = %s::uuid
                        """,
                        [score, klass, uuid],
                    )

        self.stdout.write(self.style.SUCCESS(
            f"Updated PMT for {len(updates)} individual rows across {len(groups)} households (cutoff={cutoff})."
        ))
