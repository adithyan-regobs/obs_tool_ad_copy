"""
Kong path overlap validator.

A Kong route group is identified by (tag, http_method) — the Gateway tab's
Auth/No-Auth card is NOT part of that identity. Two groups of the same method may
legitimately claim the SAME path: Kong serves the one with the higher
``regex_priority`` and shadows the other. That is the override the gateway relies
on (a public ``/health`` group at 200 shadowing the service's group at 0).

What is NOT legal is two groups claiming one path at the same priority: there is
no tie-break, so which route Kong serves is arbitrary — in practice creation
order, which nothing in devlift controls.

Until now this rule existed only in the frontend (``addPath`` in
GatewayContentV4.tsx), so every non-UI writer — the chat/MCP flow, a stale tab, a
hand-rolled curl — could plant an unrankable duplicate. This module is the
server-side rule the frontend check mirrors; keep the two in step.

Deliberately DB-free, like the other domain validators: the caller fetches the
live routes (``KongRouteConfigsRepository.list_routes_for_gateway`` already scopes
env + region and eager-loads the group) and hands them over. That keeps one rule
usable by both the Gateway save path and the chat/MCP creation path, which load
their rows very differently.
"""

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from app.domain.policies.kong_route_group_naming import default_group_key
from app.utils.kong_path_compiler import compile_route_path


@dataclass(frozen=True)
class ExistingRoute:
    """
    One live route, flattened from KongRouteConfigModel + its group.

    ``route_group_key`` is the group's tag. A route with NO group row (every row
    the legacy chat/MCP writer ever created) has no tag and no priority — see
    ``KongPathOverlapValidator.from_models`` for how those are handled.
    """

    path: str
    http_method: str
    route_group_key: str
    regex_priority: int = 0
    code: Optional[str] = None


@dataclass(frozen=True)
class PathOverlap:
    """
    One incoming path that another group of the same method already claims.

    An overlap is an OVERRIDE when the incoming priority beats the blocker's, and
    a CONFLICT otherwise. Both are reported: the caller refuses the conflicts and
    usually wants to tell the user about the overrides, since an override quietly
    changes which route Kong serves.
    """

    path: str
    http_method: str
    their_tag: str
    their_priority: int
    your_tag: str
    your_priority: int
    #: Set when the blocking route carries a code — lets a caller point at the row.
    their_code: Optional[str] = None

    @property
    def is_override(self) -> bool:
        return self.your_priority > self.their_priority

    def message(self) -> str:
        if self.is_override:
            return (
                f"{self.http_method} {self.path}: '{self.your_tag}' at priority "
                f"{self.your_priority} will override '{self.their_tag}' at "
                f"{self.their_priority}. Kong serves the higher priority; the other "
                f"group keeps the path but is shadowed."
            )
        return (
            f"{self.http_method} {self.path} is already in route group "
            f"'{self.their_tag}' at priority {self.their_priority}. Two groups "
            f"matching one path need a tie-break: give '{self.your_tag}' a "
            f"regex_priority above {self.their_priority} to override it, or add "
            f"the path under the '{self.their_tag}' tag instead."
        )


class KongPathOverlapError(ValueError):
    """
    Raised when an incoming path collides with another group it cannot out-rank.

    Carries the structured overlaps as well as the rendered strings so an API
    layer can turn them into a 409 body without re-deriving anything.
    """

    def __init__(self, conflicts: Sequence[PathOverlap]):
        self.conflicts: List[PathOverlap] = list(conflicts)
        self.errors: List[str] = [c.message() for c in self.conflicts]
        super().__init__("\n".join(self.errors))


