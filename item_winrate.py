#!/usr/bin/env python3
"""Check how buying an item affected a player's results."""

import argparse
import csv
import json
import math
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence
from zoneinfo import ZoneInfo

from stratz_common import (
    DEFAULT_ITEM_ID,
    JsonCache,
    MatchReference,
    PurchasePlan,
    QueryPlan,
    StratzClient,
    StratzError,
    add_common_arguments,
    apply_catalog_setting,
    cli_exit,
    configure_console_text,
    decode_jsonish,
    determine_win,
    describe_days,
    enum_text,
    fetch_history_pages,
    fetch_match_detail,
    get_path,
    is_ranked_match,
    normalize_int,
    parse_datetime_arg,
    parse_days_spec,
    parse_mode_matches,
    parse_timestamp,
    party_filter_matches,
    percentile_unweighted,
    player_hero_id,
    prepare_program,
    safe_settings_for_output,
    select_player,
    subtract_months,
    tutorial_notice,
)

ITEM_DEFAULTS = {
    "item_id": DEFAULT_ITEM_ID,
    "game_mode": "ranked",
    "party": "solo",
    "summary_only": False,
    "csv": None,
    "json": None,
}


@dataclass
class ItemMatchRow:
    match_id: int
    start_time_utc: str
    hero_id: int | None
    game_mode: str
    party_value: str
    result: str
    item_status: str
    first_purchase_seconds: float | None
    first_purchase_time: str
    duration_seconds: float | None
    duration: str
    stratz_url: str
    error: str = ""


def extract_purchase_events(
    player: Mapping[str, Any], purchase_plan: PurchasePlan
) -> tuple[list[Mapping[str, Any]] | None, str | None, str | None]:
    raw = get_path(player, purchase_plan.response_path)
    raw = decode_jsonish(raw)
    if raw is None:
        return None, purchase_plan.item_field, purchase_plan.time_field
    if isinstance(raw, Mapping):
        # JSON-scalar fallback: recursively locate a plausible event list.
        stack: list[Any] = [raw]
        while stack:
            current = stack.pop()
            if isinstance(current, list) and all(isinstance(v, Mapping) for v in current):
                if current:
                    keys = {str(k).lower() for k in current[0].keys()}
                    if any("item" in k for k in keys) and any("time" in k for k in keys):
                        item_key = next(k for k in current[0] if "item" in str(k).lower())
                        time_key = next(k for k in current[0] if "time" in str(k).lower())
                        return current, str(item_key), str(time_key)
            if isinstance(current, Mapping):
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
        return None, None, None
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, Mapping)], purchase_plan.item_field, purchase_plan.time_field
    return None, purchase_plan.item_field, purchase_plan.time_field


def normalise_event_time(value: Any, duration_seconds: float | None) -> float | None:
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(raw):
        return None
    if duration_seconds and raw > duration_seconds * 5 and raw / 1000 <= duration_seconds * 1.5:
        raw /= 1000
    return raw


