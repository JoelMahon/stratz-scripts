#!/usr/bin/env python3
"""Compare lane or team net worth by match start time."""

import argparse
import bisect
import csv
import html
import json
import math
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence
from zoneinfo import ZoneInfo

from stratz_common import (
    COMMON_DEFAULTS,
    DEFAULT_HERO_ID,
    DEFAULT_HERO_NAME,
    DEFAULT_PLAYER_ID,
    DEFAULT_PLAYER_NAME,
    GoldEventPlan,
    JsonCache,
    MatchReference,
    QueryPlan,
    StratzClient,
    StratzError,
    ValuePath,
    add_common_arguments,
    cli_exit,
    configure_console_text,
    decode_jsonish,
    describe_days,
    enum_text,
    fetch_history_pages,
    fetch_match_detail,
    get_path,
    is_ranked_match,
    normalize_position,
    parse_datetime_arg,
    parse_days_spec,
    parse_timestamp,
    player_account_id,
    player_hero_id,
    player_side,
    prepare_program,
    safe_settings_for_output,
    select_player,
    subtract_months,
)


SNAPSHOT_SECONDS = (300, 600)
TIME_BUCKET_MINUTES = 30
TIME_BUCKETS_PER_DAY = 24 * 60 // TIME_BUCKET_MINUTES
COMBINED_ORDER = ("ally", "enemy")
DELTA_ORDER = ("delta",)


LANE_DEFAULTS: dict[str, Any] = {
    "comparison_mode": "lane",
    "player_position": 1,
    "ranked_only": True,
    "gold_kind": "networth",
    "snapshot_method": "linear",
    "newest_half_share": 0.70,
    "draw_ratio": 0.10,
    "stomp_ratio": 0.30,
    "series_mode": "combined",
    "include_delta": True,
    "mark_low_samples": True,
    "low_sample_effective_n": 3.0,
    "trim_empty_edge_buckets": True,
    "output_dir": "dota_output/lane-gold",
    "unique_output_dir": True,
    "png": False,
    "open_report": False,
}


@dataclass
class LaneMatch:
    match_id: int
    start_timestamp: int
    start_utc: str
    start_local: str
    local_hour_decimal: float
    game_mode: str
    lobby_type: str
    values_5m: dict[str, float]
    values_10m: dict[str, float]
    comparison_5m: dict[str, float]
    comparison_10m: dict[str, float]
    role_accounts: dict[str, int | None]
    role_heroes: dict[str, int | None]
    recency_weight: float = 0.0
    hour_a: int = 0
    hour_a_weight: float = 0.0
    hour_b: int = 0
    hour_b_weight: float = 0.0


def decode_gold_events(player: Mapping[str, Any], plan: GoldEventPlan) -> list[Mapping[str, Any]] | None:
    raw = decode_jsonish(get_path(player, plan.response_path))
    if raw is None:
        return None
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, Mapping)]
    if isinstance(raw, Mapping):
        # Search recursively for a list of objects containing time + economic fields.
        stack: list[Any] = [raw]
        while stack:
            current = stack.pop()
            if isinstance(current, list):
                mappings = [v for v in current if isinstance(v, Mapping)]
                if mappings:
                    keys = {str(k).lower() for k in mappings[0]}
                    if any("time" in k for k in keys) and any(
                        ("gold" in k or "networth" in k or "net_worth" in k) for k in keys
                    ):
                        return mappings
                stack.extend(current)
            elif isinstance(current, Mapping):
                stack.extend(current.values())
    return None


def event_value(event: Mapping[str, Any], plan: GoldEventPlan, gold_kind: str) -> float | None:
    if gold_kind == "networth":
        field_name = plan.networth_field
        fallback_names = ("networth", "netWorth", "net_worth")
    else:
        field_name = plan.gold_field
        fallback_names = ("gold", "currentGold", "reliableGold")
    value: Any = event.get(field_name) if field_name else None
    if value is None:
        for name in fallback_names:
            if name in event:
                value = event[name]
                break
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def event_time(event: Mapping[str, Any], plan: GoldEventPlan) -> float | None:
    value = event.get(plan.time_field) if plan.time_field else None
    if value is None:
        for name in ("time", "timeSeconds", "timestamp", "gameTime", "seconds"):
            if name in event:
                value = event[name]
                break
    try:
        number = float(value)
        if abs(number) > 100_000:  # likely milliseconds for a short game timeline
            number /= 1000
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def snapshot_value(
    events: Sequence[Mapping[str, Any]],
    plan: GoldEventPlan,
    *,
    target: float,
    gold_kind: str,
    method: str,
) -> float | None:
    samples: list[tuple[float, float]] = []
    for event in events:
        t = event_time(event, plan)
        value = event_value(event, plan, gold_kind)
        if t is not None and value is not None:
            samples.append((t, value))
    if not samples:
        return None
    samples.sort(key=lambda row: row[0])
    times = [row[0] for row in samples]
    index = bisect.bisect_left(times, target)
    if index < len(samples) and math.isclose(samples[index][0], target, abs_tol=1e-9):
        return samples[index][1]
    if method == "previous":
        return samples[index - 1][1] if index > 0 else None
    if method == "nearest":
        choices: list[tuple[float, float]] = []
        if index > 0:
            choices.append(samples[index - 1])
        if index < len(samples):
            choices.append(samples[index])
        return min(choices, key=lambda row: abs(row[0] - target))[1] if choices else None
    # Linear interpolation needs a sample on each side of the target.
    if index == 0 or index >= len(samples):
        return None
    t0, v0 = samples[index - 1]
    t1, v1 = samples[index]
    if t1 <= t0:
        return v0
    fraction = (target - t0) / (t1 - t0)
    return v0 + fraction * (v1 - v0)


def normalise_player_position(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"", "none", "any", "all", "off", "null"}:
        return None
    match = re.fullmatch(r"(?:p|pos|position)?\s*([1-5])", text)
    if not match:
        raise StratzError("Position must be P1, P2, P3, P4, P5, or none")
    return int(match.group(1))


def comparison_spec(settings: Mapping[str, Any]) -> dict[str, Any]:
    mode = str(settings.get("comparison_mode", "lane"))
    position = normalise_player_position(settings.get("player_position"))
    if mode == "team":
        return {
            "mode": mode,
            "position": position,
            "ally_roles": (),
            "enemy_roles": (),
            "ally_label": "Allied team",
            "enemy_label": "Enemy team",
            "detail_order": (),
            "detail_labels": {},
        }
    if position is None:
        raise StratzError("Lane comparison needs --position P1..P5; use --comparison-mode team for no position filter")
    if position in {1, 5}:
        ally_roles, enemy_roles = (1, 5), (3, 4)
    elif position in {3, 4}:
        ally_roles, enemy_roles = (3, 4), (1, 5)
    else:
        ally_roles, enemy_roles = (2,), (2,)
    detail_order = tuple(
        [f"ally_p{role}" for role in ally_roles]
        + [f"enemy_p{role}" for role in enemy_roles]
    )
    detail_labels = {
        **{f"ally_p{role}": f"Allied P{role}" for role in ally_roles},
        **{f"enemy_p{role}": f"Enemy P{role}" for role in enemy_roles},
    }
    return {
        "mode": mode,
        "position": position,
        "ally_roles": ally_roles,
        "enemy_roles": enemy_roles,
        "ally_label": "Allied " + " + ".join(f"P{role}" for role in ally_roles),
        "enemy_label": "Enemy " + " + ".join(f"P{role}" for role in enemy_roles),
        "detail_order": detail_order,
        "detail_labels": detail_labels,
    }


def prefilter_lane_overview(
    ref: MatchReference,
    plan: QueryPlan,
    *,
    player_id: int,
    hero_id: int | None,
    ranked_only: bool,
    player_position: int | None,
) -> bool:
    """Cheap rejection only. Unknown overview data remains a candidate."""
    match = ref.overview
    if not isinstance(match, Mapping):
        return True
    if ranked_only:
        ranked = is_ranked_match(match, plan)
        if ranked is False:
            return False
    if plan.history_has_player_overview:
        player = select_player(match.get(plan.match_players_field), player_id=player_id, plan=plan)
        if player:
            if hero_id is not None and player_hero_id(player, plan) != hero_id:
                return False
            if plan.position_path and player_position is not None:
                raw_pos = get_path(player, plan.position_path.response_path)
                pos = normalize_position(raw_pos)
                if pos is not None and pos != player_position:
                    return False
    return True


