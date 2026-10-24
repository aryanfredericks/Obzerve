"""HTTP client for Obzerve's API: replay bundles, prompt versions, eval datasets and runs."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests

from obzerve.replay import ReplayBundle


class ObzerveAPIError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class ObzerveClient:
    def __init__(
        self,
        endpoint: str = "http://localhost:8000",
        api_key: str | None = None,
        timeout_s: float = 30.0,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.timeout_s = timeout_s
        self._session = requests.Session()
        if api_key:
            self._session.headers["Authorization"] = f"Bearer {api_key}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._session.request(
            method, f"{self.endpoint}{path}", timeout=self.timeout_s, **kwargs
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise ObzerveAPIError(resp.status_code, detail)
        return resp.json() if resp.content else None

    # Replay
    def replay_bundle(self, trace_id: str) -> ReplayBundle:
        return ReplayBundle.from_json(self._request("GET", f"/api/v1/traces/{trace_id}/replay"))

    def compare_traces(self, a: str, b: str) -> dict[str, Any]:
        return self._request("GET", "/api/v1/trace-comparison", params={"a": a, "b": b})

    # Prompt versions
    def register_prompt(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        """Register a prompt/model configuration; identical content returns the same version."""
        return self._request(
            "POST", f"/api/v1/prompts/{quote(name, safe='')}/versions", json={"config": config}
        )

    # Datasets and cases
    def dataset(
        self, name: str, *, version: int | None = None, include_bundles: bool = False
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"include_bundles": str(include_bundles).lower()}
        if version is not None:
            params["version"] = version
        return self._request("GET", f"/api/v1/datasets/{quote(name, safe='')}", params=params)

    def create_case(
        self,
        dataset: str,
        trace_id: str,
        name: str,
        assertions: list[dict[str, Any]],
        notes: str | None = None,
        replay_policy: str = "llm-live",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/datasets/{quote(dataset, safe='')}/cases",
            json={
                "trace_id": trace_id,
                "name": name,
                "assertions": assertions,
                "notes": notes,
                "replay_policy": replay_policy,
            },
        )

    # Eval runs
    def submit_run(self, run: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/v1/eval-runs", json=run)

    def compare_runs(self, baseline: str, candidate: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/eval-runs/compare/{baseline}/{candidate}")

    def gate(self, baseline: str, candidate: str, **criteria: Any) -> dict[str, Any]:
        params = {k: v for k, v in criteria.items() if v is not None}
        return self._request("GET", f"/api/v1/eval-runs/gate/{baseline}/{candidate}", params=params)
