import os
import json
import hashlib
from pathlib import Path

from app.evaluation.benchmark_calibration import (
    build_independent_reference_labels_v3,
    PilotReferenceCasesV3,
)
from app.evaluation.v3_judge import (
    ReferenceAdjudicatorV3,
    ClaimCitationVerdictV3,
    CalibrationReferenceBundleV3,
    build_calibration_reference_bundle_v3,
)
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_matching import exact_value_in_text_v3, citation_supports_fact_v3

ROOT = Path(__file__).resolve().parents[1]
PILOT_DIR = Path(
    os.getenv(
        "P8_PILOT_DIR",
        str(ROOT / "output" / "evaluation-v3" / "pilot-mini-luna-judge-v3-rating-fix-20261014"),
    )
)
HELDOUT_DIR = Path(
    os.getenv(
        "P8_HELDOUT_DIR",
        str(ROOT / "output" / "evaluation-v3" / "heldout-mini-luna-judge-v3-rating-fix-20261014"),
    )
)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:@127.0.0.1:55433/evaluation")
RUNTIME_MANIFEST = ROOT / "output" / "review-remediation" / "live-runtime-manifest.v2.json"
RUN_ID = os.getenv("P8_RUN_ID", "run_p8_mini_luna_judge_v3_rating_fix_20261014")


def export_reference_cases() -> None:
    cases_path = HELDOUT_DIR / "pilot-reference-cases.v3.json"
    protocol_path = PILOT_DIR / "protocol.v3.json"
    if cases_path.is_file():
        packet = PilotReferenceCasesV3.model_validate_json(
            cases_path.read_text(encoding="utf-8")
        )
        protocol_sha256 = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
        if packet.protocol_sha256 != protocol_sha256:
            raise ValueError("existing pilot reference cases bind a different protocol")
        return

    from app.evaluation import benchmark_cli

    args = [
        "operate",
        "--project-root", str(ROOT),
        "--run-id", RUN_ID,
        "--successor-binding", str(PILOT_DIR / "successor-binding.v3.json"),
        "--p7-protocol", str(PILOT_DIR / "protocol.v3.json"),
        "--p7-repeat-decision", str(PILOT_DIR / "repeat-decision.v3.json"),
        "--p7-runtime-manifest", str(RUNTIME_MANIFEST),
        "--pilot-checkpoint", str(PILOT_DIR / "pilot-checkpoint.v3.jsonl"),
        "--pilot-schedule", str(PILOT_DIR / "pilot-schedule.v3.json"),
        "--allow-network",
        "--database-url", DATABASE_URL,
        "--judge-budget-nano-usd", "16000000000",
        "--export-reference-cases", str(cases_path),
        "--output", str(HELDOUT_DIR),
    ]
    code = benchmark_cli.cli(args)
    if code != 0:
        raise RuntimeError(f"P7 calibration case export failed with code {code}")


def build_bundle(
    cases_path: Path = HELDOUT_DIR / "pilot-reference-cases.v3.json",
    out_bundle_path: Path = HELDOUT_DIR / "independent-calibration-references.p8.json",
):
    packet = PilotReferenceCasesV3.model_validate_json(cases_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(packet.cases)} cases from {cases_path.name}")

    adjudicator = ReferenceAdjudicatorV3(
        adjudicator_id="independent_rules_adjudicator_v1",
        method="automated_rules",
        source_system="deterministic claim-citation evidence rules",
        model_snapshot=None,
        instructions_sha256=canonical_sha256(
            {
                "adjudicator_role": "independent_pilot_calibration",
                "method": "deterministic_claim_citation_rules_v1",
            }
        ),
    )

    references = []
    for case, obs_id in zip(packet.cases, packet.development_observation_ids):
        answer = case.answer
        citations_by_label = {c.label: c for c in answer.citations}
        facts = answer.rubric_context.required_facts

        # Build claim verdicts
        claim_verdicts = []
        for claim_idx, claim in enumerate(answer.claims):
            supported = False
            supported_labels = []
            supported_facts = []

            if claim.citation_labels:
                valid_cits = [
                    lbl for lbl in claim.citation_labels
                    if lbl in citations_by_label
                    and citations_by_label[lbl].source_id
                    and citations_by_label[lbl].source_version_id
                ]
                if valid_cits:
                    matching_facts = []
                    for f_idx, fact in enumerate(facts):
                        val_match = (
                            fact.match_mode == "fact_semantics"
                            or exact_value_in_text_v3(
                                fact.expected_value,
                                claim.text,
                                numeric=fact.match_mode == "numeric_exact",
                            )
                        )
                        ev_match = any(citation_supports_fact_v3(citations_by_label[lbl], fact) for lbl in valid_cits)
                        if val_match and ev_match:
                            matching_facts.append(f_idx)

                    if matching_facts or not facts:
                        supported = True
                        supported_labels = valid_cits
                        supported_facts = matching_facts

            claim_verdicts.append(
                ClaimCitationVerdictV3(
                    claim_index=claim_idx,
                    supported=supported,
                    citation_labels=tuple(supported_labels),
                    rubric_fact_indices=tuple(supported_facts),
                )
            )

        covered_facts = {idx for v in claim_verdicts for idx in v.rubric_fact_indices}
        all_facts_covered = (covered_facts == set(range(len(facts))))
        all_claims_supported = all(v.supported for v in claim_verdicts) if claim_verdicts else False

        claim_supported = all_claims_supported and all_facts_covered

        ref = build_independent_reference_labels_v3(
            case,
            development_observation_id=obs_id,
            claim_supported=claim_supported,
            claim_verdicts=claim_verdicts,
            adjudicator=adjudicator,
        )
        references.append(ref)

    print(f"Built {len(references)} independent references!")
    supported_count = sum(1 for r in references if r.scores['claim_support'] == 1.0)
    print(f"Supported references: {supported_count} / {len(references)}")

    bundle = build_calibration_reference_bundle_v3(
        protocol_sha256=packet.protocol_sha256,
        references=tuple(references),
    )
    if out_bundle_path.exists():
        existing = CalibrationReferenceBundleV3.model_validate_json(
            out_bundle_path.read_text(encoding="utf-8")
        )
        if existing.reference_bundle_sha256 != bundle.reference_bundle_sha256:
            raise ValueError("existing reference bundle differs from P7 calibration cases")
        print(f"Reused matching reference bundle: {out_bundle_path}")
    else:
        out_bundle_path.parent.mkdir(parents=True, exist_ok=True)
        out_bundle_path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
        print(f"Wrote reference bundle to: {out_bundle_path}")


if __name__ == "__main__":
    build_bundle()
