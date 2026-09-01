
"""Shared STRATZ access, caching, configuration, and command-line helpers."""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import html
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9, unsupported but clearer error
    ZoneInfo = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants and shared defaults
# ---------------------------------------------------------------------------

API_URL = "https://api.stratz.com/graphql"
STRATZ_API_PAGE = "https://stratz.com/api"
ACCOUNT_ID_FIELD = "account_id"
TOKEN_PLACEHOLDER = "PASTE_YOUR_STRATZ_API_TOKEN_HERE"  # nosec B105
BUILD_ID = "2026.09.01.01"
DEFAULT_PLAYER_ID = 321_580_662
DEFAULT_PLAYER_NAME = "Yatoro"
DEFAULT_HERO_ID = 53
DEFAULT_HERO_NAME = "Nature's Prophet"
DEFAULT_ITEM_ID = 158
DEFAULT_ITEM_NAME = "Mjollnir"
DEFAULT_TURBO_MODE_ID = 23
DEFAULT_PAGE_SIZE = 100
DEFAULT_TIMEOUT = 45.0
DEFAULT_RETRIES = 4
DEFAULT_WORKERS = 4
DEFAULT_CATALOG_TTL_HOURS = 24.0
MAX_GRAPHQL_INT = 2_147_483_647
MAX_SAFE_GRAPHQL_LONG = 9_007_199_254_740_991

COMMON_DEFAULTS: dict[str, Any] = {
    "api_key": None,
    "player_id": DEFAULT_PLAYER_ID,
    "hero_id": DEFAULT_HERO_ID,
    "sample_by": None,
    "match_count": 150,
    "months": 24,
    "scan_limit": 5000,
    "start": None,
    "end": None,
    "timezone": "Europe/London",
    "days": "all",
    "page_size": DEFAULT_PAGE_SIZE,
    "workers": DEFAULT_WORKERS,
    "timeout": DEFAULT_TIMEOUT,
    "retries": DEFAULT_RETRIES,
    "cache_dir": None,
    "cache_enabled": True,
    "catalog_ttl_hours": DEFAULT_CATALOG_TTL_HOURS,
    "history_ttl_minutes": 15.0,
    "refresh": False,
    "tutorials": True,
    "dark_mode": True,
    "verbose": False,
}

# These settings belong at the unnamed root of config.json. A program only
# inherits the root keys it supports, so future tools can reuse these defaults
# without making today's settings review show irrelevant options.
ROOT_CONFIG_KEYS = frozenset(
    {
        *COMMON_DEFAULTS,
        "item_id",
        "game_mode",
        "party",
        "comparison_mode",
        "player_position",
        "ranked_only",
        "gold_kind",
        "snapshot_method",
        "newest_half_share",
        "draw_ratio",
        "stomp_ratio",
        "series_mode",
        "include_delta",
        "mark_low_samples",
        "low_sample_effective_n",
        "trim_empty_edge_buckets",
    }
)

# ---------------------------------------------------------------------------
# Errors and HTTP client
# ---------------------------------------------------------------------------

class StratzError(RuntimeError):
    pass


class GraphQLError(StratzError):
    def __init__(self, errors: Sequence[Mapping[str, Any]], query_name: str) -> None:
        self.errors = list(errors)
        message = "; ".join(str(error.get("message", error)) for error in errors)
        super().__init__(f"{query_name}: {message}")


ANSI = {
    "red": "\033[31;1m",
    "yellow": "\033[33;1m",
    "cyan": "\033[36;1m",
    "green": "\033[32;1m",
    "reset": "\033[0m",
}


def enable_windows_ansi() -> None:
    """Enable colours in classic Windows consoles without an extra package."""
    if os.name != "nt" or os.environ.get("NO_COLOR"):
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for standard_handle in (-11, -12):  # stdout, stderr
            handle = kernel32.GetStdHandle(standard_handle)
            mode = ctypes.c_uint()
            if handle and kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        return


enable_windows_ansi()


def configure_console_text() -> None:
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


configure_console_text()


def colour(text: str, name: str, *, stream: Any = sys.stderr) -> str:
    if os.environ.get("NO_COLOR") or not getattr(stream, "isatty", lambda: False)():
        return text
    return f"{ANSI[name]}{text}{ANSI['reset']}"


def tutorial_notice(
    title: str,
    lines: Sequence[str],
    *,
    enabled: bool = True,
    level: str = "TIP",
    stream: Any = sys.stderr,
) -> None:
    if not enabled:
        return
    shade = "red" if level == "ERROR" else "yellow" if level == "WARNING" else "cyan"
    print(colour(f"\n{level}: {title}", shade, stream=stream), file=stream)
    for line in lines:
        print(f"  {line}", file=stream)


def is_placeholder_token(value: Any) -> bool:
    text = str(value or "").strip()
    normal = re.sub(r"[^a-z0-9]+", "", text.lower())
    return not text or normal in {
        "pasteyourstratzapitokenhere",
        "yourtokenhere",
        "youractualtoken",
        "replacewithyourtoken",
        "changeme",
    }


