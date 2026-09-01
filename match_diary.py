#!/usr/bin/env python3
"""Keep an autosaving diary for a player's recent ranked Dota matches."""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.server
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence
from zoneinfo import ZoneInfo

from stratz_common import (
    ACCOUNT_ID_FIELD,
    GraphQLError,
    JsonCache,
    MatchReference,
    QueryPlan,
    SchemaInspector,
    StratzClient,
    StratzError,
    add_common_arguments,
    cli_exit,
    configure_console_text,
    determine_win,
    enum_text,
    fetch_history_pages,
    fetch_match_detail,
    field_args,
    get_path,
    is_ranked_match,
    is_required_argument,
    load_dota_catalog,
    normalize_int,
    normalize_position,
    player_account_id,
    player_hero_id,
    player_side,
    parse_timestamp,
    prepare_program,
    select_player,
    type_kind,
    unwrap_named_type,
)


METRICS = ("comms", "behaviour", "skill", "teamwork", "impact")
FUN_PHASES = ("overall", "early", "mid", "late")
SORT_MATCH_TIME = "match_time"
SORT_DIARY_STARTED = "diary_started"
SORT_DIARY_UPDATED = "diary_updated"
SORT_OPTIONS = (SORT_MATCH_TIME, SORT_DIARY_STARTED, SORT_DIARY_UPDATED)
SORT_VALUE_FIELDS = {
    SORT_MATCH_TIME: "start_time_utc",
    SORT_DIARY_STARTED: "created_at_utc",
    SORT_DIARY_UPDATED: "updated_at_utc",
}
STORE_VERSION = 2
HOLIDAY_API = "https://date.nager.at/api/v3/publicholidays/{year}/{country}"
HOLIDAY_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
HERO_ICON_DIRECTORY = Path(__file__).with_name("assets") / "hero-icons"

EU_COUNTRIES: dict[str, str] = {
    "AT": "Austria", "BE": "Belgium", "BG": "Bulgaria", "HR": "Croatia",
    "CY": "Cyprus", "CZ": "Czechia", "DK": "Denmark", "EE": "Estonia",
    "FI": "Finland", "FR": "France", "DE": "Germany", "GR": "Greece",
    "HU": "Hungary", "IE": "Ireland", "IT": "Italy", "LV": "Latvia",
    "LT": "Lithuania", "LU": "Luxembourg", "MT": "Malta",
    "NL": "Netherlands", "PL": "Poland", "PT": "Portugal", "RO": "Romania",
    "SK": "Slovakia", "SI": "Slovenia", "ES": "Spain", "SE": "Sweden",
}

DIARY_DEFAULTS: dict[str, Any] = {
    "hero_id": "any",
    "sample_by": "games",
    "match_count": 50,
    "scan_limit": 250,
    "ranked_only": True,
    "recent_hours": 12.0,
    "diary_path": "dota_output/match-diary/diary.json",
    "rich_query_depth": 2,
    "uk_holiday_subdivision": "GB-ENG",
    "eu_holiday_countries": "all",
    "holiday_timeout": 12.0,
    "browser_mode": "window",
    "browser_port": 8765,
    "browser_path": "dota-match-diary",
    "sort_by": SORT_MATCH_TIME,
    "sort_descending": True,
}


def country_flag(code: str) -> str:
    code = code.upper()
    if len(code) != 2 or not code.isalpha():
        return code
    return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in code)


def selected_eu_country_codes(value: Any) -> tuple[str, ...]:
    if value is None or str(value).strip().lower() in {"", "all", "eu", "eu27"}:
        return tuple(EU_COUNTRIES)
    if isinstance(value, str):
        raw = value.replace(";", ",").replace(" ", ",").split(",")
    elif isinstance(value, Sequence):
        raw = list(value)
    else:
        raise StratzError("eu_holiday_countries must be 'all' or EU country codes")
    codes = tuple(dict.fromkeys(str(item).strip().upper() for item in raw if str(item).strip()))
    unknown = [code for code in codes if code not in EU_COUNTRIES]
    if unknown:
        raise StratzError(f"Unknown/non-EU holiday country code(s): {', '.join(unknown)}")
    return codes


class HolidayCalendar:
    def __init__(self, *, eu_country_codes: Sequence[str]) -> None:
        self.eu_country_codes = tuple(eu_country_codes)
        self.days: dict[str, dict[str, Any]] = {}

    def add_uk(self, date_text: str, name: str) -> None:
        day = self.days.setdefault(date_text, {"uk": [], "eu": {}})
        if name not in day["uk"]:
            day["uk"].append(name)

    def add_eu(self, date_text: str, country: str, name: str) -> None:
        day = self.days.setdefault(date_text, {"uk": [], "eu": {}})
        day["eu"].setdefault(country, [])
        if name not in day["eu"][country]:
            day["eu"][country].append(name)

    def get(self, local_date: Any) -> Mapping[str, Any]:
        return self.days.get(local_date.isoformat(), {"uk": [], "eu": {}})


