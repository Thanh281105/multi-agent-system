"""Frozen Evaluation v3 gold, split, and evidence integrity tests."""

from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_gold import (
    PACKAGE7_CATEGORY_ORDER,
    PACKAGE7_PILOT_CASE_ORDER,
    PACKAGE7_REQUIRED_FACT_COUNT,
    EvaluationGoldV3,
    EvaluationSplitManifestV3,
    load_evaluation_gold_v3,
    registry_contract_sha256_v3,
)
from app.evaluation.v3_protocol import load_evaluation_experiment_v3
from app.v2.registry import build_default_v2_registry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOLD_PATH = PROJECT_ROOT / "evaluation" / "v3" / "gold.v3.json"
SPLIT_PATH = PROJECT_ROOT / "evaluation" / "v3" / "split.v3.json"
EXPERIMENT_PATH = PROJECT_ROOT / "evaluation" / "v3" / "experiment.v3.json"


def test_v3_gold_loads_exact_frozen_roster_and_resolves_all_evidence() -> None:
    loaded = load_evaluation_gold_v3(
        GOLD_PATH,
        SPLIT_PATH,
        project_root=PROJECT_ROOT,
    )

    assert len(loaded.gold.conversations) == len(loaded.split.entries) == 80
    assert Counter(case.split.value for case in loaded.gold.conversations) == {
        "development": 20,
        "held_out": 60,
    }
    assert tuple(category.value for category in loaded.gold.category_order) == (
        PACKAGE7_CATEGORY_ORDER
    )
    assert loaded.gold.frozen_pilot_ids == PACKAGE7_PILOT_CASE_ORDER
    assert loaded.required_fact_count == PACKAGE7_REQUIRED_FACT_COUNT == 264
    assert loaded.gold_sha256 == (
        "31bcb31c2cfaf1f009c8f036973cd057c6ab881bd8438b7e9c99b93ead7c1852"
    )
    assert loaded.split_sha256 == (
        "49adf66bc53410246a24e7b7fcd8c5472f2915cf4694ace32978728dc5007455"
    )


def test_pilot_work_groups_match_experiment_and_gold_split() -> None:
    loaded_gold = load_evaluation_gold_v3(
        GOLD_PATH,
        SPLIT_PATH,
        project_root=PROJECT_ROOT,
    )
    experiment = load_evaluation_experiment_v3(EXPERIMENT_PATH).config
    gold_by_id = {case.conversation_id: case for case in loaded_gold.gold.conversations}
    split_by_id = {entry.conversation_id: entry for entry in loaded_gold.split.entries}

    assert tuple(case.case_id for case in experiment.pilot_cases) == (
        PACKAGE7_PILOT_CASE_ORDER
    )
    for binding in experiment.pilot_cases:
        assert binding.work_group_id == gold_by_id[binding.case_id].work_group_id
        assert binding.work_group_id == split_by_id[binding.case_id].work_group_id


def test_v3_gold_has_exact_category_quotas_and_ordered_turns() -> None:
    loaded = load_evaluation_gold_v3(
        GOLD_PATH,
        SPLIT_PATH,
        project_root=PROJECT_ROOT,
    )
    counts = Counter(
        (case.split.value, case.category.value) for case in loaded.gold.conversations
    )
    expected = {
        ("development", "multi_constraint"): 4,
        ("development", "knowledge_source"): 4,
        ("development", "multi_expert_compare_recommendation"): 4,
        ("development", "multi_turn_memory"): 3,
        ("development", "insufficient_conflict_injection"): 3,
        ("development", "shopping_merchant"): 2,
        ("held_out", "multi_constraint"): 12,
        ("held_out", "knowledge_source"): 12,
        ("held_out", "multi_expert_compare_recommendation"): 12,
        ("held_out", "multi_turn_memory"): 8,
        ("held_out", "insufficient_conflict_injection"): 8,
        ("held_out", "shopping_merchant"): 8,
    }
    assert counts == expected
    for case in loaded.gold.conversations:
        assert tuple(turn.ordinal for turn in case.user_turns) == tuple(
            range(1, len(case.user_turns) + 1)
        )
        assert case.gold_origin == "automated_pre_sut_spec"


def test_loader_rejects_gold_hash_tampering(tmp_path: Path) -> None:
    gold, split = _payloads()
    gold["conversations"][0]["user_turns"][0]["message"] += " Đã sửa."
    gold_path, split_path = _write_assets(tmp_path, gold, split)

    with pytest.raises(ValueError, match="gold hash mismatch"):
        load_evaluation_gold_v3(
            gold_path,
            split_path,
            project_root=PROJECT_ROOT,
        )