def parse_lane_match(
    match: Mapping[str, Any] | None,
    ref: MatchReference,
    plan: QueryPlan,
    *,
    player_id: int,
    hero_id: int | None,
    ranked_only: bool,
    comparison_mode: str,
    player_position: int | None,
    tz: Any,
    gold_kind: str,
    snapshot_method: str,
) -> tuple[LaneMatch | None, str | None]:
    if match is None:
        return None, "no match object"
    if (comparison_mode == "lane" or player_position is not None) and not plan.position_path:
        raise StratzError(
            "STRATZ did not provide an explicit numbered position field. "
            "Use team comparison with --position none."
        )
    if not plan.gold_event_plan:
        raise StratzError("Could not locate STRATZ playback gold or net worth events")
    if ranked_only:
        ranked = is_ranked_match(match, plan)
        if ranked is not True:
            return None, "ranked status missing/false"

    raw_players = match.get(plan.match_players_field)
    if not isinstance(raw_players, list):
        return None, "all-player response unavailable"
    players = [p for p in raw_players if isinstance(p, Mapping)]
    selected = select_player(players, player_id=player_id, plan=plan)
    if not selected:
        return None, "selected player missing"
    if hero_id is not None and player_hero_id(selected, plan) != hero_id:
        return None, "wrong hero"
    if player_position is not None:
        selected_position = normalize_position(
            get_path(selected, plan.position_path.response_path) if plan.position_path else None
        )
        if selected_position != player_position:
            return None, f"selected player not explicit POSITION_{player_position}"
    selected_side = player_side(selected, plan)
    if selected_side is None:
        return None, "selected player's team side unavailable"

    spec = comparison_spec(
        {"comparison_mode": comparison_mode, "player_position": player_position}
    )
    desired: dict[str, Mapping[str, Any]] = {}
    if comparison_mode == "team":
        allied = [p for p in players if player_side(p, plan) == selected_side]
        enemies = [p for p in players if player_side(p, plan) == (not selected_side)]
        if len(allied) != 5 or len(enemies) != 5:
            return None, f"expected 5 players per team with known side; got {len(allied)} and {len(enemies)}"
        for index, player in enumerate(allied, 1):
            desired[f"ally_{index}"] = player
        for index, player in enumerate(enemies, 1):
            desired[f"enemy_{index}"] = player
    else:
        if not plan.position_path:
            raise StratzError("Lane comparison needs STRATZ numbered positions")
        for side_name, target_side, roles in (
            ("ally", selected_side, spec["ally_roles"]),
            ("enemy", not selected_side, spec["enemy_roles"]),
        ):
            for role in roles:
                candidates = [
                    p
                    for p in players
                    if player_side(p, plan) == target_side
                    and normalize_position(get_path(p, plan.position_path.response_path)) == role
                ]
                if len(candidates) != 1:
                    return None, f"expected exactly one {side_name} P{role}; got {len(candidates)}"
                desired[f"{side_name}_p{role}"] = candidates[0]

    values: dict[int, dict[str, float]] = {300: {}, 600: {}}
    role_accounts: dict[str, int | None] = {}
    role_heroes: dict[str, int | None] = {}
    for label, player in desired.items():
        events = decode_gold_events(player, plan.gold_event_plan)
        if not events:
            return None, f"{label} gold events unavailable"
        for target in SNAPSHOT_SECONDS:
            value = snapshot_value(
                events,
                plan.gold_event_plan,
                target=target,
                gold_kind=gold_kind,
                method=snapshot_method,
            )
            if value is None:
                return None, f"{label} has no usable {target}s economic snapshot"
            values[target][label] = value
        role_accounts[label] = player_account_id(player, plan)
        role_heroes[label] = player_hero_id(player, plan)

    comparisons: dict[int, dict[str, float]] = {}
    for target in SNAPSHOT_SECONDS:
        ally_total = sum(value for key, value in values[target].items() if key.startswith("ally_"))
        enemy_total = sum(value for key, value in values[target].items() if key.startswith("enemy_"))
        comparisons[target] = {
            "ally": ally_total,
            "enemy": enemy_total,
            "delta": ally_total - enemy_total,
        }

    start_ts = parse_timestamp(get_path(match, plan.match_start_path.response_path)) or ref.start_timestamp
    utc_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    local_dt = utc_dt.astimezone(tz)
    local_decimal = local_dt.hour + local_dt.minute / 60 + local_dt.second / 3600
    raw_mode = get_path(match, plan.game_mode_path.response_path) if plan.game_mode_path else None
    raw_lobby = get_path(match, plan.lobby_type_path.response_path) if plan.lobby_type_path else None
    return (
        LaneMatch(
            match_id=ref.match_id,
            start_timestamp=start_ts,
            start_utc=utc_dt.isoformat(),
            start_local=local_dt.isoformat(),
            local_hour_decimal=local_decimal,
            game_mode=enum_text(raw_mode),
            lobby_type=enum_text(raw_lobby),
            values_5m=values[300],
            values_10m=values[600],
            comparison_5m=comparisons[300],
            comparison_10m=comparisons[600],
            role_accounts=role_accounts,
            role_heroes=role_heroes,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Lane-gold weighting/statistics
# ---------------------------------------------------------------------------

def slot_label(slot: int) -> str:
    total_minutes = (slot % TIME_BUCKETS_PER_DAY) * TIME_BUCKET_MINUTES
    hour = (total_minutes // 60) % 24
    minute = total_minutes % 60
    return f"{hour:02d}:{minute:02d}"


def time_bucket_split(local_hour_decimal: float) -> tuple[int, float, int, float]:
    """
    Linear split across half-hour buckets labelled HH:00 / HH:30 and centred
    15 minutes later.

    13:45 -> 100% into 13:30
    13:00 -> 50% into 12:30 and 50% into 13:00
    Wraps correctly across midnight.
    """
    total_minutes = (local_hour_decimal % 24.0) * 60.0
    u = total_minutes / TIME_BUCKET_MINUTES - 0.5
    left_floor = math.floor(u)
    fraction = u - left_floor
    left = left_floor % TIME_BUCKETS_PER_DAY
    right = (left + 1) % TIME_BUCKETS_PER_DAY
    if abs(fraction) < 1e-12:
        fraction = 0.0
    if abs(fraction - 1.0) < 1e-12:
        left = right
        right = (right + 1) % TIME_BUCKETS_PER_DAY
        fraction = 0.0
    return left, 1.0 - fraction, right, fraction


def solve_decay_ratio(n: int, newest_half_share: float) -> float:
    if n <= 1:
        return 1.0
    half = (n + 1) // 2
    equal_share = half / n
    if newest_half_share <= equal_share + 1e-12:
        return 1.0
    if newest_half_share >= 0.999999:
        return 0.5

    def share(r: float) -> float:
        total = sum(r**i for i in range(n))
        first = sum(r**i for i in range(half))
        return first / total

    lo, hi = 0.000001, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if share(mid) > newest_half_share:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def assign_lane_weights(matches: list[LaneMatch], newest_half_share: float) -> float:
    matches.sort(key=lambda m: m.start_timestamp, reverse=True)
    ratio = solve_decay_ratio(len(matches), newest_half_share)
    raw = [ratio**age for age in range(len(matches))]
    total = sum(raw) or 1.0
    for match, value in zip(matches, raw):
        match.recency_weight = value / total
        a, wa, b, wb = time_bucket_split(match.local_hour_decimal)
        match.hour_a, match.hour_a_weight = a, wa
        match.hour_b, match.hour_b_weight = b, wb
    return ratio


def weighted_quantile(values: Sequence[float], weights: Sequence[float], p: float) -> float | None:
    pairs = sorted(
        (float(v), float(w))
        for v, w in zip(values, weights)
        if math.isfinite(float(v)) and math.isfinite(float(w)) and w > 0
    )
    if not pairs:
        return None
    if len(pairs) == 1:
        return pairs[0][0]
    total = sum(w for _, w in pairs)
    xs = [v for v, _ in pairs]
    cdf: list[float] = []
    cumulative = 0.0
    for _, w in pairs:
        cdf.append((cumulative + 0.5 * w) / total)
        cumulative += w
    if p <= cdf[0]:
        return xs[0]
    if p >= cdf[-1]:
        return xs[-1]
    i = bisect.bisect_left(cdf, p)
    x0, x1 = xs[i - 1], xs[i]
    p0, p1 = cdf[i - 1], cdf[i]
    if p1 <= p0:
        return x1
    f = (p - p0) / (p1 - p0)
    return x0 + f * (x1 - x0)


def effective_n(weights: Sequence[float]) -> float:
    total = sum(w for w in weights if w > 0)
    sq = sum(w * w for w in weights if w > 0)
    return total * total / sq if sq > 0 else 0.0


@dataclass
class DistributionSummary:
    contributors: int
    effective_n: float
    weight_sum: float
    minimum: float | None
    p10: float | None
    p25: float | None
    median: float | None
    p75: float | None
    p90: float | None
    maximum: float | None


def summarise_distribution(values: Sequence[float], weights: Sequence[float]) -> DistributionSummary:
    clean = [(v, w) for v, w in zip(values, weights) if w > 0 and math.isfinite(v)]
    vals = [v for v, _ in clean]
    wts = [w for _, w in clean]
    return DistributionSummary(
        contributors=len(vals),
        effective_n=effective_n(wts),
        weight_sum=sum(wts),
        minimum=min(vals) if vals else None,
        p10=weighted_quantile(vals, wts, .10),
        p25=weighted_quantile(vals, wts, .25),
        median=weighted_quantile(vals, wts, .50),
        p75=weighted_quantile(vals, wts, .75),
        p90=weighted_quantile(vals, wts, .90),
        maximum=max(vals) if vals else None,
    )


def source_for_seconds(match: LaneMatch, seconds: int) -> Mapping[str, float]:
    return match.values_5m if seconds == 300 else match.values_10m


def comparison_for_seconds(match: LaneMatch, seconds: int) -> Mapping[str, float]:
    return match.comparison_5m if seconds == 300 else match.comparison_10m


def time_weight_for_slot(match: LaneMatch, slot: int) -> float:
    weight = 0.0
    if match.hour_a == slot:
        weight += match.hour_a_weight
    if match.hour_b == slot:
        weight += match.hour_b_weight
    return weight


def chart_definitions(settings: Mapping[str, Any]) -> list[dict[str, Any]]:
    spec = comparison_spec(settings)
    series_mode = str(settings.get("series_mode", "combined"))
    include_delta = bool(settings.get("include_delta", True))
    dark_mode = bool(settings.get("dark_mode", True))
    combined_colours = (
        {"ally": "#60a5fa", "enemy": "#fb7185"}
        if dark_mode
        else {"ally": "#2563eb", "enemy": "#dc2626"}
    )
    delta_colours = {"delta": "#c4b5fd" if dark_mode else "#6d28d9"}
    charts: list[dict[str, Any]] = []
    if series_mode in {"combined", "both"}:
        charts.append({
            "kind": "combined",
            "display_name": "Team totals" if spec["mode"] == "team" else "Lane totals",
            "filename_prefix": "lane_gold_combined",
            "series_order": list(COMBINED_ORDER),
            "labels": {"ally": spec["ally_label"], "enemy": spec["enemy_label"]},
            "colours": combined_colours,
        })
    if include_delta:
        charts.append({
            "kind": "delta",
            "display_name": "Allied minus enemy delta",
            "filename_prefix": "lane_gold_delta",
            "series_order": list(DELTA_ORDER),
            "labels": {"delta": f"{spec['ally_label']} minus {spec['enemy_label']}"},
            "colours": delta_colours,
        })
    if series_mode in {"detailed", "both"} and spec["detail_order"]:
        detail_order = list(spec["detail_order"])
        detail_palette = (
            ["#60a5fa", "#34d399", "#fb7185", "#c4b5fd"]
            if dark_mode
            else ["#2563eb", "#059669", "#dc2626", "#7c3aed"]
        )
        charts.append({
            "kind": "detailed",
            "display_name": "Individual position values",
            "filename_prefix": "lane_gold_detailed",
            "series_order": detail_order,
            "labels": dict(spec["detail_labels"]),
            "colours": {key: detail_palette[i % len(detail_palette)] for i, key in enumerate(detail_order)},
        })
    return charts


def chart_series_values(match: LaneMatch, seconds: int, kind: str) -> dict[Any, float]:
    if kind == "detailed":
        return {key: float(value) for key, value in source_for_seconds(match, seconds).items()}
    combined = comparison_for_seconds(match, seconds)
    if kind == "combined":
        return {"ally": combined["ally"], "enemy": combined["enemy"]}
    if kind == "delta":
        return {"delta": combined["delta"]}
    raise ValueError(f"Unknown chart kind {kind!r}")


def samples_for_slot(
    matches: Sequence[LaneMatch], slot: int, seconds: int, chart_kind: str, series_key: Any
) -> tuple[list[float], list[float]]:
    values: list[float] = []
    weights: list[float] = []
    for match in matches:
        tw = time_weight_for_slot(match, slot)
        if tw <= 0:
            continue
        series_map = chart_series_values(match, seconds, chart_kind)
        values.append(series_map[series_key])
        weights.append(match.recency_weight * tw)
    return values, weights


def occupied_slot_bounds(
    matches: Sequence[LaneMatch],
    *,
    seconds: int,
    chart: Mapping[str, Any],
    trim: bool,
) -> tuple[int, int]:
    if not trim:
        return 0, TIME_BUCKETS_PER_DAY - 1
    occupied: list[int] = []
    for slot in range(TIME_BUCKETS_PER_DAY):
        if any(
            samples_for_slot(
                matches, slot, seconds, str(chart["kind"]), series_key
            )[0]
            for series_key in chart["series_order"]
        ):
            occupied.append(slot)
    if not occupied:
        return 0, TIME_BUCKETS_PER_DAY - 1
    return occupied[0], occupied[-1]


def trim_empty_summary_edges(
    rows: Sequence[dict[str, Any]], *, enabled: bool
) -> list[dict[str, Any]]:
    if not enabled:
        return list(rows)
    kept: list[dict[str, Any]] = []
    minutes = list(dict.fromkeys(int(row["game_minute"]) for row in rows))
    for minute in minutes:
        group = [row for row in rows if int(row["game_minute"]) == minute]
        occupied = [
            index
            for index, row in enumerate(group)
            if any(
                key.endswith("_contributors") and int(value) > 0
                for key, value in row.items()
            )
        ]
        if occupied:
            kept.extend(group[occupied[0] : occupied[-1] + 1])
    return kept


def lane_outcome(ally_value: float, enemy_value: float, draw_ratio: float, stomp_ratio: float) -> str:
    if enemy_value <= 0:
        return "draw"
    ratio = ally_value / enemy_value - 1.0
    if ratio >= stomp_ratio:
        return "stomp_win"
    if ratio >= draw_ratio:
        return "win"
    if ratio <= -stomp_ratio:
        return "stomp_loss"
    if ratio <= -draw_ratio:
        return "loss"
    return "draw"


def outcome_for_slot(
    matches: Sequence[LaneMatch], slot: int, seconds: int, draw_ratio: float, stomp_ratio: float
) -> dict[str, float]:
    categories = {"stomp_win": 0.0, "win": 0.0, "draw": 0.0, "loss": 0.0, "stomp_loss": 0.0}
    total = 0.0
    for match in matches:
        tw = time_weight_for_slot(match, slot)
        if tw <= 0:
            continue
        combined = comparison_for_seconds(match, seconds)
        w = match.recency_weight * tw
        category = lane_outcome(combined["ally"], combined["enemy"], draw_ratio, stomp_ratio)
        categories[category] += w
        total += w
    if total:
        return {k: v / total for k, v in categories.items()}
    return categories


def weighted_combined_delta_summary(matches: Sequence[LaneMatch], slot: int, seconds: int) -> DistributionSummary:
    values: list[float] = []
    weights: list[float] = []
    for match in matches:
        tw = time_weight_for_slot(match, slot)
        if tw <= 0:
            continue
        combined = comparison_for_seconds(match, seconds)
        values.append(combined["delta"])
        weights.append(match.recency_weight * tw)
    return summarise_distribution(values, weights)


# ---------------------------------------------------------------------------
# SVG / HTML rendering
# ---------------------------------------------------------------------------

def weighted_mean_std(values: Sequence[float], weights: Sequence[float]) -> tuple[float, float]:
    total = sum(weights)
    if not total:
        return 0.0, 0.0
    mean = sum(v * w for v, w in zip(values, weights)) / total
    var = sum(w * (v - mean) ** 2 for v, w in zip(values, weights)) / total
    return mean, math.sqrt(max(var, 0.0))


def kde_points(values: Sequence[float], weights: Sequence[float], y_min: float, y_max: float, count: int = 72) -> list[tuple[float, float]]:
    if not values:
        return []
    if len(values) == 1 or max(values) == min(values):
        return [(values[0], 1.0)]
    _, std = weighted_mean_std(values, weights)
    q25 = weighted_quantile(values, weights, .25)
    q75 = weighted_quantile(values, weights, .75)
    iqr_scale = ((q75 - q25) / 1.34) if q25 is not None and q75 is not None and q75 > q25 else std
    scale = min(v for v in (std, iqr_scale) if v > 1e-9) if (std > 1e-9 or iqr_scale > 1e-9) else (max(values) - min(values)) / 4
    neff = max(effective_n(weights), 1.0)
    bandwidth = 0.9 * scale * (neff ** -0.2)
    if bandwidth <= 1e-9:
        bandwidth = max((y_max - y_min) / 50, 1.0)
    bandwidth = max(bandwidth, (y_max - y_min) / 180)
    total_w = sum(weights) or 1.0
    points: list[tuple[float, float]] = []
    norm = bandwidth * math.sqrt(2 * math.pi)
    for i in range(count):
        y = y_min + (y_max - y_min) * i / (count - 1)
        density = sum(w * math.exp(-0.5 * ((y - v) / bandwidth) ** 2) for v, w in zip(values, weights)) / (total_w * norm)
        points.append((y, density))
    max_density = max(d for _, d in points) or 1.0
    return [(y, d / max_density) for y, d in points]


def nice_ticks(minimum: float, maximum: float, count: int = 7) -> list[float]:
    if maximum <= minimum:
        return [minimum]
    rough = (maximum - minimum) / max(count - 1, 1)
    power = 10 ** math.floor(math.log10(rough))
    fraction = rough / power
    step_fraction = 1 if fraction <= 1 else 2 if fraction <= 2 else 5 if fraction <= 5 else 10
    step = step_fraction * power
    start = math.floor(minimum / step) * step
    end = math.ceil(maximum / step) * step
    ticks: list[float] = []
    value = start
    while value <= end + step * 0.001:
        ticks.append(value)
        value += step
    return ticks


def series_offsets(series_order: Sequence[Any]) -> dict[Any, float]:
    count = len(series_order)
    if count <= 1:
        return {series_order[0]: 0.0}
    span = 0.62 if count >= 4 else 0.44
    return {key: ((i / (count - 1)) - 0.5) * span for i, key in enumerate(series_order)}


def half_violin_width(slot_width: float, series_count: int) -> float:
    if series_count <= 1:
        return slot_width * 0.22
    if series_count == 2:
        return slot_width * 0.13
    return slot_width * 0.065


def svg_distribution_chart(
    matches: Sequence[LaneMatch],
    *,
    seconds: int,
    title: str,
    gold_label: str,
    chart: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> str:
    order = list(chart["series_order"])
    first_slot, last_slot = occupied_slot_bounds(
        matches,
        seconds=seconds,
        chart=chart,
        trim=bool(settings.get("trim_empty_edge_buckets", True)),
    )
    visible_slots = list(range(first_slot, last_slot + 1))
    left, right, top, bottom = 200, 80, 210, 270
    width = max(1800, left + right + 170 * len(visible_slots))
    height = 1200
    plot_w = width - left - right
    plot_h = height - top - bottom
    slot_w = plot_w / len(visible_slots)
    labels: Mapping[Any, str] = chart["labels"]
    colours: Mapping[Any, str] = chart["colours"]
    offsets = series_offsets(order)
    half_violin = half_violin_width(slot_w, len(order))
    mark_low_samples = bool(settings.get("mark_low_samples", True))
    low_sample_n = float(settings.get("low_sample_effective_n", 3.0))
    dark_mode = bool(settings.get("dark_mode", True))
    theme = (
        {
            "background": "#0b1220",
            "foreground": "#f8fafc",
            "muted": "#cbd5e1",
            "axis": "#94a3b8",
            "grid": "#334155",
            "hour_grid": "#1e293b",
            "missing": "#64748b",
        }
        if dark_mode
        else {
            "background": "#ffffff",
            "foreground": "#111827",
            "muted": "#4b5563",
            "axis": "#6b7280",
            "grid": "#e5e7eb",
            "hour_grid": "#f3f4f6",
            "missing": "#9ca3af",
        }
    )

    all_values: list[float] = []
    for match in matches:
        series_map = chart_series_values(match, seconds, str(chart["kind"]))
        all_values.extend(series_map.values())
    if not all_values:
        y_min, y_max = 0.0, 1.0
    else:
        data_min, data_max = min(all_values), max(all_values)
        pad = max((data_max - data_min) * 0.06, 100)
        if str(chart["kind"]) == "delta":
            limit = max(abs(data_min), abs(data_max)) + pad
            y_min, y_max = -limit, limit
        else:
            y_min = max(0.0, data_min - pad)
            y_max = data_max + pad

    def x_slot(slot: int) -> float:
        return left + (slot - first_slot + 0.5) * slot_w

    def y_px(value: float) -> float:
        return top + (y_max - value) / (y_max - y_min) * plot_h

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
        f'<style>text{{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;fill:{theme["foreground"]}}}.axis{{stroke:{theme["axis"]};stroke-width:1}}.grid{{stroke:{theme["grid"]};stroke-width:1}}.hourgrid{{stroke:{theme["hour_grid"]};stroke-width:1}}.whisker{{stroke:{theme["foreground"]}}}.missing{{fill:{theme["missing"]};font-size:27px}}.low-sample{{stroke-dasharray:7 5}}.low-note{{fill:{theme["muted"]};font-size:33px;font-weight:600}}</style>',
        '<defs>',
        *[
            f'<pattern id="low-sample-{i}" width="9" height="9" patternUnits="userSpaceOnUse"><rect width="9" height="9" fill="{colours[key]}" fill-opacity="0.20"/><circle cx="2.25" cy="2.25" r="1.8" fill="{colours[key]}"/></pattern>'
            for i, key in enumerate(order)
        ],
        '</defs>',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{theme["background"]}"/>',
        f'<text x="{left}" y="78" font-size="75" font-weight="700">{html.escape(title)}</text>',
        f'<text x="{left}" y="138" font-size="39" fill="{theme["muted"]}">Showing {len(visible_slots)} local-time half-hour buckets. Each match splits linearly across adjacent bucket centres.</text>',
    ]
    if mark_low_samples:
        parts.append(f'<text class="low-note" x="{left}" y="190">Dotted and faded violin = effective N below {low_sample_n:g}</text>')

    for tick in nice_ticks(y_min, y_max):
        if tick < y_min - 1e-6 or tick > y_max + 1e-6:
            continue
        y = y_px(tick)
        parts.append(f'<line class="grid" x1="{left}" x2="{width-right}" y1="{y:.2f}" y2="{y:.2f}"/>')
        parts.append(f'<text x="{left-20}" y="{y+11:.2f}" font-size="33" text-anchor="end">{tick:,.0f}</text>')

    if str(chart["kind"]) == "delta" and y_min < 0 < y_max:
        zero_y = y_px(0.0)
        parts.append(f'<line class="axis" x1="{left}" x2="{width-right}" y1="{zero_y:.2f}" y2="{zero_y:.2f}"/>')

    for slot in visible_slots:
        xc = x_slot(slot)
        line_class = "grid" if slot % 2 == 0 else "hourgrid"
        parts.append(f'<line class="{line_class}" x1="{xc-slot_w/2:.2f}" x2="{xc-slot_w/2:.2f}" y1="{top}" y2="{top+plot_h}"/>')
        label_y = top + plot_h + 55 + (38 if slot % 2 else 0)
        parts.append(f'<text x="{xc:.2f}" y="{label_y}" font-size="30" text-anchor="middle">{slot_label(slot)}</text>')

    for slot in visible_slots:
        for series_key in order:
            values, weights = samples_for_slot(matches, slot, seconds, str(chart["kind"]), series_key)
            summary = summarise_distribution(values, weights)
            xc = x_slot(slot) + offsets[series_key] * slot_w
            if not values or summary.median is None:
                parts.append(f'<text class="missing" x="{xc:.2f}" y="{top+plot_h-4}" text-anchor="middle">·</text>')
                continue
            curve = kde_points(values, weights, y_min, y_max)
            if len(curve) > 1:
                left_pts = [f"{xc - density*half_violin:.2f},{y_px(y):.2f}" for y, density in curve]
                right_pts = [f"{xc + density*half_violin:.2f},{y_px(y):.2f}" for y, density in reversed(curve)]
                path = "M" + " L".join(left_pts + right_pts) + " Z"
                colour = colours[series_key]
                label = labels[series_key]
                low_sample = mark_low_samples and summary.effective_n < low_sample_n
                pattern_id = order.index(series_key)
                fill = f"url(#low-sample-{pattern_id})" if low_sample else colour
                opacity = "0.70" if low_sample else "0.18"
                stroke = colour
                stroke_width = "2.4" if low_sample else "1.1"
                css_class = ' class="low-sample"' if low_sample else ""
                parts.append(f'<path{css_class} d="{path}" fill="{fill}" fill-opacity="{opacity}" stroke="{stroke}" stroke-width="{stroke_width}" data-effective-n="{summary.effective_n:.3f}"><title>{slot_label(slot)} {html.escape(str(label))}; contributors {summary.contributors}; effective N {summary.effective_n:.2f}</title></path>')

            xcap = half_violin * 0.62
            yminp, ymaxp = y_px(summary.minimum), y_px(summary.maximum)
            parts.append(f'<line class="whisker" x1="{xc:.2f}" x2="{xc:.2f}" y1="{yminp:.2f}" y2="{ymaxp:.2f}" stroke-width="0.8"/>')
            parts.append(f'<line class="whisker" x1="{xc-xcap:.2f}" x2="{xc+xcap:.2f}" y1="{yminp:.2f}" y2="{yminp:.2f}" stroke-width="0.8"/>')
            parts.append(f'<line class="whisker" x1="{xc-xcap:.2f}" x2="{xc+xcap:.2f}" y1="{ymaxp:.2f}" y2="{ymaxp:.2f}" stroke-width="0.8"/>')
            parts.append(f'<line x1="{xc:.2f}" x2="{xc:.2f}" y1="{y_px(summary.p10):.2f}" y2="{y_px(summary.p90):.2f}" stroke="{colours[series_key]}" stroke-width="2.3"/>')
            parts.append(f'<line x1="{xc:.2f}" x2="{xc:.2f}" y1="{y_px(summary.p25):.2f}" y2="{y_px(summary.p75):.2f}" stroke="{colours[series_key]}" stroke-width="5" stroke-linecap="round"/>')
            parts.append(f'<line x1="{xc-xcap:.2f}" x2="{xc+xcap:.2f}" y1="{y_px(summary.median):.2f}" y2="{y_px(summary.median):.2f}" stroke="{theme["foreground"]}" stroke-width="2.2"/>')

    parts.append(f'<line class="axis" x1="{left}" x2="{width-right}" y1="{top+plot_h}" y2="{top+plot_h}"/>')
    parts.append(f'<line class="axis" x1="{left}" x2="{left}" y1="{top}" y2="{top+plot_h}"/>')
    timezone_name = html.escape(str(settings.get("timezone", "Europe/London")))
    parts.append(f'<text x="{left + plot_w/2:.2f}" y="{height-55}" font-size="42" text-anchor="middle">Match start time bucket ({timezone_name})</text>')
    parts.append(f'<text x="55" y="{top + plot_h/2:.2f}" font-size="42" text-anchor="middle" transform="rotate(-90 55 {top + plot_h/2:.2f})">{html.escape(gold_label)}</text>')

    legend_spacing = 580
    legend_x = max(left, width - (legend_spacing * len(order) + 300))
    for i, series_key in enumerate(order):
        x = legend_x + i * legend_spacing
        parts.append(f'<rect x="{x}" y="150" width="42" height="42" rx="6" fill="{colours[series_key]}" fill-opacity="0.7"/>')
        parts.append(f'<text x="{x+60}" y="184" font-size="36">{html.escape(str(labels[series_key]))}</text>')
    parts.append('</svg>')
    return "".join(parts)


def fmt_num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def fmt_dist(summary: DistributionSummary) -> str:
    if summary.median is None:
        return "n/a"
    return (
        f"<strong>{fmt_num(summary.median)}</strong> "
        f"<span class='muted'>[25th {fmt_num(summary.p25)}, 75th {fmt_num(summary.p75)}] "
        f"10th {fmt_num(summary.p10)}, 90th {fmt_num(summary.p90)}; "
        f"min {fmt_num(summary.minimum)}, max {fmt_num(summary.maximum)}; "
        f"n={summary.contributors}, eff={summary.effective_n:.1f}</span>"
    )


def combined_summary_rows(matches: Sequence[LaneMatch], draw_ratio: float, stomp_ratio: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for seconds in SNAPSHOT_SECONDS:
        for slot in range(TIME_BUCKETS_PER_DAY):
            ally_values, ally_weights = samples_for_slot(matches, slot, seconds, "combined", "ally")
            enemy_values, enemy_weights = samples_for_slot(matches, slot, seconds, "combined", "enemy")
            ally_summary = summarise_distribution(ally_values, ally_weights)
            enemy_summary = summarise_distribution(enemy_values, enemy_weights)
            delta_summary = weighted_combined_delta_summary(matches, slot, seconds)
            outcomes = outcome_for_slot(matches, slot, seconds, draw_ratio, stomp_ratio)
            row: dict[str, Any] = {
                "game_minute": seconds // 60,
                "local_bucket_index": slot,
                "local_bucket": slot_label(slot),
                "ally_minus_enemy_median": delta_summary.median,
                **{f"outcome_{k}": v for k, v in outcomes.items()},
            }
            for prefix, summary in (("ally", ally_summary), ("enemy", enemy_summary), ("delta", delta_summary)):
                for key, value in asdict(summary).items():
                    row[f"{prefix}_{key}"] = value
            rows.append(row)
    return rows


def detailed_summary_rows(
    matches: Sequence[LaneMatch], chart: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for seconds in SNAPSHOT_SECONDS:
        for slot in range(TIME_BUCKETS_PER_DAY):
            row: dict[str, Any] = {
                "game_minute": seconds // 60,
                "local_bucket_index": slot,
                "local_bucket": slot_label(slot),
            }
            for series_key in chart["series_order"]:
                values, weights = samples_for_slot(matches, slot, seconds, "detailed", series_key)
                summary = summarise_distribution(values, weights)
                for key, value in asdict(summary).items():
                    row[f"{series_key}_{key}"] = value
            rows.append(row)
    return rows


def delta_summary_rows(matches: Sequence[LaneMatch]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for seconds in SNAPSHOT_SECONDS:
        for slot in range(TIME_BUCKETS_PER_DAY):
            values, weights = samples_for_slot(matches, slot, seconds, "delta", "delta")
            summary = summarise_distribution(values, weights)
            row: dict[str, Any] = {
                "game_minute": seconds // 60,
                "local_bucket_index": slot,
                "local_bucket": slot_label(slot),
            }
            for key, value in asdict(summary).items():
                row[f"delta_{key}"] = value
            rows.append(row)
    return rows


def html_table_combined(rows: Sequence[dict[str, Any]]) -> str:
    body: list[str] = []
    for row in rows:
        ally = DistributionSummary(int(row["ally_contributors"]), float(row["ally_effective_n"]), float(row["ally_weight_sum"]), row["ally_minimum"], row["ally_p10"], row["ally_p25"], row["ally_median"], row["ally_p75"], row["ally_p90"], row["ally_maximum"])
        enemy = DistributionSummary(int(row["enemy_contributors"]), float(row["enemy_effective_n"]), float(row["enemy_weight_sum"]), row["enemy_minimum"], row["enemy_p10"], row["enemy_p25"], row["enemy_median"], row["enemy_p75"], row["enemy_p90"], row["enemy_maximum"])
        outcomes = [("SW", row["outcome_stomp_win"]), ("W", row["outcome_win"]), ("D", row["outcome_draw"]), ("L", row["outcome_loss"]), ("SL", row["outcome_stomp_loss"])]
        outcome_text = " · ".join(f"{label} {float(v)*100:.0f}%" for label, v in outcomes)
        body.append("<tr>" + f"<td>{int(row['game_minute'])}m</td><td>{html.escape(str(row['local_bucket']))}</td><td>{fmt_dist(ally)}</td><td>{fmt_dist(enemy)}</td><td>{fmt_num(row['ally_minus_enemy_median'])}</td><td class='nowrap'>{outcome_text}</td>" + "</tr>")
    return "".join(body)


def html_table_detailed(
    rows: Sequence[dict[str, Any]], chart: Mapping[str, Any]
) -> str:
    body: list[str] = []
    for row in rows:
        summaries = {
            series_key: DistributionSummary(
                int(row[f"{series_key}_contributors"]),
                float(row[f"{series_key}_effective_n"]),
                float(row[f"{series_key}_weight_sum"]),
                row[f"{series_key}_minimum"], row[f"{series_key}_p10"],
                row[f"{series_key}_p25"], row[f"{series_key}_median"],
                row[f"{series_key}_p75"], row[f"{series_key}_p90"],
                row[f"{series_key}_maximum"],
            )
            for series_key in chart["series_order"]
        }
        body.append(
            "<tr>"
            + f"<td>{int(row['game_minute'])}m</td><td>{html.escape(str(row['local_bucket']))}</td>"
            + "".join(f"<td>{fmt_dist(summaries[key])}</td>" for key in chart["series_order"])
            + "</tr>"
        )
    return "".join(body)


def html_table_delta(rows: Sequence[dict[str, Any]]) -> str:
    body: list[str] = []
    for row in rows:
        summary = DistributionSummary(int(row["delta_contributors"]), float(row["delta_effective_n"]), float(row["delta_weight_sum"]), row["delta_minimum"], row["delta_p10"], row["delta_p25"], row["delta_median"], row["delta_p75"], row["delta_p90"], row["delta_maximum"])
        body.append("<tr>" + f"<td>{int(row['game_minute'])}m</td><td>{html.escape(str(row['local_bucket']))}</td><td>{fmt_dist(summary)}</td>" + "</tr>")
    return "".join(body)


def render_lane_html(matches: Sequence[LaneMatch], chart_svgs: Mapping[tuple[str, int], str], tables: Mapping[str, Sequence[dict[str, Any]]], settings: Mapping[str, Any], decay_ratio: float) -> str:
    selected = len(matches)
    half = (selected + 1) // 2
    newest_share = sum(m.recency_weight for m in matches[:half]) if matches else 0
    gold_label = "Net worth (gold)" if settings["gold_kind"] == "networth" else "Cash gold"
    charts = chart_definitions(settings)
    spec = comparison_spec(settings)
    hero_id_text = "all" if settings.get("hero_id") is None else str(settings["hero_id"])
    dark_mode = bool(settings.get("dark_mode", True))
    theme_css = (
        "--bg:#0b1220;--fg:#f8fafc;--muted:#cbd5e1;--border:#334155;"
        "--panel:#111827;--chart:#0b1220;--heading:#172033;--code:#1e293b"
        if dark_mode
        else
        "--bg:#fff;--fg:#111827;--muted:#6b7280;--border:#e5e7eb;"
        "--panel:#f9fafb;--chart:#fff;--heading:#fff;--code:#f3f4f6"
    )
    sections: list[str] = []
    for chart in charts:
        kind = str(chart["kind"])
        label = html.escape(str(chart["display_name"]))
        sections.append(f"<h2>{label}</h2>")
        sections.append(f"<h3>5:00 in-game</h3><div class='chart'>{chart_svgs[(kind, 300)]}</div>")
        sections.append(f"<h3>10:00 in-game</h3><div class='chart'>{chart_svgs[(kind, 600)]}</div>")
        if kind == "combined":
            sections.append("<h3>Combined weighted summary table</h3>")
            sections.append(f"<p>Cells show the median, 25th and 75th percentiles, 10th and 90th percentiles, min, max, contributor count, and effective weighted N. Outcome compares {html.escape(spec['ally_label'])} with {html.escape(spec['enemy_label'])}.</p>")
            sections.append(f"<div class='table-wrap'><table><thead><tr><th>Mark</th><th>Local bucket</th><th>{html.escape(spec['ally_label'])}</th><th>{html.escape(spec['enemy_label'])}</th><th>Allied minus enemy median</th><th>Outcome: SW/W/D/L/SL</th></tr></thead><tbody>{html_table_combined(tables['combined'])}</tbody></table></div>")
        elif kind == "detailed":
            sections.append("<h3>Detailed weighted summary table</h3>")
            headers = "".join(f"<th>{html.escape(str(chart['labels'][key]))}</th>" for key in chart["series_order"])
            sections.append(f"<div class='table-wrap'><table><thead><tr><th>Mark</th><th>Local bucket</th>{headers}</tr></thead><tbody>{html_table_detailed(tables['detailed'], chart)}</tbody></table></div>")
        elif kind == "delta":
            sections.append("<h3>Delta weighted summary table</h3>")
            sections.append(f"<div class='table-wrap'><table><thead><tr><th>Mark</th><th>Local bucket</th><th>{html.escape(spec['ally_label'])} minus {html.escape(spec['enemy_label'])}</th></tr></thead><tbody>{html_table_delta(tables['delta'])}</tbody></table></div>")
    return f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>STRATZ net worth by time of day</title>
<style>
:root{{{theme_css}}}
body{{font-family:system-ui,-apple-system,\"Segoe UI\",sans-serif;margin:0;background:var(--bg);color:var(--fg)}}
main{{max-width:1800px;margin:auto;padding:22px}} h1{{margin:0 0 8px}} h2{{margin-top:32px}} h3{{margin-top:18px}}
.meta{{display:flex;gap:18px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--border);padding:12px 14px;border-radius:8px}}
.chart{{overflow-x:auto;border:1px solid var(--border);border-radius:8px;background:var(--chart)}} .chart svg{{min-width:1180px;width:100%;height:auto;display:block}}
.table-wrap{{overflow:auto;max-height:75vh;border:1px solid var(--border);border-radius:8px}} table{{border-collapse:collapse;width:100%;font-size:12px}}
th,td{{padding:7px 8px;border-bottom:1px solid var(--border);vertical-align:top;text-align:left}} th{{position:sticky;top:0;background:var(--heading);z-index:2}}
.muted{{color:var(--muted);font-size:10px}} .nowrap{{white-space:nowrap}} code{{background:var(--code);padding:1px 4px;border-radius:3px}}
</style></head><body><main>
<h1>{html.escape(str(settings['hero_name']))} gold by local start time</h1>
<div class=\"meta\">
<div><strong>Selected:</strong> {selected} usable matches</div>
<div><strong>Hero:</strong> {html.escape(str(settings['hero_name']))} ({hero_id_text})</div>
<div><strong>Comparison:</strong> {html.escape(spec['ally_label'])} vs {html.escape(spec['enemy_label'])}</div>
<div><strong>Position filter:</strong> {'none' if spec['position'] is None else f"P{spec['position']}"}</div>
<div><strong>Metric:</strong> {html.escape(gold_label)}</div>
<div><strong>Timezone:</strong> {html.escape(str(settings['timezone']))}</div>
<div><strong>Local days:</strong> {html.escape(describe_days(settings.get('days', 'all')))}</div>
<div><strong>Views:</strong> {html.escape(str(settings.get('series_mode', 'combined')))}; delta {'on' if settings.get('include_delta') else 'off'}</div>
<div><strong>Theme:</strong> {'dark' if dark_mode else 'light'}</div>
<div><strong>Empty edge buckets:</strong> {'trimmed' if settings.get('trim_empty_edge_buckets', True) else 'kept'}</div>
<div><strong>Recency:</strong> geometric r={decay_ratio:.6f}; newest {half}/{selected} = {newest_share*100:.1f}% weight</div>
</div>
<p>Match starts use {html.escape(str(settings['timezone']))} local time. Each start is split linearly across half-hour buckets. For example, 13:45 belongs entirely to 13:30, while 13:00 is split equally between 12:30 and 13:00.</p>
<p>The violin shows weighted density. The high-contrast bar is the median. The coloured stems show the 25th to 75th and 10th to 90th percentiles. The thin capped line shows min and max. Percentiles use recency and time-of-day weights. {'Dotted violins are faded by 30% when effective N is below ' + str(settings.get('low_sample_effective_n', 3)) + '.' if settings.get('mark_low_samples', True) else ''}</p>
{''.join(sections)}
</main></body></html>"""

def maybe_write_png(svg_path: Path) -> Path:
    """Optional: use cairosvg if present; matplotlib cannot reliably import arbitrary SVG."""
    try:
        import cairosvg  # type: ignore
    except ImportError as exc:
        raise StratzError(
            "--png requested. PNG conversion requires cairosvg (`python -m pip install cairosvg`). "
            "The SVG/HTML outputs require no extra dependency."
        ) from exc
    png_path = svg_path.with_suffix(".png")
    cairosvg.svg2png(bytestring=svg_path.read_bytes(), write_to=str(png_path))
    return png_path


def build_output_dir(base_dir: Path, *, unique: bool) -> Path:
    if not unique:
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir
    base_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    candidate = base_dir / stamp
    suffix = 1
    while candidate.exists():
        candidate = base_dir / f"{stamp}_{suffix:02d}"
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def write_csv_rows(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_lane_outputs(matches: Sequence[LaneMatch], settings: Mapping[str, Any], decay_ratio: float) -> dict[str, Path]:
    out = build_output_dir(Path(str(settings["output_dir"])), unique=bool(settings.get("unique_output_dir", True)))
    gold_label = "Net worth (gold)" if settings["gold_kind"] == "networth" else "Cash gold"
    charts = chart_definitions(settings)
    chart_svgs: dict[tuple[str, int], str] = {}
    paths: dict[str, Path] = {"output_dir": out}
    trim_edges = bool(settings.get("trim_empty_edge_buckets", True))

    for chart in charts:
        kind = str(chart["kind"])
        for seconds in SNAPSHOT_SECONDS:
            minute = seconds // 60
            svg = svg_distribution_chart(matches, seconds=seconds, title=f"{gold_label} at {minute}:00 by match start time: {chart['display_name']}", gold_label=gold_label, chart=chart, settings=settings)
            chart_svgs[(kind, seconds)] = svg
            key = f"{kind}_{minute}m_svg"
            filename = f"{chart['filename_prefix']}_{minute}m.svg"
            paths[key] = out / filename
            paths[key].write_text(svg, encoding="utf-8")

    tables: dict[str, list[dict[str, Any]]] = {}
    if any(str(chart["kind"]) == "combined" for chart in charts):
        tables["combined"] = trim_empty_summary_edges(
            combined_summary_rows(matches, float(settings["draw_ratio"]), float(settings["stomp_ratio"])),
            enabled=trim_edges,
        )
        paths["combined_summary_csv"] = out / "lane_gold_combined_summary.csv"
        write_csv_rows(paths["combined_summary_csv"], tables["combined"])
    if any(str(chart["kind"]) == "detailed" for chart in charts):
        detailed_chart = next(chart for chart in charts if str(chart["kind"]) == "detailed")
        tables["detailed"] = trim_empty_summary_edges(
            detailed_summary_rows(matches, detailed_chart), enabled=trim_edges
        )
        paths["detailed_summary_csv"] = out / "lane_gold_detailed_summary.csv"
        write_csv_rows(paths["detailed_summary_csv"], tables["detailed"])
    if any(str(chart["kind"]) == "delta" for chart in charts):
        tables["delta"] = trim_empty_summary_edges(
            delta_summary_rows(matches), enabled=trim_edges
        )
        paths["delta_summary_csv"] = out / "lane_gold_delta_summary.csv"
        write_csv_rows(paths["delta_summary_csv"], tables["delta"])

    paths["html"] = out / "lane_gold_report.html"
    paths["html"].write_text(render_lane_html(matches, chart_svgs, tables, settings, decay_ratio), encoding="utf-8")

    raw_rows: list[dict[str, Any]] = []
    for m in matches:
        row: dict[str, Any] = {
            "match_id": m.match_id,
            "start_utc": m.start_utc,
            "start_local": m.start_local,
            "game_mode": m.game_mode,
            "lobby_type": m.lobby_type,
            "recency_weight": m.recency_weight,
            "slot_a": m.hour_a,
            "slot_a_label": slot_label(m.hour_a),
            "slot_a_weight": m.hour_a_weight,
            "slot_b": m.hour_b,
            "slot_b_label": slot_label(m.hour_b),
            "slot_b_weight": m.hour_b_weight,
        }
        for seconds, source in ((300, m.values_5m), (600, m.values_10m)):
            for series_key, value in source.items():
                row[f"{series_key}_{seconds//60}m"] = value
            combined = comparison_for_seconds(m, seconds)
            row[f"ally_{seconds//60}m"] = combined["ally"]
            row[f"enemy_{seconds//60}m"] = combined["enemy"]
            row[f"delta_{seconds//60}m"] = combined["delta"]
        for series_key in m.role_accounts:
            row[f"{series_key}_account"] = m.role_accounts.get(series_key)
            row[f"{series_key}_hero"] = m.role_heroes.get(series_key)
        raw_rows.append(row)
    paths["matches_csv"] = out / "lane_gold_matches.csv"
    write_csv_rows(paths["matches_csv"], raw_rows)

    paths["json"] = out / "lane_gold_data.json"
    paths["json"].write_text(json.dumps({"settings": safe_settings_for_output(settings), "decay_ratio": decay_ratio, "charts": [dict(chart) for chart in charts], "matches": [asdict(m) for m in matches], "tables": tables}, indent=2, ensure_ascii=False), encoding="utf-8")
    if settings.get("png"):
        for key, path in list(paths.items()):
            if key.endswith("_svg"):
                paths[key[:-4] + "png"] = maybe_write_png(path)
    return paths


def run_lane_gold(settings: Mapping[str, Any], client: StratzClient, plan: QueryPlan, cache: JsonCache) -> None:
    spec = comparison_spec(settings)
    hero_id = int(settings["hero_id"]) if settings.get("hero_id") is not None else None
    if not plan.detail_all_query:
        raise StratzError(
            "STRATZ requires a player filter for match details. The lane program "
            "will not guess opponent roles."
        )
    if (spec["mode"] == "lane" or spec["position"] is not None) and not plan.position_path:
        raise StratzError(
            "STRATZ explicit position data is unavailable. Team comparison still works with --position none."
        )
    if not plan.gold_event_plan:
        raise StratzError("STRATZ playback gold or net worth events not found")
    if settings["gold_kind"] == "networth" and not plan.gold_event_plan.networth_field and not plan.gold_event_plan.raw_json:
        raise StratzError("Gold event schema exposes no networth field; use --gold-kind cash")

    if ZoneInfo is None:
        raise StratzError("Python zoneinfo unavailable; Python 3.10+ required")
    try:
        tz = ZoneInfo(str(settings["timezone"]))
    except Exception as exc:
        raise StratzError(
            f"Could not load timezone {settings['timezone']!r}. On Windows try `python -m pip install tzdata`."
        ) from exc

    sample_by = str(settings["sample_by"])
    wanted = int(settings["match_count"])
    scan_limit = int(settings["scan_limit"])
    end = parse_datetime_arg(settings.get("end"), end=True) or datetime.now(timezone.utc)
    start = parse_datetime_arg(settings.get("start"))
    if start is None and sample_by == "months":
        start = subtract_months(end, int(settings["months"]))
    if start is not None and start > end:
        raise StratzError("--start must be before --end")
    accepted: list[LaneMatch] = []
    rejects: dict[str, int] = {}
    scanned = 0
    allowed_days = parse_days_spec(settings.get("days", "all"))

    for page in fetch_history_pages(
        client,
        plan,
        cache,
        player_id=int(settings["player_id"]),
        page_size=int(settings["page_size"]),
        history_ttl_minutes=float(settings["history_ttl_minutes"]),
        max_matches=scan_limit,
        start_timestamp=int(start.timestamp()) if start is not None else None,
        end_timestamp=int(end.timestamp()),
        verbose=bool(settings["verbose"]),
    ):
        candidates = [
            ref
            for ref in page
            if datetime.fromtimestamp(ref.start_timestamp, tz=tz).weekday() in allowed_days
            if prefilter_lane_overview(
                ref,
                plan,
                player_id=int(settings["player_id"]),
                hero_id=hero_id,
                ranked_only=bool(settings["ranked_only"]),
                player_position=spec["position"],
            )
        ]
        scanned += len(page)
        if not candidates:
            continue
        # Preserve strict recency order despite concurrent detail requests.
        detailed: dict[int, Mapping[str, Any] | None | Exception] = {}
        with ThreadPoolExecutor(max_workers=int(settings["workers"])) as executor:
            futures = {
                executor.submit(
                    fetch_match_detail,
                    client,
                    plan,
                    cache,
                    ref,
                    player_id=int(settings["player_id"]),
                    all_players=True,
                ): ref
                for ref in candidates
            }
            for future in as_completed(futures):
                ref = futures[future]
                try:
                    detailed[ref.match_id] = future.result()
                except Exception as exc:
                    detailed[ref.match_id] = exc

        for ref in sorted(candidates, key=lambda r: r.start_timestamp, reverse=True):
            raw = detailed.get(ref.match_id)
            if isinstance(raw, Exception):
                reason = f"retrieval error: {type(raw).__name__}"
                rejects[reason] = rejects.get(reason, 0) + 1
                continue
            lane_match, reason = parse_lane_match(
                raw if isinstance(raw, Mapping) else None,
                ref,
                plan,
                player_id=int(settings["player_id"]),
                hero_id=hero_id,
                ranked_only=bool(settings["ranked_only"]),
                comparison_mode=spec["mode"],
                player_position=spec["position"],
                tz=tz,
                gold_kind=str(settings["gold_kind"]),
                snapshot_method=str(settings["snapshot_method"]),
            )
            if lane_match:
                accepted.append(lane_match)
                if sample_by == "games" and len(accepted) >= wanted:
                    break
            else:
                key = reason or "unknown rejection"
                rejects[key] = rejects.get(key, 0) + 1
        if sample_by == "games" and len(accepted) >= wanted:
            break
        if scanned >= scan_limit:
            break

    accepted.sort(key=lambda m: m.start_timestamp, reverse=True)
    if sample_by == "games":
        accepted = accepted[:wanted]
    if not accepted:
        raise StratzError(
            "No usable lane matches found. Use --verbose to inspect schema and filter details."
        )
    decay_ratio = assign_lane_weights(accepted, float(settings["newest_half_share"]))
    paths = write_lane_outputs(accepted, settings, decay_ratio)

    half = (len(accepted) + 1) // 2
    actual_share = sum(m.recency_weight for m in accepted[:half])
    half_life = math.log(0.5) / math.log(decay_ratio) if 0 < decay_ratio < 1 else math.inf
    print("\nSTRATZ LANE NET WORTH ANALYSIS")
    print("=" * 78)
    if sample_by == "games":
        print(f"Selected usable matches: {len(accepted)} / requested {wanted}")
    else:
        print(f"Selected usable matches: {len(accepted)} across the requested {settings['months']}-month window")
    print(f"History matches scanned: {scanned}")
    print(f"Timezone: {settings['timezone']} (DST handled by zoneinfo)")
    print(f"Local days: {describe_days(settings.get('days', 'all'))}")
    print(f"Metric: {settings['gold_kind']} at 5:00 and 10:00 in-game")
    print(f"Comparison: {spec['ally_label']} vs {spec['enemy_label']}")
    position_text = "none" if spec["position"] is None else f"P{spec['position']}"
    print(f"Position filter: {position_text}")
    print(f"Views: {settings.get('series_mode')} ; delta {'on' if settings.get('include_delta') else 'off'}")
    print(
        f"Recency ratio: r={decay_ratio:.6f}; newest {half} = {actual_share*100:.1f}% "
        f"of weight; half-life ≈ {half_life:.1f} matches"
    )
    print(f"Time weighting: linear split across 48 half-hour {settings['timezone']} buckets centred 15 minutes after their labels")
    print(f"NEW OUTPUT DIRECTORY: {paths['output_dir'].resolve()}")
    print(f"OPEN THIS REPORT: {paths['html'].resolve()}")
    print("\nOutputs:")
    for name, path in paths.items():
        print(f"  {name:<12} {path}")
    if sample_by == "games" and len(accepted) < wanted:
        print(
            f"\nWARNING: only {len(accepted)} usable matches were found before --scan-limit={scan_limit}."
        )
    if settings.get("verbose") and rejects:
        print("\nRejected candidate reasons:")
        for reason, count in sorted(rejects.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {count:>5}  {reason}")

    if settings.get("open_report"):
        import webbrowser
        webbrowser.open(paths["html"].resolve().as_uri())

def add_lane_arguments(parser: argparse.ArgumentParser) -> None:
    add_common_arguments(parser)
    parser.add_argument("--comparison-mode", choices=("lane", "team"), default=None, help="Compare the selected player's lane matchup, or total allied vs enemy team net worth.")
    parser.add_argument("--position", "--role", dest="player_position", default=None, metavar="P1..P5_OR_NONE", help="Optional selected-player position filter. Lane mode requires P1..P5; team mode also accepts none.")
    ranked = parser.add_mutually_exclusive_group()
    ranked.add_argument("--ranked-only", dest="ranked_only", action="store_true", default=None)
    ranked.add_argument("--all-lobbies", dest="ranked_only", action="store_false", default=None, help="Disable ranked-only filter.")
    parser.add_argument("--gold-kind", choices=("networth", "cash"), default=None, help="Economic value to compare: networth includes held items; cash is unspent gold only. Default networth.")
    parser.add_argument("--snapshot-method", choices=("linear", "previous", "nearest"), default=None)
    parser.add_argument("--newest-half-share", type=float, default=None, help="Target recency weight belonging to newest half; default 0.70.")
    parser.add_argument("--draw-ratio", type=float, default=None, help="Allied/enemy ratio band for a draw; default +/-0.10.")
    parser.add_argument("--stomp-ratio", type=float, default=None, help="Allied/enemy ratio threshold for a stomp; default +/-0.30.")
    parser.add_argument("--series-mode", choices=("combined", "detailed", "both"), default=None, help="Report view: combined totals, detailed roles, or both. Default combined.")
    delta = parser.add_mutually_exclusive_group()
    delta.add_argument("--include-delta", dest="include_delta", action="store_true", default=None, help="Include allied-minus-enemy delta charts and tables. Default on.")
    delta.add_argument("--no-delta", dest="include_delta", action="store_false", default=None, help="Disable delta charts/tables.")
    low_samples = parser.add_mutually_exclusive_group()
    low_samples.add_argument("--mark-low-samples", dest="mark_low_samples", action="store_true", default=None, help="Use dotted violins when effective N is below the threshold (default).")
    low_samples.add_argument("--no-low-sample-marking", dest="mark_low_samples", action="store_false", default=None, help="Draw low-sample violins normally.")
    parser.add_argument("--low-sample-effective-n", type=float, default=None, help="Dotted-violin threshold (default 3.0, calibrated from a 150-match run).")
    empty_edges = parser.add_mutually_exclusive_group()
    empty_edges.add_argument("--trim-empty-edges", dest="trim_empty_edge_buckets", action="store_true", default=None, help="Remove empty time buckets from the start and end of charts and tables (default).")
    empty_edges.add_argument("--keep-empty-edges", dest="trim_empty_edge_buckets", action="store_false", default=None, help="Keep all 48 time buckets, including empty buckets at the edges.")
    parser.add_argument("-o", "--output-dir", default=None, help="Output root directory. By default a timestamped subfolder is created within it.")
    unique = parser.add_mutually_exclusive_group()
    unique.add_argument("--unique-output-dir", dest="unique_output_dir", action="store_true", default=None, help="Create a timestamped subfolder inside --output-dir. Default on.")
    unique.add_argument("--flat-output-dir", dest="unique_output_dir", action="store_false", default=None, help="Write directly into --output-dir and overwrite matching files.")
    parser.add_argument("--png", action="store_true", default=None, help="Also convert SVGs to PNG (optional cairosvg dependency).")
    parser.add_argument("--open-report", action="store_true", default=None, help="Open generated HTML report in default browser.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare lane matchups or whole teams at 5 and 10 minutes."
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the offline maths, role, team, and report checks, then exit.",
    )
    add_lane_arguments(parser)
    return parser


def configure_lane(
    settings: MutableMapping[str, Any],
    _catalog: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    settings["player_position"] = normalise_player_position(
        settings.get("player_position")
    )


def validate_lane(settings: Mapping[str, Any]) -> None:
    spec = comparison_spec(settings)
    if spec["mode"] == "team" and str(settings.get("series_mode")) != "combined":
        raise StratzError(
            "Team comparison supports --series-mode combined because position data may be missing"
        )
    share = float(settings["newest_half_share"])
    if not 0.5 < share < 1:
        raise StratzError("--newest-half-share must be between 0.5 and 1")
    draw = float(settings["draw_ratio"])
    stomp = float(settings["stomp_ratio"])
    if not 0 <= draw < stomp < 1:
        raise StratzError("Set 0 <= --draw-ratio < --stomp-ratio < 1")
    if float(settings["low_sample_effective_n"]) <= 0:
        raise StratzError("--low-sample-effective-n must be greater than 0")

def synthetic_lane_parse_fixture() -> tuple[Mapping[str, Any], Any, dict[tuple[bool, int], int]]:
    class Plan:
        pass

    plan = Plan()
    plan.position_path = ValuePath("position", ("position",))
    plan.hero_path = ValuePath("heroId", ("heroId",))
    plan.account_path = ValuePath("accountId", ("accountId",))
    plan.player_radiant_path = ValuePath("isRadiant", ("isRadiant",))
    plan.player_slot_path = None
    plan.lobby_type_path = ValuePath("lobbyType", ("lobbyType",))
    plan.game_mode_path = ValuePath("gameMode", ("gameMode",))
    plan.match_start_path = ValuePath("start", ("start",))
    plan.match_players_field = "players"
    plan.gold_event_plan = GoldEventPlan(
        selection="goldEvents { time networth }",
        response_path=("goldEvents",),
        time_field="time",
        gold_field=None,
        unreliable_gold_field=None,
        networth_field="networth",
    )
    accounts: dict[tuple[bool, int], int] = {}
    players: list[dict[str, Any]] = []
    for radiant in (True, False):
        for position in range(1, 6):
            account = (100 if radiant else 200) + position
            accounts[(radiant, position)] = account
            base = (1000 if radiant else 1100) + position * 100
            players.append(
                {
                    "accountId": account,
                    "heroId": 53 if radiant else 1,
                    "position": f"POSITION_{position}",
                    "isRadiant": radiant,
                    "goldEvents": [
                        {"time": 0, "networth": base},
                        {"time": 300, "networth": base + 500},
                        {"time": 600, "networth": base + 1200},
                    ],
                }
            )
    match = {
        "players": players,
        "lobbyType": "RANKED",
        "gameMode": "ALL_PICK",
        "start": 1_700_000_000,
    }
    return match, plan, accounts


def synthetic_matches() -> list[LaneMatch]:
    matches: list[LaneMatch] = []
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for i in range(150):
        hour = (i * 7) % 24
        minute = (i * 13) % 60
        dt = base - timedelta(days=i // 4, hours=(23 - hour), minutes=(59 - minute))
        local_decimal = hour + minute / 60
        noise = math.sin(i * 0.7) * 160
        values_5m = {
            "ally_p1": 2200 + noise + hour * 4,
            "ally_p5": 1400 - noise * .1,
            "enemy_p3": 2050 - noise * .2,
            "enemy_p4": 1450 + noise * .1,
        }
        values_10m = {
            "ally_p1": 4800 + noise * 1.7 + hour * 8,
            "ally_p5": 2800 - noise * .15,
            "enemy_p3": 4400 - noise * .3,
            "enemy_p4": 2950 + noise * .2,
        }
        comparison_5m = {
            "ally": values_5m["ally_p1"] + values_5m["ally_p5"],
            "enemy": values_5m["enemy_p3"] + values_5m["enemy_p4"],
        }
        comparison_5m["delta"] = comparison_5m["ally"] - comparison_5m["enemy"]
        comparison_10m = {
            "ally": values_10m["ally_p1"] + values_10m["ally_p5"],
            "enemy": values_10m["enemy_p3"] + values_10m["enemy_p4"],
        }
        comparison_10m["delta"] = comparison_10m["ally"] - comparison_10m["enemy"]
        m = LaneMatch(
            match_id=9_000_000_000 - i,
            start_timestamp=int(dt.timestamp()),
            start_utc=dt.isoformat(),
            start_local=dt.isoformat(),
            local_hour_decimal=local_decimal,
            game_mode="ALL_PICK_RANKED",
            lobby_type="RANKED",
            values_5m=values_5m,
            values_10m=values_10m,
            comparison_5m=comparison_5m,
            comparison_10m=comparison_10m,
            role_accounts={"ally_p1": DEFAULT_PLAYER_ID, "ally_p5": None, "enemy_p3": None, "enemy_p4": None},
            role_heroes={"ally_p1": DEFAULT_HERO_ID, "ally_p5": 1, "enemy_p3": 2, "enemy_p4": 3},
        )
        matches.append(m)
    return matches


def run_self_test() -> int:
    assert parse_days_spec("all") == frozenset(range(7))
    assert parse_days_spec("work") == frozenset(range(5))
    assert parse_days_spec("weekends") == frozenset({5, 6})
    assert parse_days_spec("MWF") == frozenset({0, 2, 4})
    assert parse_days_spec("Tu-Th") == frozenset({1, 2, 3})
    assert parse_days_spec("nes") == frozenset({2})
    try:
        parse_days_spec("T")
    except StratzError as exc:
        assert "ambiguous" in str(exc)
    else:
        raise AssertionError("T must be rejected as an ambiguous weekday")

    # Hour weighting exact examples + midnight wrap.
    assert time_bucket_split(13.75) == (27, 1.0, 28, 0.0)
    h = time_bucket_split(13.0)
    assert h[0] == 25 and h[2] == 26 and abs(h[1] - .5) < 1e-12 and abs(h[3] - .5) < 1e-12
    assert time_bucket_split(13.25) == (26, 1.0, 27, 0.0)
    h = time_bucket_split(0.0)
    assert h[0] == 47 and h[2] == 0 and abs(h[1] - .5) < 1e-12

    ratio = solve_decay_ratio(150, .70)
    assert abs(ratio - 0.9887662701091534) < 1e-10
    weights = [ratio**i for i in range(150)]
    assert abs(sum(weights[:75]) / sum(weights) - .70) < 1e-9

    assert weighted_quantile([0, 10], [1, 1], .5) == 5
    assert effective_n([1, 1, 1, 1]) == 4

    raw_match, parse_plan, accounts = synthetic_lane_parse_fixture()
    ref = MatchReference(123, 1_700_000_000)
    for position, expected_keys in (
        (1, {"ally_p1", "ally_p5", "enemy_p3", "enemy_p4"}),
        (5, {"ally_p1", "ally_p5", "enemy_p3", "enemy_p4"}),
        (3, {"ally_p3", "ally_p4", "enemy_p1", "enemy_p5"}),
        (4, {"ally_p3", "ally_p4", "enemy_p1", "enemy_p5"}),
        (2, {"ally_p2", "enemy_p2"}),
    ):
        parsed, reason = parse_lane_match(
            raw_match, ref, parse_plan,
            player_id=accounts[(True, position)], hero_id=53,
            ranked_only=True, comparison_mode="lane", player_position=position,
            tz=timezone.utc, gold_kind="networth", snapshot_method="linear",
        )
        assert reason is None and parsed is not None
        assert set(parsed.values_5m) == expected_keys
        assert parsed.comparison_5m["delta"] == parsed.comparison_5m["ally"] - parsed.comparison_5m["enemy"]

    any_hero_match, reason = parse_lane_match(
        raw_match, ref, parse_plan,
        player_id=accounts[(True, 1)], hero_id=None,
        ranked_only=True, comparison_mode="lane", player_position=1,
        tz=timezone.utc, gold_kind="networth", snapshot_method="linear",
    )
    assert reason is None and any_hero_match is not None

    parse_plan.position_path = None
    team_match, reason = parse_lane_match(
        raw_match, ref, parse_plan,
        player_id=accounts[(True, 1)], hero_id=53,
        ranked_only=True, comparison_mode="team", player_position=None,
        tz=timezone.utc, gold_kind="networth", snapshot_method="linear",
    )
    assert reason is None and team_match is not None
    assert len(team_match.values_5m) == 10
    assert team_match.comparison_5m["ally"] == sum(v for k, v in team_match.values_5m.items() if k.startswith("ally_"))

    matches = synthetic_matches()
    ratio2 = assign_lane_weights(matches, .70)
    assert abs(sum(m.recency_weight for m in matches) - 1) < 1e-12
    combined_rows = combined_summary_rows(matches, .10, .30)
    test_settings = dict(LANE_DEFAULTS)
    test_settings["series_mode"] = "both"
    detailed_chart = next(chart for chart in chart_definitions(test_settings) if chart["kind"] == "detailed")
    detailed_rows = detailed_summary_rows(matches, detailed_chart)
    delta_rows = delta_summary_rows(matches)
    assert len(combined_rows) == 96 and len(detailed_rows) == 96 and len(delta_rows) == 96
    svg5 = svg_distribution_chart(matches, seconds=300, title="test", gold_label="Net worth", chart=chart_definitions(test_settings)[0], settings=test_settings)
    assert "00" in svg5 and "23" in svg5 and "<svg" in svg5
    assert 'class="low-sample"' in svg5 and "effective N below 3" in svg5
    assert "#0b1220" in svg5 and "#60a5fa" in svg5 and "#fb7185" in svg5
    assert 'fill-opacity="0.70"' in svg5
    assert 'font-size="75"' in svg5 and 'font-size="42"' in svg5

    light_settings = dict(test_settings)
    light_settings["dark_mode"] = False
    light_chart = chart_definitions(light_settings)[0]
    light_svg = svg_distribution_chart(
        matches,
        seconds=300,
        title="test",
        gold_label="Net worth",
        chart=light_chart,
        settings=light_settings,
    )
    assert "#ffffff" in light_svg and "#2563eb" in light_svg and "#dc2626" in light_svg

    edge_rows = [
        {"game_minute": 5, "local_bucket_index": slot, "ally_contributors": 1 if slot in {4, 7} else 0}
        for slot in range(12)
    ]
    trimmed = trim_empty_summary_edges(edge_rows, enabled=True)
    assert [row["local_bucket_index"] for row in trimmed] == [4, 5, 6, 7]

    with tempfile.TemporaryDirectory(prefix="stratz-self-test-") as test_root:
        settings = dict(COMMON_DEFAULTS)
        settings.update(LANE_DEFAULTS)
        settings["player_name"] = DEFAULT_PLAYER_NAME
        settings["hero_name"] = DEFAULT_HERO_NAME
        settings["output_dir"] = test_root
        settings["unique_output_dir"] = False
        settings["series_mode"] = "combined"
        settings["include_delta"] = True
        paths = write_lane_outputs(matches, settings, ratio2)
        for key in ("combined_5m_svg", "combined_10m_svg", "delta_5m_svg", "delta_10m_svg", "html", "combined_summary_csv", "delta_summary_csv", "matches_csv", "json"):
            assert paths[key].exists() and paths[key].stat().st_size > 0
    print("Self-test passed:")
    print("  weekday aliases, ranges, groups, and ambiguous T rejection: OK")
    print("  time bucket split: 13:45=100% 13:30, 13:00=50/50 12:30+13:00, 13:15=100% 13:00, midnight wrap OK")
    print(f"  recency: r={ratio:.15f}; newest 75/150 = 70.000%")
    print("  weighted quantile/effective N: OK")
    print("  P1 to P5 lane logic and role-free team logic: OK")
    print("  synthetic 48-bucket combined/delta 5m/10m SVG and tables: OK")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    configure_console_text()
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_test()
    settings, client, plan, cache = prepare_program(
        args,
        mode="lane-gold",
        mode_defaults=LANE_DEFAULTS,
        configure=configure_lane,
        validate=validate_lane,
        value_parsers={"player_position": normalise_player_position},
        allow_any_hero=True,
    )
    run_lane_gold(settings, client, plan, cache)
    return 0


if __name__ == "__main__":
    cli_exit(main)