class HolidayService:
    """Fetch and cache national public holidays without sending diary data."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        timeout: float,
        uk_subdivision: str,
        eu_country_codes: Sequence[str],
        cache_enabled: bool = True,
    ) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.uk_subdivision = uk_subdivision.upper()
        self.eu_country_codes = tuple(eu_country_codes)
        self.cache_enabled = cache_enabled

    def _cache_path(self, year: int, country: str) -> Path:
        return self.cache_dir / f"{year}_{country}.json"

    def _read_cache(self, path: Path, *, allow_stale: bool) -> list[dict[str, Any]] | None:
        if not self.cache_enabled:
            return None
        try:
            if not allow_stale and time.time() - path.stat().st_mtime > HOLIDAY_CACHE_MAX_AGE_SECONDS:
                return None
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, list):
            return None
        return [dict(item) for item in decoded if isinstance(item, Mapping)]

    def _fetch_country_year(self, year: int, country: str) -> tuple[list[dict[str, Any]], str | None]:
        path = self._cache_path(year, country)
        cached = self._read_cache(path, allow_stale=False)
        if cached is not None:
            return cached, None
        request = urllib.request.Request(
            HOLIDAY_API.format(year=year, country=country),
            headers={"Accept": "application/json", "User-Agent": "dota-match-diary/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310 - fixed HTTPS host
                decoded = json.loads(response.read().decode("utf-8"))
            if not isinstance(decoded, list):
                raise ValueError("response is not a JSON list")
            rows = [dict(item) for item in decoded if isinstance(item, Mapping)]
            if self.cache_enabled:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, path)
            return rows, None
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            stale = self._read_cache(path, allow_stale=True)
            if stale is not None:
                return stale, f"{country} {year}: refresh failed; using cached holidays ({exc})"
            return [], f"{country} {year}: holiday data unavailable ({exc})"

    @staticmethod
    def _is_public(row: Mapping[str, Any]) -> bool:
        types = row.get("types")
        return not isinstance(types, list) or any(str(value) in {"Public", "Bank"} for value in types)

    def load(self, years: Sequence[int]) -> tuple[HolidayCalendar, list[str]]:
        calendar = HolidayCalendar(eu_country_codes=self.eu_country_codes)
        warnings: list[str] = []
        requests = [(year, country) for year in sorted(set(years)) for country in ("GB", *self.eu_country_codes)]
        results: dict[tuple[int, str], list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(requests)))) as executor:
            futures = {
                executor.submit(self._fetch_country_year, year, country): (year, country)
                for year, country in requests
            }
            for future in as_completed(futures):
                key = futures[future]
                rows, warning = future.result()
                results[key] = rows
                if warning:
                    warnings.append(warning)
        for (_year, country), rows in results.items():
            for row in rows:
                date_text = str(row.get("date") or "")
                name = str(row.get("name") or row.get("localName") or "Public holiday")
                if not date_text or not self._is_public(row):
                    continue
                if country == "GB":
                    counties = row.get("counties")
                    applies = bool(row.get("global")) or counties is None or (
                        isinstance(counties, list) and self.uk_subdivision in counties
                    )
                    if applies:
                        calendar.add_uk(date_text, name)
                elif bool(row.get("global")):
                    calendar.add_eu(date_text, country, name)
        return calendar, warnings


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_diary() -> dict[str, Any]:
    return {
        "general_comments": "",
        "comeback_or_throw": False,
        "fun": {phase: None for phase in FUN_PHASES},
        "players": {},
    }


def empty_player_diary() -> dict[str, Any]:
    return {
        "comments": "",
        "ratings": {metric: None for metric in METRICS},
        "doomism": {"chat": None, "gameplay": None},
        "avoid": False,
        "friended": False,
        "token_farming": False,
        "comms_frequency": None,
        "mute_likelihood": None,
    }


def normalise_rating(value: Any, maximum: int, minimum: int = 1) -> int | None:
    if value in {None, "", "skip"}:
        return None
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def normalise_diary(raw: Any) -> dict[str, Any]:
    result = empty_diary()
    if not isinstance(raw, Mapping):
        return result
    result["general_comments"] = str(raw.get("general_comments") or "")
    result["comeback_or_throw"] = bool(raw.get("comeback_or_throw", False))
    raw_fun = raw.get("fun")
    if isinstance(raw_fun, Mapping):
        result["fun"] = {
            phase: normalise_rating(raw_fun.get(phase), 5) for phase in FUN_PHASES
        }
    raw_players = raw.get("players")
    if isinstance(raw_players, Mapping):
        for key, value in raw_players.items():
            if not isinstance(value, Mapping):
                continue
            player = empty_player_diary()
            player["comments"] = str(value.get("comments") or "")
            raw_ratings = value.get("ratings")
            if isinstance(raw_ratings, Mapping):
                player["ratings"] = {
                    metric: normalise_rating(raw_ratings.get(metric), 5)
                    for metric in METRICS
                }
            raw_doomism = value.get("doomism")
            if isinstance(raw_doomism, Mapping):
                player["doomism"] = {
                    kind: normalise_rating(raw_doomism.get(kind), 5)
                    for kind in ("chat", "gameplay")
                }
            player["avoid"] = bool(value.get("avoid", False))
            player["friended"] = bool(value.get("friended", False))
            player["token_farming"] = bool(value.get("token_farming", False))
            player["comms_frequency"] = normalise_rating(
                value.get("comms_frequency"), 5
            )
            player["mute_likelihood"] = normalise_rating(
                value.get("mute_likelihood"), 5
            )
            result["players"][str(key)] = player
    return result


def player_diary_has_content(player: Mapping[str, Any]) -> bool:
    if str(player.get("comments") or "").strip():
        return True
    ratings = player.get("ratings")
    if isinstance(ratings, Mapping) and any(
        ratings.get(metric) is not None for metric in METRICS
    ):
        return True
    doomism = player.get("doomism")
    doomism_has_content = (
        isinstance(doomism, Mapping)
        and any(doomism.get(kind) is not None for kind in ("chat", "gameplay"))
    )
    return any((
        doomism_has_content,
        bool(player.get("avoid")),
        bool(player.get("friended")),
        bool(player.get("token_farming")),
        player.get("comms_frequency") is not None,
        player.get("mute_likelihood") is not None,
    ))


def diary_has_content(diary: Mapping[str, Any]) -> bool:
    if str(diary.get("general_comments") or "").strip():
        return True
    if bool(diary.get("comeback_or_throw")):
        return True
    fun = diary.get("fun")
    if isinstance(fun, Mapping) and any(fun.get(phase) is not None for phase in FUN_PHASES):
        return True
    players = diary.get("players")
    return isinstance(players, Mapping) and any(
        isinstance(player, Mapping) and player_diary_has_content(player)
        for player in players.values()
    )


def migrate_v1_diary(raw: Any) -> Any:
    """Preserve the meaning of ratings saved before every scale became 1–5."""
    if not isinstance(raw, Mapping):
        return raw
    migrated = copy.deepcopy(dict(raw))
    fun = migrated.get("fun")
    if isinstance(fun, MutableMapping):
        for phase in FUN_PHASES:
            old = normalise_rating(fun.get(phase), 10)
            fun[phase] = None if old is None else int(round(1 + (old - 1) * 4 / 9))
    players = migrated.get("players")
    if isinstance(players, MutableMapping):
        for player in players.values():
            if not isinstance(player, MutableMapping):
                continue
            old_comms = normalise_rating(player.get("comms_frequency"), 4, minimum=0)
            player["comms_frequency"] = None if old_comms is None else old_comms + 1
            old_mute = normalise_rating(player.get("mute_likelihood"), 2, minimum=0)
            player["mute_likelihood"] = (
                None if old_mute is None else {0: 1, 1: 3, 2: 5}[old_mute]
            )
    return migrated


class DiaryStore:
    """Atomic JSON persistence; only non-empty diary entries are retained."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "format_version": STORE_VERSION,
            "updated_at_utc": utc_now_iso(),
            "matches": {},
        }
        self.load()

    def load(self) -> None:
        try:
            decoded = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            raise StratzError(f"Could not read diary {self.path}: {exc}") from exc
        if not isinstance(decoded, Mapping):
            raise StratzError(f"Diary {self.path} must contain a JSON object")
        try:
            version = int(decoded.get("format_version", 0))
        except (TypeError, ValueError):
            version = 0
        if version not in {1, STORE_VERSION}:
            raise StratzError(
                f"Unsupported diary format in {self.path}; expected version 1 or {STORE_VERSION}"
            )
        matches: dict[str, Any] = {}
        raw_matches = decoded.get("matches")
        if raw_matches is not None and not isinstance(raw_matches, Mapping):
            raise StratzError(f"Diary {self.path} has an invalid matches object")
        for key, record in (raw_matches or {}).items():
            if not isinstance(record, Mapping):
                continue
            raw_diary = record.get("diary")
            if version == 1:
                raw_diary = migrate_v1_diary(raw_diary)
            diary = normalise_diary(raw_diary)
            if diary_has_content(diary):
                matches[str(key)] = {**record, "diary": diary}
        self.data = dict(decoded)
        self.data["matches"] = matches

    @property
    def matches(self) -> MutableMapping[str, Any]:
        return self.data["matches"]

    def save_entry(
        self, match_id: int, gameplay: Mapping[str, Any], diary: Mapping[str, Any]
    ) -> None:
        key = str(match_id)
        cleaned = normalise_diary(diary)
        if diary_has_content(cleaned):
            existing = self.matches.get(key, {})
            created = (
                existing.get("created_at_utc")
                if isinstance(existing, Mapping)
                else None
            ) or utc_now_iso()
            self.matches[key] = {
                "match_id": match_id,
                "created_at_utc": created,
                "updated_at_utc": utc_now_iso(),
                "gameplay": copy.deepcopy(dict(gameplay)),
                "diary": cleaned,
            }
        else:
            self.matches.pop(key, None)
        self.save()

    def save(self) -> None:
        self.data["format_version"] = STORE_VERSION
        self.data["updated_at_utc"] = utc_now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, self.path)