@pytest.mark.parametrize("mutation", ["duplicate_id", "category_quota"])
def test_gold_model_rejects_duplicate_ids_and_quota_drift(mutation: str) -> None:
    gold, _ = _payloads()
    if mutation == "duplicate_id":
        gold["conversations"][1]["conversation_id"] = gold["conversations"][0][
            "conversation_id"
        ]
    else:
        gold["conversations"][20]["category"] = "knowledge_source"

    with pytest.raises(ValueError):
        EvaluationGoldV3.model_validate(gold)


def test_gold_model_rejects_cross_split_work_leakage() -> None:
    gold, _ = _payloads()
    development_work = gold["conversations"][0]["work_ids"][0]
    held_out = next(
        case for case in gold["conversations"] if case["split"] == "held_out"
    )
    held_out["leakage_keys"].append({"kind": "work", "value": development_work})

    with pytest.raises(ValueError, match="cross-split leakage"):
        EvaluationGoldV3.model_validate(gold)


@pytest.mark.parametrize(
    ("support_kind", "expected_error"),
    [
        ("source_excerpt", "span/excerpt mismatch"),
        ("catalog_pointer", "not obtainable"),
    ],
)
def test_loader_rejects_bad_evidence(
    tmp_path: Path,
    support_kind: str,
    expected_error: str,
) -> None:
    gold, split = _payloads()
    fact = _first_fact(gold, support_kind)
    if support_kind == "source_excerpt":
        fact["support"]["start"] += 1
        fact["support"]["end"] += 1
    else:
        fact["support"]["json_pointer"] = "/missing"
    gold_path, split_path = _write_assets(
        tmp_path,
        gold,
        split,
        bind_gold=True,
    )

    with pytest.raises(ValueError, match=expected_error):
        load_evaluation_gold_v3(
            gold_path,
            split_path,
            project_root=PROJECT_ROOT,
        )


def test_loader_rejects_bad_catalog_value(tmp_path: Path) -> None:
    gold, split = _payloads()
    fact = _first_fact(gold, "catalog_pointer")
    fact["support"]["expected_value"] = -1
    fact["expected_value"] = -1
    gold_path, split_path = _write_assets(
        tmp_path,
        gold,
        split,
        bind_gold=True,
    )

    with pytest.raises(ValueError, match="pointer/value mismatch"):
        load_evaluation_gold_v3(
            gold_path,
            split_path,
            project_root=PROJECT_ROOT,
        )


def test_loader_rejects_unsupported_action(tmp_path: Path) -> None:
    gold, split = _payloads()
    boundary = gold["conversations"][0]["action_capability_blueprint"]
    previous_id = boundary["allowed"][0]["capability_id"]
    boundary["allowed"][0]["capability_id"] = "order.create"
    boundary["required"] = [
        "order.create" if item == previous_id else item for item in boundary["required"]
    ]
    gold_path, split_path = _write_assets(
        tmp_path,
        gold,
        split,
        bind_gold=True,
    )

    with pytest.raises(
        ValueError,
        match=(
            "successful attempts|no prompt/gold obligation|"
            "partition the frozen registry"
        ),
    ):
        load_evaluation_gold_v3(
            gold_path,
            split_path,
            project_root=PROJECT_ROOT,
        )


def test_loader_binds_full_default_v2_registry_contract(tmp_path: Path) -> None:
    gold, split = _payloads()
    assert gold["registry_contract_sha256"] == registry_contract_sha256_v3()

    gold["registry_contract_sha256"] = "0" * 64
    gold_path, split_path = _write_assets(
        tmp_path,
        gold,
        split,
        bind_gold=True,
    )
    with pytest.raises(ValueError, match="registry contract has drifted"):
        load_evaluation_gold_v3(
            gold_path,
            split_path,
            project_root=PROJECT_ROOT,
        )


def test_loader_rejects_registry_metadata_drift(tmp_path: Path) -> None:
    gold, split = _payloads()
    gold["conversations"][0]["action_capability_blueprint"]["allowed"][0][
        "confirmation_policy"
    ] = "required"
    gold_path, split_path = _write_assets(
        tmp_path,
        gold,
        split,
        bind_gold=True,
    )

    with pytest.raises(ValueError, match="metadata differs from v2 registry"):
        load_evaluation_gold_v3(
            gold_path,
            split_path,
            project_root=PROJECT_ROOT,
        )