class KongPathOverlapValidator:
    """Cross-group path overlap rules for Kong route groups."""

    @staticmethod
    def path_keys(path: str) -> Tuple[str, ...]:
        """
        Every stored spelling one path can have, for comparison.

        Compiled first, because the two sides arrive in different forms: the
        Gateway tab sends decompiled paths and the chat/MCP flow sends raw Kong
        regexes. ``compile_route_path`` is idempotent, so compiling both is safe.

        The anchored/unanchored pair mirrors ``check_route_exists``: terragrunt
        holds a handful of routes written without a trailing ``$`` (e.g.
        ``~/goblin-service/actuator/health``), and comparing only the anchored
        form would miss them — reporting a path as free while Kong already
        matches it.
        """
        compiled = compile_route_path(path)
        if compiled.endswith("$"):
            return (compiled, compiled[:-1])
        return (compiled, compiled + "$")

    @staticmethod
    def from_models(rows: Iterable, default_tag: str = "") -> List[ExistingRoute]:
        """
        Flatten KongRouteConfigModel rows (group eager-loaded) into ExistingRoute.

        A row with no group row still CLAIMS its path in terragrunt — under its own
        service's default group, which is where ``route_group_key or service_name``
        puts it in the v2 generator. Every route the legacy chat/MCP writer ever
        created is in that state, so dropping them would make the check blind to
        exactly the routes most likely to collide.

        The fallback tag is derived PER ROW from that row's own ``api_name``, not
        from the caller's service: these rows are gateway-wide, so a group-less
        route belonging to another service defaults to THAT service's group.
        Labelling them all with one caller-supplied tag would invent collisions
        between unrelated services and hide real ones.

        ``default_tag`` is the last resort for a row with neither a group nor an
        api_name. A row still unnameable after that is dropped rather than guessed
        at: a conflict reported against a group we cannot name gives the user
        nothing to act on.
        """
        out: List[ExistingRoute] = []
        for row in rows:
            group = getattr(row, "route_group", None)
            tag = (getattr(group, "route_group_key", "") or "").strip()
            if not tag:
                tag = default_group_key(getattr(row, "api_name", "") or "") or (default_tag or "").strip()
            if not tag:
                continue
            out.append(
                ExistingRoute(
                    path=getattr(row, "route_path", "") or "",
                    http_method=(getattr(row, "http_method", "") or "").strip().upper(),
                    route_group_key=tag,
                    regex_priority=int(getattr(group, "regex_priority", 0) or 0),
                    code=getattr(row, "code", None),
                )
            )
        return out

    @staticmethod
    def plan(
        paths: Sequence[str],
        http_method: str,
        route_group_key: str,
        regex_priority: int,
        existing: Sequence[ExistingRoute],
        ignore_codes: Sequence[str] = (),
    ) -> Tuple[List[PathOverlap], List[PathOverlap]]:
        """
        Classify every incoming path against the live routes. Changes nothing.

        Returns ``(conflicts, overrides)``. Used directly for a dry run — the
        Gateway tab warns while the user is still typing — and by ``validate``
        for the enforcing call.

        Rules, in order:
          - Only the SAME method can collide. A path under another method is a
            different Kong route entirely.
          - The SAME tag is the same group: re-saving a path it already owns is
            not an overlap, and neither is moving it between plugin buckets.
          - A different tag collides. It is an override only if this group's
            priority strictly beats the blocker's; equal or lower is refused.

        The blocker is the HIGHEST-priority other group holding the path, not
        merely the first found. With groups at 0 and 500 both claiming a path, an
        incoming 100 out-ranks one and is shadowed by the other — so it is still
        refused, and the message names the 500 it actually has to beat. (The
        frontend compares against a single origin row; it cannot show more than
        one owner per path, so this is the stricter of the two. Same verdict
        whenever a path has only one other owner, which is the normal case.)

        ``ignore_codes`` excludes rows the caller is itself rewriting — a route
        MOVING between groups keeps its code, and would otherwise be reported as
        conflicting with the place it is leaving.
        """
        method = (http_method or "").strip().upper()
        tag = (route_group_key or "").strip()
        mine = int(regex_priority or 0)
        skip = {c for c in ignore_codes if c}

        # Index once: same method, a DIFFERENT tag, not a row we are rewriting.
        # Keyed by every spelling of the path, so either form finds the row.
        #
        # `already_mine` is the same scan for the TARGET group. A path this group
        # already owns is claiming nothing new, so it gets no verdict at all —
        # without it, re-saving an untouched path would be refused on account of
        # an override that was already there and already deployed, and a path
        # sitting in two groups on purpose could never be saved again from either
        # side.
        index: dict = {}
        already_mine: set = set()
        for route in existing:
            if route.http_method != method:
                continue
            if route.code and route.code in skip:
                continue
            if not route.path:
                continue
            keys = KongPathOverlapValidator.path_keys(route.path)
            if route.route_group_key.strip() == tag:
                already_mine.update(keys)
                continue
            for key in keys:
                index.setdefault(key, []).append(route)

        conflicts: List[PathOverlap] = []
        overrides: List[PathOverlap] = []
        for raw in paths:
            if not (raw or "").strip():
                continue
            keys = KongPathOverlapValidator.path_keys(raw)
            if already_mine.intersection(keys):
                continue
            # A path can index under two keys; dedupe by identity so one blocking
            # row is not counted twice when both spellings hit it.
            blockers = {id(r): r for key in keys for r in index.get(key, [])}
            if not blockers:
                continue
            worst = max(blockers.values(), key=lambda r: r.regex_priority)
            overlap = PathOverlap(
                path=keys[0],
                http_method=method,
                their_tag=worst.route_group_key,
                their_priority=worst.regex_priority,
                your_tag=tag,
                your_priority=mine,
                their_code=worst.code,
            )
            (overrides if overlap.is_override else conflicts).append(overlap)

        return conflicts, overrides

    @staticmethod
    def validate(
        paths: Sequence[str],
        http_method: str,
        route_group_key: str,
        regex_priority: int,
        existing: Sequence[ExistingRoute],
        ignore_codes: Sequence[str] = (),
    ) -> List[PathOverlap]:
        """
        Enforce the rule: raise on any conflict, return the accepted overrides.

        Nothing is applied when one path is refused — the caller must not be left
        half-saved, which is the same all-or-nothing the Gateway tab does before
        it touches its own state.
        """
        conflicts, overrides = KongPathOverlapValidator.plan(
            paths=paths,
            http_method=http_method,
            route_group_key=route_group_key,
            regex_priority=regex_priority,
            existing=existing,
            ignore_codes=ignore_codes,
        )
        if conflicts:
            raise KongPathOverlapError(conflicts)
        return overrides
