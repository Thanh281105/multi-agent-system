"""Minimal authenticated Qdrant REST adapter for sample knowledge retrieval."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

from app.knowledge.embedding import HashingTextEmbedder
from app.shared.embedding_runtime import EmbeddingRuntime

Transport = Callable[[str, str, dict[str, Any] | None], tuple[int, Any]]


class KnowledgeStoreUnavailableError(RuntimeError):
    """Raised when Qdrant cannot satisfy a bounded request."""


class KnowledgeStoreContractError(RuntimeError):
    """Raised when Qdrant returns an incompatible response shape."""


class QdrantKnowledgeStore:
    """Versioned vector knowledge adapter with explicit sample-data metadata."""

    def __init__(
        self,
        base_url: str,
        *,
        collection: str = "sample_market_knowledge",
        api_key: str = "",
        timeout_seconds: float = 3,
        embedder: EmbeddingRuntime | None = None,
        transport: Transport | None = None,
        request_retries: int = 2,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("qdrant base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "qdrant base_url must not contain credentials/query/fragment"
            )
        if not collection or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in collection
        ):
            raise ValueError("qdrant collection contains unsupported characters")
        if not 0.1 <= timeout_seconds <= 30:
            raise ValueError("qdrant timeout_seconds must be between 0.1 and 30")
        if not 0 <= request_retries <= 5:
            raise ValueError("request_retries must be between 0 and 5")
        self.base_url = base_url.rstrip("/")
        self.collection = collection
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.embedder = embedder or HashingTextEmbedder()
        self._transport = transport or self._http_transport
        self._request_retries = request_retries

    def ready(self) -> bool:
        """Verify service health and the usable seeded collection contract."""

        self._call("GET", "/readyz", accepted_statuses=(200,))
        path = f"/collections/{quote(self.collection, safe='')}"
        _, body = self._call("GET", path, accepted_statuses=(200,))
        self._validate_collection(body, require_points=True)
        return True

    def ensure_collection(self) -> None:
        path = f"/collections/{quote(self.collection, safe='')}"
        status, body = self._call(
            "GET",
            path,
            accepted_statuses=(200, 404),
        )
        if status == 200:
            self._validate_collection(body)
            return

        status, body = self._call(
            "PUT",
            path,
            payload={
                "vectors": {
                    "size": self.embedder.dimensions,
                    "distance": "Cosine",
                }
            },
            accepted_statuses=(200, 409),
        )
        if status == 409:
            _, body = self._call("GET", path, accepted_statuses=(200,))
            self._validate_collection(body)
            return
        self._require_ok(body)

    def upsert_documents(self, documents: Iterable[Mapping[str, str]]) -> int:
        points: list[dict[str, Any]] = []
        for document in documents:
            document_id = document.get("id", "").strip()
            title = document.get("title", "").strip()
            content = document.get("content", "").strip()
            if not document_id or not title or not content:
                raise ValueError("knowledge documents require id, title and content")
            points.append(
                {
                    "id": str(
                        uuid5(
                            NAMESPACE_URL,
                            f"ecommerce-multi-agent:{document_id}",
                        )
                    ),
                    "vector": self.embedder.embed(f"{title} {content}"),
                    "payload": {
                        "document_id": document_id,
                        "title": title,
                        "content": content,
                        "source": "sample_thesis_dataset",
                        "sample_data": True,
                        "embedding_method": self.embedder.method,
                    },
                }
            )
        if not points:
            raise ValueError("at least one knowledge document is required")
        path = f"/collections/{quote(self.collection, safe='')}/points?wait=true"
        _, body = self._call(
            "PUT",
            path,
            payload={"points": points},
            accepted_statuses=(200,),
        )
        self._require_ok(body)
        return len(points)

    def search(self, query: str, limit: int = 3) -> dict[str, Any]:
        cleaned = query.strip()
        if not cleaned:
            raise ValueError("query must not be blank")
        if not 1 <= limit <= 10:
            raise ValueError("limit must be between 1 and 10")
        path = f"/collections/{quote(self.collection, safe='')}/points/query"
        _, body = self._call(
            "POST",
            path,
            payload={
                "query": self.embedder.embed(cleaned),
                "limit": limit,
                "with_payload": True,
                "with_vector": False,
            },
            accepted_statuses=(200,),
        )
        self._require_ok(body)
        points = self._extract_points(body)
        matches: list[dict[str, Any]] = []
        for point in points:
            payload = point.get("payload")
            score = point.get("score")
            if not isinstance(payload, dict) or not isinstance(score, (int, float)):
                raise KnowledgeStoreContractError("qdrant point payload is invalid")
            if float(score) <= 0:
                continue
            document_id = payload.get("document_id")
            title = payload.get("title")
            content = payload.get("content")
            if not all(
                isinstance(value, str) for value in (document_id, title, content)
            ):
                raise KnowledgeStoreContractError("qdrant document fields are invalid")
            matches.append(
                {
                    "id": document_id,
                    "title": title,
                    "content": content,
                    "score": round(float(score), 6),
                    "source": str(payload.get("source", "sample_thesis_dataset")),
                    "sample_data": bool(payload.get("sample_data", True)),
                }
            )
        return {
            "count": len(matches),
            "documents": matches,
            "query": cleaned,
            "method": f"qdrant_{self.embedder.method}",
        }

    def _validate_collection(self, body: Any, *, require_points: bool = False) -> None:
        self._require_ok(body)
        try:
            vectors = body["result"]["config"]["params"]["vectors"]
            size = vectors["size"]
            distance = vectors["distance"]
        except (KeyError, TypeError) as exc:
            raise KnowledgeStoreContractError(
                "qdrant collection configuration is invalid"
            ) from exc
        if size != self.embedder.dimensions or str(distance).casefold() != "cosine":
            raise KnowledgeStoreContractError(
                "qdrant collection vector configuration does not match runtime"
            )
        if require_points:
            try:
                points_count = body["result"]["points_count"]
            except (KeyError, TypeError) as exc:
                raise KnowledgeStoreContractError(
                    "qdrant collection point count is invalid"
                ) from exc
            if (
                not isinstance(points_count, int)
                or isinstance(points_count, bool)
                or points_count < 1
            ):
                raise KnowledgeStoreContractError(
                    "qdrant collection must contain seeded knowledge points"
                )

    @staticmethod
    def _extract_points(body: Any) -> list[dict[str, Any]]:
        try:
            result = body["result"]
            points = result["points"] if isinstance(result, dict) else result
        except (KeyError, TypeError) as exc:
            raise KnowledgeStoreContractError(
                "qdrant query response is invalid"
            ) from exc
        if not isinstance(points, list) or not all(
            isinstance(point, dict) for point in points
        ):
            raise KnowledgeStoreContractError("qdrant query points are invalid")
        return points

    @staticmethod
    def _require_ok(body: Any) -> None:
        if not isinstance(body, dict) or body.get("status") != "ok":
            raise KnowledgeStoreContractError("qdrant response status is invalid")

    def _call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        accepted_statuses: tuple[int, ...],
    ) -> tuple[int, Any]:
        last_status = 0
        for attempt in range(self._request_retries + 1):
            try:
                status, body = self._transport(method, path, payload)
            except KnowledgeStoreUnavailableError:
                if attempt >= self._request_retries:
                    raise
                time.sleep(0.05 * (attempt + 1))
                continue
            last_status = status
            if status in accepted_statuses:
                return status, body
            if status not in {429, 502, 503, 504} or attempt >= self._request_retries:
                break
            time.sleep(0.05 * (attempt + 1))
        raise KnowledgeStoreUnavailableError(
            f"qdrant request failed with HTTP status {last_status or 'unknown'}"
        )

    def _http_transport(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> tuple[int, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["api-key"] = self.api_key
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return response.status, self._decode_body(response.read())
        except HTTPError as exc:
            return exc.code, self._decode_body(exc.read())
        except (URLError, TimeoutError, OSError) as exc:
            raise KnowledgeStoreUnavailableError("qdrant transport failed") from exc

    @staticmethod
    def _decode_body(raw: bytes) -> Any:
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
