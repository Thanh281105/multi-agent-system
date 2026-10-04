"""A clearer work source must preserve the frozen corpus and gold evidence."""

import hashlib
import json
from pathlib import Path

from app.knowledge.ingestion import validate_manifest_pair, validate_publish_readiness
from app.knowledge.v2_contracts import BookMappingManifest, SourceManifest

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / "data/knowledge/books-v1"
NEW = ROOT / "data/knowledge/books-v2"


def test_authorship_revision_preserves_frozen_gold_and_other_sources():
    gold = json.loads((ROOT / "evaluation/v3/gold.v3.json").read_bytes())
    assert (
        hashlib.sha256((OLD / "sources.json").read_bytes()).hexdigest()
        == gold["source_assets"]["sources"]["sha256"]
    )
    original = SourceManifest.model_validate_json((OLD / "sources.json").read_bytes())
    revised = SourceManifest.model_validate_json((NEW / "sources.json").read_bytes())
    old_by_id = {source.source_id: source for source in original.sources}
    new_by_id = {source.source_id: source for source in revised.sources}
    assert len(old_by_id) == len(new_by_id) == 20
    assert set(old_by_id) == set(new_by_id)
    changed = {
        source_id
        for source_id in old_by_id
        if old_by_id[source_id] != new_by_id[source_id]
    }
    assert changed == {"src_sapiens_author"}
    source = new_by_id["src_sapiens_author"]
    assert source.content_markdown.startswith(
        old_by_id["src_sapiens_author"].content_markdown
    )
    assert "Yuval Noah Harari is the author of Sapiens" in source.content_markdown
    assert source.metadata.authors == ("Yuval Noah Harari",)
    assert source.support_scope.value == "work" and source.edition_identifier is None
    assert source.source_version_id != old_by_id["src_sapiens_author"].source_version_id


def test_authorship_revision_keeps_mapping_decisions_and_frozen_source_spans():
    sources = SourceManifest.model_validate_json((NEW / "sources.json").read_bytes())
    mappings = BookMappingManifest.model_validate_json(
        (NEW / "mappings.json").read_bytes()
    )
    original = BookMappingManifest.model_validate_json(
        (OLD / "mappings.json").read_bytes()
    )
    validate_manifest_pair(sources, mappings)
    validate_publish_readiness(mappings)
    assert sources.corpus_version == mappings.corpus_version != original.corpus_version
    assert mappings.mappings == original.mappings and len(mappings.mappings) == 200
    by_id = {source.source_id: source for source in sources.sources}
    gold = json.loads((ROOT / "evaluation/v3/gold.v3.json").read_bytes())
    for case in gold["conversations"]:
        for fact in case["required_fact_blueprints"]:
            support = fact["support"]
            if support["kind"] == "source_excerpt":
                content = by_id[support["record_id"]].content_markdown
                assert (
                    content[support["start"] : support["end"]]
                    == support["exact_excerpt"]
                )