def test_shopper_pilot_requires_proposal_and_stops_for_confirmation() -> None:
    gold, _ = _payloads()
    case = _case(gold, "dev_shopping_merchant_01")
    boundary = case["action_capability_blueprint"]

    assert case["identity_fixture"] == {
        "identity_fixture_id": "identity_shopper_write_v3",
        "role": "shopper",
        "authenticated": True,
        "scopes": ["ecommerce.read", "ecommerce.write"],
    }
    assert boundary["required"] == [
        "shopper.cart.read",
        "shopper.checkout.preview",
        "shopper.checkout.propose",
    ]
    assert boundary["expected_outcome"] == "awaiting_confirmation"
    assert boundary["confirmation_state"] == "unconfirmed"
    assert case["required_response_mode"] == "await_confirmation"
    assert "shopper.checkout.execute" in boundary["forbidden"]
    fixture = case["sandbox_fixture"]
    assert fixture["fixture_id"] == "sandbox_dev_shopping_merchant_01"
    assert fixture["confirmed_proposal_id"] is None
    assert fixture["target_capability_id"] == "shopper.checkout.propose"
    assert fixture["proposal_parameters"] == {
        "kind": "checkout_proposal",
        "capability_id": "shopper.checkout.propose",
        "cart_id": "cart_dev_shopping_merchant_01",
        "expected_version": 1,
    }
    assert {line["product_id"] for line in fixture["cart"]["lines"]} == {
        80,
        179,
    }


def test_merchant_write_case_requires_offer_proposal_only() -> None:
    gold, _ = _payloads()
    case = _case(gold, "dev_shopping_merchant_02")
    boundary = case["action_capability_blueprint"]

    assert case["identity_fixture"]["role"] == "merchant"
    assert case["identity_fixture"]["scopes"] == [
        "ecommerce.read",
        "merchant.read",
        "merchant.write",
    ]
    assert boundary["required"] == [
        "merchant.inventory.read",
        "merchant.offer.propose",
    ]
    assert boundary["expected_outcome"] == "awaiting_confirmation"
    assert "merchant.offer.execute" in boundary["forbidden"]
    fixture = case["sandbox_fixture"]
    assert fixture["confirmed_proposal_id"] is None
    assert fixture["target_capability_id"] == "merchant.offer.propose"
    assert fixture["proposal_parameters"] == {
        "kind": "offer_proposal",
        "capability_id": "merchant.offer.propose",
        "offer_id": "offer_product_59",
        "expected_version": 1,
        "new_price_vnd": 70900,
    }
    assert {offer["product_id"] for offer in fixture["merchant"]["offers"]} == {
        59,
        60,
    }


@pytest.mark.parametrize(
    ("conversation_id", "role", "attempted", "allowed_reads"),
    [
        (
            "held_shopping_merchant_05",
            "shopper",
            "merchant.offer.propose",
            (),
        ),
        (
            "held_shopping_merchant_06",
            "merchant",
            "shopper.checkout.propose",
            (),
        ),
    ],
)
def test_shopper_and_merchant_capabilities_are_isolated(
    conversation_id: str,
    role: str,
    attempted: str,
    allowed_reads: tuple[str, ...],
) -> None:
    gold, _ = _payloads()
    case = _case(gold, conversation_id)
    boundary = case["action_capability_blueprint"]

    assert case["identity_fixture"]["role"] == role
    assert [item["capability_id"] for item in boundary["attempted"]] == [attempted]
    assert tuple(item["capability_id"] for item in boundary["allowed"]) == allowed_reads
    assert boundary["required"] == []
    assert boundary["expected_outcome"] == "denied"
    assert case["required_response_mode"] == "deny"


@pytest.mark.parametrize(
    ("conversation_id", "missing_scope", "attempted", "allowed_reads"),
    [
        (
            "held_shopping_merchant_03",
            "ecommerce.write",
            "shopper.checkout.propose",
            ("shopper.cart.read", "shopper.checkout.preview"),
        ),
        (
            "held_shopping_merchant_04",
            "merchant.write",
            "merchant.offer.propose",
            ("merchant.inventory.read",),
        ),
    ],
)
def test_write_scope_is_required_for_proposals(
    conversation_id: str,
    missing_scope: str,
    attempted: str,
    allowed_reads: tuple[str, ...],
) -> None:
    gold, _ = _payloads()
    case = _case(gold, conversation_id)
    boundary = case["action_capability_blueprint"]

    assert missing_scope not in case["identity_fixture"]["scopes"]
    assert [item["capability_id"] for item in boundary["attempted"]] == [attempted]
    assert tuple(item["capability_id"] for item in boundary["allowed"]) == allowed_reads
    assert boundary["expected_outcome"] == "denied"


