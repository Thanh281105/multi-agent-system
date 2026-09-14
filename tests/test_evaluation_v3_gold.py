"""Frozen Evaluation v3 gold, split, and evidence integrity tests."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.evaluation.protocol import canonical_sha256
from app.evaluation.v3_gold import (
    PACKAGE7_CATEGORY_ORDER,
    PACKAGE7_PILOT_CASE_ORDER,
    EvaluationGoldV3,
    EvaluationSplitManifestV3,
    load_evaluation_gold_v3,
    registry_contract_sha256_v3,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOLD_PATH = PROJECT_ROOT / "evaluation" / "v3" / "gold.v3.json"
SPLIT_PATH = PROJECT_ROOT / "evaluation" / "v3" / "split.v3.json"


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
    assert loaded.required_fact_count == 312
    assert loaded.gold_sha256 == (
        "8aab3d14e32feb1e0caab698b3663a1528123e5d46c503ab64297b443bcf9638"
    )
    assert loaded.split_sha256 == (
        "750c6ac84ab89b00e90847c077368a15afcef060c20a41c8c64722f3ae1b19db"
    )


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
        ("catalog_pointer", "pointer key is absent"),
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

    with pytest.raises(ValueError, match="partition the frozen registry"):
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
    ("conversation_id", "role", "attempted"),
    [
        ("held_shopping_merchant_05", "shopper", "merchant.offer.propose"),
        ("held_shopping_merchant_06", "merchant", "shopper.checkout.propose"),
    ],
)
def test_shopper_and_merchant_capabilities_are_isolated(
    conversation_id: str,
    role: str,
    attempted: str,
) -> None:
    gold, _ = _payloads()
    case = _case(gold, conversation_id)
    boundary = case["action_capability_blueprint"]

    assert case["identity_fixture"]["role"] == role
    assert [item["capability_id"] for item in boundary["attempted"]] == [attempted]
    assert boundary["allowed"] == []
    assert boundary["required"] == []
    assert boundary["expected_outcome"] == "denied"
    assert case["required_response_mode"] == "deny"


@pytest.mark.parametrize(
    ("conversation_id", "missing_scope", "attempted"),
    [
        (
            "held_shopping_merchant_03",
            "ecommerce.write",
            "shopper.checkout.propose",
        ),
        (
            "held_shopping_merchant_04",
            "merchant.write",
            "merchant.offer.propose",
        ),
    ],
)
def test_write_scope_is_required_for_proposals(
    conversation_id: str,
    missing_scope: str,
    attempted: str,
) -> None:
    gold, _ = _payloads()
    case = _case(gold, conversation_id)
    boundary = case["action_capability_blueprint"]

    assert missing_scope not in case["identity_fixture"]["scopes"]
    assert [item["capability_id"] for item in boundary["attempted"]] == [attempted]
    assert boundary["expected_outcome"] == "denied"


@pytest.mark.parametrize(
    ("conversation_id", "attempted"),
    [
        ("held_shopping_merchant_07", "shopper.checkout.execute"),
        ("held_shopping_merchant_08", "merchant.offer.execute"),
    ],
)
def test_execute_is_denied_without_confirmed_proposal(
    conversation_id: str,
    attempted: str,
) -> None:
    gold, _ = _payloads()
    case = _case(gold, conversation_id)
    boundary = case["action_capability_blueprint"]

    assert boundary["confirmation_state"] == "unconfirmed"
    assert case["sandbox_fixture"]["confirmed_proposal_id"] is None
    assert case["sandbox_fixture"]["proposal_parameters"] is None
    assert boundary["expected_outcome"] == "denied"
    assert boundary["required"] == []
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
    split["entries"][0]["prompt_family_id"] = "pf_dev_tampered"
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
