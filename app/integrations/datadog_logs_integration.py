"""
Datadog Logs Integration Client

Queries application logs from Datadog Logs Search API v2.
Mirrors the CloudWatchLogsIntegration interface so the service layer is
provider-agnostic.

Tenant isolation: every query is scoped to `kube_namespace:{tenant}-ns`, a tag
the Datadog Agent stamps on every container log line when running as a
DaemonSet on EKS with log collection enabled.

Service identification: uses the `kube_deployment` tag (exact match, stable
across pod restarts) rather than wildcarding on `pod_name`.

auth_config shape (from log_provider_config.auth_config JSONB):
    {
        "credentials_secret_arn": "arn:aws:secretsmanager:...:secret:obstool/datadog/<tenant>-XXXXXX",
        "site": "datadoghq.eu",
        # Optional cross-account AWS credentials for fetching the secret.
        # If omitted, the default boto3 credential chain is used.
        "assume_role_arn": "arn:aws:iam::...:role/...",
        "external_id": "..."
    }

The Secrets Manager secret body must be JSON:
    {"api_key": "...", "app_key": "..."}

Credentials are never stored plaintext in the DB — only the ARN pointer lives
there. The integration resolves the ARN on first use and caches the result
for the lifetime of the instance.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_SITE = "datadoghq.com"
DEFAULT_TIMEOUT = 30.0
SEARCH_ENDPOINT = "/api/v2/logs/events/search"
AGGREGATE_ENDPOINT = "/api/v2/logs/analytics/aggregate"
VALIDATE_ENDPOINT = "/api/v1/validate"


class DatadogLogsError(Exception):
    """Raised when a Datadog Logs operation fails."""


class DatadogLogsIntegration:
    """Async client for querying Datadog Logs."""

    def __init__(self, auth_config: dict | None = None):
        auth_config = auth_config or {}
        self._secret_arn: str = auth_config.get("credentials_secret_arn", "")
        self._site: str = auth_config.get("site") or DEFAULT_SITE
        self._base_url = f"https://api.{self._site}"
        # AWS auth for fetching the secret (same shape CloudWatchLogsIntegration uses)
        self._aws_auth_config: dict = {
            k: v for k, v in auth_config.items()
            if k in ("assume_role_arn", "external_id", "region", "profile")
        }
        # Cached credentials — resolved lazily on first API call
        self._api_key: str = ""
        self._app_key: str = ""
        self._creds_resolved: bool = False

    async def _ensure_credentials(self) -> None:
        """Fetch api_key/app_key from Secrets Manager on first use.

        Raises DatadogLogsError if the secret is missing or malformed.
        """
        if self._creds_resolved:
            return
        if not self._secret_arn:
            raise DatadogLogsError(
                "Datadog auth_config missing credentials_secret_arn — "
                "credentials must be stored in AWS Secrets Manager"
            )

        # Imported here to avoid a circular dep at module load
        from app.integrations.aws_integration import AWSIntegration

        try:
            secret = await AWSIntegration.get_secret(self._aws_auth_config, self._secret_arn)
        except Exception as exc:
            logger.error("Failed to fetch Datadog credentials from %s: %s", self._secret_arn, exc)
            raise DatadogLogsError(f"Failed to fetch Datadog credentials from Secrets Manager: {exc}") from exc

        value = secret.get("value") if isinstance(secret, dict) else secret
        if not isinstance(value, dict):
            raise DatadogLogsError(
                "Datadog credentials secret must be JSON with api_key and app_key fields"
            )
        self._api_key = value.get("api_key", "")
        self._app_key = value.get("app_key", "")
        if not self._api_key or not self._app_key:
            raise DatadogLogsError(
                "Datadog credentials secret is missing api_key and/or app_key"
            )
        self._creds_resolved = True

    # ── Public API (mirrors CloudWatchLogsIntegration) ──────────────────

    async def get_log_events(
        self,
        namespace: str,
        start: datetime,
        end: datetime,
        service_name: str | None = None,
        search: str | None = None,
        level: str | None = None,
        filter_pattern: str | None = None,
        limit: int = 500,
        next_token: str | None = None,
    ) -> dict[str, Any]:
        """Fetch log events oldest-first (to match CloudWatch contract).

        `filter_pattern` is accepted for parity with the CloudWatch signature
        but ignored — Datadog uses a completely different query language, so
        we always build the query from raw `search`/`level`.
        """
        query = self._build_query(namespace, service_name, search, level)
        return await self._search(query, start, end, limit, next_token, sort="timestamp")

    async def get_log_events_newest(
        self,
        namespace: str,
        start: datetime,
        end: datetime,
        service_name: str | None = None,
        search: str | None = None,
        level: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Fetch the newest log events first.

        Datadog natively supports reverse sort (`sort: -timestamp`), so this is
        a single API call — no CloudWatch-style smart-window loop needed.

        Returns the same shape CloudWatch's get_log_events_newest does:
            {events, nextToken, has_older, oldest_timestamp}
        """
        query = self._build_query(namespace, service_name, search, level)
        result = await self._search(query, start, end, limit, None, sort="-timestamp")
        events = result.get("events", [])
        next_cursor = result.get("nextToken")
        oldest_timestamp = events[-1]["timestamp"] if events else None
        return {
            "events": events,
            "nextToken": None,
            # Datadog's cursor means "there are more results older than the last
            # one we returned" — exactly what has_older tracks for the frontend.
            "has_older": next_cursor is not None,
            "oldest_timestamp": oldest_timestamp,
        }

    async def get_log_streams(
        self,
        namespace: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List pods (log streams) in a namespace by scanning recent logs and
        deduping on the `pod_name` tag.

        Datadog doesn't expose a native "list pods" endpoint for logs, so we
        approximate it by pulling the N most recent events and extracting
        unique pod names.
        """
        query = f"kube_namespace:{namespace}"
        now = datetime.now(timezone.utc)
        # Look back 1 hour for recent pods
        from datetime import timedelta
        start = now - timedelta(hours=1)

        try:
            result = await self._search(query, start, now, limit * 5, None, sort="-timestamp")
        except DatadogLogsError as exc:
            logger.warning("get_log_streams: search failed: %s", exc)
            return []

        streams: dict[str, dict[str, Any]] = {}
        for event in result.get("events", []):
            pod = event.get("logStreamName") or ""
            if not pod:
                continue
            ts = event.get("timestamp", 0)
            existing = streams.get(pod)
            if existing is None:
                streams[pod] = {
                    "logStreamName": pod,
                    "lastEventTimestamp": ts,
                    "firstEventTimestamp": ts,
                }
            else:
                if ts > existing["lastEventTimestamp"]:
                    existing["lastEventTimestamp"] = ts
                if ts < existing["firstEventTimestamp"]:
                    existing["firstEventTimestamp"] = ts
            if len(streams) >= limit:
                break

        return sorted(
            streams.values(),
            key=lambda s: s["lastEventTimestamp"],
            reverse=True,
        )

    async def get_log_volume(
        self,
        namespace: str,
        start: datetime,
        end: datetime,
        service_name: str | None = None,
        interval_minutes: int = 5,
    ) -> list[dict[str, Any]]:
        """Get log volume over time for histogram visualization.

        Uses the Datadog Logs Analytics aggregate endpoint with a timestamp
        histogram bucket.
        """
        await self._ensure_credentials()
        query = self._build_query(namespace, service_name, None, None)
        body = {
            "filter": {
                "query": query,
                "from": self._to_iso(start),
                "to": self._to_iso(end),
            },
            "compute": [
                {"aggregation": "count", "type": "total"},
            ],
            "group_by": [
                {
                    "facet": "@timestamp",
                    "type": "timestamp",
                    "interval": f"{interval_minutes}m",
                }
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.post(
                    f"{self._base_url}{AGGREGATE_ENDPOINT}",
                    json=body,
                    headers=self._headers(),
                )
                if response.status_code == 404:
                    return []
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            logger.error("Datadog aggregate failed (%s): %s", exc.response.status_code, exc.response.text)
            raise DatadogLogsError(f"Failed to get log volume: {exc}") from exc
        except httpx.HTTPError as exc:
            logger.error("Datadog aggregate request failed: %s", exc)
            raise DatadogLogsError(f"Failed to get log volume: {exc}") from exc

        # Response shape:
        # {"data": {"buckets": [{"by": {"@timestamp": "<iso>"}, "computes": {"c0": <count>}}]}}
        buckets = payload.get("data", {}).get("buckets", []) or []
        volume: list[dict[str, Any]] = []
        for bucket in buckets:
            by = bucket.get("by", {}) or {}
            computes = bucket.get("computes", {}) or {}
            ts = by.get("@timestamp", "")
            count_val = 0
            if computes:
                # Grab the first compute value (we only asked for one)
                count_val = int(next(iter(computes.values()), 0) or 0)
            volume.append({"timestamp": ts, "count": count_val})
        return volume

    async def health(self) -> bool:
        """Check Datadog credentials are valid via GET /api/v1/validate."""
        try:
            await self._ensure_credentials()
        except DatadogLogsError:
            return False
        if not self._api_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.get(
                    f"{self._base_url}{VALIDATE_ENDPOINT}",
                    headers={"DD-API-KEY": self._api_key},
                )
                return response.status_code == 200
        except httpx.HTTPError as exc:
            logger.warning("Datadog health check failed: %s", exc)
            return False

    @staticmethod
    def build_filter_pattern(
        search: str | None = None,
        level: str | None = None,
    ) -> str | None:
        """Build a Datadog query fragment from search/level.

        Datadog query syntax is space-separated AND. Level maps to `status:*`.
        Example: build_filter_pattern("payment failed", "error")
          → 'status:error "payment failed"'
        """
        parts: list[str] = []
        if level:
            normalized = level.strip().lower()
            # Common aliases
            if normalized in ("warn", "warning"):
                normalized = "warn"
            parts.append(f"status:{normalized}")
        if search:
            # Quote the search term if it contains spaces
            if " " in search:
                parts.append(f'"{search}"')
            else:
                parts.append(search)
        return " ".join(parts) if parts else None

    # ── Internals ────────────────────────────────────────────────────────

    async def _search(
        self,
        query: str,
        start: datetime,
        end: datetime,
        limit: int,
        cursor: str | None,
        sort: str,
    ) -> dict[str, Any]:
        """Call POST /api/v2/logs/events/search and normalize the response."""
        await self._ensure_credentials()
        body: dict[str, Any] = {
            "filter": {
                "query": query,
                "from": self._to_iso(start),
                "to": self._to_iso(end),
            },
            "sort": sort,
            "page": {"limit": min(limit, 1000)},
        }
        if cursor:
            body["page"]["cursor"] = cursor

        logger.info(
            "Datadog logs search: site=%s query=%r from=%s to=%s limit=%d sort=%s",
            self._site, query, body["filter"]["from"], body["filter"]["to"], body["page"]["limit"], sort,
        )
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                response = await client.post(
                    f"{self._base_url}{SEARCH_ENDPOINT}",
                    json=body,
                    headers=self._headers(),
                )
                if response.status_code == 404:
                    return {"events": [], "nextToken": None}
                response.raise_for_status()
                payload = response.json()
                logger.info(
                    "Datadog logs search returned %d events (has_more_cursor=%s)",
                    len(payload.get("data", []) or []),
                    bool(payload.get("meta", {}).get("page", {}).get("after")),
                )
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Datadog logs search failed (%s): %s",
                exc.response.status_code,
                exc.response.text[:500],
            )
            raise DatadogLogsError(f"Failed to query logs: {exc}") from exc
        except httpx.HTTPError as exc:
            logger.error("Datadog logs search request failed: %s", exc)
            raise DatadogLogsError(f"Failed to query logs: {exc}") from exc

        events = [self._normalize_event(e) for e in payload.get("data", []) or []]
        next_cursor = (
            payload.get("meta", {}).get("page", {}).get("after")
            if isinstance(payload.get("meta"), dict)
            else None
        )
        return {"events": events, "nextToken": next_cursor}

    def _build_query(
        self,
        namespace: str,
        service_name: str | None,
        search: str | None,
        level: str | None,
    ) -> str:
        """Compose the full Datadog query string with tenant + service scope."""
        parts: list[str] = [f"kube_namespace:{namespace}"]
        if service_name:
            # kube_deployment is the cleanest service identifier — exact match,
            # stable across pod restarts. Requires the Datadog Cluster Agent
            # (enabled by default in the Operator-managed install).
            parts.append(f"kube_deployment:{service_name}")
        extras = self.build_filter_pattern(search=search, level=level)
        if extras:
            parts.append(extras)
        return " ".join(parts)

    def _headers(self) -> dict[str, str]:
        return {
            "DD-API-KEY": self._api_key,
            "DD-APPLICATION-KEY": self._app_key,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _normalize_event(raw: dict) -> dict[str, Any]:
        """Map a Datadog log event to the CloudWatch-shaped dict the rest of
        the system expects."""
        attrs = raw.get("attributes", {}) or {}
        # Datadog nests the log attributes one level deeper
        inner = attrs.get("attributes", {}) or {}
        tags = attrs.get("tags", []) or []

        # Extract pod_name from tags (list of "key:value" strings)
        pod_name = ""
        for tag in tags:
            if isinstance(tag, str) and tag.startswith("pod_name:"):
                pod_name = tag.split(":", 1)[1]
                break
        if not pod_name:
            pod_name = inner.get("pod_name") or attrs.get("host", "") or ""

        timestamp_ms = 0
        ts_raw = attrs.get("timestamp")
        if isinstance(ts_raw, str):
            try:
                dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                timestamp_ms = int(dt.timestamp() * 1000)
            except ValueError:
                timestamp_ms = 0
        elif isinstance(ts_raw, (int, float)):
            timestamp_ms = int(ts_raw)

        message = attrs.get("message", "") or ""

        return {
            "timestamp": timestamp_ms,
            "message": message,
            "logStreamName": pod_name,
            "eventId": raw.get("id", ""),
        }

    @staticmethod
    def _to_iso(dt: datetime) -> str:
        """Convert datetime to ISO8601 with Z suffix (Datadog expects UTC)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
