#!/usr/bin/env python3
"""Pull fixture / ladder / per-player career appearances from PlayHQ for Tweed Coast Tigers.

Walks EVERY SEQJ grade (not just TCT-involving) so career counts include games at
other clubs within SEQJ. Aggregates by PlayHQ player ID, which is stable across
games and seasons.

Outputs:
  data/fixture.json   — current season fixture for both Div 1 and Div 3
  data/ladder.json    — current season ladder for both Div 1 and Div 3
  data/players.json   — career tallies for every player who ever appeared for a TCT team
  data/cache/season-{year}.json — per-season cached aggregates (historical seasons skip refetch)

Env:
  PLAYHQ_API_KEY      — required
  PLAYHQ_ORG_ID       — defaults to TCT JAFC
  PLAYHQ_REFETCH_ALL  — if "1", ignores cache and re-fetches all seasons
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
CACHE.mkdir(parents=True, exist_ok=True)

API_KEY = os.environ.get("PLAYHQ_API_KEY", "")
ORG_ID = os.environ.get("PLAYHQ_ORG_ID", "ee46372a-3637-4685-97fa-38c43e8a9d78")
TENANT = "afl"
BASE = "https://api.playhq.com/v1"

if not API_KEY:
    print("ERROR: PLAYHQ_API_KEY not set", file=sys.stderr)
    sys.exit(1)

CURRENT_YEAR = datetime.now().year
SEASONS_TO_FETCH = list(range(2022, CURRENT_YEAR + 1))
REFETCH_ALL = os.environ.get("PLAYHQ_REFETCH_ALL") == "1"
CACHE_SCHEMA_VERSION = 3  # bump when per-season cache shape changes
SUMMARY_WORKERS = int(os.environ.get("PLAYHQ_WORKERS", "8"))

HEADERS = {"x-api-key": API_KEY, "x-phq-tenant": TENANT}


def api_get(path, retries=4):
    url = path if path.startswith("http") else f"{BASE}{path}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(2 ** attempt, 30)
                time.sleep(wait)
                last_err = e
                continue
            if e.code == 404:
                return None
            if e.code >= 500 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                last_err = e
                continue
            raise
        except Exception as e:
            last_err = e
            time.sleep(1 + attempt)
    raise last_err


def paginate(path):
    cursor = ""
    while True:
        sep = "&" if "?" in path else "?"
        url = path + (f"{sep}cursor={cursor}" if cursor else "")
        d = api_get(url)
        if not d:
            return
        for item in d.get("data", []):
            yield item
        meta = d.get("metadata", {})
        if not meta.get("hasMore"):
            return
        cursor = meta.get("nextCursor", "")
        if not cursor:
            return


def list_seasons():
    return list(paginate(f"/organisations/{ORG_ID}/seasons?pageSize=50"))


def list_tct_teams(season_id):
    return list(paginate(
        f"/organisations/{ORG_ID}/seasons/{season_id}/teams?pageSize=100"
    ))


def list_all_grades(season_id):
    """All grades in this SEQJ season (not just TCT-involving)."""
    return list(paginate(f"/seasons/{season_id}/grades?pageSize=100"))


def grade_games(grade_id):
    return list(paginate(f"/grades/{grade_id}/games?pageSize=100"))


def grade_ladder(grade_id):
    d = api_get(f"/grades/{grade_id}/ladder")
    if not d or not d.get("data"):
        return []
    ladders = d["data"][0].get("ladders", [])
    if not ladders:
        return []
    return ladders[0].get("standings", [])


def game_summary(game_id):
    d = api_get(f"/games/{game_id}/summary")
    return d.get("data") if d else None


def normalise_name(first, last):
    return f"{(first or '').strip().title()} {(last or '').strip().title()}".strip()


def fetch_season_summary(season):
    """Walk every grade in the season, pull /summary for every FINAL game,
    aggregate appearances by player ID across all teams."""
    print(f"\n=== Season {season['name']} ({season['id']}) ===")

    # TCT teams (so we can flag which games are TCT vs other-club)
    tct_teams = list_tct_teams(season["id"])
    tct_team_ids = {t["id"] for t in tct_teams}
    tct_team_names = {t["id"]: t["name"] for t in tct_teams}
    print(f"  TCT teams in season: {len(tct_teams)}")

    # All grades in SEQJ
    all_grades = list_all_grades(season["id"])
    print(f"  All SEQJ grades: {len(all_grades)}")

    # Per-player tally (keyed by PlayHQ player ID)
    # { pid: {first, last, games: [{game_id, date, team_name, team_id, is_tct, round, fill_in, captain, number}], goals, behinds, bog, by_team: counter, tct_games, season_year } }
    players = {}

    # Game records (for fixture/ladder building later — we keep TCT-involving only)
    tct_games = []

    grade_count = 0
    final_games_seen = 0
    summary_calls = 0
    start = time.time()

    for grade in all_grades:
        grade_count += 1
        gid = grade["id"]
        gname = grade.get("name", "")
        try:
            games = grade_games(gid)
        except Exception as e:
            print(f"  ! grade {gname[:40]} fetch err: {e}")
            continue
        finals = [g for g in games if g.get("status") == "FINAL"]
        if not finals:
            continue

        # Lookup competitor teamID -> team_name (PlayHQ doesn't expose team list endpoint
        # we can use, so we build it from the games themselves)
        team_name_map = {}
        for g in games:
            for c in g.get("competitors", []):
                team_name_map[c["id"]] = c["name"]

        # Track if this grade has any TCT team
        grade_has_tct = any(c["id"] in tct_team_ids for g in games for c in g.get("competitors", []))

        if grade_has_tct:
            # Capture full game records for TCT games (used for fixture/results)
            for g in games:
                if not any(c["id"] in tct_team_ids for c in g.get("competitors", [])):
                    continue
                tct_comp = next(c for c in g["competitors"] if c["id"] in tct_team_ids)
                opp_comp = next((c for c in g["competitors"] if c["id"] != tct_comp["id"]), None)
                sched = g.get("schedule") or {}
                venue = g.get("venue") or {}
                addr = venue.get("address") or {}
                tct_games.append({
                    "id": g["id"],
                    "status": g.get("status"),
                    "round": (g.get("round") or {}).get("name"),
                    "date": sched.get("date"),
                    "time": sched.get("time"),
                    "venue": venue.get("name"),
                    "surface": venue.get("surfaceName"),
                    "suburb": addr.get("suburb"),
                    "tct_team_id": tct_comp["id"],
                    "tct_team_name": tct_comp["name"],
                    "grade_id": gid,
                    "grade_name": gname,
                    "tct_is_home": tct_comp.get("isHomeTeam"),
                    "home": next((c["name"] for c in g["competitors"] if c.get("isHomeTeam")), None),
                    "away": next((c["name"] for c in g["competitors"] if not c.get("isHomeTeam")), None),
                    "tct_score": tct_comp.get("scoreTotal"),
                    "opp_score": opp_comp.get("scoreTotal") if opp_comp else None,
                    "tct_outcome": tct_comp.get("outcome"),
                    "url": g.get("url"),
                })

        # Concurrent /summary calls — these are independent and the bottleneck.
        summaries = {}
        if finals:
            with ThreadPoolExecutor(max_workers=SUMMARY_WORKERS) as ex:
                futs = {ex.submit(game_summary, g["id"]): g for g in finals}
                for fut in as_completed(futs):
                    g = futs[fut]
                    try:
                        s = fut.result()
                        if s:
                            summaries[g["id"]] = s
                    except Exception as e:
                        print(f"    summary err {g['id'][:8]}: {e}")
            summary_calls += len(summaries)
            final_games_seen += len(finals)

        for g in finals:
            summary = summaries.get(g["id"])
            if not summary:
                continue
            for app in summary.get("appearances", []):
                if app.get("roleType") != "Player":
                    continue
                pid = app.get("id")
                if not pid:
                    continue
                team_id = app.get("teamID")
                team_name = team_name_map.get(team_id, "Unknown")
                if pid not in players:
                    players[pid] = {
                        "id": pid,
                        "first": app.get("firstName"),
                        "last": app.get("lastName"),
                        "name": normalise_name(app.get("firstName"), app.get("lastName")),
                        "games": [],
                        "goals": 0,
                        "behinds": 0,
                        "bog": 0,
                        "by_team": defaultdict(int),
                        "tct_games": 0,
                        "ever_tct": False,
                    }
                p = players[pid]
                is_tct = team_id in tct_team_ids
                if is_tct:
                    p["tct_games"] += 1
                    p["ever_tct"] = True
                p["games"].append({
                    "game_id": g["id"],
                    "date": (g.get("schedule") or {}).get("date"),
                    "round": (g.get("round") or {}).get("name"),
                    "team_id": team_id,
                    "team_name": team_name,
                    "is_tct": is_tct,
                    "is_fill_in": app.get("isFillIn", False),
                    "captain": app.get("captainRole"),
                    "number": app.get("playerNumber"),
                })
                p["by_team"][team_name] += 1
                for s in app.get("scoreSubTotal", []) or []:
                    t = s.get("type"); v = s.get("value", 0)
                    if t == "TOTAL_GOALS":
                        p["goals"] += v
                    elif t == "TOTAL_BEHINDS":
                        p["behinds"] += v
                if app.get("bestPlayer") in (1, "1", "BEST_ON_GROUND"):
                    p["bog"] += 1

        # Progress every 20 grades
        if grade_count % 20 == 0:
            elapsed = time.time() - start
            print(f"  ... {grade_count}/{len(all_grades)} grades, {summary_calls} summaries, {elapsed:.0f}s elapsed")

    elapsed = time.time() - start
    print(f"  done: {grade_count} grades, {final_games_seen} final games, {summary_calls} summary calls, {elapsed:.0f}s")
    print(f"  unique players seen: {len(players)}, ever-TCT: {sum(1 for p in players.values() if p['ever_tct'])}")

    # Drop per-game arrays from the cache to keep file size small (~10x smaller).
    # We only need season-level aggregates to compute career totals.
    out_players = {}
    for pid, p in players.items():
        out_players[pid] = {
            "n": p["name"],
            "g": len(p["games"]),
            "t": p["tct_games"],
            "T": dict(p["by_team"]),  # by_team
            "G": p["goals"],
            "B": p["behinds"],
            "K": p["bog"],
        }

    return {
        "schema": CACHE_SCHEMA_VERSION,
        "year": int(season["name"]) if season["name"].isdigit() else None,
        "season_id": season["id"],
        "season_name": season["name"],
        "tct_teams": [{"id": t["id"], "name": t["name"], "grade_id": (t.get("grade") or {}).get("id"), "grade_name": (t.get("grade") or {}).get("name")} for t in tct_teams],
        "tct_team_ids": list(tct_team_ids),
        "tct_games": tct_games,
        "players": out_players,
    }


def cache_path(year):
    return CACHE / f"season-{year}.json"


def load_or_fetch_season(season):
    name = season["name"]
    if not name.isdigit():
        return None
    year = int(name)
    if year not in SEASONS_TO_FETCH:
        return None
    cp = cache_path(year)
    is_current = (year == CURRENT_YEAR)
    if cp.exists() and not is_current and not REFETCH_ALL:
        try:
            cached = json.loads(cp.read_text())
            if cached.get("schema") == CACHE_SCHEMA_VERSION:
                print(f"\n=== Season {year} (cached) ===")
                return cached
            print(f"\n=== Season {year} (schema change, refetching) ===")
        except Exception as e:
            print(f"  cache read err for {year}: {e}")
    data = fetch_season_summary(season)
    cp.write_text(json.dumps(data))  # no indent — keep cache small
    print(f"  wrote {cp} ({cp.stat().st_size//1024} KB)")
    return data


def build_fixture(current_data):
    """Pull both Div 1 and Div 3 U15 Boys fixtures from current season."""
    div1 = next((t for t in current_data["tct_teams"] if t["grade_name"] == "U15 Boys Division 1"), None)
    div3 = next((t for t in current_data["tct_teams"] if "U15 Boys Division 3" in (t["grade_name"] or "")), None)
    out = {"updated": datetime.now(timezone.utc).isoformat(), "divisions": {}}
    for label, team in (("div1", div1), ("div3", div3)):
        if not team:
            continue
        try:
            grade_games_list = grade_games(team["grade_id"])
        except Exception as e:
            print(f"fixture err {label}: {e}")
            continue
        out["divisions"][label] = {
            "grade_id": team["grade_id"],
            "grade_name": team["grade_name"],
            "tct_team_id": team["id"],
            "tct_team_name": team["name"],
            "games": [
                {
                    "id": g["id"],
                    "status": g.get("status"),
                    "round": (g.get("round") or {}).get("name"),
                    "round_num": _round_num((g.get("round") or {}).get("name")),
                    "is_final_round": (g.get("round") or {}).get("isFinalRound", False),
                    "date": (g.get("schedule") or {}).get("date"),
                    "time": (g.get("schedule") or {}).get("time"),
                    "venue": (g.get("venue") or {}).get("name"),
                    "surface": (g.get("venue") or {}).get("surfaceName"),
                    "address": ((g.get("venue") or {}).get("address") or {}).get("line1"),
                    "suburb": ((g.get("venue") or {}).get("address") or {}).get("suburb"),
                    "competitors": [
                        {
                            "id": c["id"],
                            "name": c["name"],
                            "is_home": c.get("isHomeTeam"),
                            "score": c.get("scoreTotal"),
                            "outcome": c.get("outcome"),
                            "is_tct": c["id"] == team["id"],
                        }
                        for c in g.get("competitors", [])
                    ],
                    "url": g.get("url"),
                }
                for g in grade_games_list
            ],
        }
    return out


def _round_num(name):
    if not name:
        return None
    s = name.replace("Round ", "").strip()
    try:
        return int(s)
    except ValueError:
        return None


def build_ladder(current_data):
    div1 = next((t for t in current_data["tct_teams"] if t["grade_name"] == "U15 Boys Division 1"), None)
    div3 = next((t for t in current_data["tct_teams"] if "U15 Boys Division 3" in (t["grade_name"] or "")), None)
    out = {"updated": datetime.now(timezone.utc).isoformat(), "divisions": {}}
    for label, team in (("div1", div1), ("div3", div3)):
        if not team:
            continue
        standings = grade_ladder(team["grade_id"])
        out["divisions"][label] = {
            "grade_id": team["grade_id"],
            "grade_name": team["grade_name"],
            "tct_team_id": team["id"],
            "standings": [
                {
                    "rank": s.get("ranking", 0) + 1,
                    "team_id": s.get("team", {}).get("id"),
                    "team_name": s.get("team", {}).get("name"),
                    "is_tct": s.get("team", {}).get("id") == team["id"],
                    "played": s.get("played"),
                    "won": s.get("won"),
                    "lost": s.get("lost"),
                    "drawn": s.get("drawn"),
                    "byes": s.get("byes"),
                    "points_for": s.get("pointsFor"),
                    "points_against": s.get("pointsAgainst"),
                    "percentage": s.get("percentage"),
                    "competition_points": s.get("competitionPoints"),
                }
                for s in standings
            ],
        }
    return out


def build_players(all_seasons):
    """Aggregate per-player career across all cached seasons. Output only players
    who appeared for any TCT team at least once (anywhere in the cache window)."""
    # First pass: identify all player IDs who ever played for TCT (across any season).
    tct_pids = set()
    for s in all_seasons:
        if not s:
            continue
        tct_team_ids = set(s.get("tct_team_ids", []))
        for pid, p in s.get("players", {}).items():
            if p.get("t", 0) > 0:
                tct_pids.add(pid)

    print(f"\n  Players who ever played for TCT: {len(tct_pids)}")

    out = {"updated": datetime.now(timezone.utc).isoformat(), "players": {}}
    by_name = defaultdict(list)

    for pid in tct_pids:
        career = {
            "id": pid,
            "name": "",
            "games_total": 0,
            "tct_games": 0,
            "other_games": 0,
            "goals": 0,
            "behinds": 0,
            "bog": 0,
            "by_year": {},
            "by_team": defaultdict(int),
            "first_year": None,
            "last_year": None,
        }
        for s in all_seasons:
            if not s:
                continue
            year = s.get("year")
            p = s.get("players", {}).get(pid)
            if not p:
                continue
            career["name"] = p.get("n") or career["name"]
            career["games_total"] += p.get("g", 0)
            career["tct_games"] += p.get("t", 0)
            career["other_games"] += (p.get("g", 0) - p.get("t", 0))
            career["goals"] += p.get("G", 0)
            career["behinds"] += p.get("B", 0)
            career["bog"] += p.get("K", 0)
            career["by_year"][str(year)] = p.get("g", 0)
            for t, n in (p.get("T") or {}).items():
                career["by_team"][t] += n
            if career["first_year"] is None or (year and year < career["first_year"]):
                career["first_year"] = year
            if career["last_year"] is None or (year and year > career["last_year"]):
                career["last_year"] = year
        career["by_team"] = dict(career["by_team"])
        out["players"][pid] = career
        if career["name"]:
            by_name[career["name"].lower()].append(pid)

    out["by_name"] = {n: ids for n, ids in by_name.items()}
    return out


def main():
    seasons = list_seasons()
    print(f"Found {len(seasons)} seasons in org")
    seasons.sort(key=lambda s: s["name"], reverse=True)

    all_season_data = []
    current_data = None
    for s in seasons:
        if not s["name"].isdigit():
            continue
        year = int(s["name"])
        if year not in SEASONS_TO_FETCH:
            continue
        d = load_or_fetch_season(s)
        if d:
            all_season_data.append(d)
            if year == CURRENT_YEAR:
                current_data = d

    if not current_data:
        print("WARN: no current-season data")
        sys.exit(2)

    print("\n=== Building outputs ===")
    fixture = build_fixture(current_data)
    ladder = build_ladder(current_data)
    players = build_players(all_season_data)

    (DATA / "fixture.json").write_text(json.dumps(fixture, indent=2))
    (DATA / "ladder.json").write_text(json.dumps(ladder, indent=2))
    (DATA / "players.json").write_text(json.dumps(players))  # no indent — large file

    div1_games = len(fixture["divisions"].get("div1", {}).get("games", []))
    div3_games = len(fixture["divisions"].get("div3", {}).get("games", []))
    print(f"  fixture: div1={div1_games} games, div3={div3_games} games")
    print(f"  ladder:  {sum(len(d['standings']) for d in ladder['divisions'].values())} teams")
    print(f"  players: {len(players['players'])} TCT-touched players")
    print("done.")


if __name__ == "__main__":
    main()