@pytest.mark.parametrize(
    ("conversation_id", "attempted", "allowed_reads"),
    [
        (
            "held_shopping_merchant_07",
            "shopper.checkout.execute",
            ("shopper.cart.read", "shopper.checkout.preview"),
        ),
        (
            "held_shopping_merchant_08",
            "merchant.offer.execute",
            ("merchant.inventory.read",),
        ),
    ],
)
def test_execute_is_denied_without_confirmed_proposal(
    conversation_id: str,
    attempted: str,
    allowed_reads: tuple[str, ...],
) -> None:
    gold, _ = _payloads()
    case = _case(gold, conversation_id)
    boundary = case["action_capability_blueprint"]

    assert boundary["confirmation_state"] == "unconfirmed"
    assert case["sandbox_fixture"]["confirmed_proposal_id"] is None
    assert case["sandbox_fixture"]["proposal_parameters"] is None
    assert boundary["expected_outcome"] == "denied"
    assert boundary["required"] == []
    assert tuple(item["capability_id"] for item in boundary["allowed"]) == allowed_reads
    assert boundary["attempted"] == [
        next(
            item
            for item in boundary["attempted"]
            if item["capability_id"] == attempted and item["effect"] == "execute"
        )
    ]


def test_loader_rejects_sandbox_catalog_or_prompt_drift(tmp_path: Path) -> None:
    gold, split = _payloads()
    case = _case(gold, "dev_shopping_merchant_01")
    case["sandbox_fixture"]["cart"]["lines"][0]["unit_price_vnd"] += 1
    gold_path, split_path = _write_assets(
        tmp_path,
        gold,
        split,
        bind_gold=True,
        bind_sandbox_fixtures=True,
    )
    with pytest.raises(ValueError, match="cart price differs"):
        load_evaluation_gold_v3(
            gold_path,
            split_path,
            project_root=PROJECT_ROOT,
        )

    gold, split = _payloads()
    case = _case(gold, "dev_shopping_merchant_01")
    case["user_turns"][0]["message"] = "Hãy tạo đề xuất thanh toán."
    gold_path, split_path = _write_assets(
        tmp_path,
        gold,
        split,
        bind_gold=True,
    )
    with pytest.raises(ValueError, match="omits a bound product name"):
        load_evaluation_gold_v3(
            gold_path,
            split_path,
            project_root=PROJECT_ROOT,
        )


def test_shopping_prompts_do_not_leak_registry_capability_ids() -> None:
    gold, _ = _payloads()
    sample_boundary = gold["conversations"][0]["action_capability_blueprint"]
    registry_ids = set(sample_boundary["forbidden"]) | {
        item["capability_id"] for item in sample_boundary["allowed"]
    }

    for case in gold["conversations"]:
        if case["category"] != "shopping_merchant":
            continue
        prompt = "\n".join(turn["message"] for turn in case["user_turns"])
        assert all(capability_id not in prompt for capability_id in registry_ids)
        target = case["sandbox_fixture"]["target_capability_id"]
        boundary = case["action_capability_blueprint"]
        bound_ids = {
            item["capability_id"]
            for item in (*boundary["allowed"], *boundary["attempted"])
        }
        assert target in bound_ids


def test_split_binds_identical_sandbox_fixture_for_every_repeat(
    tmp_path: Path,
) -> None:
    gold, split = _payloads()
    case = _case(gold, "dev_shopping_merchant_01")
    entry = _entry(split, case["conversation_id"])
    assert entry["sandbox_fixture_id"] == case["sandbox_fixture"]["fixture_id"]
    assert entry["sandbox_fixture_sha256"] == canonical_sha256(case["sandbox_fixture"])

    entry["sandbox_fixture_sha256"] = "0" * 64
    split["roster_sha256"] = canonical_sha256(split["entries"])
    gold_path, split_path = _write_assets(tmp_path, gold, split)
    with pytest.raises(ValueError, match="does not exactly match gold"):
        load_evaluation_gold_v3(
            gold_path,
            split_path,
            project_root=PROJECT_ROOT,
        )


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("foreign_product", "products must exactly match"),
        ("confirmed_proposal", "Input should be None"),
    ],
)
def test_gold_model_rejects_non_reproducible_sandbox_state(
    mutation: str,
    expected_error: str,
) -> None:
    gold, _ = _payloads()
    fixture = _case(gold, "dev_shopping_merchant_01")["sandbox_fixture"]
    if mutation == "foreign_product":
        fixture["cart"]["lines"][0]["product_id"] = 1
    else:
        fixture["confirmed_proposal_id"] = "proposal_already_confirmed"

    with pytest.raises(ValueError, match=expected_error):
        EvaluationGoldV3.model_validate(gold)


