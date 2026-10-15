"""Verify this engineering challenge and optionally export fresh-turn requests.

No app imports, database access, model calls, or scoring of SUT outputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def pointer(value: object, path: str) -> object:
    current = value
    for part in path.removeprefix("/").split("/") if path else []:
        part = part.replace("~1", "/").replace("~0", "~")
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-requests", type=Path)
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    root = directory.parents[2]
    packet = json.loads((directory / "packet.json").read_text(encoding="utf-8"))
    lock = json.loads((directory / "lock.json").read_text(encoding="utf-8"))
    assert digest(packet) == lock["packet_canonical_sha256"], "packet hash mismatch"
    expected_lock = lock["lock_canonical_sha256"]
    assert (
        digest({k: v for k, v in lock.items() if k != "lock_canonical_sha256"})
        == expected_lock
    )
    assert lock["source_authorities"] == packet["source_authorities"]
    for filename, expected in lock["artifact_raw_sha256s"].items():
        assert (
            hashlib.sha256((directory / filename).read_bytes()).hexdigest() == expected
        )
    inputs = {}
    raw_lines = {}
    for authority in packet["source_authorities"]:
        path = (root / authority["path"]).resolve()
        assert path.is_relative_to(root), "source path escapes workspace"
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == authority["raw_sha256"], authority[
            "path"
        ]
        if path.suffix == ".jsonl":
            raw_lines[authority["path"]] = raw.decode("utf-8").splitlines()
            inputs[authority["path"]] = [
                json.loads(line) for line in raw_lines[authority["path"]]
            ]
        else:
            inputs[authority["path"]] = json.loads(raw)
    fact_ids = set()
    for case in packet["cases"]:
        assert case["fresh_conversation"] is True
        assert case["user_message"].strip()
        case_fact_ids = {fact["fact_id"] for fact in case["oracle"]["facts"]}
        assert set(case["oracle"]["required_fact_ids"]).issubset(case_fact_ids)
        inventory = case["oracle"].get("authority_inventory")
        if inventory is not None:
            rows = inputs[inventory["source_file"]]
            assert len(rows) == inventory["record_count"]
            matches = sum(
                inventory["casefold_name_query"] in row["name"].casefold()
                for row in rows
            )
            assert matches == inventory["matching_record_count"]
        for fact in case["oracle"]["facts"]:
            assert fact["fact_id"] not in fact_ids
            fact_ids.add(fact["fact_id"])
            binding = fact["authority"]
            data = inputs[binding["source_file"]]
            record = pointer(data, binding["record_pointer"])
            assert digest(record) == binding["record_canonical_sha256"]
            value = pointer(record, binding["value_pointer"])
            assert value == binding["source_value"], fact["fact_id"]
            span = binding.get("span")
            if span is not None:
                container = (
                    raw_lines[binding["source_file"]][
                        int(binding["record_pointer"][1:])
                    ]
                    if span["container"] == "jsonl_record"
                    else value
                )
                assert container[span["start"] : span["end"]] == span["text"]
            if fact["match_mode"] in {"numeric_value", "exact_value"}:
                assert fact["expected_value"] == value
        sample = case["oracle"].get("review_sample")
        if sample is not None:
            rows = inputs[sample["source_file"]]
            selected = [
                (index, row)
                for index, row in enumerate(rows)
                if row["product_external_id"] == sample["product_external_id"]
            ]
            assert len(selected) == sample["sample_count"]
            assert (
                dict(Counter(str(row["rating"]) for _, row in selected))
                == sample["rating_histogram"]
            )
            assert [index + 1 for index, _ in selected] == sample["jsonl_line_numbers"]
            assert [digest(row) for _, row in selected] == sample[
                "record_canonical_sha256s"
            ]
    assert len(packet["cases"]) == lock["case_count"] == 20
    assert len({case["case_id"] for case in packet["cases"]}) == 20
    audit_ids = packet["human_audit_plan"]["precommitted_case_ids"]
    assert len(audit_ids) == 12 and len(set(audit_ids)) == 12
    strata = Counter(
        case["stratum"] for case in packet["cases"] if case["case_id"] in audit_ids
    )
    assert len(strata) == 6 and set(strata.values()) == {2}
    assert len(packet["evaluator_probes"]) == lock["evaluator_probe_count"] == 5
    for probe in packet["evaluator_probes"]:
        assert all(citation in fact_ids for citation in probe["cited_fact_ids"])
    print(
        f"verified: 20 fresh-turn cases, 5 evaluator probes, "
        f"{len(fact_ids)} source-bound facts"
    )
    print(f"packet canonical sha256: {lock['packet_canonical_sha256']}")
    print("operator lock and manual human audit: pending; no SUT request executed")
    if args.export_requests is not None:
        destination = args.export_requests.resolve()
        assert destination.is_relative_to(root), "export must stay inside workspace"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8", newline="\n") as output:
            for case in packet["cases"]:
                output.write(
                    json.dumps(
                        {
                            "case_id": case["case_id"],
                            "stratum": case["stratum"],
                            "fresh_conversation": True,
                            "message": case["user_message"],
                            "packet_canonical_sha256": lock["packet_canonical_sha256"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        print(f"exported 20 requests to {destination}; not submitted to SUT")


if __name__ == "__main__":
    main()
