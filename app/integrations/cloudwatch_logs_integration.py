"""
CloudWatch Logs Integration Client

Queries application logs from AWS CloudWatch Logs.
Tenant isolation enforced by log group naming: /devlift/eks/{tenant}-ns

Fluent Bit DaemonSet on EKS ships container stdout/stderr to CloudWatch with:
  - Log Group:  /devlift/eks/{namespace}
  - Log Stream: {pod_name}

Since namespace = "{tenant}-ns", logs are naturally tenant-isolated.
The backend always derives the log group from the authenticated tenant's JWT.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aioboto3

from app.core.config import settings

logger = logging.getLogger(__name__)

# ── CloudWatch-specific constants for newest-first smart window ──
INITIAL_WINDOW_SECONDS_ANCHORED = 60
INITIAL_WINDOW_SECONDS_FALLBACK = 600
MAX_SMART_WINDOW_ATTEMPTS = 15
MAX_EXPANSION_HOURS = 24
MIN_WINDOW_SECONDS = 1
MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.2


class CloudWatchLogsIntegration:
    """Async client for querying CloudWatch Logs."""

    def __init__(
        self,
        region: str | None = None,
        log_group_prefix: str | None = None,
        auth_config: dict | None = None,
    ):
        self.region = region or settings.cloudwatch_logs_region
        self.log_group_prefix = log_group_prefix or settings.cloudwatch_logs_group_prefix
        self._auth_config = auth_config
        self._session = aioboto3.Session()

    async def _get_client(self, service_name: str = "logs"):
        """Create an authenticated CloudWatch client.

        Uses auth_config credentials (with AssumeRole support) when available,
        otherwise falls back to default credentials.
        """
        if self._auth_config:
            from app.integrations.aws_integration import AWSIntegration
            session_kwargs = await AWSIntegration._get_client_kwargs(self._auth_config)
            session = aioboto3.Session(**session_kwargs)
            return session.client(service_name, region_name=self.region)
        return self._session.client(service_name, region_name=self.region)

    def _log_group(self, namespace: str) -> str:
        """Build the log group name for a namespace.

        Fluent Bit writes to: /devlift/eks/{namespace}
        Namespace is always {tenant}-ns, so logs are tenant-scoped.
        """
        return f"{self.log_group_prefix}/{namespace}"

    async def _call_filter_with_retry(self, **params) -> dict:
        """Call FilterLogEvents with exponential backoff on ThrottlingException.

        AWS FilterLogEvents TPS limits are per-account, per-region, and NOT adjustable:
          us-east-1: 25 TPS, most regions: 10 TPS, some: 5 TPS.
        The smart window loop can make several rapid calls, so we retry on throttle.
        """
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with await self._get_client() as client:
                    return await client.filter_log_events(**params)
            except Exception as exc:
                if "ThrottlingException" in type(exc).__name__ or "Throttling" in str(exc):
                    if attempt == MAX_RETRIES:
                        raise
                    delay = RETRY_BASE_DELAY * (2 ** attempt)  # 0.2s, 0.4s, 0.8s
                    logger.warning(
                        "FilterLogEvents throttled, retry %d/%d in %.1fs",
                        attempt + 1, MAX_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
        # Unreachable — loop either returns or raises.
        raise CloudWatchLogsError("FilterLogEvents retry loop exhausted")

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
        """
        Fetch log events from a namespace's log group (oldest-first).

        Args:
            namespace: K8s namespace (e.g., "aspora-ns")
            start: Start time (UTC)
            end: End time (UTC)
            service_name: Filter to a specific service's pods (e.g., "payment-api")
            search: Free-text search term. Converted to filter_pattern internally.
            level: Log level filter. Converted to filter_pattern internally.
            filter_pattern: Pre-built CloudWatch filter pattern. Used internally by
                the smart window loop to avoid rebuilding on every call. If None and
                search/level are provided, a pattern is built from them.
            limit: Max events to return
            next_token: Pagination token from previous response

        Returns:
            {
                "events": [{"timestamp": int, "message": str, "logStreamName": str}, ...],
                "nextToken": str | None
            }
        """
        if filter_pattern is None and (search or level):
            filter_pattern = self.build_filter_pattern(search=search, level=level)

        log_group = self._log_group(namespace)
        params: dict[str, Any] = {
            "logGroupName": log_group,
            "startTime": self._to_epoch_ms(start),
            "endTime": self._to_epoch_ms(end),
            "limit": limit,
            "interleaved": True,
        }
        if service_name:
            params["logStreamNamePrefix"] = service_name
        if filter_pattern:
            params["filterPattern"] = filter_pattern
        if next_token:
            params["nextToken"] = next_token

        try:
            response = await self._call_filter_with_retry(**params)
            return {
                "events": [
                    {
                        "timestamp": e["timestamp"],
                        "message": self._parse_log_message(e["message"]),
                        "logStreamName": e.get("logStreamName", ""),
                        "eventId": e.get("eventId", ""),
                    }
                    for e in response.get("events", [])
                ],
                "nextToken": response.get("nextToken"),
            }
        except Exception as exc:
            if "ResourceNotFoundException" in type(exc).__name__ or "ResourceNotFoundException" in str(exc):
                logger.info("Log group not found: %s — service not deployed yet", log_group)
                return {"events": [], "nextToken": None}
            logger.error("CloudWatch query failed for %s: %s", log_group, exc)
            raise CloudWatchLogsError(f"Failed to query logs: {exc}") from exc

    async def get_log_streams(
        self,
        namespace: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        List log streams (pods) in a namespace's log group.

        Returns:
            List of {"logStreamName": str, "lastEventTimestamp": int}
        """
        log_group = self._log_group(namespace)
        try:
            async with await self._get_client() as client:
                response = await client.describe_log_streams(
                    logGroupName=log_group,
                    orderBy="LastEventTime",
                    descending=True,
                    limit=limit,
                )
                return [
                    {
                        "logStreamName": s["logStreamName"],
                        "lastEventTimestamp": s.get("lastEventTimestamp", 0),
                        "firstEventTimestamp": s.get("firstEventTimestamp", 0),
                    }
                    for s in response.get("logStreams", [])
                ]
        except Exception as exc:
            if "ResourceNotFoundException" in type(exc).__name__ or "ResourceNotFoundException" in str(exc):
                logger.info("Log group not found: %s — no streams yet", log_group)
                return []
            logger.error("Failed to list log streams for %s: %s", log_group, exc)
            raise CloudWatchLogsError(f"Failed to list log streams: {exc}") from exc

    async def _describe_log_streams_page(
        self,
        namespace: str,
        next_token: str | None = None,
    ) -> dict:
        """Single page of DescribeLogStreams ordered by LastEventTime (descending)."""
        log_group = self._log_group(namespace)
        params: dict[str, Any] = {
            "logGroupName": log_group,
            "orderBy": "LastEventTime",
            "descending": True,
            "limit": 50,
        }
        if next_token:
            params["nextToken"] = next_token
        try:
            async with await self._get_client() as client:
                response = await client.describe_log_streams(**params)
                return {
                    "streams": [
                        {
                            "logStreamName": s["logStreamName"],
                            "lastEventTimestamp": s.get("lastEventTimestamp", 0),
                        }
                        for s in response.get("logStreams", [])
                    ],
                    "nextToken": response.get("nextToken"),
                }
        except Exception as exc:
            if "ResourceNotFoundException" in type(exc).__name__ or "ResourceNotFoundException" in str(exc):
                return {"streams": [], "nextToken": None}
            raise

    async def get_log_events_newest(
        self,
        namespace: str,
        start: datetime,
        end: datetime,
        service_name: str | None = None,
        search: str | None = None,
        level: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Fetch the newest events in a time range, returned newest-first.

        CloudWatch FilterLogEvents only returns oldest-first, so we use:
          1. Pre-flight: DescribeLogStreams to find latest log timestamp
          2. Smart window: query a tight window, shrink if too full, expand if empty

        This complexity is CloudWatch-specific. Other providers (Loki, Datadog)
        implement this method as a single API call with native reverse sort.
        """
        filter_pattern = self.build_filter_pattern(search=search, level=level)

        # ── Phase 1: Pre-flight — find where logs actually exist ──
        anchor_time = end
        anchor_is_precise = False

        try:
            # Fetch up to 2 pages (100 streams) to handle namespaces with many services.
            # Cannot use logStreamNamePrefix with orderBy=LastEventTime (AWS limitation).
            all_streams: list[dict] = []
            page_token: str | None = None
            for _ in range(2):
                streams_result = await self._describe_log_streams_page(
                    namespace=namespace, next_token=page_token,
                )
                all_streams.extend(streams_result.get("streams", []))
                page_token = streams_result.get("nextToken")
                if not page_token:
                    break

            if service_name:
                all_streams = [
                    s for s in all_streams
                    if s["logStreamName"].startswith(service_name)
                ]

            if all_streams:
                latest_ts = max(s.get("lastEventTimestamp", 0) for s in all_streams)
                if latest_ts > 0:
                    candidate = datetime.fromtimestamp(
                        latest_ts / 1000,
                        tz=start.tzinfo or timezone.utc,
                    )
                    # Only trust the anchor if it's within our query range AND recent
                    # enough to guard against DescribeLogStreams eventual consistency.
                    if start <= candidate <= end:
                        staleness = (end - candidate).total_seconds()
                        if staleness <= INITIAL_WINDOW_SECONDS_FALLBACK:
                            anchor_time = candidate
                            anchor_is_precise = True
                            logger.info(
                                "Pre-flight: latest log at %s for %s/%s (%.0fs ago)",
                                anchor_time.isoformat(), namespace, service_name, staleness,
                            )
                        else:
                            logger.info(
                                "Pre-flight: anchor %s is %.0fs stale — using end_time instead",
                                candidate.isoformat(), staleness,
                            )
        except Exception as exc:
            logger.warning("Pre-flight DescribeLogStreams failed: %s — using range end", exc)

        # ── Phase 2: Smart window — shrink if too full, expand if empty ──
        # Always start with the wider 600s window regardless of anchor precision.
        # A precise anchor tells us WHERE logs live, not HOW MANY per second — a
        # low-throughput service (e.g. 12 events/min) in a 60s window yields only
        # ~12 events, which is a poor initial UX. The shrink-on-saturation logic
        # below halves the window if we hit the limit, so starting wider is safe
        # even for high-throughput services.
        window_seconds = INITIAL_WINDOW_SECONDS_FALLBACK
        last_events: list = []
        last_next_token: str | None = None
        last_window_start: datetime = start  # track for has_older

        # IMPORTANT: filter_log_events returns a nextToken even when there are no
        # more events (empirically verified — paginating once returns 0 events).
        # So `nextToken is None` is NOT a reliable "done" signal. The only trustworthy
        # signal is `len(events) < limit` — we got fewer than we asked for, meaning
        # the window is fully drained. The rare 1 MB response cap edge case (where
        # fewer events are returned despite more existing) is accepted as a minor
        # inaccuracy — far better than the alternative of shrinking to 1s forever.
        for _ in range(MAX_SMART_WINDOW_ATTEMPTS):
            window_start = max(
                anchor_time - timedelta(seconds=window_seconds),
                start,
            )
            window_end = end
            last_window_start = window_start

            result = await self.get_log_events(
                namespace=namespace,
                start=window_start,
                end=window_end,
                service_name=service_name,
                filter_pattern=filter_pattern,
                limit=limit,
            )
            events = result.get("events", [])
            next_token = result.get("nextToken")
            last_events = events
            last_next_token = next_token

            if len(events) == 0:
                # Empty window → EXPAND toward range start (regardless of spurious token)
                if window_start <= start:
                    break  # Covered full range — no logs
                window_seconds *= 2
                if window_seconds > MAX_EXPANSION_HOURS * 3600:
                    window_seconds = MAX_EXPANSION_HOURS * 3600
                continue

            if len(events) < limit:
                # Fewer events than requested → we have all of them in this window. Done.
                break

            # len(events) == limit → window is saturated, more events exist.
            # SHRINK toward end_time to isolate the newest slice.
            window_seconds = max(window_seconds // 2, MIN_WINDOW_SECONDS)
            if window_seconds <= MIN_WINDOW_SECONDS:
                # Even 1s hit the limit (>500 events/sec). Paginate forward within
                # the 1s window to collect all, then take the newest.
                all_events_in_window = list(events)
                page_token = next_token
                while page_token and len(all_events_in_window) < limit * 20:
                    page_result = await self.get_log_events(
                        namespace=namespace,
                        start=window_start,
                        end=window_end,
                        service_name=service_name,
                        filter_pattern=filter_pattern,
                        limit=limit,
                        next_token=page_token,
                    )
                    page_events = page_result.get("events", [])
                    if not page_events:
                        break
                    all_events_in_window.extend(page_events)
                    page_token = page_result.get("nextToken")
                last_events = all_events_in_window
                last_next_token = page_token
                break

        all_events = last_events

        # Take the newest `limit` events (events are oldest-first within the window)
        newest_events = all_events[-limit:] if len(all_events) > limit else all_events

        # Reverse to newest-first
        newest_events = list(reversed(newest_events))

        has_older = (
            len(all_events) > limit          # We trimmed — more exist
            or last_window_start > start      # Haven't covered full range
        )

        oldest_timestamp = newest_events[-1]["timestamp"] if newest_events else None

        return {
            "events": newest_events,
            "nextToken": None,
            "has_older": has_older,
            "oldest_timestamp": oldest_timestamp,
        }

    async def start_query(
        self,
        namespace: str,
        start: datetime,
        end: datetime,
        query: str,
    ) -> str:
        """
        Start a CloudWatch Logs Insights query (for aggregations/volume).

        Args:
            namespace: K8s namespace
            start: Start time
            end: End time
            query: CloudWatch Logs Insights query string

        Returns:
            Query ID (use get_query_results to poll for results)
        """
        log_group = self._log_group(namespace)
        try:
            async with await self._get_client() as client:
                response = await client.start_query(
                    logGroupName=log_group,
                    startTime=int(start.timestamp()),
                    endTime=int(end.timestamp()),
                    queryString=query,
                )
                return response["queryId"]
        except Exception as exc:
            if "ResourceNotFoundException" in type(exc).__name__ or "ResourceNotFoundException" in str(exc):
                logger.info("Log group not found for query: %s", log_group)
                return ""
            raise CloudWatchLogsError(f"Failed to start log query: {exc}") from exc

    async def get_query_results(self, query_id: str) -> dict[str, Any]:
        """
        Get results of a Logs Insights query.

        Returns:
            {"status": "Running|Complete|Failed", "results": [[{"field": str, "value": str}]]}
        """
        try:
            async with await self._get_client() as client:
                response = await client.get_query_results(queryId=query_id)
                return {
                    "status": response["status"],
                    "results": response.get("results", []),
                }
        except Exception as exc:
            if "ResourceNotFoundException" in type(exc).__name__ or "ResourceNotFoundException" in str(exc):
                logger.warning("Query ID not found: %s — query may have expired", query_id)
                return {"status": "Failed", "results": []}
            raise CloudWatchLogsError(f"Failed to get query results: {exc}") from exc

    async def get_log_events_newest_first(
        self,
        namespace: str,
        start: datetime,
        end: datetime,
        service_name: str | None = None,
        search: str | None = None,
        level: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Fetch log events sorted newest first using Logs Insights.

        Unlike FilterLogEvents (oldest first only), Insights supports sort desc.
        Returns parsed log messages (Fluent Bit JSON unwrapped).
        """
        import asyncio

        # Build Insights query
        filters = []
        if service_name:
            filters.append(f"filter @logStream like /^{service_name}/")
        if search:
            filters.append(f'filter @message like /{search}/')
        if level:
            upper = level.upper()
            lower = level.lower()
            filters.append(f'filter @message like /(?i){upper}|{lower}/')

        filter_clause = " | ".join(filters)
        query = f"{filter_clause + ' | ' if filter_clause else ''}fields @timestamp, @message, @logStream | sort @timestamp desc | limit {limit}"

        query_id = await self.start_query(namespace, start, end, query)
        if not query_id:
            return {"events": [], "nextToken": None}

        # Poll for results
        result = {"status": "Running", "results": []}
        for _ in range(30):
            await asyncio.sleep(1)
            result = await self.get_query_results(query_id)
            if result["status"] in ("Complete", "Failed", "Cancelled"):
                break

        if result["status"] != "Complete":
            logger.warning("Insights query %s finished with status: %s", query_id, result["status"])
            return {"events": [], "nextToken": None}

        events = []
        for row in result.get("results", []):
            entry = {field["field"]: field["value"] for field in row}
            raw_message = entry.get("@message", "")
            events.append({
                "timestamp": int(datetime.fromisoformat(entry["@timestamp"].replace("Z", "+00:00")).timestamp() * 1000) if "@timestamp" in entry else 0,
                "message": self._parse_log_message(raw_message),
                "logStreamName": entry.get("@logStream", ""),
                "eventId": entry.get("@ptr", ""),
            })

        return {"events": events, "nextToken": None}

    async def get_log_volume(
        self,
        namespace: str,
        start: datetime,
        end: datetime,
        service_name: str | None = None,
        interval_minutes: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Get log volume over time (for histogram visualization).

        Uses Logs Insights query to count events per time bucket.
        """
        if service_name:
            query = (
                f"filter @logStream like /^{service_name}/ | "
                f"fields @timestamp | "
                f"stats count() as count by bin({interval_minutes}m) as bucket | "
                f"sort bucket asc"
            )
        else:
            query = (
                f"fields @timestamp | "
                f"stats count() as count by bin({interval_minutes}m) as bucket | "
                f"sort bucket asc"
            )
        query_id = await self.start_query(namespace, start, end, query)

        # Empty query_id means log group doesn't exist yet
        if not query_id:
            return []

        # Poll for results (Insights queries are async)
        import asyncio
        for _ in range(60):  # Max 60 seconds
            await asyncio.sleep(1)
            result = await self.get_query_results(query_id)
            if result["status"] in ("Complete", "Failed", "Cancelled"):
                break

        if result["status"] != "Complete":
            raise CloudWatchLogsError(f"Log volume query failed with status: {result['status']}")

        volume = []
        for row in result.get("results", []):
            entry = {field["field"]: field["value"] for field in row}
            volume.append({
                "timestamp": entry.get("bucket", ""),
                "count": int(float(entry.get("count", 0))),
            })
        return volume

    async def health(self) -> bool:
        """Check if CloudWatch Logs is reachable."""
        try:
            async with await self._get_client() as client:
                await client.describe_log_groups(limit=1)
                return True
        except Exception:
            return False

    @staticmethod
    def build_filter_pattern(
        search: str | None = None,
        level: str | None = None,
    ) -> str | None:
        """
        Build a CloudWatch filter pattern.

        Args:
            search: Free-text search
            level: Log level (info, warn, error, etc.)

        Returns:
            CloudWatch filter pattern string, or None if no filters
        """
        parts = []
        if level:
            parts.append(f'?"{level.upper()}" ?"{level.lower()}"')
        if search:
            parts.append(f'"{search}"')
        return " ".join(parts) if parts else None

    @staticmethod
    def _parse_log_message(raw: str) -> str:
        """Extract the actual log line from Fluent Bit JSON wrapper.

        Fluent Bit with Merge_Log + Keep_Log Off produces:
        {"time":"...","stream":"stdout","logtag":"F","message":"<actual log>","kubernetes":{...}}

        Older Fluent Bit configs (without Merge_Log) use "log" instead of "message":
        {"time":"...","stream":"stdout","_p":"F","log":"<actual message>","kubernetes":{...}}

        For structured loggers (e.g. KNative queue-proxy), the "message" value is itself
        JSON with a nested "message" key — extract the inner message in that case.

        Returns just the log content, or the raw string if not JSON.
        """
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                text = parsed.get("log") or parsed.get("message")
                if text and isinstance(text, str):
                    # Check if the value is nested JSON with its own "message"
                    try:
                        inner = json.loads(text)
                        if isinstance(inner, dict) and "message" in inner:
                            return inner["message"].rstrip("\n")
                    except (json.JSONDecodeError, TypeError):
                        pass
                    return text.rstrip("\n")
        except (json.JSONDecodeError, TypeError):
            pass
        return raw

    @staticmethod
    def _is_noise(message: str) -> bool:
        """Filter out noisy log lines like health checks."""
        noise_patterns = (
            "GET /health",
            "GET /healthz",
            "GET /readyz",
            "GET /livez",
            "health check",
        )
        msg_lower = message.lower()
        return any(p.lower() in msg_lower for p in noise_patterns)

    @staticmethod
    def _to_epoch_ms(dt: datetime) -> int:
        """Convert datetime to millisecond epoch for CloudWatch API."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)


class CloudWatchLogsError(Exception):
    """Raised when a CloudWatch Logs operation fails."""