def test_split_model_rejects_pilot_and_exclusion_drift() -> None:
    _, split = _payloads()
    pilot_drift = deepcopy(split)
    pilot_drift["frozen_pilot_ids"][0] = "dev_multi_constraint_99"
    with pytest.raises(ValueError, match="pilot IDs"):
        EvaluationSplitManifestV3.model_validate(pilot_drift)

    exclusion_drift = deepcopy(split)
    held_out = next(
        entry for entry in exclusion_drift["entries"] if entry["split"] == "held_out"
    )
    held_out["product_ids"].append(80)
    with pytest.raises(ValueError, match="exposed product"):
        EvaluationSplitManifestV3.model_validate(exclusion_drift)


def test_loader_rejects_split_family_drift_even_with_rebound_roster(
    tmp_path: Path,
) -> None:
    gold, split = _payloads()
    split["entries"][0]["prompt_family_id"] = "prompt_tampered_catalog_family"
    split["roster_sha256"] = canonical_sha256(split["entries"])
    gold_path, split_path = _write_assets(tmp_path, gold, split)

    with pytest.raises(ValueError, match="does not exactly match gold"):
        load_evaluation_gold_v3(
            gold_path,
            split_path,
            project_root=PROJECT_ROOT,
        )


def test_loader_rejects_any_result_or_sut_artifact_reference(
    tmp_path: Path,
) -> None:
    gold, split = _payloads()
    gold["conversations"][0]["user_turns"][0]["message"] = (
        "Đọc evaluation/results/captured_output.json."
    )
    gold_path, split_path = _write_assets(
        tmp_path,
        gold,
        split,
        bind_gold=True,
    )

    with pytest.raises(ValueError, match="SUT/result artifacts"):
        load_evaluation_gold_v3(
            gold_path,
            split_path,
            project_root=PROJECT_ROOT,
        )


def test_work_and_semantic_families_are_real_groupings_without_split_aliases() -> None:
    gold, _ = _payloads()
    cases = gold["conversations"]
    split_alias = re.compile(
        r"(?:^|[_.-])(dev|development|held|heldout|test)(?:[_.-]|$)"
    )

    for field in (
        "work_group_id",
        "prompt_family_id",
        "scenario_family_id",
        "paraphrase_family_id",
    ):
        values = [case[field] for case in cases]
        assert all(
            value != case["conversation_id"]
            for value, case in zip(values, cases, strict=True)
        )
        assert all(split_alias.search(value) is None for value in values)
        counts = Counter(values)
        assert sum(count for count in counts.values() if count > 1) >= 60

    groups_by_work_set: dict[tuple[str, ...], set[str]] = {}
    work_sets_by_group: dict[str, set[tuple[str, ...]]] = {}
    family_splits: dict[tuple[str, str], set[str]] = {}
    for case in cases:
        work_set = tuple(sorted(case["work_ids"]))
        if work_set:
            groups_by_work_set.setdefault(work_set, set()).add(case["work_group_id"])
        work_sets_by_group.setdefault(case["work_group_id"], set()).add(work_set)
        for field in (
            "prompt_family_id",
            "scenario_family_id",
            "paraphrase_family_id",
        ):
            family_splits.setdefault((field, case[field]), set()).add(case["split"])
    assert all(len(groups) == 1 for groups in groups_by_work_set.values())
    assert all(len(work_sets) == 1 for work_sets in work_sets_by_group.values())
    assert all(len(splits) == 1 for splits in family_splits.values())

    assert (
        _case(gold, "dev_multi_constraint_01")["work_group_id"]
        == _case(gold, "dev_knowledge_source_01")["work_group_id"]
    )
    assert (
        _case(gold, "held_shopping_merchant_01")["prompt_family_id"]
        == _case(gold, "held_shopping_merchant_03")["prompt_family_id"]
    )
    assert (
        _case(gold, "held_shopping_merchant_02")["paraphrase_family_id"]
        == _case(gold, "held_shopping_merchant_05")["paraphrase_family_id"]
    )


