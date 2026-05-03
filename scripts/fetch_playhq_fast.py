#!/usr/bin/env python3
"""Fast refresh of fixture.json + ladder.json only (no player career walk).

Runs in ~30s vs ~40min for the full fetch_playhq.py. Used by the every-15-min
weekend cron so the ladder reflects results within minutes of PlayHQ ingesting them.
The full script (fetch_playhq.py) still runs daily to refresh players.json.

Reuses helpers from fetch_playhq.py — same env vars (PLAYHQ_API_KEY, PLAYHQ_ORG_ID).
"""

import json
from datetime import datetime
from pathlib import Path

import fetch_playhq as full

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main():
    seasons = full.list_seasons()
    current = next(
        (s for s in seasons if s["name"].isdigit() and int(s["name"]) == datetime.now().year),
        None,
    )
    if not current:
        raise SystemExit(f"No season found for {datetime.now().year}")

    tct_teams_raw = full.list_tct_teams(current["id"])
    # Reshape to match what build_fixture / build_ladder expect
    tct_teams = [
        {
            "id": t["id"],
            "name": t["name"],
            "grade_id": (t.get("grade") or {}).get("id"),
            "grade_name": (t.get("grade") or {}).get("name"),
        }
        for t in tct_teams_raw
    ]
    current_data = {"tct_teams": tct_teams}

    fixture = full.build_fixture(current_data)
    ladder = full.build_ladder(current_data)

    (DATA / "fixture.json").write_text(json.dumps(fixture, indent=2))
    (DATA / "ladder.json").write_text(json.dumps(ladder, indent=2))

    div1_games = len(fixture["divisions"].get("div1", {}).get("games", []))
    div3_games = len(fixture["divisions"].get("div3", {}).get("games", []))
    teams_in_ladder = sum(len(d["standings"]) for d in ladder["divisions"].values())
    print(f"fast: fixture div1={div1_games} div3={div3_games}, ladder={teams_in_ladder} teams")


if __name__ == "__main__":
    main()