class LatestSnapshotWriter:
    """Coalesce diary writes off the UI/request path; one serial writer owns disk I/O."""

    def __init__(self, store: DiaryStore, *, debounce_seconds: float = 0.25) -> None:
        self.store = store
        self.debounce_seconds = debounce_seconds
        self.condition = threading.Condition()
        self.pending: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.change_number = 0
        self.saving = False
        self.closed = False
        self.last_error: str | None = None
        self.thread = threading.Thread(target=self._run, name="diary-latest-snapshot-writer", daemon=True)
        self.thread.start()

    def submit(self, match_id: int, gameplay: Mapping[str, Any], diary: Mapping[str, Any]) -> None:
        with self.condition:
            self.pending[str(match_id)] = (
                copy.deepcopy(dict(gameplay)), normalise_diary(diary)
            )
            self.change_number += 1
            self.condition.notify_all()

    def _run(self) -> None:
        while True:
            with self.condition:
                while not self.pending and not self.closed:
                    self.condition.wait()
                if self.closed and not self.pending:
                    return
                observed = self.change_number
                self.condition.wait(self.debounce_seconds)
                if observed != self.change_number and not self.closed:
                    continue
                batch = self.pending
                self.pending = {}
                self.saving = True
            try:
                for key, (gameplay, diary) in batch.items():
                    if diary_has_content(diary):
                        existing = self.store.matches.get(key, {})
                        created = (
                            existing.get("created_at_utc")
                            if isinstance(existing, Mapping)
                            else None
                        ) or utc_now_iso()
                        self.store.matches[key] = {
                            "match_id": int(key),
                            "created_at_utc": created,
                            "updated_at_utc": utc_now_iso(),
                            "gameplay": gameplay,
                            "diary": diary,
                        }
                    else:
                        self.store.matches.pop(key, None)
                self.store.save()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                with self.condition:
                    # A failed batch remains the latest state unless a newer
                    # snapshot for that match has already arrived.
                    for key, snapshot in batch.items():
                        self.pending.setdefault(key, snapshot)
            finally:
                with self.condition:
                    self.saving = False
                    self.condition.notify_all()

    def flush(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        with self.condition:
            self.condition.notify_all()
            while (self.pending or self.saving) and time.monotonic() < deadline:
                self.condition.wait(min(0.1, max(0.0, deadline - time.monotonic())))
            return not self.pending and not self.saving

    def close(self) -> None:
        self.flush()
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.thread.join(timeout=2.0)


def _safe_selection(
    inspector: SchemaInspector,
    type_name: str,
    *,
    depth: int,
    ancestors: frozenset[str] = frozenset(),
    omit: frozenset[str] = frozenset(),
) -> list[str]:
    """Select every argument-free scalar and shallow nested object the schema allows."""
    selections: list[str] = []
    for name, field in sorted(inspector.fields(type_name).items()):
        if name in omit or name.startswith("__"):
            continue
        if any(is_required_argument(arg) for arg in field_args(field).values()):
            continue
        kind = type_kind(field.get("type"))
        nested_name = unwrap_named_type(field.get("type"))
        if kind in {"SCALAR", "ENUM"}:
            selections.append(name)
        elif (
            kind in {"OBJECT", "INTERFACE"}
            and depth > 0
            and nested_name
            and nested_name not in ancestors
        ):
            nested = _safe_selection(
                inspector,
                nested_name,
                depth=depth - 1,
                ancestors=ancestors | {type_name},
            )
            if nested:
                selections.append(f"{name} {{ {' '.join(nested)} }}")
    return selections


def build_rich_match_query(
    client: StratzClient, plan: QueryPlan, *, depth: int
) -> str | None:
    if not plan.players_all_supported:
        return None
    inspector = SchemaInspector(client)
    match_parts = _safe_selection(
        inspector,
        plan.match_type_name,
        depth=max(0, depth),
        omit=frozenset({plan.match_players_field}),
    )
    player_parts = _safe_selection(
        inspector, plan.match_player_type, depth=max(0, depth)
    )
    if not player_parts:
        return None
    return f"""
    query MatchDiaryRich($matchId: {plan.match_root_arg_type}) {{
      {plan.match_root_field}({plan.match_root_arg}: $matchId) {{
        {' '.join(match_parts)}
        {plan.match_players_field} {{ {' '.join(player_parts)} }}
      }}
    }}
    """


def fetch_rich_match(
    client: StratzClient,
    plan: QueryPlan,
    cache: JsonCache,
    reference: MatchReference,
    *,
    player_id: int,
    rich_query: str | None,
) -> Mapping[str, Any] | None:
    if not rich_query:
        return fetch_match_detail(
            client, plan, cache, reference, player_id=player_id, all_players=True
        )
    digest = hashlib.sha256(rich_query.encode("utf-8")).hexdigest()
    key = f"{reference.match_id}_{digest[:16]}_diary-rich"
    cached = cache.get("match-response", key)
    if isinstance(cached, Mapping):
        match = cached.get(plan.match_root_field)
        if match is None or isinstance(match, Mapping):
            return match
    try:
        data = client.query(
            rich_query,
            {"matchId": reference.match_id},
            query_name=f"Rich match {reference.match_id}",
        )
    except GraphQLError:
        return fetch_match_detail(
            client, plan, cache, reference, player_id=player_id, all_players=True
        )
    match = data.get(plan.match_root_field)
    if match is not None and not isinstance(match, Mapping):
        raise StratzError(f"Match {reference.match_id}: unexpected match object")
    cache.put(
        "match-response",
        key,
        data,
        metadata={
            "kind": "graphql-data-fragment",
            "match_id": reference.match_id,
            "query_sha256": digest,
            "coverage": "broad diary snapshot: argument-free scalar and shallow nested fields",
        },
    )
    return match


def _nested_name(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key in ("displayName", "name", "personaName", "proSteamAccountName"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for nested in value.values():
            candidate = _nested_name(nested)
            if candidate:
                return candidate
    return None


def make_gameplay_snapshot(
    raw: Mapping[str, Any],
    reference: MatchReference,
    plan: QueryPlan,
    *,
    tracked_player_id: int,
    hero_names: Mapping[int, str],
) -> dict[str, Any]:
    tracked = select_player(
        raw.get(plan.match_players_field), player_id=tracked_player_id, plan=plan
    )
    won = determine_win(raw, tracked, plan) if tracked else None
    tracked_side = player_side(tracked, plan) if tracked else None
    raw_players = raw.get(plan.match_players_field)
    if isinstance(raw_players, Mapping):
        raw_players = [raw_players]
    players: list[dict[str, Any]] = []
    for index, player in enumerate(raw_players if isinstance(raw_players, list) else []):
        if not isinstance(player, Mapping):
            continue
        account_id = player_account_id(player, plan)
        hero_id = player_hero_id(player, plan)
        side = player_side(player, plan)
        steam = player.get("steamAccount") or player.get("account") or player.get("player")
        name = _nested_name(steam)
        anonymous = steam.get("isAnonymous") if isinstance(steam, Mapping) else None
        stratz_public = steam.get("isStratzPublic") if isinstance(steam, Mapping) else None
        player_key = str(account_id) if account_id is not None else f"slot-{index}"
        players.append(
            {
                "key": player_key,
                ACCOUNT_ID_FIELD: account_id,
                "name": name or (f"Player {account_id}" if account_id else "Anonymous player"),
                "hero_id": hero_id,
                "hero_name": hero_names.get(hero_id or -1, f"Hero {hero_id}" if hero_id else "Unknown hero"),
                "position": normalize_position(
                    get_path(player, plan.position_path.response_path)
                    if plan.position_path
                    else None
                ),
                "side": "Radiant" if side is True else "Dire" if side is False else "Unknown",
                "is_ally": side == tracked_side if side is not None and tracked_side is not None else None,
                "is_self": account_id == tracked_player_id,
                "profile_public": None if anonymous is None else not bool(anonymous),
                "is_stratz_public": None if stratz_public is None else bool(stratz_public),
                "main_role": None,
                "main_role_percent": None,
                "role_sample_size": None,
                "role_checked_at_utc": None,
                "token_farming_detected": None,
            }
        )
    players.sort(key=lambda row: (not bool(row["is_ally"]), row["side"], row["position"] or 99))
    duration = (
        get_path(raw, plan.duration_path.response_path) if plan.duration_path else None
    )
    mode = get_path(raw, plan.game_mode_path.response_path) if plan.game_mode_path else None
    lobby = get_path(raw, plan.lobby_type_path.response_path) if plan.lobby_type_path else None
    return {
        "match_id": reference.match_id,
        "start_time_utc": datetime.fromtimestamp(
            reference.start_timestamp, tz=timezone.utc
        ).isoformat(),
        "duration_seconds": normalize_int(duration),
        "game_mode": enum_text(mode),
        "lobby_type": enum_text(lobby),
        "ranked": is_ranked_match(raw, plan),
        "result": "win" if won is True else "loss" if won is False else "unknown",
        "tracked_player_id": tracked_player_id,
        "players": players,
        "stratz_url": f"https://stratz.com/matches/{reference.match_id}",
        "raw": copy.deepcopy(dict(raw)),
    }


def fetch_player_main_role(
    settings: Mapping[str, Any],
    client: StratzClient,
    plan: QueryPlan,
    cache: JsonCache,
    player_id: int,
) -> dict[str, Any]:
    """Summarise the last 50 ranked matches that expose a numbered role."""
    if player_id <= 0:
        raise StratzError("This player has no usable STRATZ account ID")
    positions: list[int] = []
    for page in fetch_history_pages(
        client,
        plan,
        cache,
        player_id=player_id,
        page_size=min(100, max(50, int(settings["page_size"]))),
        history_ttl_minutes=float(settings["history_ttl_minutes"]),
        max_matches=500,
        verbose=bool(settings["verbose"]),
    ):
        for reference in page:
            raw = reference.overview
            if not isinstance(raw, Mapping) or is_ranked_match(raw, plan) is not True:
                continue
            player = select_player(
                raw.get(plan.match_players_field), player_id=player_id, plan=plan
            )
            if not isinstance(player, Mapping) or plan.position_path is None:
                continue
            position = normalize_position(get_path(player, plan.position_path.response_path))
            if position is None:
                continue
            positions.append(position)
            if len(positions) >= 50:
                break
        if len(positions) >= 50:
            break
    return summarise_main_role(positions)


def summarise_main_role(positions: Sequence[int]) -> dict[str, Any]:
    if not positions:
        raise StratzError("STRATZ returned no ranked games with role data for this player")
    counts = Counter(positions)
    main_role = min(counts, key=lambda position: (-counts[position], position))
    return {
        "main_role": main_role,
        "main_role_percent": round(100.0 * counts[main_role] / len(positions), 1),
        "role_sample_size": len(positions),
        "role_checked_at_utc": utc_now_iso(),
    }


def effective_export_diary(
    gameplay: Mapping[str, Any], diary: Mapping[str, Any]
) -> dict[str, Any]:
    """Force computed off-role cases into the exported diary boolean."""
    result = normalise_diary(diary)
    players = gameplay.get("players")
    if not isinstance(players, list):
        return result
    for player in players:
        if not isinstance(player, Mapping):
            continue
        main_role = normalize_int(player.get("main_role"))
        match_role = normalize_int(player.get("position"))
        if main_role is None or match_role is None or main_role == match_role:
            continue
        key = str(player.get("key"))
        observed = result["players"].setdefault(key, empty_player_diary())
        observed["token_farming"] = True
    return result


def fetch_recent_games(
    settings: Mapping[str, Any],
    client: StratzClient,
    plan: QueryPlan,
    cache: JsonCache,
) -> tuple[list[dict[str, Any]], list[str]]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=float(settings["recent_hours"]))
    catalog = load_dota_catalog(
        client,
        cache,
        ttl_hours=float(settings["catalog_ttl_hours"]),
        tutorials=bool(settings["tutorials"]),
    )
    hero_names = {
        int(row["id"]): str(row["name"])
        for row in catalog.get("heroes", [])
        if normalize_int(row.get("id")) is not None
    }
    rich_query = build_rich_match_query(
        client, plan, depth=int(settings["rich_query_depth"])
    )
    refs: list[MatchReference] = []
    for page in fetch_history_pages(
        client,
        plan,
        cache,
        player_id=int(settings["player_id"]),
        page_size=int(settings["page_size"]),
        history_ttl_minutes=float(settings["history_ttl_minutes"]),
        max_matches=int(settings["scan_limit"]),
        start_timestamp=int(start.timestamp()),
        end_timestamp=int(end.timestamp()),
        verbose=bool(settings["verbose"]),
    ):
        refs.extend(page)
    details: dict[int, Mapping[str, Any] | None | Exception] = {}
    with ThreadPoolExecutor(max_workers=int(settings["workers"])) as executor:
        futures = {
            executor.submit(
                fetch_rich_match,
                client,
                plan,
                cache,
                ref,
                player_id=int(settings["player_id"]),
                rich_query=rich_query,
            ): ref
            for ref in refs
        }
        for future in as_completed(futures):
            ref = futures[future]
            try:
                details[ref.match_id] = future.result()
            except Exception as exc:  # keep the GUI useful when one match fails
                details[ref.match_id] = exc
    games: list[dict[str, Any]] = []
    warnings: list[str] = []
    for ref in sorted(refs, key=lambda value: value.start_timestamp, reverse=True):
        raw = details.get(ref.match_id)
        if isinstance(raw, Exception):
            warnings.append(f"Match {ref.match_id}: {raw}")
            continue
        if not isinstance(raw, Mapping):
            warnings.append(f"Match {ref.match_id}: no match data returned")
            continue
        if bool(settings["ranked_only"]) and is_ranked_match(raw, plan) is not True:
            continue
        games.append(
            make_gameplay_snapshot(
                raw,
                ref,
                plan,
                tracked_player_id=int(settings["player_id"]),
                hero_names=hero_names,
            )
        )
    return games, warnings


def fetch_game_by_id(
    settings: Mapping[str, Any],
    client: StratzClient,
    plan: QueryPlan,
    cache: JsonCache,
    match_id: int,
) -> dict[str, Any]:
    """Fetch one explicit match regardless of age and prepare it for diary entry."""
    if match_id <= 0:
        raise StratzError("Match ID must be a positive integer")
    rich_query = build_rich_match_query(
        client, plan, depth=int(settings["rich_query_depth"])
    )
    placeholder = MatchReference(match_id, 0)
    raw = fetch_rich_match(
        client,
        plan,
        cache,
        placeholder,
        player_id=int(settings["player_id"]),
        rich_query=rich_query,
    )
    if not isinstance(raw, Mapping):
        raise StratzError(f"STRATZ returned no data for match {match_id}")
    tracked = select_player(
        raw.get(plan.match_players_field),
        player_id=int(settings["player_id"]),
        plan=plan,
    )
    if tracked is None:
        raise StratzError(
            f"Player {settings['player_id']} is not present in match {match_id}"
        )
    timestamp = parse_timestamp(get_path(raw, plan.match_start_path.response_path))
    if timestamp is None:
        raise StratzError(f"Match {match_id} has no usable start time")
    catalog = load_dota_catalog(
        client,
        cache,
        ttl_hours=float(settings["catalog_ttl_hours"]),
        tutorials=bool(settings["tutorials"]),
    )
    hero_names = {
        int(row["id"]): str(row["name"])
        for row in catalog.get("heroes", [])
        if normalize_int(row.get("id")) is not None
    }
    return make_gameplay_snapshot(
        raw,
        MatchReference(match_id, timestamp),
        plan,
        tracked_player_id=int(settings["player_id"]),
        hero_names=hero_names,
    )


def export_records(
    records: Sequence[Mapping[str, Any]],
    *,
    gameplay_detail: str,
    diary_detail: str,
) -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    for record in records:
        gameplay = record.get("gameplay") if isinstance(record.get("gameplay"), Mapping) else {}
        diary = effective_export_diary(gameplay, record.get("diary") or {})
        row: dict[str, Any] = {"match_id": record.get("match_id") or gameplay.get("match_id")}
        if gameplay_detail == "summary":
            row["gameplay"] = {
                key: copy.deepcopy(gameplay.get(key))
                for key in (
                    "start_time_utc", "duration_seconds", "game_mode", "lobby_type",
                    "ranked", "result", "stratz_url",
                )
            }
            row["gameplay"]["players"] = [
                {key: copy.deepcopy(player.get(key)) for key in (
                    "key", ACCOUNT_ID_FIELD, "name", "hero_id", "hero_name", "position",
                    "side", "is_ally", "is_self", "profile_public", "is_stratz_public",
                    "main_role", "main_role_percent", "role_sample_size",
                    "role_checked_at_utc", "token_farming_detected",
                )}
                for player in gameplay.get("players", [])
                if isinstance(player, Mapping)
            ]
        elif gameplay_detail == "full":
            row["gameplay"] = copy.deepcopy(dict(gameplay))
        if diary_detail == "text":
            row["diary"] = {
                "general_comments": diary["general_comments"],
                "player_comments": {
                    key: value["comments"]
                    for key, value in diary["players"].items()
                    if str(value.get("comments") or "").strip()
                },
                "token_farming": {
                    key: True
                    for key, value in diary["players"].items()
                    if bool(value.get("token_farming"))
                },
            }
        elif diary_detail == "full":
            row["diary"] = diary
        exported.append(row)
    return exported


def records_to_markdown(records: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "# Dota match diary export",
        "",
        f"Generated: {utc_now_iso()}",
        "",
        "The ratings are diarist judgements, not objective measurements. Null means skipped.",
    ]
    for record in records:
        lines.extend(["", f"## Match {record.get('match_id')}", ""])
        gameplay = record.get("gameplay")
        if isinstance(gameplay, Mapping):
            for key in ("start_time_utc", "result", "duration_seconds", "game_mode", "lobby_type", "stratz_url"):
                if gameplay.get(key) not in {None, ""}:
                    lines.append(f"- {key.replace('_', ' ').title()}: {gameplay[key]}")
            players = gameplay.get("players")
            if isinstance(players, list):
                lines.extend(["", "### Players", ""])
                for player in players:
                    if isinstance(player, Mapping):
                        relation = (
                            "self" if player.get("is_self")
                            else "ally" if player.get("is_ally")
                            else "enemy"
                        )
                        profile_visibility = (
                            "public" if player.get("profile_public") is True
                            else "private" if player.get("profile_public") is False
                            else "unknown"
                        )
                        lines.append(
                            f"- {player.get('name')} — {player.get('hero_name')} "
                            f"({relation}, P{player.get('position') or '?'}, key {player.get('key')}, "
                            f"profile {profile_visibility}"
                            + (
                                f", main role P{player.get('main_role')} at {player.get('main_role_percent')}% "
                                f"of {player.get('role_sample_size')} sampled ranked games, "
                                f"token farming {'yes' if player.get('token_farming_detected') else 'no'}"
                                if player.get("main_role") is not None else ""
                            )
                            + ")"
                        )
            if "raw" in gameplay:
                lines.extend([
                    "", "### Full gameplay JSON", "", "```json",
                    json.dumps(gameplay, indent=2, ensure_ascii=False), "```",
                ])
        diary = record.get("diary")
        if isinstance(diary, Mapping):
            lines.extend(["", "### Diary", ""])
            lines.append(json.dumps(diary, indent=2, ensure_ascii=False))
    return "\n".join(lines) + "\n"



class BrowserDiaryService:
    """Thread-safe state and background work behind the localhost browser UI."""

    def __init__(
        self,
        *,
        store: DiaryStore,
        settings: MutableMapping[str, Any],
        refresh_callback: Callable[[], tuple[list[dict[str, Any]], list[str]]],
        target_callback: Callable[[int], dict[str, Any]],
        role_callback: Callable[[int], dict[str, Any]],
        holiday_service: HolidayService,
    ) -> None:
        self.store = store
        self.settings = settings
        self.refresh_callback = refresh_callback
        self.target_callback = target_callback
        self.role_callback = role_callback
        self.holiday_service = holiday_service
        self.writer = LatestSnapshotWriter(store)
        self.lock = threading.RLock()
        self.games: dict[str, dict[str, Any]] = {}
        self.diaries: dict[str, dict[str, Any]] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.holidays = HolidayCalendar(eu_country_codes=holiday_service.eu_country_codes)
        self.query_in_progress = False
        self.role_checks_in_progress: set[int] = set()
        self.status = "Opening diary…"
        for key, record in store.matches.items():
            if not isinstance(record, Mapping) or not isinstance(record.get("gameplay"), Mapping):
                continue
            self.games[str(key)] = copy.deepcopy(dict(record["gameplay"]))
            self._ensure_profile_visibility(self.games[str(key)])
            self.diaries[str(key)] = normalise_diary(record.get("diary"))
            self.metadata[str(key)] = {
                "created_at_utc": str(record.get("created_at_utc") or ""),
                "updated_at_utc": str(record.get("updated_at_utc") or ""),
            }
        self._load_holidays_async()

    @staticmethod
    def _ensure_profile_visibility(game: MutableMapping[str, Any]) -> None:
        """Backfill visibility on snapshots saved before the field was added."""
        players = game.get("players")
        raw = game.get("raw")
        raw_players = raw.get("players") if isinstance(raw, Mapping) else None
        if not isinstance(players, list) or not isinstance(raw_players, list):
            return
        raw_by_id: dict[int, Mapping[str, Any]] = {}
        for raw_player in raw_players:
            if not isinstance(raw_player, Mapping):
                continue
            account_id = normalize_int(raw_player.get("steamAccountId") or raw_player.get("accountId"))
            if account_id is not None:
                raw_by_id[account_id] = raw_player
        for player in players:
            if not isinstance(player, MutableMapping) or "profile_public" in player:
                continue
            raw_player = raw_by_id.get(normalize_int(player.get(ACCOUNT_ID_FIELD)) or -1, {})
            steam = raw_player.get("steamAccount") if isinstance(raw_player, Mapping) else None
            anonymous = steam.get("isAnonymous") if isinstance(steam, Mapping) else None
            stratz_public = steam.get("isStratzPublic") if isinstance(steam, Mapping) else None
            player["profile_public"] = None if anonymous is None else not bool(anonymous)
            player["is_stratz_public"] = None if stratz_public is None else bool(stratz_public)

    @staticmethod
    def _preserve_role_results(previous: Mapping[str, Any], current: MutableMapping[str, Any]) -> None:
        old_players = previous.get("players") if isinstance(previous.get("players"), list) else []
        new_players = current.get("players") if isinstance(current.get("players"), list) else []
        old_by_id = {
            normalize_int(player.get(ACCOUNT_ID_FIELD)): player
            for player in old_players
            if (
                isinstance(player, Mapping)
                and normalize_int(player.get(ACCOUNT_ID_FIELD)) is not None
            )
        }
        fields = (
            "main_role", "main_role_percent", "role_sample_size",
            "role_checked_at_utc", "token_farming_detected",
        )
        for player in new_players:
            if not isinstance(player, MutableMapping):
                continue
            old = old_by_id.get(normalize_int(player.get(ACCOUNT_ID_FIELD)))
            if not isinstance(old, Mapping):
                continue
            for field in fields:
                if old.get(field) is not None:
                    player[field] = copy.deepcopy(old[field])

    def _holiday_years(self) -> list[int]:
        years = {datetime.now().year}
        try:
            tz = ZoneInfo(str(self.settings["timezone"]))
        except Exception:
            tz = timezone.utc
        for game in self.games.values():
            try:
                years.add(datetime.fromisoformat(str(game["start_time_utc"])).astimezone(tz).year)
            except Exception:
                pass
        return sorted(years)

    def _load_holidays_async(self) -> None:
        years = self._holiday_years()

        def work() -> None:
            calendar, warnings = self.holiday_service.load(years)
            with self.lock:
                self.holidays = calendar
                if warnings and not self.query_in_progress:
                    self.status = (
                        f"Holiday flags loaded with {len(warnings)} warning(s); "
                        "cached dates were used where possible."
                    )

        threading.Thread(target=work, name="browser-diary-holidays", daemon=True).start()

    def _public_game(self, key: str, game: Mapping[str, Any]) -> dict[str, Any]:
        public = copy.deepcopy({name: value for name, value in game.items() if name != "raw"})
        current_diary = self.diaries.get(key, empty_diary())
        public["diary"] = copy.deepcopy(current_diary)
        public["has_diary"] = diary_has_content(current_diary)
        public["overall_fun"] = current_diary.get("fun", {}).get("overall")
        public.update(self.metadata.get(key, {}))
        try:
            tz = ZoneInfo(str(self.settings["timezone"]))
            local = datetime.fromisoformat(str(game["start_time_utc"])).astimezone(tz)
            local_date = local.date()
            public.update(
                local_date=local_date.isoformat(),
                local_day_label=local.strftime("%A, %d %B %Y"),
                local_time=local.strftime("%H:%M"),
                local_datetime=local.strftime("%A, %d %B %Y at %H:%M %Z"),
            )
            holiday = self.holidays.get(local_date)
            uk_names = list(holiday.get("uk") or [])
            eu = holiday.get("eu") if isinstance(holiday.get("eu"), Mapping) else {}
            eu_codes = sorted(str(code) for code in eu)
            is_weekend = local_date.weekday() >= 5
            if uk_names and eu_codes:
                public["day_class"] = "both"
            elif uk_names:
                public["day_class"] = "uk"
            elif eu_codes:
                public["day_class"] = "eu"
            else:
                public["day_class"] = "weekend" if is_weekend else ""
            public["holiday_flags"] = " ".join(country_flag(code) for code in eu_codes)
            details: list[str] = []
            if is_weekend:
                details.append("Weekend")
            if uk_names:
                details.append(f"UK ({self.holiday_service.uk_subdivision}): {', '.join(uk_names)}")
            for code in eu_codes:
                details.append(
                    f"{country_flag(code)} {EU_COUNTRIES.get(code, code)} — "
                    + ", ".join(str(name) for name in eu.get(code, []))
                )
            public["day_details"] = " | ".join(details) or "Ordinary working day"
        except Exception:
            public.update(
                local_date="", local_day_label="Unknown date", local_time="",
                local_datetime="Unknown date", day_class="", holiday_flags="",
                day_details="",
            )
        public["self_hero_name"] = next(
            (
                str(player.get("hero_name") or "Unknown hero")
                for player in public.get("players", [])
                if isinstance(player, Mapping) and player.get("is_self")
            ),
            "Unknown hero",
        )
        public["self_hero_id"] = next(
            (
                normalize_int(player.get("hero_id"))
                for player in public.get("players", [])
                if isinstance(player, Mapping) and player.get("is_self")
            ),
            None,
        )
        return public

    def state_payload(self) -> dict[str, Any]:
        with self.lock:
            default_sort_by = str(self.settings.get("sort_by", SORT_MATCH_TIME))
            default_sort_descending = bool(self.settings.get("sort_descending", True))
            ordered = self._sorted_game_items(
                default_sort_by,
                descending=default_sort_descending,
            )
            return {
                "games": [self._public_game(key, game) for key, game in ordered],
                "dark_mode": bool(self.settings.get("dark_mode", True)),
                "default_sort_by": default_sort_by,
                "default_sort_descending": default_sort_descending,
                "recent_hours": float(self.settings["recent_hours"]),
                "query_in_progress": self.query_in_progress,
                "status": self.status,
                "writer_error": self.writer.last_error,
            }

    def _sorted_game_items(
        self, sort_by: str, *, descending: bool
    ) -> list[tuple[str, dict[str, Any]]]:
        value_field = SORT_VALUE_FIELDS.get(sort_by, SORT_VALUE_FIELDS[SORT_MATCH_TIME])

        def value(item: tuple[str, dict[str, Any]]) -> str:
            key, game = item
            if value_field in {"created_at_utc", "updated_at_utc"}:
                return str(self.metadata.get(key, {}).get(value_field) or "")
            return str(game.get(value_field) or "")

        present = [item for item in self.games.items() if value(item)]
        missing = [item for item in self.games.items() if not value(item)]
        return sorted(present, key=value, reverse=descending) + missing

    def save_diary(self, match_id: int, raw: Any) -> None:
        key = str(match_id)
        with self.lock:
            if key not in self.games:
                raise StratzError(f"Match {match_id} is not loaded")
            diary = normalise_diary(raw)
            self.diaries[key] = diary
            now = utc_now_iso()
            if diary_has_content(diary):
                meta = self.metadata.setdefault(key, {})
                meta.setdefault("created_at_utc", now)
                meta["updated_at_utc"] = now
            else:
                self.metadata.pop(key, None)
            gameplay = copy.deepcopy(self.games[key])
        self.writer.submit(match_id, gameplay, diary)

    def start_refresh(self, *, reason: str) -> str:
        with self.lock:
            if self.query_in_progress:
                return "A STRATZ query is already running; editing remains available."
            self.query_in_progress = True
            hours = float(self.settings["recent_hours"])
            self.status = f"Checking the {hours:g}-hour window in the background…"

        def work() -> None:
            try:
                games, warnings = self.refresh_callback()
                with self.lock:
                    for gameplay in games:
                        key = str(gameplay["match_id"])
                        previous = self.games.get(key)
                        if isinstance(previous, Mapping):
                            self._preserve_role_results(previous, gameplay)
                        self.games[key] = copy.deepcopy(gameplay)
                        self.diaries.setdefault(key, empty_diary())
                        if diary_has_content(self.diaries[key]):
                            self.writer.submit(int(key), gameplay, self.diaries[key])
                    self.status = f"Check complete: {len(games)} game(s) available; {len(warnings)} warning(s)."
            except Exception as exc:
                with self.lock:
                    self.status = f"Refresh failed: {exc}"
            finally:
                with self.lock:
                    self.query_in_progress = False
                self._load_holidays_async()

        threading.Thread(target=work, name=f"browser-diary-refresh-{reason}", daemon=True).start()
        return self.status

    def extend_and_refresh(self) -> str:
        with self.lock:
            self.settings["recent_hours"] = float(self.settings["recent_hours"]) + 1.0
        return self.start_refresh(reason="extended")

    def add_match(self, match_id: int) -> str:
        key = str(match_id)
        with self.lock:
            if key in self.games:
                return f"Match {match_id} is already loaded."
            if self.query_in_progress:
                raise StratzError("Wait for the current STRATZ query to finish, then add the match ID.")
            self.query_in_progress = True
            self.status = f"Fetching match {match_id} in the background…"

        def work() -> None:
            try:
                gameplay = self.target_callback(match_id)
                with self.lock:
                    self.games[key] = copy.deepcopy(gameplay)
                    self.diaries.setdefault(key, empty_diary())
                    self.status = f"Match {match_id} is ready and becomes permanent when you enter diary data."
            except Exception as exc:
                with self.lock:
                    self.status = f"Could not add match {match_id}: {exc}"
            finally:
                with self.lock:
                    self.query_in_progress = False
                self._load_holidays_async()

        threading.Thread(target=work, name=f"browser-diary-match-{match_id}", daemon=True).start()
        return self.status

    def start_role_check(self, match_id: int, player_key: str) -> str:
        match_key = str(match_id)
        with self.lock:
            game = self.games.get(match_key)
            if game is None:
                raise StratzError(f"Match {match_id} is not loaded")
            players = game.get("players") if isinstance(game.get("players"), list) else []
            player = next(
                (row for row in players if isinstance(row, MutableMapping) and str(row.get("key")) == player_key),
                None,
            )
            if player is None:
                raise StratzError("Player is not present in this match")
            if player.get("profile_public") is not True:
                raise StratzError("Role history checks are available only for public profiles")
            account_id = normalize_int(player.get(ACCOUNT_ID_FIELD))
            if account_id is None:
                raise StratzError("This player has no usable STRATZ account ID")
            if account_id in self.role_checks_in_progress:
                return "That player's role check is already running."
            self.role_checks_in_progress.add(account_id)
            player["role_check_status"] = "checking"
            player.pop("role_check_error", None)

        def work() -> None:
            try:
                result = self.role_callback(account_id)
                with self.lock:
                    for game_key, loaded_game in self.games.items():
                        loaded_players = (
                            loaded_game.get("players")
                            if isinstance(loaded_game.get("players"), list)
                            else []
                        )
                        for loaded_player in loaded_players:
                            if (
                                not isinstance(loaded_player, MutableMapping)
                                or normalize_int(loaded_player.get(ACCOUNT_ID_FIELD)) != account_id
                            ):
                                continue
                            loaded_player.update(result)
                            loaded_player["role_check_status"] = "done"
                            loaded_player.pop("role_check_error", None)
                            main_role = normalize_int(result.get("main_role"))
                            match_role = normalize_int(loaded_player.get("position"))
                            detected = (
                                main_role is not None
                                and match_role is not None
                                and main_role != match_role
                            )
                            loaded_player["token_farming_detected"] = detected
                            diary = self.diaries.setdefault(game_key, empty_diary())
                            observed = diary["players"].setdefault(
                                str(loaded_player.get("key")), empty_player_diary()
                            )
                            observed["token_farming"] = detected
                        if diary_has_content(self.diaries.get(game_key, {})):
                            self.writer.submit(int(game_key), loaded_game, self.diaries[game_key])
            except Exception as exc:
                with self.lock:
                    for loaded_game in self.games.values():
                        loaded_players = (
                            loaded_game.get("players")
                            if isinstance(loaded_game.get("players"), list)
                            else []
                        )
                        for loaded_player in loaded_players:
                            if (
                                isinstance(loaded_player, MutableMapping)
                                and normalize_int(loaded_player.get(ACCOUNT_ID_FIELD)) == account_id
                            ):
                                loaded_player["role_check_status"] = "error"
                                loaded_player["role_check_error"] = str(exc)
            finally:
                with self.lock:
                    self.role_checks_in_progress.discard(account_id)

        threading.Thread(target=work, name=f"browser-diary-role-{account_id}", daemon=True).start()
        return f"Checking the last 50 ranked games with role data for player {account_id}…"

    def export_content(self, query: Mapping[str, Sequence[str]]) -> tuple[bytes, str, str]:
        gameplay_detail = (query.get("gameplay") or ["summary"])[0]
        diary_detail = (query.get("diary") or ["full"])[0]
        scope = (query.get("scope") or ["all"])[0]
        selected = (query.get("id") or [""])[0]
        fmt = (query.get("format") or ["markdown"])[0]
        sort_by = (query.get("sort_by") or [SORT_MATCH_TIME])[0]
        descending = (query.get("descending") or ["true"])[0].lower() not in {
            "false", "0", "no",
        }
        with self.lock:
            keys = [selected] if scope == "selected" and selected else [
                key for key, _game in self._sorted_game_items(sort_by, descending=descending)
                if diary_has_content(self.diaries.get(key, {}))
            ]
            records = []
            for key in keys:
                if key not in self.games or not diary_has_content(self.diaries.get(key, {})):
                    continue
                meta = self.metadata.get(key, {})
                gameplay = copy.deepcopy(self.games[key])
                records.append({
                    "match_id": int(key),
                    **meta,
                    "gameplay": gameplay,
                    "diary": effective_export_diary(gameplay, self.diaries[key]),
                })
        rows = export_records(records, gameplay_detail=gameplay_detail, diary_detail=diary_detail)
        if fmt == "json":
            content = json.dumps(
                {
                    "format": "dota-match-diary-export-v1",
                    "generated_at_utc": utc_now_iso(),
                    "matches": rows,
                },
                indent=2,
                ensure_ascii=False,
            ) + "\n"
            return content.encode("utf-8"), "application/json; charset=utf-8", "dota-diary-export.json"
        return records_to_markdown(rows).encode("utf-8"), "text/markdown; charset=utf-8", "dota-diary-export.md"

    def close(self) -> None:
        self.writer.close()


class DiaryHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR can distribute requests across unrelated listeners,
    # which is especially confusing for a fixed-address local application.
    allow_reuse_address = os.name != "nt"


def make_diary_handler(
    service: BrowserDiaryService, url_path: str, html: bytes
) -> type[http.server.BaseHTTPRequestHandler]:
    prefix = f"/{url_path.strip('/')}/"

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "DotaDiary/2"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _send(
            self, status: int, content: bytes, content_type: str,
            *, cache_control: str = "no-store",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(content)

        def _json(self, status: int, value: Any) -> None:
            self._send(
                status,
                json.dumps(value, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _route(self) -> tuple[str, Mapping[str, Sequence[str]]] | None:
            parsed = urllib.parse.urlparse(self.path)
            if not parsed.path.startswith(prefix):
                return None
            return parsed.path[len(prefix):], urllib.parse.parse_qs(parsed.query)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            route = self._route()
            if route is None:
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            path, query = route
            try:
                if path in {"", "index.html"}:
                    self._send(200, html, "text/html; charset=utf-8")
                elif match := re.fullmatch(r"hero-icons/(\d+)\.png", path):
                    icon_path = HERO_ICON_DIRECTORY / f"{int(match.group(1))}.png"
                    if not icon_path.is_file():
                        self._send(404, b"Not found", "text/plain; charset=utf-8")
                        return
                    self._send(
                        200, icon_path.read_bytes(), "image/png",
                        cache_control="public, max-age=86400",
                    )
                elif path == "api/state":
                    self._json(200, service.state_payload())
                elif path == "api/export":
                    content, content_type, filename = service.export_content(query)
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(content)
                elif path == "favicon.ico":
                    self._send(204, b"", "image/x-icon")
                else:
                    self._send(404, b"Not found", "text/plain; charset=utf-8")
            except Exception as exc:
                self._json(500, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            route = self._route()
            if route is None:
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            if self.headers.get("X-Diary-Request") != "1":
                self._send(403, b"Missing local diary request header", "text/plain; charset=utf-8")
                return
            path, _query = route
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 8 * 1024 * 1024:
                    raise StratzError("Request is too large")
                payload = json.loads(self.rfile.read(length) or b"{}")
                if path.startswith("api/diary/"):
                    service.save_diary(int(path.rsplit("/", 1)[1]), payload)
                    self._json(202, {"status": "Snapshot accepted; disk save is running in the background."})
                elif path == "api/refresh":
                    self._json(202, {"status": service.start_refresh(reason="new")})
                elif path == "api/extend":
                    self._json(202, {"status": service.extend_and_refresh()})
                elif path == "api/add-match":
                    self._json(202, {"status": service.add_match(int(payload["match_id"]))})
                elif path == "api/check-role":
                    self._json(202, {"status": service.start_role_check(
                        int(payload["match_id"]), str(payload["player_key"])
                    )})
                else:
                    self._send(404, b"Not found", "text/plain; charset=utf-8")
            except Exception as exc:
                self._json(400, {"error": str(exc)})

    return Handler


def open_diary_browser(url: str, mode: str) -> None:
    """Open the system browser, prioritising a true new window on Windows."""
    if mode == "tab":
        webbrowser.open_new_tab(url)
        return
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
            ) as key:
                prog_id = str(winreg.QueryValueEx(key, "ProgId")[0])
            command = str(winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, prog_id + r"\shell\open\command"))
            match = re.match(r'^\s*"([^"]+)"|^\s*([^\s]+)', command)
            executable = os.path.expandvars((match.group(1) or match.group(2)) if match else "")
            browser_name = Path(executable).name.lower()
            if browser_name in {"msedge.exe", "chrome.exe", "brave.exe", "opera.exe", "vivaldi.exe"}:
                command = [executable, "--new-window", url]
                subprocess.Popen(command, close_fds=True)  # noqa: S603 - registered default browser
                return
            if browser_name == "firefox.exe":
                command = [executable, "-new-window", url]
                subprocess.Popen(command, close_fds=True)  # noqa: S603 - registered default browser
                return
        except (OSError, ValueError):
            pass
    webbrowser.open_new(url)


def validate_diary(settings: Mapping[str, Any]) -> None:
    if float(settings["recent_hours"]) <= 0:
        raise StratzError("--recent-hours must be greater than 0")
    if int(settings["rich_query_depth"]) < 0 or int(settings["rich_query_depth"]) > 3:
        raise StratzError("--rich-query-depth must be between 0 and 3")
    subdivision = str(settings["uk_holiday_subdivision"]).strip().upper()
    if subdivision not in {"GB-ENG", "GB-WLS", "GB-SCT", "GB-NIR"}:
        raise StratzError("uk_holiday_subdivision must be GB-ENG, GB-WLS, GB-SCT, or GB-NIR")
    if float(settings["holiday_timeout"]) <= 0:
        raise StratzError("--holiday-timeout must be greater than 0")
    if str(settings.get("browser_mode", "window")) not in {"window", "tab"}:
        raise StratzError("browser_mode must be 'window' or 'tab'")
    if str(settings.get("sort_by", SORT_MATCH_TIME)) not in SORT_OPTIONS:
        raise StratzError(f"sort_by must be one of: {', '.join(SORT_OPTIONS)}")
    if not 1 <= int(settings.get("browser_port", 8765)) <= 65535:
        raise StratzError("browser_port must be between 1 and 65535")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(settings.get("browser_path", ""))):
        raise StratzError("browser_path must be a lowercase human-readable URL name, such as dota-match-diary")
    selected_eu_country_codes(settings.get("eu_holiday_countries"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open an autosaving localhost browser diary for a player's recent ranked Dota matches."
    )
    add_common_arguments(parser)
    parser.add_argument(
        "--recent-hours", type=float, default=None,
        help="Show undiaried matches this many hours back (default 12).",
    )
    parser.add_argument("--diary-path", default=None, help="Persistent diary JSON path.")
    parser.add_argument(
        "--rich-query-depth", type=int, default=None,
        help="Nested STRATZ data depth, 0-3 (default 2).",
    )
    parser.add_argument(
        "--uk-holiday-subdivision",
        choices=("GB-ENG", "GB-WLS", "GB-SCT", "GB-NIR"),
        default=None,
        help="UK bank-holiday region (default GB-ENG).",
    )
    parser.add_argument(
        "--eu-holiday-countries", default=None,
        help="'all' or comma-separated EU country codes whose national public holidays receive flags.",
    )
    parser.add_argument(
        "--holiday-timeout", type=float, default=None,
        help="Public-holiday API timeout seconds (default 12).",
    )
    ranked = parser.add_mutually_exclusive_group()
    ranked.add_argument("--ranked-only", dest="ranked_only", action="store_true", default=None)
    ranked.add_argument("--include-unranked", dest="ranked_only", action="store_false", default=None)
    browser = parser.add_mutually_exclusive_group()
    browser.add_argument(
        "--browser-window", dest="browser_mode", action="store_const",
        const="window", default=None,
        help="Ask the default browser for a new window (default).",
    )
    browser.add_argument(
        "--browser-tab", dest="browser_mode", action="store_const",
        const="tab", default=None,
        help="Open the diary in a new tab of the default browser.",
    )
    parser.add_argument(
        "--browser-port", type=int, default=None,
        help="Stable localhost port for the diary (default 8765).",
    )
    parser.add_argument("--browser-path", default=None, help="Stable readable URL path (default dota-match-diary).")
    parser.add_argument(
        "--sort-by", choices=SORT_OPTIONS, default=None,
        help="Initial match ordering field (default match_time).",
    )
    sort_direction = parser.add_mutually_exclusive_group()
    sort_direction.add_argument(
        "--sort-descending", dest="sort_descending", action="store_true",
        default=None,
        help="Show the newest/recently changed matches first (default).",
    )
    sort_direction.add_argument(
        "--sort-ascending", dest="sort_descending", action="store_false",
        default=None,
        help="Show the oldest/earliest matches first.",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Start the local diary server without opening a browser (prints its private URL).",
    )
    parser.add_argument("--self-test", action="store_true")
    return parser


def run_self_test() -> int:
    assert not diary_has_content(empty_diary())
    diary = empty_diary()
    diary["players"]["42"] = empty_player_diary()
    diary["players"]["42"]["ratings"]["teamwork"] = 5
    diary["players"]["42"]["doomism"]["chat"] = 4
    diary["players"]["42"]["comms_frequency"] = 1
    diary["players"]["42"]["mute_likelihood"] = 5
    diary["players"]["42"]["avoid"] = True
    diary["players"]["42"]["friended"] = True
    diary["players"]["42"]["token_farming"] = True
    assert diary_has_content(diary)
    with tempfile.TemporaryDirectory(prefix="match-diary-test-") as root:
        path = Path(root) / "diary.json"
        store = DiaryStore(path)
        store.save_entry(123, {"match_id": 123, "result": "win", "players": []}, diary)
        loaded = DiaryStore(path)
        assert loaded.matches["123"]["diary"]["players"]["42"]["ratings"]["teamwork"] == 5
        loaded_player = loaded.matches["123"]["diary"]["players"]["42"]
        assert loaded_player["doomism"]["chat"] == 4
        assert loaded_player["comms_frequency"] == 1
        assert loaded_player["mute_likelihood"] == 5
        assert loaded_player["avoid"] is True
        assert loaded_player["friended"] is True
        assert loaded_player["token_farming"] is True
        role_summary = summarise_main_role([1] * 30 + [2] * 20)
        assert role_summary["main_role"] == 1
        assert role_summary["main_role_percent"] == 60.0
        loaded.save_entry(123, {"match_id": 123}, empty_diary())
        assert "123" not in DiaryStore(path).matches
        holiday_root = Path(root) / "holidays"
        holiday_root.mkdir()
        (holiday_root / "2026_GB.json").write_text(
            json.dumps([{
                "date": "2026-08-31", "name": "Summer Bank Holiday",
                "global": False, "counties": ["GB-ENG", "GB-WLS"], "types": ["Bank"],
            }]), encoding="utf-8",
        )
        (holiday_root / "2026_FR.json").write_text(
            json.dumps([{
                "date": "2026-07-14", "name": "Bastille Day",
                "global": True, "counties": None, "types": ["Public"],
            }]), encoding="utf-8",
        )
        holidays, holiday_warnings = HolidayService(
            holiday_root, timeout=1, uk_subdivision="GB-ENG",
            eu_country_codes=("FR",), cache_enabled=True,
        ).load([2026])
        assert not holiday_warnings
        assert holidays.days["2026-08-31"]["uk"] == ["Summer Bank Holiday"]
        assert holidays.days["2026-07-14"]["eu"]["FR"] == ["Bastille Day"]
        assert country_flag("FR") == "🇫🇷"
        latest_path = Path(root) / "latest.json"
        latest_store = DiaryStore(latest_path)
        writer = LatestSnapshotWriter(latest_store, debounce_seconds=0.02)
        started = time.perf_counter()
        for number in range(100):
            newest = empty_diary()
            newest["general_comments"] = f"latest-{number}"
            writer.submit(456, {"match_id": 456, "players": []}, newest)
        assert time.perf_counter() - started < 0.25
        assert writer.flush(timeout=3)
        writer.close()
        assert DiaryStore(latest_path).matches["456"]["diary"]["general_comments"] == "latest-99"

        class FakeHolidayService:
            eu_country_codes: tuple[str, ...] = ()
            uk_subdivision = "GB-ENG"

            def load(self, _years: Sequence[int]) -> tuple[HolidayCalendar, list[str]]:
                return HolidayCalendar(eu_country_codes=()), []

        web_store = DiaryStore(Path(root) / "web.json")
        web_diary = empty_diary()
        web_diary["general_comments"] = "served"
        gameplay = {
            "match_id": 789, "start_time_utc": "2026-08-31T12:00:00+00:00",
            "result": "win", "players": [{
                "key": "42", ACCOUNT_ID_FIELD: 42, "name": "Role tester",
                "hero_id": 1, "hero_name": "Test hero", "position": 4, "profile_public": True,
                "is_ally": True, "is_self": False,
            }], "raw": {"large": True},
        }
        web_store.save_entry(789, gameplay, web_diary)
        web_service = BrowserDiaryService(
            store=web_store,
            settings={
                "timezone": "Europe/London", "dark_mode": True, "recent_hours": 12.0,
                "sort_by": SORT_DIARY_UPDATED, "sort_descending": False,
            },
            refresh_callback=lambda: ([], []), target_callback=lambda _match_id: gameplay,
            role_callback=lambda _player_id: {
                "main_role": 2,
                "main_role_percent": 72.0,
                "role_sample_size": 50,
                "role_checked_at_utc": utc_now_iso(),
            },
            holiday_service=FakeHolidayService(),  # type: ignore[arg-type]
        )
        test_token = "self-test-token"
        test_server = DiaryHTTPServer(
            ("127.0.0.1", 0), make_diary_handler(web_service, test_token, b"<html>test</html>")
        )
        if os.name == "nt":
            try:
                duplicate_server = DiaryHTTPServer(
                    test_server.server_address,
                    make_diary_handler(web_service, test_token, b"<html>duplicate</html>"),
                )
            except OSError:
                pass
            else:
                duplicate_server.server_close()
                raise AssertionError("Windows must reject a second diary server on the same port")
        test_thread = threading.Thread(target=test_server.serve_forever, daemon=True)
        test_thread.start()
        base = f"http://127.0.0.1:{test_server.server_address[1]}/{test_token}/"
        with urllib.request.urlopen(base + "api/state", timeout=2) as response:  # nosec B310 - loopback self-test
            served = json.loads(response.read().decode("utf-8"))
        assert served["games"][0]["diary"]["general_comments"] == "served"
        assert "raw" not in served["games"][0]
        assert served["games"][0]["self_hero_id"] is None
        assert served["default_sort_by"] == SORT_DIARY_UPDATED
        assert served["default_sort_descending"] is False
        icon_url = base + "hero-icons/1.png"
        with urllib.request.urlopen(icon_url, timeout=2) as response:  # nosec B310 - loopback self-test
            assert response.headers.get_content_type() == "image/png"
            assert response.read(8) == b"\x89PNG\r\n\x1a\n"
        updated = empty_diary()
        updated["general_comments"] = "accepted asynchronously"
        request = urllib.request.Request(
            base + "api/diary/789", data=json.dumps(updated).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Diary-Request": "1"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:  # nosec B310 - loopback self-test
            assert response.status == 202
        assert web_service.writer.flush(timeout=3)
        assert DiaryStore(web_store.path).matches["789"]["diary"]["general_comments"] == "accepted asynchronously"
        role_request = urllib.request.Request(
            base + "api/check-role", data=json.dumps({"match_id": 789, "player_key": "42"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Diary-Request": "1"}, method="POST",
        )
        with urllib.request.urlopen(role_request, timeout=2) as response:  # nosec B310 - loopback self-test
            assert response.status == 202
        deadline = time.monotonic() + 2
        while web_service.role_checks_in_progress and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not web_service.role_checks_in_progress
        checked_player = web_service.games["789"]["players"][0]
        assert checked_player["main_role"] == 2
        assert checked_player["token_farming_detected"] is True
        assert web_service.diaries["789"]["players"]["42"]["token_farming"] is True
        assert web_service.writer.flush(timeout=3)
        assert DiaryStore(web_store.path).matches["789"]["diary"]["players"]["42"]["token_farming"] is True
        test_server.shutdown()
        test_server.server_close()
        web_service.close()
    record = {"match_id": 7, "gameplay": {"match_id": 7, "raw": {"large": True}, "players": []}, "diary": diary}
    summary = export_records([record], gameplay_detail="summary", diary_detail="full")
    assert "raw" not in summary[0]["gameplay"]
    assert "teamwork" in records_to_markdown(summary)
    print(
        "Self-test passed: persistence, export, holidays, roles, sorting "
        "defaults, icons, and server isolation are OK"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    configure_console_text()
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_test()
    # A diary is itself the settings UI; avoid the older terminal settings review.
    args.defaults = True
    settings, client, plan, cache = prepare_program(
        args,
        mode="match-diary",
        mode_defaults=DIARY_DEFAULTS,
        validate=validate_diary,
        allow_any_hero=True,
    )
    store = DiaryStore(Path(str(settings["diary_path"])))
    holiday_service = HolidayService(
        Path(str(settings["cache_dir"])) / "holiday-calendars",
        timeout=float(settings["holiday_timeout"]),
        uk_subdivision=str(settings["uk_holiday_subdivision"]),
        eu_country_codes=selected_eu_country_codes(settings.get("eu_holiday_countries")),
        cache_enabled=bool(settings["cache_enabled"]),
    )

    def refresh() -> tuple[list[dict[str, Any]], list[str]]:
        return fetch_recent_games(settings, client, plan, cache)

    def target(match_id: int) -> dict[str, Any]:
        return fetch_game_by_id(settings, client, plan, cache, match_id)

    def role_check(player_id: int) -> dict[str, Any]:
        return fetch_player_main_role(settings, client, plan, cache, player_id)

    if not isinstance(settings, MutableMapping):
        settings = dict(settings)
    service = BrowserDiaryService(
        store=store,
        settings=settings,
        refresh_callback=refresh,
        target_callback=target,
        role_callback=role_check,
        holiday_service=holiday_service,
    )
    html_path = Path(__file__).with_name("match_diary_web.html")
    try:
        html = html_path.read_bytes()
    except OSError as exc:
        raise StratzError(f"Could not load browser UI {html_path}: {exc}") from exc
    port = int(settings["browser_port"])
    url_path = str(settings["browser_path"]).strip("/")
    try:
        server = DiaryHTTPServer(("127.0.0.1", port), make_diary_handler(service, url_path, html))
    except OSError as exc:
        service.close()
        raise StratzError(
            f"Could not start the diary at http://127.0.0.1:{port}/{url_path}/; "
            f"the fixed port may already be in use: {exc}"
        ) from exc
    url = f"http://127.0.0.1:{port}/{url_path}/"
    print(f"Diary URL: {url}")
    print("The diary server stays active while this process runs. Press Ctrl+C here to stop it.")
    service.start_refresh(reason="startup")

    if not args.no_browser:
        def open_browser() -> None:
            open_diary_browser(url, str(settings.get("browser_mode", "window")))

        threading.Timer(0.2, open_browser).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping diary server…")
    finally:
        server.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    cli_exit(main)