@pytest.mark.parametrize("mutation", ["singletons", "split_alias", "work_set_drift"])
def test_gold_model_rejects_grouping_games(mutation: str) -> None:
    gold, _ = _payloads()
    if mutation == "singletons":
        for case in gold["conversations"]:
            case["work_group_id"] = case["conversation_id"]
    elif mutation == "split_alias":
        case = gold["conversations"][0]
        case["prompt_family_id"] = "prompt_dev_hidden_alias"
        next(key for key in case["leakage_keys"] if key["kind"] == "prompt_family")[
            "value"
        ] = "prompt_dev_hidden_alias"
    else:
        _case(gold, "dev_knowledge_source_01")["work_group_id"] = (
            "work_sapiens_secondary_group"
        )

    with pytest.raises(ValueError, match="group|family|work"):
        EvaluationGoldV3.model_validate(gold)


def test_gold_model_rejects_relabelled_cross_split_prompt_template() -> None:
    gold, _ = _payloads()
    development = _case(gold, "dev_multi_constraint_01")
    held_out = _case(gold, "held_multi_constraint_01")
    development_name = next(
        fact["expected_value"]
        for fact in development["required_fact_blueprints"]
        if fact["support"].get("json_pointer") == "/name"
    )
    held_out_name = next(
        fact["expected_value"]
        for fact in held_out["required_fact_blueprints"]
        if fact["support"].get("json_pointer") == "/name"
    )
    held_out["user_turns"][0]["message"] = development["user_turns"][0][
        "message"
    ].replace(development_name, held_out_name)

    with pytest.raises(ValueError, match="semantic prompt template overlaps"):
        EvaluationGoldV3.model_validate(gold)


@pytest.mark.parametrize(
    ("development_id", "held_out_id", "replacements"),
    [
        (
            "dev_shopping_merchant_02",
            "held_shopping_merchant_02",
            (
                (
                    "Bàn Về Khế Ước Xã Hội (Tái Bản)",
                    "Lên Tàu Cùng Socrates - Đi Tìm Ý Nghĩa Cuộc Sống Từ Các Triết Gia",
                ),
                (
                    "Quân Vương – Thuật Cai Trị (Tái Bản)",
                    "Nghệ Thuật PR Bản Thân (Tái Bản 2018)",
                ),
                ("70.900", "139.300"),
            ),
        ),
        (
            "dev_shopping_merchant_01",
            "held_shopping_merchant_01",
            (
                ("Sapiens Lược Sử Loài Người (Tái Bản 2022)", "Dự Án Phượng Hoàng"),
                (
                    "Không Đến Một (Tái Bản - Bìa Cam)",
                    "Lên Tàu Cùng Socrates - Đi Tìm Ý Nghĩa Cuộc Sống Từ Các Triết Gia",
                ),
            ),
        ),
    ],
)
def test_gold_model_rejects_cross_split_near_duplicate_with_polite_filler(
    development_id: str,
    held_out_id: str,
    replacements: tuple[tuple[str, str], ...],
) -> None:
    gold, _ = _payloads()
    copied_prompt = _case(gold, development_id)["user_turns"][0]["message"]
    for old, new in replacements:
        copied_prompt = copied_prompt.replace(old, new)
    _case(gold, held_out_id)["user_turns"][0]["message"] = (
        f"Xin vui lòng {copied_prompt[0].lower()}{copied_prompt[1:]}"
    )

    with pytest.raises(ValueError, match="near-duplicate semantic prompt template"):
        EvaluationGoldV3.model_validate(gold)


def test_heldout_is_disjoint_from_development_and_all_exposure_exclusions() -> None:
    gold, split = _payloads()
    development = [
        case for case in gold["conversations"] if case["split"] == "development"
    ]
    held_out = [case for case in gold["conversations"] if case["split"] == "held_out"]
    development_works = {work for case in development for work in case["work_ids"]}
    held_out_works = {work for case in held_out for work in case["work_ids"]}
    development_sources = {
        source for case in development for source in case["source_ids"]
    }
    held_out_sources = {source for case in held_out for source in case["source_ids"]}
    exclusions = split["exclusions"]
    blocked_products = set(exclusions["held_out_prior_v1_v2_product_ids"]) | set(
        exclusions["held_out_development_probe_product_ids"]
    )

    assert development_works.isdisjoint(held_out_works)
    assert development_sources.isdisjoint(held_out_sources)
    assert all(blocked_products.isdisjoint(case["product_ids"]) for case in held_out)
    assert set(exclusions["held_out_development_probe_work_ids"]).isdisjoint(
        held_out_works
    )
    assert set(exclusions["held_out_development_probe_source_ids"]).isdisjoint(
        held_out_sources
    )


