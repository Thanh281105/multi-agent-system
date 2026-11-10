import hashlib
import json
import sys
from pathlib import Path

from app.evaluation.benchmark_calibration import (
    PilotReferenceCasesV3,
    build_development_calibration_case_v3,
    build_independent_reference_labels_v3,
)
from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_judge import (
    ClaimCitationVerdictV3,
    ReferenceAdjudicatorV3,
    build_calibration_reference_bundle_v3,
)
from app.evaluation.v3_matching import citation_supports_fact_v3, exact_value_in_text_v3

RULES = (
    "claim supported iff it has its own citations and every required fact "
    "it states (exact modes: value in claim text; fact_semantics: any own "
    "citation supports the fact) is source-matched; semantic facts are not judged"
)
cases_path, out, man = map(Path, sys.argv[1:4])
raw = cases_path.read_bytes()
packet = json.loads(raw)
typed = PilotReferenceCasesV3.model_validate(packet)
adj = ReferenceAdjudicatorV3(
    adjudicator_id="independent_rules_adjudicator_v1",
    method="automated_rules",
    source_system="deterministic claim-citation evidence rules",
    instructions_sha256=canonical_sha256({"instructions": RULES}),
)
refs = []
for case, obs in zip(typed.cases, typed.development_observation_ids, strict=True):
    a = case.answer
    cit = {c.label: c for c in a.citations}
    facts = a.rubric_context.required_facts
    verdicts = []
    for i, cl in enumerate(a.claims):
        labels = [lab for lab in cl.citation_labels if lab in cit]
        idx = []
        for k, f in enumerate(facts):
            ok = f.match_mode == "fact_semantics" or exact_value_in_text_v3(
                f.expected_value, cl.text, numeric=f.match_mode == "numeric_exact"
            )
            if ok and any(citation_supports_fact_v3(cit[lab], f) for lab in labels):
                idx.append(k)
        sup = bool(labels) and bool(idx)
        verdicts.append(
            ClaimCitationVerdictV3(
                claim_index=i,
                supported=sup,
                citation_labels=tuple(labels) if sup else (),
                rubric_fact_indices=tuple(idx) if sup else (),
            )
        )
    covered = {k for v in verdicts for k in v.rubric_fact_indices}
    ok = all(v.supported for v in verdicts) and covered == set(range(len(facts)))
    dev = build_development_calibration_case_v3(
        calibration_id=case.calibration_id, answer=a
    )
    refs.append(
        build_independent_reference_labels_v3(
            dev,
            development_observation_id=obs,
            claim_supported=ok,
            claim_verdicts=verdicts,
            adjudicator=adj,
        )
    )
b = build_calibration_reference_bundle_v3(
    protocol_sha256=packet["protocol_sha256"], references=refs
)
out.write_text(b.model_dump_json(indent=2) + "\n", encoding="utf-8")
man.write_text(
    json.dumps(
        {
            "status": "automated_rules_not_human_review",
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "bundle_sha256": b.reference_bundle_sha256,
            "rules": RULES,
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "positive_claim_support": sum(
                r.scores["claim_support"] == 1.0 for r in refs
            ),
            "n": len(refs),
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
print("ok", len(refs), sum(r.scores["claim_support"] == 1.0 for r in refs))
