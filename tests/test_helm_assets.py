"""Static and configuration-contract checks for the reference Helm chart."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from app.core.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHART_ROOT = PROJECT_ROOT / "deploy" / "helm" / "ecommerce-multi-agent"
TEMPLATES = CHART_ROOT / "templates"


def _yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_chart_profiles_are_explicit_and_never_contain_plaintext_secrets() -> None:
    chart = _yaml(CHART_ROOT / "Chart.yaml")
    production = _yaml(CHART_ROOT / "values.yaml")
    kind = _yaml(CHART_ROOT / "values-kind.yaml")
    templates = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(TEMPLATES.glob("*"))
    )

    assert chart["apiVersion"] == "v2"
    assert chart["kubeVersion"].startswith(">=1.29")
    assert production["profile"] == "production"
    assert production["replicaCount"] == 1
    assert production["existingSecret"] == ""
    assert production["internalDependencies"]["enabled"] is False
    assert production["bootstrap"]["seedData"]["enabled"] is False
    assert production["bootstrap"]["seedKnowledge"]["enabled"] is False
    assert kind["profile"] == "kind"
    assert kind["existingSecret"] == "ecommerce-multi-agent-runtime"
    assert kind["internalDependencies"]["enabled"] is True
    assert kind["config"] == {
        "qdrantUrl": "http://ecommerce-multi-agent-qdrant:6333",
        "modelRuntimeMode": "off",
        "embeddingBackend": "hashing",
        "gatewayPrincipalPolicies": "kind:default:ecommerce.read",
    }
    assert "kind: Secret" not in templates
    assert "stringData:" not in templates
    assert "hostPath:" not in templates
    assert "hostNetwork:" not in templates
    assert "privileged: true" not in templates


def test_chart_schema_guards_single_replica_profiles_and_sample_seeding() -> None:
    schema = json.loads((CHART_ROOT / "values.schema.json").read_text("utf-8"))

    assert schema["properties"]["replicaCount"] == {"const": 1}
    assert schema["properties"]["existingSecret"]["minLength"] == 1
    production_rule = schema["allOf"][0]["then"]["properties"]
    assert production_rule["bootstrap"]["properties"]["seedData"]["properties"][
        "enabled"
    ] == {"const": False}
    assert production_rule["bootstrap"]["properties"]["seedKnowledge"]["properties"][
        "enabled"
    ] == {"const": False}
    assert production_rule["internalDependencies"]["properties"]["enabled"] == {
        "const": False
    }


def test_application_workloads_are_bounded_and_fail_closed() -> None:
    deployment = (TEMPLATES / "deployment.yaml").read_text(encoding="utf-8")
    migration = (TEMPLATES / "migration-job.yaml").read_text(encoding="utf-8")
    service_account = (TEMPLATES / "serviceaccount.yaml").read_text(encoding="utf-8")
    helpers = (TEMPLATES / "_helpers.tpl").read_text(encoding="utf-8")
    values = _yaml(CHART_ROOT / "values.yaml")

    for source in (deployment, migration):
        assert "automountServiceAccountToken: false" in source
        assert "toYaml .Values.securityContext" in source
        assert "toYaml .Values.podSecurityContext" in source
        assert "resources:" in source
        assert "sizeLimit: 64Mi" in source
    assert "ecommerce-multi-agent.runtimeSecretEnv" in deployment
    assert "secretKeyRef:" in migration
    assert helpers.count("secretKeyRef:") == 6
    assert "optional: true" in helpers
    assert values["securityContext"]["allowPrivilegeEscalation"] is False
    assert values["securityContext"]["readOnlyRootFilesystem"] is True
    assert values["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert values["podSecurityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert "path: /livez" in deployment
    assert "path: /readyz" in deployment
    assert deployment.index("name: migrate") < deployment.index("name: seed-data")
    assert deployment.index("name: seed-data") < deployment.index(
        "name: seed-knowledge"
    )
    assert "helm.sh/hook: pre-install,pre-upgrade" in migration
    assert "hook-delete-policy: before-hook-creation,hook-succeeded" in migration
    assert "automountServiceAccountToken: false" in service_account


def test_monitoring_reads_bearer_token_from_existing_secret() -> None:
    monitoring = (TEMPLATES / "monitoring.yaml").read_text(encoding="utf-8")

    assert "kind: ServiceMonitor" in monitoring
    assert "authorization:" in monitoring
    assert "type: Bearer" in monitoring
    assert "credentials:" in monitoring
    assert 'include "ecommerce-multi-agent.secretName"' in monitoring
    assert "key: {{ .Values.secretKeys.operationsApiKey }}" in monitoring
    assert "bearerTokenSecret:" not in monitoring
    assert "kind: PrometheusRule" in monitoring
    assert "http_request_duration_seconds_bucket" in monitoring
    assert "dependency_readiness_checks_total" in monitoring


def test_kind_dependency_images_use_verified_non_root_ids_and_persistent_paths() -> (
    None
):
    dependencies = (TEMPLATES / "internal-dependencies.yaml").read_text(
        encoding="utf-8"
    )

    for user_id in ("70", "999", "1000"):
        assert f"runAsUser: {user_id}" in dependencies
    for mount in (
        "/var/lib/postgresql/data",
        "/data",
        "/qdrant/storage",
        "/qdrant/snapshots",
    ):
        assert f"mountPath: {mount}" in dependencies
    assert dependencies.count("volumeClaimTemplates:") == 3
    assert dependencies.count("readOnlyRootFilesystem: true") == 3
    assert "REDISCLI_AUTH" in dependencies
    assert "QDRANT__TELEMETRY_DISABLED" in dependencies
    assert "QDRANT_INIT_FILE_PATH" in dependencies


def test_kind_and_production_settings_satisfy_runtime_validation() -> None:
    common: dict[str, object] = {
        "_env_file": None,
        "app_env": "production",
        "database_url": (
            "postgresql+psycopg://ecommerce:strong-db-password@postgres/ecommerce"
        ),
        "gateway_api_keys": "kind:strong-gateway-secret",
        "operations_api_key": "strong-operations-key",
        "legacy_chat_enabled": False,
        "shared_state_backend": "redis",
        "redis_url": "redis://:strong-redis-password@redis:6379/0",
        "knowledge_backend": "qdrant",
        "qdrant_api_key": "strong-qdrant-key",
    }

    kind = Settings(
        **common,
        model_runtime_mode="off",
        embedding_backend="hashing",
    )
    production = Settings(
        **common,
        model_runtime_mode="hybrid",
        embedding_backend="auto",
        openai_api_key="test-provider-secret",
    )

    assert kind.model_runtime_mode == "off"
    assert kind.embedding_backend == "hashing"
    assert production.openai_api_key_value == "test-provider-secret"


def test_grafana_dashboard_is_valid_and_covers_core_signals() -> None:
    dashboard = json.loads(
        (CHART_ROOT / "files" / "ecommerce-multi-agent-dashboard.json").read_text(
            encoding="utf-8"
        )
    )
    titles = {panel["title"] for panel in dashboard["panels"]}

    assert dashboard["uid"] == "ecommerce-multi-agent"
    assert titles == {
        "Gateway request rate",
        "Gateway error ratio",
        "HTTP latency p95",
        "Dependency readiness failures",
        "Agent operation outcomes",
    }


def test_ci_lints_and_schema_validates_both_helm_profiles() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "azure/setup-helm@v4" in workflow
    assert "helm lint deploy/helm/ecommerce-multi-agent" in workflow
    assert "helm template kind" in workflow
    assert "helm template production" in workflow
    assert "kubeconform@sha256:" in workflow
    assert "-strict -summary /manifests/ecommerce-kind.yaml" in workflow