def test_sandbox_reset_fixtures_round_trip_and_match_every_split_entry() -> None:
    gold, split = _payloads()
    parsed = EvaluationGoldV3.model_validate(gold)
    round_tripped = EvaluationGoldV3.model_validate(parsed.model_dump(mode="json"))
    entries = {entry["conversation_id"]: entry for entry in split["entries"]}

    for case in round_tripped.conversations:
        fixture = case.sandbox_fixture
        if fixture is None:
            continue
        dumped = fixture.model_dump(mode="json")
        assert dumped["reset_revision"] == 1
        assert dumped["confirmed_proposal_id"] is None
        entry = entries[case.conversation_id]
        assert entry["sandbox_fixture_id"] == dumped["fixture_id"]
        assert entry["sandbox_fixture_sha256"] == canonical_sha256(dumped)


def test_multi_expert_cases_use_catalog_and_knowledge_without_review_claims() -> None:
    gold, _ = _payloads()
    registry = build_default_v2_registry()
    cases = [
        case
        for case in gold["conversations"]
        if case["category"] == "multi_expert_compare_recommendation"
    ]
    assert len(cases) == 16

    for case in cases:
        boundary = case["action_capability_blueprint"]
        required = set(boundary["required"])
        assert required == {
            "product.catalog.search",
            "product.compare",
            "knowledge.retrieve",
        }
        assert required == {item["capability_id"] for item in boundary["attempted"]}
        assert "review.compare" in boundary["forbidden"]
        assert len({registry.capability(item).service for item in required}) >= 2
        assert any(
            fact["support"]["kind"] == "source_excerpt"
            for fact in case["required_fact_blueprints"]
        )
        assert len(case["product_ids"]) == 2


def test_gold_model_rejects_required_capability_without_gold_obligation() -> None:
    gold, _ = _payloads()
    case = _case(gold, "dev_multi_expert_01")
    boundary = case["action_capability_blueprint"]
    definition = build_default_v2_registry().capability("review.compare")
    review_contract = {
        "capability_id": definition.capability,
        "allowed_modes": sorted(mode.value for mode in definition.allowed_modes),
        "required_scopes": sorted(definition.required_permissions),
        "effect": definition.effect.value,
        "confirmation_policy": definition.confirmation_policy.value,
    }
    boundary["allowed"].append(review_contract)
    boundary["required"].append("review.compare")
    boundary["attempted"].append(deepcopy(review_contract))
    boundary["forbidden"].remove("review.compare")

    with pytest.raises(ValueError, match="no prompt/gold obligation"):
        EvaluationGoldV3.model_validate(gold)


def test_shopping_facts_match_capability_outputs_and_denials_score_only_boundary() -> (
    None
):
    gold, _ = _payloads()
    registry_ids = {
        definition.capability
        for definition in build_default_v2_registry().list_capabilities()
    }
    expected_fields = {
        "dev_shopping_merchant_01": ["/name", "/price_vnd"] * 2,
        "dev_shopping_merchant_02": ["/price_vnd"] * 2,
        "held_shopping_merchant_01": ["/name", "/price_vnd"] * 2,
        "held_shopping_merchant_02": ["/price_vnd"] * 2,
    }

    for case in gold["conversations"]:
        if case["category"] != "shopping_merchant":
            continue
        boundary = case["action_capability_blueprint"]
        allowed = {item["capability_id"] for item in boundary["allowed"]}
        assert allowed | set(boundary["forbidden"]) == registry_ids
        assert allowed.isdisjoint(boundary["forbidden"])
        if boundary["expected_outcome"] == "denied":
            assert case["answerability"] == "unanswerable"
            assert case["required_fact_blueprints"] == []
            assert set(
                item["capability_id"] for item in boundary["attempted"]
            ).issubset(boundary["forbidden"])
            continue
        fields = [
            fact["support"]["json_pointer"] for fact in case["required_fact_blueprints"]
        ]
        assert fields == expected_fields[case["conversation_id"]]
        assert "/rating" not in fields


def test_gold_model_rejects_fact_unavailable_from_allowed_capability_output() -> None:
    gold, _ = _payloads()
    fact = _case(gold, "dev_shopping_merchant_02")["required_fact_blueprints"][0]
    fact["support"]["json_pointer"] = "/rating"
    fact["support"]["expected_value"] = 4.8
    fact["expected_value"] = 4.8

    with pytest.raises(ValueError, match="not obtainable"):
        EvaluationGoldV3.model_validate(gold)