class StratzClient:
    """Small dependency-free GraphQL client with retry/backoff behaviour."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        verbose: bool = False,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.verbose = verbose

    def query(
        self,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        query_name: str = "GraphQL query",
    ) -> Mapping[str, Any]:
        payload = json.dumps(
            {"query": query, "variables": dict(variables or {})}
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "dota-scripts/2.0",
            # Retained from the original script; STRATZ GraphQL accepts it and
            # it avoids browser-style preflight enforcement errors.
            "graphql-require-preflight": "1",
        }

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                API_URL, data=payload, headers=headers, method="POST"
            )
            try:
                # API_URL is a fixed HTTPS constant, never user input.
                with urllib.request.urlopen(  # nosec B310
                    request, timeout=self.timeout_seconds
                ) as response:
                    body = response.read().decode("utf-8")
                decoded = json.loads(body)
                if decoded.get("errors"):
                    raise GraphQLError(decoded["errors"], query_name)
                data = decoded.get("data")
                if not isinstance(data, Mapping):
                    raise StratzError(f"{query_name}: response has no data object")
                return data
            except GraphQLError:
                raise
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", errors="replace")
                last_error = StratzError(
                    f"{query_name}: HTTP {error.code}: {body[:1200]}"
                )
                retryable = error.code == 429 or 500 <= error.code < 600
                if not retryable or attempt >= self.retries:
                    raise last_error from error
                retry_after = error.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(2**attempt, 20)
                except ValueError:
                    delay = min(2**attempt, 20)
                if self.verbose:
                    print(
                        f"{query_name}: HTTP {error.code}; retrying in {delay:g}s",
                        file=sys.stderr,
                    )
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                if attempt >= self.retries:
                    raise StratzError(
                        f"{query_name} failed after {self.retries + 1} attempts: {error}"
                    ) from error
                delay = min(2**attempt, 20)
                if self.verbose:
                    print(
                        f"{query_name}: transient failure; retrying in {delay:g}s",
                        file=sys.stderr,
                    )
                time.sleep(delay)

        raise StratzError(f"{query_name} failed: {last_error}")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def default_cache_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if root:
            return Path(root) / "dota-scripts" / "cache"
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "dota-scripts"
    return Path.home() / ".cache" / "dota-scripts"


class JsonCache:
    """Disk cache that preserves the exact GraphQL data object plus provenance.

    A query fingerprint is part of every match/history key.  This matters because
    GraphQL responses are fragments, not promises that a whole match was fetched.
    Old v4 raw JSON cache files remain readable.
    """

    FORMAT_VERSION = 2

    def __init__(
        self, root: Path, *, refresh: bool = False, enabled: bool = True
    ) -> None:
        self.root = root
        self.refresh = refresh
        self.enabled = enabled

    @staticmethod
    def _safe_key(key: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", key)

    def path(self, namespace: str, key: str) -> Path:
        return self.root / self._safe_key(namespace) / f"{self._safe_key(key)}.json"

    def get(
        self, namespace: str, key: str, *, max_age_seconds: float | None = None
    ) -> Any | None:
        if not self.enabled or self.refresh:
            return None
        path = self.path(namespace, key)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None

        if isinstance(decoded, Mapping) and decoded.get("cache_format") == self.FORMAT_VERSION:
            saved_at = decoded.get("saved_at")
            if max_age_seconds is not None:
                try:
                    age = time.time() - float(saved_at)
                except (TypeError, ValueError):
                    return None
                if age > max_age_seconds:
                    return None
            return decoded.get("data")
        # Backwards compatibility for v4's unwrapped match-detail files.
        if max_age_seconds is not None:
            try:
                if time.time() - path.stat().st_mtime > max_age_seconds:
                    return None
            except OSError:
                return None
        return decoded

    def put(
        self,
        namespace: str,
        key: str,
        value: Any,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        path = self.path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        record = {
            "cache_format": self.FORMAT_VERSION,
            "saved_at": time.time(),
            "saved_at_utc": datetime.now(timezone.utc).isoformat(),
            "namespace": namespace,
            "key": key,
            "metadata": dict(metadata or {}),
            "data": value,
        }
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


CATALOG_QUERY = """
query DotaCatalog {
  constants {
    heroes { id displayName }
    items { id displayName }
  }
}
"""

FALLBACK_CATALOG: dict[str, list[dict[str, Any]]] = {
    "heroes": [
        {"id": 53, "name": "Nature's Prophet", "aliases": ["np", "furion"]},
    ],
    "items": [
        {"id": 158, "name": "Mjollnir", "aliases": ["mjolnir", "mjol"]},
    ],
}

KNOWN_ALIASES: dict[str, dict[int, list[str]]] = {
    "heroes": {53: ["np", "natures prophet", "nature prophet", "furion"]},
    "items": {158: ["mjolnir", "mjol"]},
}


def normalise_alias(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def clean_catalog_rows(raw_rows: Any, kind: str) -> list[dict[str, Any]]:
    if not isinstance(raw_rows, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            continue
        entry_id = normalize_int(raw.get("id"))
        name = re.sub(
            r"[\x00-\x1f\x7f]+",
            "",
            str(raw.get("displayName") or raw.get("name") or ""),
        ).strip()
        if entry_id is None or not name or entry_id in seen:
            continue
        seen.add(entry_id)
        rows.append(
            {
                "id": entry_id,
                "name": name,
                "aliases": KNOWN_ALIASES.get(kind, {}).get(entry_id, []),
            }
        )
    return sorted(rows, key=lambda row: (normalise_alias(row["name"]), row["id"]))


def load_dota_catalog(
    client: StratzClient,
    cache: JsonCache,
    *,
    ttl_hours: float = DEFAULT_CATALOG_TTL_HOURS,
    tutorials: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    cached = cache.get(
        "catalog",
        "heroes-items-v1",
        max_age_seconds=max(0.0, ttl_hours) * 3600,
    )
    if isinstance(cached, Mapping):
        heroes = clean_catalog_rows(cached.get("heroes"), "heroes")
        items = clean_catalog_rows(cached.get("items"), "items")
        if heroes and items:
            return {"heroes": heroes, "items": items}

    try:
        data = client.query(CATALOG_QUERY, query_name="Hero/item catalogue")
        constants = data.get("constants")
        if not isinstance(constants, Mapping):
            raise StratzError("STRATZ returned no constants catalogue")
        heroes = clean_catalog_rows(constants.get("heroes"), "heroes")
        items = clean_catalog_rows(constants.get("items"), "items")
        if not heroes or not items:
            raise StratzError("STRATZ returned an empty hero or item catalogue")
        cleaned = {"heroes": heroes, "items": items}
        cache.put(
            "catalog",
            "heroes-items-v1",
            cleaned,
            metadata={
                "kind": "STRATZ constants",
                "query_sha256": hashlib.sha256(CATALOG_QUERY.encode("utf-8")).hexdigest(),
                "ttl_hours": ttl_hours,
            },
        )
        return cleaned
    except StratzError as exc:
        tutorial_notice(
            "Could not refresh the live hero/item names",
            [
                str(exc),
                "Using the small built-in fallback list for this run.",
                "Check your connection/token, or retry without --refresh.",
            ],
            enabled=tutorials,
            level="WARNING",
        )
        return {key: [dict(row) for row in rows] for key, rows in FALLBACK_CATALOG.items()}


def catalog_alias_index(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    index: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        keys = [str(row.get("id")), str(row.get("name", ""))]
        keys.extend(str(alias) for alias in row.get("aliases", []) or [])
        for key in keys:
            normal = normalise_alias(key)
            if normal:
                bucket = index.setdefault(normal, [])
                if not any(existing.get("id") == row.get("id") for existing in bucket):
                    bucket.append(row)
    return index


def resolve_catalog_entry(
    value: Any,
    kind: str,
    catalog: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Mapping[str, Any]:
    rows = catalog.get(kind, [])
    normal = normalise_alias(value)
    hits = catalog_alias_index(rows).get(normal, [])
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        choices = ", ".join(f"{row['name']} ({row['id']})" for row in hits[:8])
        label = "hero" if kind == "heroes" else "item"
        raise StratzError(f"Ambiguous {label} {value!r}: {choices}")
    label = "hero" if kind == "heroes" else "item"
    raise StratzError(
        f"Unknown {label} {value!r}. Use --list-{kind} to see the live STRATZ list."
    )


def matching_catalog_rows(
    rows: Sequence[Mapping[str, Any]], search: str
) -> list[Mapping[str, Any]]:
    needle = normalise_alias(search)
    if not needle:
        return list(rows)
    return [
        row
        for row in rows
        if needle in normalise_alias(row.get("name"))
        or any(needle in normalise_alias(alias) for alias in row.get("aliases", []) or [])
        or needle == normalise_alias(row.get("id"))
    ]


def print_catalog_grid(rows: Sequence[Mapping[str, Any]], *, limit: int = 60) -> None:
    shown = list(rows[:limit])
    labels = [f"{row['id']:>4}  {row['name']}" for row in shown]
    width = max([len(label) for label in labels] + [20]) + 3
    columns = max(1, min(3, 100 // width))
    for start in range(0, len(labels), columns):
        print("".join(label.ljust(width) for label in labels[start : start + columns]).rstrip())
    if len(rows) > limit:
        print(f"...and {len(rows) - limit} more; type a narrower search.")


def choose_catalog_entry(
    kind: str,
    catalog: Mapping[str, Sequence[Mapping[str, Any]]],
    current: Any,
) -> Mapping[str, Any] | None:
    rows = list(catalog.get(kind, []))
    label = "hero" if kind == "heroes" else "item"
    print(f"\nChoose a {label}. Type an ID, full name, alias, or part of its name.")
    search = input(f"Search {label} [{current}] (blank keeps current): ").strip()
    if not search:
        return None
    exact = catalog_alias_index(rows).get(normalise_alias(search), [])
    if len(exact) == 1:
        return exact[0]
    matches = matching_catalog_rows(rows, search)
    if not matches:
        print(colour(f"No {label} matched {search!r}.", "yellow", stream=sys.stdout))
        return None
    print_catalog_grid(matches)
    if len(matches) == 1:
        return matches[0]
    choice = input("Type the ID you want (blank cancels): ").strip()
    if not choice:
        return None
    try:
        return resolve_catalog_entry(choice, kind, catalog)
    except StratzError as exc:
        print(colour(str(exc), "yellow", stream=sys.stdout))
        return None


# ---------------------------------------------------------------------------
# GraphQL schema introspection helpers
# ---------------------------------------------------------------------------

TYPE_REF_SELECTION = """
kind
name
ofType {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType { kind name }
      }
    }
  }
}
"""


def unwrap_named_type(type_ref: Mapping[str, Any] | None) -> str | None:
    current = type_ref
    while current:
        name = current.get("name")
        if name:
            return str(name)
        current = current.get("ofType")
    return None


def type_kind(type_ref: Mapping[str, Any] | None) -> str | None:
    current = type_ref
    while current:
        kind = current.get("kind")
        if kind not in {"NON_NULL", "LIST"}:
            return str(kind) if kind else None
        current = current.get("ofType")
    return None


def contains_list(type_ref: Mapping[str, Any] | None) -> bool:
    current = type_ref
    while current:
        if current.get("kind") == "LIST":
            return True
        current = current.get("ofType")
    return False


def graphql_type_text(type_ref: Mapping[str, Any]) -> str:
    kind = type_ref.get("kind")
    name = type_ref.get("name")
    nested = type_ref.get("ofType")
    if kind == "NON_NULL":
        if not nested:
            raise StratzError("Malformed NON_NULL GraphQL type")
        return f"{graphql_type_text(nested)}!"
    if kind == "LIST":
        if not nested:
            raise StratzError("Malformed LIST GraphQL type")
        return f"[{graphql_type_text(nested)}]"
    if name:
        return str(name)
    raise StratzError(f"Cannot render GraphQL type: {type_ref}")


def is_required_argument(argument: Mapping[str, Any]) -> bool:
    return argument.get("type", {}).get("kind") == "NON_NULL"


def field_args(field: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {argument["name"]: argument for argument in field.get("args") or []}


def choose_field(
    fields: Mapping[str, Mapping[str, Any]], candidates: Sequence[str]
) -> Mapping[str, Any] | None:
    for candidate in candidates:
        if candidate in fields:
            return fields[candidate]
    lowered = {name.lower(): field for name, field in fields.items()}
    for candidate in candidates:
        hit = lowered.get(candidate.lower())
        if hit:
            return hit
    return None


class SchemaInspector:
    def __init__(self, client: StratzClient) -> None:
        self.client = client
        self._type_cache: dict[str, Mapping[str, Any]] = {}
        self.query_type_name, self.root_fields = self._load_root_query()

    def _load_root_query(self) -> tuple[str, dict[str, Mapping[str, Any]]]:
        query = f"""
        query RootSchema {{
          __schema {{
            queryType {{
              name
              fields(includeDeprecated: true) {{
                name
                args {{ name type {{ {TYPE_REF_SELECTION} }} }}
                type {{ {TYPE_REF_SELECTION} }}
              }}
            }}
          }}
        }}
        """
        data = self.client.query(query, query_name="Schema root introspection")
        query_type = data["__schema"]["queryType"]
        fields = {field["name"]: field for field in query_type["fields"]}
        return str(query_type["name"]), fields

    def get_type(self, name: str) -> Mapping[str, Any]:
        if name in self._type_cache:
            return self._type_cache[name]
        query = f"""
        query TypeSchema($name: String!) {{
          __type(name: $name) {{
            kind
            name
            fields(includeDeprecated: true) {{
              name
              args {{ name type {{ {TYPE_REF_SELECTION} }} }}
              type {{ {TYPE_REF_SELECTION} }}
            }}
            inputFields {{ name type {{ {TYPE_REF_SELECTION} }} }}
            enumValues(includeDeprecated: true) {{ name }}
          }}
        }}
        """
        data = self.client.query(
            query, {"name": name}, query_name=f"Schema introspection for {name}"
        )
        result = data.get("__type")
        if not isinstance(result, Mapping):
            raise StratzError(f"GraphQL type {name!r} was not found")
        self._type_cache[name] = result
        return result

    def fields(self, type_name: str) -> dict[str, Mapping[str, Any]]:
        return {
            field["name"]: field
            for field in (self.get_type(type_name).get("fields") or [])
        }

    def input_fields(self, type_name: str) -> dict[str, Mapping[str, Any]]:
        return {
            field["name"]: field
            for field in (self.get_type(type_name).get("inputFields") or [])
        }


@dataclass(frozen=True)
class ValuePath:
    selection: str
    response_path: tuple[str, ...]


def scalar_value_path(
    inspector: SchemaInspector,
    parent_type: str,
    scalar_candidates: Sequence[str],
    object_candidates: Sequence[str] = (),
    object_id_candidates: Sequence[str] = ("id",),
) -> ValuePath | None:
    fields = inspector.fields(parent_type)
    scalar = choose_field(fields, scalar_candidates)
    if scalar and type_kind(scalar["type"]) in {"SCALAR", "ENUM"}:
        name = str(scalar["name"])
        return ValuePath(name, (name,))

    for object_candidate in object_candidates:
        obj = choose_field(fields, (object_candidate,))
        if not obj or type_kind(obj["type"]) != "OBJECT":
            continue
        obj_name = str(obj["name"])
        obj_type = unwrap_named_type(obj["type"])
        if not obj_type:
            continue
        child_fields = inspector.fields(obj_type)
        child = choose_field(child_fields, object_id_candidates)
        if child and type_kind(child["type"]) in {"SCALAR", "ENUM"}:
            child_name = str(child["name"])
            return ValuePath(
                f"{obj_name} {{ {child_name} }}", (obj_name, child_name)
            )
    return None


def get_path(value: Any, path: Sequence[str]) -> Any:
    current = value
    for segment in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
    return current


def dedupe_selections(selections: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for selection in selections:
        normalized = " ".join(selection.split())
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(selection)
    return out


# ---------------------------------------------------------------------------
# Dynamic STRATZ query plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PurchasePlan:
    selection: str
    response_path: tuple[str, ...]
    item_field: str | None
    time_field: str | None
    description: str
    raw_json: bool = False


@dataclass(frozen=True)
class GoldEventPlan:
    selection: str
    response_path: tuple[str, ...]
    time_field: str | None
    gold_field: str | None
    unreliable_gold_field: str | None
    networth_field: str | None
    raw_json: bool = False
    description: str = ""


@dataclass(frozen=True)
class QueryPlan:
    player_root_field: str
    player_root_arg: str
    player_root_arg_type: str
    matches_field: str
    matches_request_arg: str
    take_field: str
    take_type: str
    pagination_field: str
    pagination_type: str
    pagination_kind: str
    match_id_field: str
    start_time_field: str
    player_name_path: ValuePath | None

    match_root_field: str
    match_root_arg: str
    match_root_arg_type: str
    match_type_name: str
    match_players_field: str
    match_player_type: str
    players_filter_arg: str | None
    players_filter_arg_type: str | None
    players_all_supported: bool

    hero_path: ValuePath
    account_path: ValuePath | None
    position_path: ValuePath | None
    player_radiant_path: ValuePath | None
    player_slot_path: ValuePath | None
    player_win_path: ValuePath | None
    party_path: ValuePath | None
    party_semantics: str | None

    match_start_path: ValuePath
    duration_path: ValuePath | None
    game_mode_path: ValuePath | None
    lobby_type_path: ValuePath | None
    match_radiant_win_path: ValuePath | None

    purchase_plan: PurchasePlan | None
    gold_event_plan: GoldEventPlan | None

    history_query: str
    history_has_player_overview: bool
    detail_all_query: str | None
    detail_one_query: str


def discover_purchase_plan(
    inspector: SchemaInspector, player_type: str
) -> PurchasePlan | None:
    traversal_keywords = (
        "playback", "purchase", "item", "event", "inventory", "timeline"
    )
    queue: list[tuple[str, tuple[str, ...], int]] = [(player_type, (), 0)]
    visited: set[tuple[str, tuple[str, ...]]] = set()
    candidates: list[tuple[int, tuple[str, ...], str, str, str]] = []
    raw_candidates: list[tuple[int, tuple[str, ...], str]] = []

    while queue:
        type_name, prefix, depth = queue.pop(0)
        key = (type_name, prefix)
        if key in visited:
            continue
        visited.add(key)
        try:
            fields = inspector.fields(type_name)
        except StratzError:
            continue

        for field_name, field_obj in fields.items():
            kind = type_kind(field_obj["type"])
            named = unwrap_named_type(field_obj["type"])
            path = prefix + (field_name,)
            path_text = ".".join(path).lower()

            if contains_list(field_obj["type"]) and named and kind == "OBJECT":
                try:
                    event_fields = inspector.fields(named)
                except StratzError:
                    event_fields = {}
                item_field = choose_field(
                    event_fields,
                    ("itemId", "itemID", "item", "id", "itemTypeId"),
                )
                time_field = choose_field(
                    event_fields,
                    ("time", "timeSeconds", "timestamp", "gameTime", "seconds"),
                )
                if (
                    item_field
                    and time_field
                    and type_kind(item_field["type"]) in {"SCALAR", "ENUM"}
                    and type_kind(time_field["type"]) in {"SCALAR", "ENUM"}
                ):
                    score = 0
                    score += 120 if "purchase" in path_text else 0
                    score += 45 if "item" in path_text else 0
                    score += 20 if "playback" in path_text else 0
                    score += 10 if field_name.lower().endswith("events") else 0
                    candidates.append(
                        (
                            score,
                            path,
                            str(item_field["name"]),
                            str(time_field["name"]),
                            named,
                        )
                    )

            if kind == "SCALAR":
                lower = field_name.lower()
                score = (100 if "purchase" in lower else 0) + (
                    30 if "item" in lower else 0
                ) + (20 if "playback" in lower else 0)
                if score:
                    raw_candidates.append((score, path, field_name))

            if depth >= 4 or not named or kind != "OBJECT":
                continue
            lower = field_name.lower()
            if depth == 0 or any(keyword in lower for keyword in traversal_keywords):
                queue.append((named, path, depth + 1))

    if candidates:
        _, path, item_field, time_field, event_type = max(
            candidates, key=lambda row: row[0]
        )
        leaf = f"{path[-1]} {{ {item_field} {time_field} }}"
        for segment in reversed(path[:-1]):
            leaf = f"{segment} {{ {leaf} }}"
        return PurchasePlan(
            selection=leaf,
            response_path=path,
            item_field=item_field,
            time_field=time_field,
            description=f"{'.'.join(path)} ({event_type})",
        )

    if raw_candidates:
        _, path, _ = max(raw_candidates, key=lambda row: row[0])
        leaf = path[-1]
        for segment in reversed(path[:-1]):
            leaf = f"{segment} {{ {leaf} }}"
        return PurchasePlan(
            selection=leaf,
            response_path=path,
            item_field=None,
            time_field=None,
            description=f"{'.'.join(path)} (JSON scalar)",
            raw_json=True,
        )
    return None


def discover_gold_event_plan(
    inspector: SchemaInspector, player_type: str
) -> GoldEventPlan | None:
    """Locate replay economic snapshots without assuming one schema spelling."""
    queue: list[tuple[str, tuple[str, ...], int]] = [(player_type, (), 0)]
    candidates: list[tuple[int, GoldEventPlan]] = []
    visited: set[tuple[str, tuple[str, ...]]] = set()

    while queue:
        type_name, prefix, depth = queue.pop(0)
        if (type_name, prefix) in visited:
            continue
        visited.add((type_name, prefix))
        fields = inspector.fields(type_name)
        for name, obj in fields.items():
            path = prefix + (name,)
            lower_path = ".".join(path).lower()
            named = unwrap_named_type(obj["type"])
            kind = type_kind(obj["type"])

            if contains_list(obj["type"]) and named and kind == "OBJECT":
                event_fields = inspector.fields(named)
                time_field = choose_field(
                    event_fields,
                    ("time", "timeSeconds", "timestamp", "gameTime", "seconds"),
                )
                gold_field = choose_field(
                    event_fields, ("gold", "reliableGold", "currentGold")
                )
                unreliable = choose_field(
                    event_fields, ("unreliableGold", "unreliable")
                )
                networth = choose_field(
                    event_fields, ("networth", "netWorth", "net_worth")
                )
                if time_field and (gold_field or networth):
                    selection_fields = [str(time_field["name"])]
                    for f in (gold_field, unreliable, networth):
                        if f and str(f["name"]) not in selection_fields:
                            selection_fields.append(str(f["name"]))
                    leaf = f"{path[-1]} {{ {' '.join(selection_fields)} }}"
                    for segment in reversed(path[:-1]):
                        leaf = f"{segment} {{ {leaf} }}"
                    score = 0
                    score += 180 if "playerupdategoldevents" in lower_path else 0
                    score += 90 if "gold" in name.lower() else 0
                    score += 35 if "playback" in lower_path else 0
                    score += 15 if "event" in name.lower() else 0
                    plan = GoldEventPlan(
                        selection=leaf,
                        response_path=path,
                        time_field=str(time_field["name"]),
                        gold_field=str(gold_field["name"]) if gold_field else None,
                        unreliable_gold_field=(
                            str(unreliable["name"]) if unreliable else None
                        ),
                        networth_field=str(networth["name"]) if networth else None,
                        description=f"{'.'.join(path)} ({named})",
                    )
                    candidates.append((score, plan))

            if kind == "SCALAR" and any(k in name.lower() for k in ("gold", "playback")):
                # JSON scalar fallback, useful for older schema variants.
                score = 70 if "gold" in name.lower() else 20
                leaf = name
                for segment in reversed(path[:-1]):
                    leaf = f"{segment} {{ {leaf} }}"
                candidates.append(
                    (
                        score,
                        GoldEventPlan(
                            selection=leaf,
                            response_path=path,
                            time_field=None,
                            gold_field=None,
                            unreliable_gold_field=None,
                            networth_field=None,
                            raw_json=True,
                            description=f"{'.'.join(path)} (JSON scalar)",
                        ),
                    )
                )

            if depth < 3 and named and kind == "OBJECT":
                lower = name.lower()
                if depth == 0 or any(
                    token in lower for token in ("playback", "timeline", "event", "stat")
                ):
                    queue.append((named, path, depth + 1))

    return max(candidates, key=lambda row: row[0])[1] if candidates else None


def discover_query_plan(client: StratzClient, *, verbose: bool = False) -> QueryPlan:
    inspector = SchemaInspector(client)

    player_root = choose_field(inspector.root_fields, ("player",))
    match_root = choose_field(inspector.root_fields, ("match",))
    if not player_root or not match_root:
        raise StratzError("STRATZ schema has no expected player/match root fields")

    player_root_arg = choose_field(
        field_args(player_root), ("steamAccountId", "playerId", "id")
    )
    match_root_arg = choose_field(field_args(match_root), ("id", "matchId"))
    if not player_root_arg or not match_root_arg:
        raise StratzError("Could not identify player or match root ID argument")

    player_type = unwrap_named_type(player_root["type"])
    match_type = unwrap_named_type(match_root["type"])
    if not player_type or not match_type:
        raise StratzError("Could not identify player/match return types")

    player_fields = inspector.fields(player_type)
    player_name_path = scalar_value_path(
        inspector,
        player_type,
        ("displayName", "name", "personaName"),
        ("steamAccount", "account"),
        ("name", "displayName", "personaName"),
    )
    matches_field = choose_field(player_fields, ("matches",))
    if not matches_field:
        raise StratzError("STRATZ player type has no matches field")
    matches_args = field_args(matches_field)
    request_arg = choose_field(matches_args, ("request",))
    if not request_arg:
        raise StratzError("Could not identify player.matches request argument")
    request_type = unwrap_named_type(request_arg["type"])
    if not request_type:
        raise StratzError("Could not identify matches request input type")
    request_fields = inspector.input_fields(request_type)
    take_field = choose_field(request_fields, ("take", "limit"))
    before_field = choose_field(request_fields, ("before",))
    skip_field = choose_field(request_fields, ("skip", "offset"))
    pagination_field = before_field or skip_field
    if not take_field or not pagination_field:
        raise StratzError("Matches request lacks usable take/pagination fields")

    match_fields = inspector.fields(match_type)
    match_id = choose_field(match_fields, ("id", "matchId"))
    start_field = choose_field(match_fields, ("startDateTime", "startTime", "startDate"))
    players_field = choose_field(match_fields, ("players", "matchPlayers"))
    if not match_id or not start_field or not players_field:
        raise StratzError("Match schema lacks id/start/players fields")
    match_player_type = unwrap_named_type(players_field["type"])
    if not match_player_type:
        raise StratzError("Could not identify match-player type")

    player_filter_arg = choose_field(
        field_args(players_field), ("steamAccountId", "playerId", "accountId")
    )
    unknown_required_args = [
        arg
        for arg in field_args(players_field).values()
        if is_required_argument(arg)
        and (not player_filter_arg or arg["name"] != player_filter_arg["name"])
    ]
    players_all_supported = not unknown_required_args and all(
        not is_required_argument(arg) for arg in field_args(players_field).values()
    )

    mp_fields = inspector.fields(match_player_type)
    hero_path = scalar_value_path(
        inspector,
        match_player_type,
        ("heroId", "heroID"),
        ("hero",),
        ("id", "heroId"),
    )
    if not hero_path:
        raise StratzError("Could not identify hero ID on match player")
    account_path = scalar_value_path(
        inspector,
        match_player_type,
        ("steamAccountId", "accountId", "playerId"),
        ("steamAccount", "player"),
        ("id", "steamAccountId"),
    )
    position_path = scalar_value_path(
        inspector, match_player_type, ("position", "rolePosition", "role")
    )
    player_radiant_path = scalar_value_path(
        inspector, match_player_type, ("isRadiant", "radiant")
    )
    player_slot_path = scalar_value_path(
        inspector, match_player_type, ("playerSlot", "slot")
    )
    player_win_path = scalar_value_path(
        inspector, match_player_type, ("isVictory", "win", "won")
    )

    party_path: ValuePath | None = None
    party_semantics: str | None = None
    for candidates, semantics in (
        (("partyId", "partyID"), "id"),
        (("partySize",), "size"),
        (("partyIndex",), "index"),
    ):
        party_path = scalar_value_path(inspector, match_player_type, candidates)
        if party_path:
            party_semantics = semantics
            break
    if not party_path:
        party_path = scalar_value_path(
            inspector, match_player_type, (), ("party",), ("id", "partyId")
        )
        if party_path:
            party_semantics = "id"

    match_start_path = ValuePath(str(start_field["name"]), (str(start_field["name"]),))
    duration_path = scalar_value_path(
        inspector, match_type, ("durationSeconds", "duration", "gameDuration")
    )
    game_mode_path = scalar_value_path(
        inspector,
        match_type,
        ("gameMode", "gameModeId"),
        ("gameMode",),
        ("id",),
    )
    lobby_type_path = scalar_value_path(
        inspector,
        match_type,
        ("lobbyType", "lobbyTypeId"),
        ("lobbyType",),
        ("id",),
    )
    radiant_win_path = scalar_value_path(
        inspector, match_type, ("didRadiantWin", "radiantWin", "isRadiantWin")
    )

    purchase_plan = discover_purchase_plan(inspector, match_player_type)
    gold_event_plan = discover_gold_event_plan(inspector, match_player_type)

    match_common_selections = dedupe_selections(
        [
            match_start_path.selection,
            duration_path.selection if duration_path else "",
            game_mode_path.selection if game_mode_path else "",
            lobby_type_path.selection if lobby_type_path else "",
            radiant_win_path.selection if radiant_win_path else "",
        ]
    )
    player_common_selections = dedupe_selections(
        [
            hero_path.selection,
            account_path.selection if account_path else "",
            position_path.selection if position_path else "",
            player_radiant_path.selection if player_radiant_path else "",
            player_slot_path.selection if player_slot_path else "",
            player_win_path.selection if player_win_path else "",
            party_path.selection if party_path else "",
        ]
    )

    # History query tries to include only the selected player's overview when
    # the schema permits it; otherwise it safely falls back to refs only.
    history_defs = [
        f"$playerId: {graphql_type_text(player_root_arg['type'])}",
        f"$take: {graphql_type_text(take_field['type'])}",
        f"$page: {graphql_type_text(pagination_field['type'])}",
    ]
    history_player_fragment = ""
    history_has_player_overview = False
    if player_filter_arg and not unknown_required_args:
        history_defs.append(
            f"$historyPlayerId: {graphql_type_text(player_filter_arg['type'])}"
        )
        players_call = (
            f"{players_field['name']}({player_filter_arg['name']}: $historyPlayerId)"
        )
        history_player_fragment = (
            f"{players_call} {{ {' '.join(player_common_selections)} }}"
        )
        history_has_player_overview = True
    elif players_all_supported:
        history_player_fragment = (
            f"{players_field['name']} {{ {' '.join(player_common_selections)} }}"
        )
        history_has_player_overview = True

    history_match_selections = dedupe_selections(
        [
            str(match_id["name"]),
            str(start_field["name"]),
            game_mode_path.selection if game_mode_path else "",
            lobby_type_path.selection if lobby_type_path else "",
            history_player_fragment,
        ]
    )
    history_query = f"""
    query PlayerMatches({', '.join(history_defs)}) {{
      {player_root['name']}({player_root_arg['name']}: $playerId) {{
        {matches_field['name']}(
          {request_arg['name']}: {{
            {take_field['name']}: $take,
            {pagination_field['name']}: $page
          }}
        ) {{
          {' '.join(history_match_selections)}
        }}
      }}
    }}
    """

    # Selected-player detail query (works even when `players` needs an ID arg).
    detail_one_defs = [f"$matchId: {graphql_type_text(match_root_arg['type'])}"]
    if player_filter_arg:
        detail_one_defs.append(
            f"$selectedPlayerId: {graphql_type_text(player_filter_arg['type'])}"
        )
        one_players_call = (
            f"{players_field['name']}({player_filter_arg['name']}: $selectedPlayerId)"
        )
    elif players_all_supported:
        one_players_call = str(players_field["name"])
    else:
        raise StratzError(
            "STRATZ players field requires unsupported arguments; cannot query player data"
        )

    one_player_selections = dedupe_selections(
        player_common_selections
        + ([purchase_plan.selection] if purchase_plan else [])
        + ([gold_event_plan.selection] if gold_event_plan else [])
    )
    detail_one_query = f"""
    query MatchDetailOne({', '.join(detail_one_defs)}) {{
      {match_root['name']}({match_root_arg['name']}: $matchId) {{
        {' '.join(match_common_selections)}
        {one_players_call} {{ {' '.join(one_player_selections)} }}
      }}
    }}
    """

    detail_all_query: str | None = None
    if players_all_supported:
        all_player_selections = dedupe_selections(
            player_common_selections
            + ([gold_event_plan.selection] if gold_event_plan else [])
        )
        detail_all_query = f"""
        query MatchDetailAll($matchId: {graphql_type_text(match_root_arg['type'])}) {{
          {match_root['name']}({match_root_arg['name']}: $matchId) {{
            {' '.join(match_common_selections)}
            {players_field['name']} {{ {' '.join(all_player_selections)} }}
          }}
        }}
        """

    plan = QueryPlan(
        player_root_field=str(player_root["name"]),
        player_root_arg=str(player_root_arg["name"]),
        player_root_arg_type=graphql_type_text(player_root_arg["type"]),
        matches_field=str(matches_field["name"]),
        matches_request_arg=str(request_arg["name"]),
        take_field=str(take_field["name"]),
        take_type=graphql_type_text(take_field["type"]),
        pagination_field=str(pagination_field["name"]),
        pagination_type=graphql_type_text(pagination_field["type"]),
        pagination_kind="before" if before_field else "skip",
        match_id_field=str(match_id["name"]),
        start_time_field=str(start_field["name"]),
        player_name_path=player_name_path,
        match_root_field=str(match_root["name"]),
        match_root_arg=str(match_root_arg["name"]),
        match_root_arg_type=graphql_type_text(match_root_arg["type"]),
        match_type_name=match_type,
        match_players_field=str(players_field["name"]),
        match_player_type=match_player_type,
        players_filter_arg=str(player_filter_arg["name"]) if player_filter_arg else None,
        players_filter_arg_type=(
            graphql_type_text(player_filter_arg["type"]) if player_filter_arg else None
        ),
        players_all_supported=players_all_supported,
        hero_path=hero_path,
        account_path=account_path,
        position_path=position_path,
        player_radiant_path=player_radiant_path,
        player_slot_path=player_slot_path,
        player_win_path=player_win_path,
        party_path=party_path,
        party_semantics=party_semantics,
        match_start_path=match_start_path,
        duration_path=duration_path,
        game_mode_path=game_mode_path,
        lobby_type_path=lobby_type_path,
        match_radiant_win_path=radiant_win_path,
        purchase_plan=purchase_plan,
        gold_event_plan=gold_event_plan,
        history_query=history_query,
        history_has_player_overview=history_has_player_overview,
        detail_all_query=detail_all_query,
        detail_one_query=detail_one_query,
    )

    if verbose:
        print("STRATZ schema plan", file=sys.stderr)
        print(f"  player type:        {player_type}", file=sys.stderr)
        print(f"  match type:         {match_type}", file=sys.stderr)
        print(f"  match-player type:  {match_player_type}", file=sys.stderr)
        print(
            f"  explicit position:  {position_path.response_path if position_path else 'NOT FOUND'}",
            file=sys.stderr,
        )
        print(
            f"  gold events:        {gold_event_plan.description if gold_event_plan else 'NOT FOUND'}",
            file=sys.stderr,
        )
        print(
            f"  purchases:          {purchase_plan.description if purchase_plan else 'NOT FOUND'}",
            file=sys.stderr,
        )
        print(f"  all-player query:   {bool(detail_all_query)}", file=sys.stderr)
    return plan


# ---------------------------------------------------------------------------
# Generic STRATZ parsing / match retrieval helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MatchReference:
    match_id: int
    start_timestamp: int
    overview: Mapping[str, Any] | None = None


def parse_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:  # milliseconds
            number /= 1000
        if number > 100_000_000:
            return int(number)
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return None
    return None


def enum_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        for key in ("name", "id", "value"):
            if key in value:
                return enum_text(value[key])
        return json.dumps(value, sort_keys=True)
    return str(value)


def normalize_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in {"true", "yes", "1", "radiant", "win", "won", "victory"}:
            return True
        if lower in {"false", "no", "0", "dire", "loss", "lost", "defeat"}:
            return False
    return None


def normalize_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        text = enum_text(value)
        match = re.search(r"-?\d+", text)
        return int(match.group()) if match else None


def decode_jsonish(value: Any) -> Any:
    """Decode GraphQL fields that may arrive as JSON text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def normalize_position(value: Any) -> int | None:
    """Read explicit STRATZ position values without guessing a lane."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and int(value) in {1, 2, 3, 4, 5}:
        return int(value)
    text = enum_text(value).upper().strip()
    match = re.fullmatch(r"(?:POSITION[_ -]?)?([1-5])", text)
    if match:
        return int(match.group(1))
    # Some GraphQL enum serializers include a namespace prefix/suffix; accept
    # POSITION_1 etc but not SAFE_LANE/OFF_LANE/CARRY because those would be
    # semantic role inference rather than the explicit numbered position.
    match = re.search(r"(?:^|_)POSITION_([1-5])(?:$|_)", text)
    return int(match.group(1)) if match else None


def player_side(player: Mapping[str, Any], plan: QueryPlan) -> bool | None:
    if plan.player_radiant_path:
        value = normalize_bool(get_path(player, plan.player_radiant_path.response_path))
        if value is not None:
            return value
    if plan.player_slot_path:
        slot = normalize_int(get_path(player, plan.player_slot_path.response_path))
        if slot is not None:
            # Dota player slot convention: Radiant 0..4, Dire 128..132.
            if 0 <= slot <= 4:
                return True
            if 128 <= slot <= 132:
                return False
    return None


def player_account_id(player: Mapping[str, Any], plan: QueryPlan) -> int | None:
    if not plan.account_path:
        return None
    return normalize_int(get_path(player, plan.account_path.response_path))


def player_hero_id(player: Mapping[str, Any], plan: QueryPlan) -> int | None:
    return normalize_int(get_path(player, plan.hero_path.response_path))


def select_player(
    raw_players: Any, *, player_id: int, plan: QueryPlan
) -> Mapping[str, Any] | None:
    if isinstance(raw_players, Mapping):
        raw_players = [raw_players]
    if not isinstance(raw_players, list):
        return None
    if plan.account_path:
        for player in raw_players:
            if isinstance(player, Mapping) and player_account_id(player, plan) == player_id:
                return player
    # A server-side steamAccountId filter normally returns exactly one row.
    if len(raw_players) == 1 and isinstance(raw_players[0], Mapping):
        return raw_players[0]
    return None


def is_ranked_match(match: Mapping[str, Any], plan: QueryPlan) -> bool | None:
    """Return None when schema gives no evidence either way."""
    evidence_seen = False
    if plan.lobby_type_path:
        evidence_seen = True
        value = get_path(match, plan.lobby_type_path.response_path)
        numeric = normalize_int(value)
        text = enum_text(value).upper()
        if numeric == 7 or "RANKED" in text:
            return True
        # Lobby type exists and is a known non-ranked numeric/string value.
        if numeric is not None or text:
            lobby_says_ranked = False
        else:
            lobby_says_ranked = None
    else:
        lobby_says_ranked = None

    if plan.game_mode_path:
        evidence_seen = True
        value = get_path(match, plan.game_mode_path.response_path)
        text = enum_text(value).upper()
        if "RANKED" in text:
            return True

    if not evidence_seen:
        return None
    if lobby_says_ranked is False:
        return False
    # A game-mode enum may not encode ranked/unranked, so lack of RANKED in it
    # is not enough to call the game unranked.  If lobby type was absent, mark
    # unknown and reject under ranked-only rather than guess.
    return None


def parse_mode_matches(raw_mode: Any, desired: str) -> bool:
    if desired.lower() == "any":
        return True
    raw_text = enum_text(raw_mode).strip().lower().replace("-", "_")
    desired_text = desired.strip().lower().replace("-", "_")
    try:
        if int(desired_text) == normalize_int(raw_mode):
            return True
    except ValueError:
        pass
    if desired_text == "turbo":
        return normalize_int(raw_mode) == DEFAULT_TURBO_MODE_ID or "turbo" in raw_text
    return desired_text == raw_text or desired_text in raw_text


def party_filter_matches(value: Any, semantics: str | None, desired: str) -> bool:
    if desired == "any":
        return True
    if semantics is None:
        raise StratzError(
            "STRATZ did not provide a party field this program knows how to read. Use --party any."
        )
    numeric = normalize_int(value)
    if semantics == "size":
        solo = numeric in {None, 0, 1}
    else:
        # partyId / partyIndex normally null/0/-1 when not in a party.
        solo = value in {None, "", False} or numeric in {None, -1, 0}
    return solo if desired == "solo" else not solo


def determine_win(
    match: Mapping[str, Any], player: Mapping[str, Any], plan: QueryPlan
) -> bool | None:
    if plan.player_win_path:
        win = normalize_bool(get_path(player, plan.player_win_path.response_path))
        if win is not None:
            return win
    if plan.match_radiant_win_path:
        radiant_win = normalize_bool(
            get_path(match, plan.match_radiant_win_path.response_path)
        )
        radiant = player_side(player, plan)
        if radiant_win is not None and radiant is not None:
            return radiant_win == radiant
    return None


def initial_page_value(plan: QueryPlan) -> int:
    if plan.pagination_kind != "before":
        return 0
    ptype = plan.pagination_type.upper()
    return MAX_GRAPHQL_INT if "INT" in ptype and "LONG" not in ptype else MAX_SAFE_GRAPHQL_LONG


def fetch_history_pages(
    client: StratzClient,
    plan: QueryPlan,
    cache: JsonCache,
    *,
    player_id: int,
    page_size: int,
    history_ttl_minutes: float = 15.0,
    max_matches: int | None = None,
    start_timestamp: int | None = None,
    end_timestamp: int | None = None,
    verbose: bool = False,
) -> Iterable[list[MatchReference]]:
    page = initial_page_value(plan)
    seen: set[int] = set()
    yielded = 0

    while True:
        variables: dict[str, Any] = {
            "playerId": player_id,
            "take": page_size,
            "page": page,
        }
        if "$historyPlayerId" in plan.history_query:
            variables["historyPlayerId"] = player_id
        signature = hashlib.sha256(plan.history_query.encode("utf-8")).hexdigest()[:16]
        variable_signature = hashlib.sha256(
            json.dumps(variables, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        cache_key = f"{player_id}_{signature}_{variable_signature}"
        cached = cache.get(
            "history-response",
            cache_key,
            max_age_seconds=max(0.0, history_ttl_minutes) * 60,
        )
        if isinstance(cached, Mapping):
            data = cached
            if verbose:
                print("Reused cached history page", file=sys.stderr)
        else:
            data = client.query(
                plan.history_query, variables, query_name="Player match history"
            )
            cache.put(
                "history-response",
                cache_key,
                data,
                metadata={
                    "kind": "graphql-data-fragment",
                    "query_sha256": hashlib.sha256(
                        plan.history_query.encode("utf-8")
                    ).hexdigest(),
                    "variables": variables,
                    "coverage": "player match-history page; fields selected by this build",
                    "ttl_minutes": history_ttl_minutes,
                },
            )
        player_obj = data.get(plan.player_root_field)
        if not isinstance(player_obj, Mapping):
            raise StratzError(
                "STRATZ returned no player data. Check account ID/profile visibility."
            )
        raw_matches = player_obj.get(plan.matches_field) or []
        if not isinstance(raw_matches, list):
            raise StratzError("Unexpected player.matches response")
        if verbose:
            print(f"Fetched history page: {len(raw_matches)} matches", file=sys.stderr)
        if not raw_matches:
            break

        refs: list[MatchReference] = []
        page_ids: list[int] = []
        page_times: list[int] = []
        for raw in raw_matches:
            if not isinstance(raw, Mapping):
                continue
            match_id = normalize_int(raw.get(plan.match_id_field))
            ts = parse_timestamp(raw.get(plan.start_time_field))
            if match_id is None or ts is None or match_id in seen:
                continue
            seen.add(match_id)
            page_ids.append(match_id)
            page_times.append(ts)
            if end_timestamp is not None and ts > end_timestamp:
                continue
            if start_timestamp is not None and ts < start_timestamp:
                continue
            refs.append(MatchReference(match_id, ts, raw))

        refs.sort(key=lambda r: r.start_timestamp, reverse=True)
        if refs:
            if max_matches is not None:
                refs = refs[: max(0, max_matches - yielded)]
            yielded += len(refs)
            if refs:
                yield refs

        if max_matches is not None and yielded >= max_matches:
            break
        if start_timestamp is not None and page_times and min(page_times) < start_timestamp:
            break
        if len(raw_matches) < page_size or not page_ids:
            break
        if plan.pagination_kind == "before":
            next_page = min(page_ids)
            if next_page >= page:
                raise StratzError("STRATZ before-pagination did not advance")
            page = next_page
        else:
            page += len(raw_matches)


def fetch_match_detail(
    client: StratzClient,
    plan: QueryPlan,
    cache: JsonCache,
    reference: MatchReference,
    *,
    player_id: int,
    all_players: bool,
) -> Mapping[str, Any] | None:
    query = plan.detail_all_query if all_players else plan.detail_one_query
    if not query:
        raise StratzError(
            "STRATZ requires a player filter for match details, so the lane program "
            "cannot safely identify every numbered position."
        )
    full_signature = hashlib.sha256(query.encode("utf-8")).hexdigest()
    signature = full_signature[:16]
    cache_key = f"{reference.match_id}_{signature}_{'all' if all_players else 'one'}"
    cached = cache.get("match-response", cache_key)
    if isinstance(cached, Mapping):
        cached_match = cached.get(plan.match_root_field)
        if cached_match is None or isinstance(cached_match, Mapping):
            return cached_match

    # v4 stored the match object directly. Reuse it when the same query hash is
    # available; the old SHA-1 key remains query-specific and therefore safe.
    old_signature = hashlib.sha1(
        query.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:12]
    old_key = f"{reference.match_id}_{old_signature}_{'all' if all_players else 'one'}"
    old_cached = cache.get("match-detail", old_key)
    if isinstance(old_cached, Mapping):
        return old_cached

    variables: dict[str, Any] = {"matchId": reference.match_id}
    if not all_players and "$selectedPlayerId" in query:
        variables["selectedPlayerId"] = player_id
    data = client.query(query, variables, query_name=f"Match {reference.match_id}")
    raw_match = data.get(plan.match_root_field)
    if raw_match is None:
        return None
    if not isinstance(raw_match, Mapping):
        raise StratzError(f"Match {reference.match_id}: unexpected match object")
    cache.put(
        "match-response",
        cache_key,
        data,
        metadata={
            "kind": "graphql-data-fragment",
            "match_id": reference.match_id,
            "query_sha256": full_signature,
            "variables": variables,
            "coverage": (
                "all players; only fields selected by this build"
                if all_players
                else "selected player; only fields selected by this build"
            ),
        },
    )
    return raw_match


def fetch_player_display_name(
    client: StratzClient,
    plan: QueryPlan,
    cache: JsonCache,
    *,
    player_id: int,
    ttl_hours: float = 24.0,
) -> str:
    fallback = f"Player {player_id}"
    if not plan.player_name_path:
        return fallback
    cache_key = str(player_id)
    cached = cache.get(
        "player-name", cache_key, max_age_seconds=max(0.0, ttl_hours) * 3600
    )
    if isinstance(cached, Mapping):
        cached_name = str(cached.get("name") or "").strip()
        if cached_name:
            return cached_name
    query = f"""
    query PlayerIdentity($playerId: {plan.player_root_arg_type}) {{
      {plan.player_root_field}({plan.player_root_arg}: $playerId) {{
        {plan.player_name_path.selection}
      }}
    }}
    """
    try:
        data = client.query(
            query, {"playerId": player_id}, query_name="Player name"
        )
        player = data.get(plan.player_root_field)
        name = str(
            get_path(player, plan.player_name_path.response_path)
            if isinstance(player, Mapping)
            else ""
        ).strip()
        if name:
            cache.put(
                "player-name",
                cache_key,
                {"name": name, "player_id": player_id},
                metadata={"kind": "STRATZ player display name", "ttl_hours": ttl_hours},
            )
            return name
    except StratzError:
        pass
    return fallback

def subtract_months(dt: datetime, months: int) -> datetime:
    if months < 0:
        raise ValueError("months must be non-negative")
    year = dt.year
    month = dt.month - months
    while month <= 0:
        month += 12
        year -= 1
    # clamp day to target month's last day
    next_month = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=dt.tzinfo)
    last_day = (next_month - timedelta(days=1)).day
    return dt.replace(year=year, month=month, day=min(dt.day, last_day))


def parse_datetime_arg(text: str | None, *, end: bool = False) -> datetime | None:
    if not text:
        return None
    raw = text.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if len(text.strip()) == 10 and end:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt.astimezone(timezone.utc)


WEEKDAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
)
WEEKDAY_ALIASES: dict[int, set[str]] = {
    0: {"m", "mo", "mon", "monday"},
    1: {"tu", "tue", "tues", "tuesday"},
    2: {"w", "we", "wed", "weds", "wednesday"},
    3: {"th", "thu", "thur", "thurs", "thursday"},
    4: {"f", "fr", "fri", "friday"},
    5: {"sa", "sat", "saturday"},
    6: {"su", "sun", "sunday"},
}


def _day_number(token: str) -> int:
    key = re.sub(r"[^a-z]", "", token.lower())
    if not key:
        raise StratzError("Empty day name in --days")
    if key == "t":
        raise StratzError("Day alias 'T' is ambiguous; use Tu or Th")
    alias_matches = {
        number
        for number, aliases in WEEKDAY_ALIASES.items()
        if key in aliases
    }
    if len(alias_matches) == 1:
        return next(iter(alias_matches))
    matches = {
        number for number, name in enumerate(WEEKDAY_NAMES)
        if key in name.lower()
    }
    if len(matches) == 1:
        return next(iter(matches))
    if not matches:
        raise StratzError(f"Unknown day {token!r}")
    choices = ", ".join(WEEKDAY_NAMES[number] for number in sorted(matches))
    raise StratzError(f"Day {token!r} is ambiguous ({choices})")


def parse_days_spec(value: Any) -> frozenset[int]:
    """Parse friendly weekday selections into Monday=0 through Sunday=6."""
    if value is None:
        return frozenset(range(7))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        selected: set[int] = set()
        for part in value:
            selected.update(parse_days_spec(part))
        if not selected:
            raise StratzError("--days must select at least one day")
        return frozenset(selected)

    raw = str(value).strip()
    key = re.sub(r"[^a-z]", "", raw.lower())
    if key in {"all", "every", "everyday", "daily", "sevendays"}:
        return frozenset(range(7))
    if key in {"work", "workday", "workdays", "weekday", "weekdays"}:
        return frozenset(range(5))
    if key in {"weekend", "weekends"}:
        return frozenset({5, 6})

    range_match = re.fullmatch(r"\s*(.+?)\s*(?:-|\bto\b)\s*(.+?)\s*", raw, re.IGNORECASE)
    if range_match:
        first = _day_number(range_match.group(1))
        last = _day_number(range_match.group(2))
        selected = {first}
        while first != last:
            first = (first + 1) % 7
            selected.add(first)
        return frozenset(selected)

    if len(key) > 1 and set(key) <= {"m", "w", "f"}:
        compact = {"m": 0, "w": 2, "f": 4}
        return frozenset(compact[letter] for letter in key)

    parts = [part for part in re.split(r"[,/+\s]+", raw) if part]
    if len(parts) > 1:
        return frozenset(_day_number(part) for part in parts)
    return frozenset({_day_number(raw)})


def describe_days(value: Any) -> str:
    selected = parse_days_spec(value)
    if selected == frozenset(range(7)):
        return "all days"
    if selected == frozenset(range(5)):
        return "Monday to Friday"
    if selected == frozenset({5, 6}):
        return "weekends"
    return ", ".join(WEEKDAY_NAMES[number] for number in sorted(selected))


def percentile_unweighted(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    x = p * (len(ordered) - 1)
    lo = int(math.floor(x))
    hi = int(math.ceil(x))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (x - lo)


def rate(n: int, d: int) -> str:
    return "n/a" if not d else f"{n / d * 100:.1f}%"

def default_config_path() -> Path:
    return Path(__file__).resolve().parent / "config.json"


def default_example_config_path() -> Path:
    return Path(__file__).resolve().parent / "config.example.json"


def load_config(path: Path, *, ignore: bool = False) -> dict[str, Any]:
    if ignore:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise StratzError(f"Invalid config JSON {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise StratzError(f"Config {path} must contain a JSON object")
    return data


def save_mode_config(
    path: Path,
    mode: str,
    mode_defaults: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> None:
    data = load_config(path)
    safe = safe_settings_for_output(settings)
    root_keys = ROOT_CONFIG_KEYS - {"api_key"}
    data.update({key: safe[key] for key in root_keys if key in safe})
    data[mode] = {
        key: safe[key]
        for key in mode_defaults
        if key not in ROOT_CONFIG_KEYS and key in safe
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def safe_settings_for_output(settings: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(settings)
    if out.get("api_key"):
        out["api_key"] = "<redacted>"
    for key, value in list(out.items()):
        if isinstance(value, Path):
            out[key] = str(value)
    return out


def merge_settings(
    mode: str,
    defaults: Mapping[str, Any],
    args: argparse.Namespace,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    resolved = dict(COMMON_DEFAULTS)
    resolved.update(defaults)
    supported = set(resolved)
    resolved.update(
        {
            key: value
            for key, value in config.items()
            if key in supported and not isinstance(value, Mapping)
        }
    )
    mode_cfg = config.get(mode, {})
    if mode_cfg is not None and not isinstance(mode_cfg, Mapping):
        raise StratzError(f"Config key {mode!r} must contain a JSON object")
    if isinstance(mode_cfg, Mapping):
        resolved.update({key: value for key, value in mode_cfg.items() if key in supported})
    for key, value in vars(args).items():
        if key in {
            "mode", "defaults", "config", "factory_settings", "list_heroes",
            "list_items", "self_test",
        }:
            continue
        if value is not None:
            resolved[key] = value
    if resolved.get("cache_dir") is None:
        resolved["cache_dir"] = str(default_cache_dir())
    resolved["api_key"] = resolved.get("api_key")
    return resolved


def parse_edit_value(current: Any, raw: str) -> Any:
    if current is None:
        lower = raw.lower()
        if lower in {"none", "null", ""}:
            return None
        # best effort JSON scalar
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    if isinstance(current, bool):
        lower = raw.strip().lower()
        if lower in {"1", "true", "yes", "y", "on"}:
            return True
        if lower in {"0", "false", "no", "n", "off"}:
            return False
        raise ValueError("enter true/false")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    return raw


def interactive_review(
    mode: str,
    settings: MutableMapping[str, Any],
    *,
    config_path: Path,
    catalog: Mapping[str, Sequence[Mapping[str, Any]]],
    mode_defaults: Mapping[str, Any],
    value_parsers: Mapping[str, Callable[[Any], Any]] | None = None,
) -> bool:
    if not sys.stdin.isatty():
        print("stdin is non-interactive; proceeding with resolved settings (use -D to suppress this notice).", file=sys.stderr)
        return True
    editable = [
        key for key in settings.keys()
        if key not in {"api_key", "player_name", "hero_name", "item_name"}
    ]
    while True:
        print(f"\n{mode}: resolved settings")
        print("=" * 72)
        print(f"  API key: {'set' if settings.get('api_key') else 'MISSING (config.json or --api-key)'}")
        for index, key in enumerate(editable, 1):
            print(f"  {index:>2}. {key:<22} = {settings[key]!r}")
        print("\n[y] run   [number] edit   [s] save these mode defaults   [q] quit")
        choice = input("> ").strip().lower()
        if choice in {"y", "yes", ""}:
            return True
        if choice in {"q", "quit", "n", "no"}:
            return False
        if choice == "s":
            save_mode_config(config_path, mode, mode_defaults, settings)
            print(f"Saved to {config_path}")
            continue
        try:
            index = int(choice) - 1
            key = editable[index]
        except (ValueError, IndexError):
            print("Choose y, s, q, or a setting number.")
            continue
        if key == "hero_id":
            selected = choose_catalog_entry("heroes", catalog, settings[key])
            if selected:
                settings["hero_id"] = int(selected["id"])
                settings["hero_name"] = str(selected["name"])
            continue
        if key == "item_id":
            selected = choose_catalog_entry("items", catalog, settings[key])
            if selected:
                settings["item_id"] = int(selected["id"])
                settings["item_name"] = str(selected["name"])
            continue
        if value_parsers and key in value_parsers:
            raw = input(f"{key} [{settings[key]!r}] -> ").strip()
            if raw:
                try:
                    settings[key] = value_parsers[key](raw)
                except (StratzError, ValueError) as exc:
                    print(f"Invalid value: {exc}")
            continue
        raw = input(f"{key} [{settings[key]!r}] -> ").strip()
        if not raw:
            continue
        try:
            settings[key] = parse_edit_value(settings[key], raw)
        except ValueError as exc:
            print(f"Invalid value: {exc}")


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-D", "--defaults", action="store_true", help="Run immediately with config.json settings; explicit CLI overrides still apply.")
    parser.add_argument("-F", "--factory-settings", "--factory-defaults", dest="factory_settings", action="store_true", default=argparse.SUPPRESS, help="Ignore config.json and use baked-in settings; explicit CLI overrides still apply.")
    parser.add_argument("--config", default=argparse.SUPPRESS, help="Alternative JSON config path (default: config.json beside this script).")
    parser.add_argument("--api-key", default=None, help="One-run STRATZ API token override. Normally set api_key in ignored config.json.")
    parser.add_argument("--player-id", type=int, default=None, help="Steam account ID to analyse.")
    parser.add_argument("--hero-id", default=None, metavar="ID_OR_NAME", help="Hero ID, name, or case/spacing-insensitive alias. Programs that support it also accept any.")
    parser.add_argument("--sample-by", choices=("games", "months"), default=None, help="Use a target number of qualifying games (default) or every qualifying game in the time window.")
    parser.add_argument("-n", "--match-count", type=int, default=None, help="Target qualifying games when --sample-by games (default 150).")
    parser.add_argument("--months", type=int, default=None, help="Calendar months back when --sample-by months and --start is omitted (default 24).")
    parser.add_argument("--scan-limit", type=int, default=None, help="Maximum history matches to inspect while finding qualifying games.")
    parser.add_argument("--start", default=None, help="Inclusive ISO date/datetime; UTC if no offset.")
    parser.add_argument("--end", default=None, help="Inclusive ISO date/datetime; UTC if no offset.")
    parser.add_argument("--timezone", default=None, help="IANA local timezone used by reports (default Europe/London).")
    parser.add_argument("--days", default=None, metavar="DAYS", help="Local weekdays to include: all, work, weekends, MWF, Tu-Th, or day names/aliases. T alone is ambiguous.")
    parser.add_argument("--page-size", type=int, default=None, help="STRATZ history page size.")
    parser.add_argument("--workers", type=int, default=None, help="Concurrent match detail requests.")
    parser.add_argument("--timeout", type=float, default=None, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=None, help="Retries for transient HTTP/429/5xx failures.")
    parser.add_argument("--cache-dir", default=None, help="Match-detail JSON cache directory.")
    caching = parser.add_mutually_exclusive_group()
    caching.add_argument("--cache", dest="cache_enabled", action="store_true", default=None, help="Enable history, match-fragment, and hero/item caching (default).")
    caching.add_argument("--no-cache", dest="cache_enabled", action="store_false", default=None, help="Do not read or write any cache files for this run.")
    parser.add_argument("--catalog-ttl-hours", type=float, default=None, help="Hours before live hero/item names expire (default 24).")
    parser.add_argument("--history-ttl-minutes", type=float, default=None, help="Minutes before player-history pages refresh (default 15). Match details remain reusable.")
    parser.add_argument("--refresh", action="store_true", default=None, help="Bypass all caches and refresh match, history, and hero/item data.")
    tutorials = parser.add_mutually_exclusive_group()
    tutorials.add_argument("--tutorials", dest="tutorials", action="store_true", default=argparse.SUPPRESS, help="Show mistake-prevention tips (default).")
    tutorials.add_argument("--no-tutorials", dest="tutorials", action="store_false", default=argparse.SUPPRESS, help="Suppress tutorial tips; errors are still shown.")
    theme = parser.add_mutually_exclusive_group()
    theme.add_argument("--dark-mode", dest="dark_mode", action="store_true", default=None, help="Use the dark report and graph theme (default).")
    theme.add_argument("--light-mode", dest="dark_mode", action="store_false", default=None, help="Use the light report and graph theme.")
    parser.add_argument("--list-heroes", action="store_true", default=None, help="Print the live STRATZ hero ID/name grid and exit.")
    parser.add_argument("--list-items", action="store_true", default=None, help="Print the live STRATZ item ID/name grid and exit.")
    parser.add_argument("-v", "--verbose", action="store_true", default=None, help="Verbose schema/filter diagnostics.")

def apply_catalog_setting(
    settings: MutableMapping[str, Any],
    *,
    id_key: str,
    name_key: str,
    kind: str,
    catalog: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    value = settings.get(id_key)
    try:
        row = resolve_catalog_entry(value, kind, catalog)
    except StratzError:
        # Numeric IDs still work if the live catalogue is unavailable. Names do not.
        numeric = normalize_int(value)
        if numeric is None or not re.fullmatch(r"\s*\d+\s*", str(value)):
            raise
        settings[id_key] = numeric
        return
    settings[id_key] = int(row["id"])
    settings[name_key] = str(row["name"])


def print_requested_catalogs(
    args: argparse.Namespace,
    catalog: Mapping[str, Sequence[Mapping[str, Any]]],
) -> bool:
    shown = False
    if bool(getattr(args, "list_heroes", False)):
        print("\nSTRATZ HERO IDS")
        print_catalog_grid(catalog.get("heroes", []), limit=10_000)
        shown = True
    if bool(getattr(args, "list_items", False)):
        print("\nSTRATZ ITEM IDS")
        print_catalog_grid(catalog.get("items", []), limit=10_000)
        shown = True
    return shown


def resolve_sample_selection(settings: MutableMapping[str, Any]) -> None:
    requested = settings.get("sample_by")
    match_count_present = settings.get("match_count") is not None
    months_present = settings.get("months") is not None
    if requested is None:
        if match_count_present and months_present:
            tutorial_notice(
                "Both match_count and months are set",
                [
                    "Using match_count for this run.",
                    "Set sample_by to 'games' or 'months' in config.json to make the choice explicit.",
                    "The other value stays available as a quick alternative.",
                ],
                enabled=bool(settings.get("tutorials", True)),
                level="TIP",
            )
        requested = "games" if match_count_present else "months"
    settings["sample_by"] = str(requested)


def validate_common_settings(settings: Mapping[str, Any]) -> None:
    parse_days_spec(settings.get("days", "all"))
    if str(settings.get("sample_by")) not in {"games", "months"}:
        raise StratzError("sample_by must be 'games' or 'months'")
    if int(settings["match_count"]) <= 0:
        raise StratzError("--match-count must be greater than 0")
    if int(settings["months"]) <= 0:
        raise StratzError("--months must be greater than 0")
    if int(settings["scan_limit"]) < int(settings["match_count"]):
        raise StratzError("--scan-limit must be at least --match-count")
    if int(settings["page_size"]) <= 0 or int(settings["workers"]) <= 0:
        raise StratzError("--page-size and --workers must be greater than 0")
    if float(settings["catalog_ttl_hours"]) < 0:
        raise StratzError("--catalog-ttl-hours cannot be negative")
    if float(settings["history_ttl_minutes"]) < 0:
        raise StratzError("--history-ttl-minutes cannot be negative")


def prepare_program(
    args: argparse.Namespace,
    *,
    mode: str,
    mode_defaults: Mapping[str, Any],
    configure: Callable[[MutableMapping[str, Any], Mapping[str, Sequence[Mapping[str, Any]]]], None] | None = None,
    validate: Callable[[Mapping[str, Any]], None] | None = None,
    value_parsers: Mapping[str, Callable[[Any], Any]] | None = None,
    allow_any_hero: bool = False,
) -> tuple[dict[str, Any], StratzClient, QueryPlan, JsonCache]:
    """Resolve settings, perform the shared setup, and return a ready API plan."""
    tutorials_arg = getattr(args, "tutorials", None)
    tutorials_enabled = True if tutorials_arg is None else bool(tutorials_arg)
    config_path = Path(str(getattr(args, "config", default_config_path())))
    factory_settings = bool(getattr(args, "factory_settings", False))

    if not factory_settings and not config_path.exists():
        example_path = default_example_config_path()
        if example_path.exists():
            tutorial_notice(
                "config.json has not been created yet",
                [
                    f"Copy {example_path.name} and rename the copy to {config_path.name}.",
                    f"Open {config_path.name} and replace {TOKEN_PLACEHOLDER!r} with your token.",
                    f"Create or view your token at {STRATZ_API_PAGE}",
                    "Your personal config.json is excluded by .gitignore.",
                ],
                enabled=tutorials_enabled,
                level="WARNING",
            )

    config = load_config(config_path, ignore=factory_settings)
    settings = merge_settings(mode, mode_defaults, args, config)
    settings["player_name"] = f"Player {settings['player_id']}"
    settings["hero_name"] = f"Hero {settings['hero_id']}"
    settings["tutorials"] = (
        tutorials_enabled if tutorials_arg is not None
        else bool(settings.get("tutorials", True))
    )
    resolve_sample_selection(settings)

    if is_placeholder_token(settings.get("api_key")):
        tutorial_notice(
            "A real STRATZ API token is required",
            [
                f"Visit {STRATZ_API_PAGE} and create or copy your token.",
                    f"Put it in {config_path.name} as the root api_key value.",
                "Do not publish config.json. Git ignores it.",
                "When using -F, pass --api-key because config.json is ignored for that run.",
            ],
            enabled=bool(settings["tutorials"]),
            level="ERROR",
        )
        if not config_path.exists() and not factory_settings:
            raise StratzError(
                f"Copy {default_example_config_path().name} to {config_path.name}, "
                f"then add a STRATZ token from {STRATZ_API_PAGE}."
            )
        if factory_settings:
            raise StratzError(
                f"Factory settings ignore {config_path.name}. Get a token at "
                f"{STRATZ_API_PAGE} and pass it with --api-key for this run."
            )
        raise StratzError(
            f"Missing or placeholder STRATZ token. Get one at {STRATZ_API_PAGE} "
            f"and put it in {config_path.name}."
        )

    client = StratzClient(
        str(settings["api_key"]),
        timeout_seconds=float(settings["timeout"]),
        retries=int(settings["retries"]),
        verbose=bool(settings["verbose"]),
    )
    cache = JsonCache(
        Path(str(settings["cache_dir"])),
        refresh=bool(settings["refresh"]),
        enabled=bool(settings["cache_enabled"]),
    )
    catalog = load_dota_catalog(
        client,
        cache,
        ttl_hours=float(settings["catalog_ttl_hours"]),
        tutorials=bool(settings["tutorials"]),
    )
    hero_choice = str(settings.get("hero_id", "")).strip().lower()
    if allow_any_hero and hero_choice in {"any", "all", "none", "null"}:
        settings["hero_id"] = None
        settings["hero_name"] = "Any hero"
    else:
        apply_catalog_setting(
            settings, id_key="hero_id", name_key="hero_name", kind="heroes", catalog=catalog
        )
    if configure:
        configure(settings, catalog)
    validate_common_settings(settings)
    if validate:
        validate(settings)

    if print_requested_catalogs(args, catalog):
        raise SystemExit(0)

    print(f"Build: {BUILD_ID}")
    print(f"Program: {Path(sys.argv[0]).resolve()}")
    if not bool(getattr(args, "defaults", False)):
        if not interactive_review(
            mode,
            settings,
            config_path=config_path,
            catalog=catalog,
            mode_defaults=mode_defaults,
            value_parsers=value_parsers,
        ):
            print("Cancelled.")
            raise SystemExit(0)
        validate_common_settings(settings)
        if validate:
            validate(settings)

    plan = discover_query_plan(client, verbose=bool(settings["verbose"]))
    settings["player_name"] = fetch_player_display_name(
        client,
        plan,
        cache,
        player_id=int(settings["player_id"]),
        ttl_hours=float(settings["catalog_ttl_hours"]),
    )
    return settings, client, plan, cache


def cli_exit(main: Callable[[], int]) -> None:
    """Run a command-line entry point with consistent, readable errors."""
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except StratzError as exc:
        print(colour(f"ERROR: {exc}", "red"), file=sys.stderr)
        raise SystemExit(2)