def format_duration(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return ""
    sign = "-" if value < 0 else ""
    seconds = abs(int(round(value)))
    return f"{sign}{seconds // 60}:{seconds % 60:02d}"


def parse_item_match(
    match: Mapping[str, Any] | None,
    reference: MatchReference,
    plan: QueryPlan,
    *,
    player_id: int,
    hero_id: int,
    item_id: int,
    game_mode: str,
    party_filter: str,
) -> ItemMatchRow | None:
    if match is None:
        return ItemMatchRow(
            reference.match_id,
            datetime.fromtimestamp(reference.start_timestamp, tz=timezone.utc).isoformat(),
            None,
            "",
            "",
            "UNKNOWN",
            "UNKNOWN",
            None,
            "",
            None,
            "",
            f"https://stratz.com/matches/{reference.match_id}",
            "STRATZ returned no match object",
        )
    player = select_player(match.get(plan.match_players_field), player_id=player_id, plan=plan)
    if not player:
        return ItemMatchRow(
            reference.match_id,
            datetime.fromtimestamp(reference.start_timestamp, tz=timezone.utc).isoformat(),
            None,
            "",
            "",
            "UNKNOWN",
            "UNKNOWN",
            None,
            "",
            None,
            "",
            f"https://stratz.com/matches/{reference.match_id}",
            "Selected player not present in response",
        )
    actual_hero = player_hero_id(player, plan)
    if actual_hero != hero_id:
        return None

    raw_mode = get_path(match, plan.game_mode_path.response_path) if plan.game_mode_path else None
    if game_mode.lower() == "ranked":
        if is_ranked_match(match, plan) is not True:
            return None
    elif game_mode.lower() != "any":
        if not plan.game_mode_path:
            raise StratzError("STRATZ exposes no recognised game mode; use --game-mode any")
        if not parse_mode_matches(raw_mode, game_mode):
            return None

    party_value = get_path(player, plan.party_path.response_path) if plan.party_path else None
    if not party_filter_matches(party_value, plan.party_semantics, party_filter):
        return None

    duration_raw = get_path(match, plan.duration_path.response_path) if plan.duration_path else None
    try:
        duration_seconds = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration_seconds = None

    won = determine_win(match, player, plan)
    result = "WIN" if won is True else "LOSS" if won is False else "UNKNOWN"

    if not plan.purchase_plan:
        raise StratzError(
            "STRATZ did not provide item-purchase events in a form this program knows. "
            "Run with --verbose and update the engine rather than treating them as absent."
        )
    events, item_field, time_field = extract_purchase_events(player, plan.purchase_plan)
    purchase_times: list[float] = []
    if events is not None and item_field and time_field:
        for event in events:
            if normalize_int(event.get(item_field)) != item_id:
                continue
            when = normalise_event_time(event.get(time_field), duration_seconds)
            if when is not None:
                purchase_times.append(when)

    if events is None:
        item_status = "UNKNOWN"
        purchase_seconds = None
    elif purchase_times:
        item_status = "WITH_ITEM"
        purchase_seconds = min(purchase_times)
    else:
        item_status = "WITHOUT_ITEM"
        purchase_seconds = None

    start_ts = parse_timestamp(get_path(match, plan.match_start_path.response_path)) or reference.start_timestamp
    return ItemMatchRow(
        match_id=reference.match_id,
        start_time_utc=datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
        hero_id=actual_hero,
        game_mode=enum_text(raw_mode),
        party_value=enum_text(party_value),
        result=result,
        item_status=item_status,
        first_purchase_seconds=purchase_seconds,
        first_purchase_time=format_duration(purchase_seconds),
        duration_seconds=duration_seconds,
        duration=format_duration(duration_seconds),
        stratz_url=f"https://stratz.com/matches/{reference.match_id}",
    )



def rate(n: int, d: int) -> str:
    return "n/a" if not d else f"{n / d * 100:.1f}%"


def print_item_summary(rows: Sequence[ItemMatchRow], settings: Mapping[str, Any], start: datetime, end: datetime) -> None:
    relevant = [row for row in rows if not row.error]
    errors = [row for row in rows if row.error]
    with_item = [row for row in relevant if row.item_status == "WITH_ITEM"]
    without_item = [row for row in relevant if row.item_status == "WITHOUT_ITEM"]
    unknown = [row for row in relevant if row.item_status == "UNKNOWN"]

    def outcome(group: Sequence[ItemMatchRow]) -> tuple[int, int]:
        return sum(r.result == "WIN" for r in group), sum(r.result == "LOSS" for r in group)

    print("\nSTRATZ ITEM WIN-RATE ANALYSIS")
    print("=" * 76)
    print(f"Player:      {settings['player_name']} ({settings['player_id']})")
    print(f"Hero:        {settings['hero_name']} ({settings['hero_id']})")
    print(f"Item:        {settings['item_name']} ({settings['item_id']})")
    print(f"Game mode:   {settings['game_mode']}")
    print(f"Party:       {settings['party']}")
    print(f"Period:      {start.isoformat()}  to  {end.isoformat()}")
    print(f"Local days:  {describe_days(settings.get('days', 'all'))}")
    print(f"Matches after filters: {len(relevant)}; retrieval errors: {len(errors)}")
    print("\nOUTCOMES")
    print("-" * 76)
    print(f"{'Group':<24} {'Wins':>7} {'Losses':>7} {'Total':>7} {'Win rate':>10}")
    for label, group in (
        ("With item", with_item),
        ("Without item", without_item),
        ("Unknown purchase data", unknown),
    ):
        wins, losses = outcome(group)
        print(f"{label:<24} {wins:>7} {losses:>7} {len(group):>7} {rate(wins, wins + losses):>10}")
    known = with_item + without_item
    wins, losses = outcome(known)
    print(f"{'Known total':<24} {wins:>7} {losses:>7} {len(known):>7} {rate(wins, wins + losses):>10}")
    if known:
        print(f"\nItem build rate: {len(with_item)}/{len(known)} ({rate(len(with_item), len(known))})")
    wi, li = outcome(with_item)
    wo, lo = outcome(without_item)
    if wi + li and wo + lo:
        diff = wi / (wi + li) - wo / (wo + lo)
        print(f"Win-rate difference with item: {diff * 100:+.1f} percentage points")

    purchase_times = [r.first_purchase_seconds for r in with_item if r.first_purchase_seconds is not None]
    win_times = [r.first_purchase_seconds for r in with_item if r.result == "WIN" and r.first_purchase_seconds is not None]
    loss_times = [r.first_purchase_seconds for r in with_item if r.result == "LOSS" and r.first_purchase_seconds is not None]
    print("\nPURCHASE TIMING")
    print("-" * 76)
    if purchase_times:
        values = [float(v) for v in purchase_times]
        timing = [
            ("Average", statistics.fmean(values)),
            ("Median", statistics.median(values)),
            ("25th percentile", percentile_unweighted(values, .25)),
            ("75th percentile", percentile_unweighted(values, .75)),
            ("Earliest", min(values)),
            ("Latest", max(values)),
            ("Average in wins", statistics.fmean(win_times) if win_times else None),
            ("Average in losses", statistics.fmean(loss_times) if loss_times else None),
        ]
        for label, value in timing:
            print(f"{label:<24} {format_duration(value):>10}")
    else:
        print("No confirmed purchase timings.")

    if errors:
        print("\nERRORS")
        for row in errors:
            print(f"  {row.match_id}: {row.error}")


def write_dataclass_csv(path: Path, rows: Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(asdict(rows[0]).keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def run_item_winrate(settings: Mapping[str, Any], client: StratzClient, plan: QueryPlan, cache: JsonCache) -> None:
    end = parse_datetime_arg(settings.get("end"), end=True) or datetime.now(timezone.utc)
    start = parse_datetime_arg(settings.get("start"))
    sample_by = str(settings["sample_by"])
    if start is None and sample_by == "months":
        start = subtract_months(end, int(settings["months"]))
    if start is not None and start > end:
        raise StratzError("--start must be before --end")
    target_games = int(settings["match_count"])
    scan_limit = int(settings["scan_limit"])
    allowed_days = parse_days_spec(settings.get("days", "all"))
    try:
        local_tz = ZoneInfo(str(settings["timezone"]))
    except Exception as exc:
        raise StratzError(
            f"Could not load timezone {settings['timezone']!r}. On Windows try `python -m pip install tzdata`."
        ) from exc
    scanned = 0
    earliest_seen = int(end.timestamp())
    rows: list[ItemMatchRow] = []

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
        scanned += len(page)
        filtered_page = [
            ref for ref in page
            if datetime.fromtimestamp(ref.start_timestamp, tz=local_tz).weekday() in allowed_days
        ]
        if filtered_page:
            earliest_seen = min(earliest_seen, min(ref.start_timestamp for ref in filtered_page))
        detailed: dict[int, Mapping[str, Any] | None | Exception] = {}
        with ThreadPoolExecutor(max_workers=int(settings["workers"])) as executor:
            futures = {
                executor.submit(
                    fetch_match_detail, client, plan, cache, ref,
                    player_id=int(settings["player_id"]), all_players=False,
                ): ref
                for ref in filtered_page
            }
            for future in as_completed(futures):
                ref = futures[future]
                try:
                    detailed[ref.match_id] = future.result()
                except Exception as exc:
                    detailed[ref.match_id] = exc

        for ref in sorted(filtered_page, key=lambda row: row.start_timestamp, reverse=True):
            raw = detailed.get(ref.match_id)
            if isinstance(raw, Exception):
                rows.append(
                    ItemMatchRow(
                        ref.match_id,
                        datetime.fromtimestamp(ref.start_timestamp, tz=timezone.utc).isoformat(),
                        None, "", "", "UNKNOWN", "UNKNOWN", None, "", None, "",
                        f"https://stratz.com/matches/{ref.match_id}", str(raw),
                    )
                )
                continue
            row = parse_item_match(
                raw if isinstance(raw, Mapping) else None,
                ref,
                plan,
                player_id=int(settings["player_id"]),
                hero_id=int(settings["hero_id"]),
                item_id=int(settings["item_id"]),
                game_mode=str(settings["game_mode"]),
                party_filter=str(settings["party"]),
            )
            if row is not None:
                rows.append(row)
            accepted_count = sum(not candidate.error for candidate in rows)
            if sample_by == "games" and accepted_count >= target_games:
                break
        if sample_by == "games" and sum(not candidate.error for candidate in rows) >= target_games:
            break
        if scanned >= scan_limit:
            break

    rows.sort(key=lambda r: r.start_time_utc, reverse=True)
    if sample_by == "games":
        kept: list[ItemMatchRow] = []
        accepted = 0
        for row in rows:
            kept.append(row)
            if not row.error:
                accepted += 1
            if accepted >= target_games:
                break
        rows = kept
    report_start = start or datetime.fromtimestamp(earliest_seen, tz=timezone.utc)
    print_item_summary(rows, settings, report_start, end)
    print(f"History matches scanned: {scanned}; sampling: {sample_by}")
    accepted_total = sum(not row.error for row in rows)
    if sample_by == "games" and accepted_total < target_games:
        tutorial_notice(
            f"Only {accepted_total} qualifying games were found",
            [
                f"The target was {target_games}; history scanning stopped at {scan_limit} matches.",
                "Try a different hero/filter, raise scan_limit, or use --sample-by months.",
            ],
            enabled=bool(settings.get("tutorials", True)),
            level="WARNING",
            stream=sys.stdout,
        )

    if not settings.get("summary_only"):
        print("\nMATCHES")
        print("-" * 118)
        print(f"{'Match ID':<12} {'UTC date':<20} {'Result':<8} {'Item':<13} {'Bought':<8} {'Mode':<18}")
        for row in rows:
            print(
                f"{row.match_id:<12} {row.start_time_utc[:19]:<20} {row.result:<8} "
                f"{row.item_status:<13} {row.first_purchase_time:<8} {row.game_mode[:18]:<18}"
            )

    if settings.get("csv"):
        write_dataclass_csv(Path(str(settings["csv"])), rows)
    if settings.get("json"):
        path = Path(str(settings["json"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"settings": safe_settings_for_output(settings), "matches": [asdict(r) for r in rows]},
                indent=2,
            ),
            encoding="utf-8",
        )

def add_item_arguments(parser: argparse.ArgumentParser) -> None:
    add_common_arguments(parser)
    parser.add_argument("--item-id", default=None, metavar="ID_OR_NAME", help="Item ID, name, or case/spacing-insensitive alias (for example: 158, Mjollnir, mjolnir).")
    parser.add_argument("--game-mode", default=None, help="Name/enum/numeric ID, or 'any'. Default ranked.")
    parser.add_argument("--party", choices=("solo", "party", "any"), default=None)
    parser.add_argument("--summary-only", action="store_true", default=None)
    parser.add_argument("--csv", default=None, help="Optional per-match CSV output path.")
    parser.add_argument("--json", default=None, help="Optional JSON output path.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare wins and losses when a player bought a chosen item. "
            "The starting example uses Yatoro, Nature's Prophet, Mjollnir, and ranked games."
        )
    )
    add_item_arguments(parser)
    return parser


def configure_item(
    settings: MutableMapping[str, Any],
    catalog: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    settings["item_name"] = f"Item {settings['item_id']}"
    apply_catalog_setting(
        settings,
        id_key="item_id",
        name_key="item_name",
        kind="items",
        catalog=catalog,
    )


def main(argv: Sequence[str] | None = None) -> int:
    configure_console_text()
    args = build_parser().parse_args(argv)
    settings, client, plan, cache = prepare_program(
        args,
        mode="item-winrate",
        mode_defaults=ITEM_DEFAULTS,
        configure=configure_item,
    )
    run_item_winrate(settings, client, plan, cache)
    return 0


if __name__ == "__main__":
    cli_exit(main)