def test_prompts_are_natural_and_do_not_leak_hidden_outcomes_or_state() -> None:
    gold, _ = _payloads()
    capability_ids = {
        definition.capability
        for definition in build_default_v2_registry().list_capabilities()
    }
    forbidden_fragments = (
        "chờ tôi xác nhận",
        "dừng chờ",
        "không có quyền",
        "từ chối",
        "không tạo đề xuất",
        "chưa có đề xuất",
        "trong sandbox",
        "phiên bản",
    )

    for case in gold["conversations"]:
        prompt = "\n".join(turn["message"] for turn in case["user_turns"])
        normalized = prompt.casefold()
        assert all(fragment not in normalized for fragment in forbidden_fragments)
        assert all(capability_id not in prompt for capability_id in capability_ids)
        if case["category"] == "shopping_merchant":
            assert re.search(r"\b(?:cart|offer)_[a-z0-9_.-]+", normalized) is None

    gold["conversations"][-1]["user_turns"][0]["message"] += " Phải từ chối."
    with pytest.raises(ValueError, match="hidden expected outcome"):
        EvaluationGoldV3.model_validate(gold)


def test_morisaki_gold_stays_within_author_and_translation_source_support() -> None:
    gold, _ = _payloads()
    affected_ids = {
        "held_knowledge_source_09",
        "held_multi_expert_09",
        "held_multi_expert_10",
    }
    expected_excerpt = (
        "National-library factual note. The National Library of Vietnam's February "
        "2024 bibliography, PDF page 177 (viewer index 176), entry 2217, identifies "
        "*Những giấc mơ ở hiệu sách Morisaki* as a work by Yagisawa Satoshi, "
        "translated by Trần Quỳnh Anh from the Japanese title 森崎書店の日々."
    )

    for case in gold["conversations"]:
        if "src_morisaki_nlv" not in case["source_ids"]:
            continue
        prompt = "\n".join(turn["message"] for turn in case["user_turns"])
        assert "chủ đề" not in prompt.casefold()
        source_facts = [
            fact
            for fact in case["required_fact_blueprints"]
            if fact["support"].get("record_id") == "src_morisaki_nlv"
        ]
        if not source_facts:
            assert case["category"] == "shopping_merchant"
            continue
        fact = source_facts[0]
        assert fact["expected_value"] == expected_excerpt
        assert "tác giả" in fact["claim_blueprint"].casefold()
        assert "người dịch" in fact["claim_blueprint"].casefold()
        if case["conversation_id"] in affected_ids:
            assert "người dịch" in prompt.casefold()


def _payloads() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        json.loads(GOLD_PATH.read_text(encoding="utf-8")),
        json.loads(SPLIT_PATH.read_text(encoding="utf-8")),
    )


def _first_fact(gold: dict[str, Any], support_kind: str) -> dict[str, Any]:
    return next(
        fact
        for case in gold["conversations"]
        for fact in case["required_fact_blueprints"]
        if fact["support"]["kind"] == support_kind
    )


def _case(gold: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    return next(
        case
        for case in gold["conversations"]
        if case["conversation_id"] == conversation_id
    )


def _entry(split: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    return next(
        entry
        for entry in split["entries"]
        if entry["conversation_id"] == conversation_id
    )


def _write_assets(
    tmp_path: Path,
    gold: dict[str, Any],
    split: dict[str, Any],
    *,
    bind_gold: bool = False,
    bind_sandbox_fixtures: bool = False,
) -> tuple[Path, Path]:
    if bind_sandbox_fixtures:
        cases = {case["conversation_id"]: case for case in gold["conversations"]}
        for entry in split["entries"]:
            fixture = cases[entry["conversation_id"]]["sandbox_fixture"]
            entry["sandbox_fixture_id"] = (
                fixture["fixture_id"] if fixture is not None else None
            )
            entry["sandbox_fixture_sha256"] = (
                canonical_sha256(fixture) if fixture is not None else None
            )
        split["roster_sha256"] = canonical_sha256(split["entries"])
    if bind_gold:
        split["gold_sha256"] = canonical_sha256(gold)
    gold_path = tmp_path / "gold.v3.json"
    split_path = tmp_path / "split.v3.json"
    gold_path.write_text(
        json.dumps(gold, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    split_path.write_text(
        json.dumps(split, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return gold_path, split_path
