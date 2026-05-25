#!/usr/bin/env python3
"""Add a full round (votes + squad changes + photos) in one shot.

Usage:
  COACH_PASSWORD=tigers2026 python3 scripts/add_round.py round_spec.json

round_spec.json schema:
{
  "round": 6,
  "date": "2026-05-24",
  "div1": {                              # optional
    "opponent": "Coorparoo Roos",
    "venue": "Giffin Park 1",
    "voters": [],                        # optional
    "photo_dir": "/tmp/r6_div1",         # optional - dir of jpegs to copy
    "played": ["Willo Bennett", "Hudson Vecht", ...],   # CRITICAL: from team sheet
    "late_outs": [],                                    # optional
    "dnp": [],                                          # optional
    "cards": [
      {"5": "Saxon Healey", "4": "Samuel Ella", "3": "Hudson Vecht",
       "2": "Archer Abbott", "1": "Skooda Walle"},
      ...3 cards total...
    ],
    "squad_updates": [
      {"name": "Hudson Vecht", "no": 23},
      {"name": "Suryan Raju",  "no": 34},
      {"name": "Jack Koschitzke", "no": null}
    ],
    "notes": "Optional free-text note appended to .md"
  },
  "div3": { ...same shape... },          # optional
  "auto_push":  true,                    # default true; set false to skip git
  "auto_refresh": true,                  # default true; trigger slow refresh
  "send_digest": true                    # default true; email summary to Kris
}

Either div may be omitted (e.g. if one division had a bye).
"""

from __future__ import annotations

import json
import os
import shutil
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAIN = ROOT / "attendance-plain.json"
VOTES_DIR = ROOT / "votes"


def slugify(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-").replace("--", "-")


def tally(cards: list[dict]) -> list[tuple[str, int]]:
    t: dict[str, int] = {}
    for card in cards:
        for points, player in card.items():
            if player:
                t[player] = t.get(player, 0) + int(points)
    return sorted(t.items(), key=lambda x: -x[1])


def bog(cards: list[dict]) -> tuple[str, int, bool] | None:
    if not cards:
        return None
    t = tally(cards)
    if not t:
        return None
    name, pts = t[0]
    unanimous = all(c.get("5") == name for c in cards) and len(cards) >= 3
    return name, pts, unanimous


def render_md(round_num: int, date: str, div_label: str, div: dict, photos_dir: Path | None) -> str:
    opp = div["opponent"]
    venue = div.get("venue", "")
    cards = div["cards"]
    voters = div.get("voters", [])

    parts = [
        f"# {div_label} R{round_num} vs {opp} — Best on Ground Votes",
        "",
        f"**Date:** {date}",
        f"**Grade:** Tweed Coast Tigers U15 Boys {div_label}",
    ]
    if venue:
        parts.append(f"**Venue:** {venue}")
    parts.append(f"**Voting:** 5-4-3-2-1 from each of {len(cards)} voter{'s' if len(cards)!=1 else ''}")
    if voters:
        parts.append(f"**Voters:** {', '.join(voters)}")
    parts.append("")

    if photos_dir and photos_dir.exists():
        photo_files = sorted(p.name for p in photos_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".heic"))
        if photo_files:
            parts.append(f"Photos: see `{photos_dir.name}/` ({', '.join(photo_files)})")
            parts.append("")

    for i, card in enumerate(cards, 1):
        parts.append(f"## Card {i}")
        for points in ("5", "4", "3", "2", "1"):
            parts.append(f"- {points}: {card.get(points, '_(blank)_')}")
        parts.append("")

    parts.append("## Final tally")
    parts.append("")
    parts.append("| Rank | Player | Votes |")
    parts.append("|---|---|---|")
    last_pts = None
    rank = 0
    for i, (name, pts) in enumerate(tally(cards), 1):
        if pts != last_pts:
            rank = i
        parts.append(f"| {rank} | {name} | {pts} |")
        last_pts = pts
    parts.append("")

    b = bog(cards)
    if b:
        name, pts, unan = b
        parts.append(f"**Best on Ground:** {name} ({pts}{' — unanimous' if unan else ''})")

    if div.get("notes"):
        parts.append("")
        parts.append(div["notes"])

    return "\n".join(parts) + "\n"


def update_squad(data: dict, div_key: str, updates: list[dict]) -> list[str]:
    """Apply squad number changes. Returns list of human-readable change descriptions."""
    sq = data["squads"][div_key]["players"]
    notes = []
    for u in updates:
        name = u["name"]
        new_no = u["no"]
        # Find player
        existing = [p for p in sq if p["name"] == name]
        if not existing:
            sq.append({"no": new_no, "name": name})
            notes.append(f"  + Added {name} #{new_no}")
            continue
        p = existing[0]
        old = p["no"]
        if old == new_no:
            continue
        # If new_no collides with another player, that other player gets bumped to None
        for other in sq:
            if other is not p and other["no"] == new_no and new_no is not None:
                other["no"] = None
                notes.append(f"  ~ {other['name']} #{new_no} → no number (bumped by {name})")
        p["no"] = new_no
        notes.append(f"  ~ {name} #{old} → #{new_no}")
    return notes


def upsert_round(data: dict, round_num: int, div1_cards: list[dict], div1_voters: list,
                 div3_cards: list[dict], div3_voters: list) -> None:
    rounds = data["votes"]["rounds"]
    entry = {
        "round": round_num,
        "div1_voters": div1_voters,
        "div1_cards": div1_cards,
        "div3_voters": div3_voters,
        "div3_cards": div3_cards,
    }
    existing = [r for r in rounds if r.get("round") == round_num]
    if existing:
        idx = rounds.index(existing[0])
        # Preserve existing cards for a division if this run didn't supply any
        if not div1_cards and existing[0].get("div1_cards"):
            entry["div1_cards"] = existing[0]["div1_cards"]
            entry["div1_voters"] = existing[0].get("div1_voters", [])
        if not div3_cards and existing[0].get("div3_cards"):
            entry["div3_cards"] = existing[0]["div3_cards"]
            entry["div3_voters"] = existing[0].get("div3_voters", [])
        rounds[idx] = entry
    else:
        rounds.append(entry)
    rounds.sort(key=lambda r: r["round"])


def upsert_played(data: dict, round_num: int, spec: dict) -> list[str]:
    """Update rounds[].divN.played/late_outs/dnp. Returns list of warnings.

    CRITICAL: the squad page's per-player game counts come from these arrays,
    NOT from players.json. Forgetting to fill them is why Grayson showed 4
    games instead of 5 on 2026-05-25.
    """
    rounds = data.setdefault("rounds", [])
    entry = next((r for r in rounds if r.get("round") == round_num), None)
    if entry is None:
        entry = {"round": round_num,
                 "div1": {"played": [], "late_outs": [], "dnp": []},
                 "div3": {"played": [], "late_outs": [], "dnp": []}}
        rounds.append(entry)
        rounds.sort(key=lambda r: r["round"])

    warnings = []
    for div_key in ("div1", "div3"):
        div = spec.get(div_key)
        if not div:
            continue
        played = div.get("played", [])
        if not played:
            warnings.append(f"  [WARN] {div_key} has no 'played' list — squad counts will not update for this round")
            continue
        entry.setdefault(div_key, {"played": [], "late_outs": [], "dnp": []})
        entry[div_key]["played"] = played
        entry[div_key]["late_outs"] = div.get("late_outs", [])
        entry[div_key]["dnp"] = div.get("dnp", [])

        # Cross-check spelling against squad
        sq_names = {p["name"] for p in data["squads"][div_key]["players"]}
        # Also accept cross-div players (they appear in the OTHER squad)
        other = "div3" if div_key == "div1" else "div1"
        other_names = {p["name"] for p in data["squads"][other]["players"]}
        for n in played:
            if n not in sq_names and n not in other_names:
                warnings.append(f"  [WARN] {div_key} played: '{n}' not found in either squad — typo?")
    return warnings


def copy_photos(src_dir: Path | None, dest_dir: Path) -> int:
    if not src_dir or not src_dir.exists():
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in src_dir.iterdir():
        if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".heic"):
            shutil.copy2(f, dest_dir / f.name)
            n += 1
    return n


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kw)


def send_digest(spec: dict, change_lines: list[str]) -> None:
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    if not (smtp_user and smtp_pass):
        print("  (skipping digest email — SMTP_USER/SMTP_PASS not set)")
        return

    round_num = spec["round"]
    date = spec["date"]
    parts = [f"<h2>TCT R{round_num} — {date}</h2>"]
    for div_key, div_label in (("div1", "Div 1"), ("div3", "Div 3")):
        div = spec.get(div_key)
        if not div:
            continue
        cards = div["cards"]
        b = bog(cards)
        parts.append(f"<h3>{div_label} vs {div['opponent']}</h3>")
        if b:
            name, pts, unan = b
            parts.append(f"<p><strong>BOG:</strong> {name} ({pts}{' — unanimous' if unan else ''})</p>")
        parts.append("<table border='0' cellpadding='4'><tr><th align='left'>Rank</th><th align='left'>Player</th><th align='right'>Votes</th></tr>")
        for i, (name, pts) in enumerate(tally(cards), 1):
            parts.append(f"<tr><td>{i}</td><td>{name}</td><td align='right'>{pts}</td></tr>")
        parts.append("</table>")
    if change_lines:
        parts.append("<h3>Squad / process notes</h3><pre>")
        parts.extend(change_lines)
        parts.append("</pre>")
    parts.append("<p><a href='https://kriswatts1981.github.io/footy-draws/'>Open footy-draws</a></p>")

    msg = EmailMessage()
    msg["Subject"] = f"TCT R{round_num} weekly digest — {date}"
    msg["From"] = smtp_user
    msg["To"] = "kriswatts@gmail.com"
    msg.set_content("HTML view required.")
    msg.add_alternative("\n".join(parts), subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(smtp_user, smtp_pass)
        s.send_message(msg)
    print("  digest emailed to kriswatts@gmail.com")


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    spec_path = Path(sys.argv[1])
    spec = json.loads(spec_path.read_text())

    if not PLAIN.exists():
        print(f"ERROR: {PLAIN} missing", file=sys.stderr)
        sys.exit(1)

    data = json.loads(PLAIN.read_text())
    round_num = spec["round"]
    date = spec["date"]

    change_lines = []

    # Squad updates
    for div_key in ("div1", "div3"):
        div = spec.get(div_key)
        if div and div.get("squad_updates"):
            change_lines.append(f"[{div_key}] squad changes:")
            change_lines.extend(update_squad(data, div_key, div["squad_updates"]))

    # Votes
    upsert_round(
        data,
        round_num,
        spec.get("div1", {}).get("cards", []),
        spec.get("div1", {}).get("voters", []),
        spec.get("div3", {}).get("cards", []),
        spec.get("div3", {}).get("voters", []),
    )

    # Played lists (drives the squad page game counts — see playerStats() in index.html)
    warnings = upsert_played(data, round_num, spec)
    if warnings:
        change_lines.append("Played-list warnings:")
        change_lines.extend(warnings)
        for w in warnings:
            print(w)

    PLAIN.write_text(json.dumps(data, indent=2))
    print(f"Updated {PLAIN}")

    # Photos + .md per division
    written_md = []
    for div_key, div_label in (("div1", "Div 1"), ("div3", "Div 3")):
        div = spec.get(div_key)
        if not div:
            continue
        slug = slugify(div["opponent"])
        prefix = "" if div_key == "div3" else "div1-"
        folder_name = f"{prefix}r{round_num}-{slug}-{date}"
        folder = VOTES_DIR / folder_name

        photo_dir = Path(div["photo_dir"]) if div.get("photo_dir") else None
        n_photos = copy_photos(photo_dir, folder)
        if n_photos:
            change_lines.append(f"  [{div_key}] copied {n_photos} photos → {folder.relative_to(ROOT)}")

        md_path = VOTES_DIR / f"{folder_name}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_md(round_num, date, div_label, div, folder if folder.exists() else None))
        written_md.append(md_path)
        print(f"Wrote {md_path}")

    # Encrypt
    print("Encrypting attendance.json...")
    run(["python3", str(ROOT / "scripts" / "encrypt_attendance.py")], cwd=ROOT)

    if spec.get("auto_push", True):
        # Commit + push
        run(["git", "-C", str(ROOT), "add", "attendance.json"])
        msg = f"data: R{round_num} votes ({date})"
        b1 = bog(spec.get("div1", {}).get("cards", []))
        b3 = bog(spec.get("div3", {}).get("cards", []))
        bog_bits = []
        if b1: bog_bits.append(f"Div 1 BOG {b1[0]} ({b1[1]})")
        if b3: bog_bits.append(f"Div 3 BOG {b3[0]} ({b3[1]})")
        if bog_bits:
            msg += " — " + ", ".join(bog_bits)
        try:
            run(["git", "-C", str(ROOT), "commit", "-m", msg])
            run(["git", "-C", str(ROOT), "pull", "--rebase", "--autostash"])
            run(["git", "-C", str(ROOT), "push"])
        except subprocess.CalledProcessError as e:
            print(f"  (git step skipped or no-op: {e})")

    if spec.get("auto_refresh", True):
        print("Triggering slow PlayHQ refresh...")
        try:
            run(["gh", "workflow", "run", "Refresh PlayHQ data", "--ref", "main"], cwd=ROOT)
        except Exception as e:
            print(f"  (gh workflow run failed: {e})")

    if spec.get("send_digest", True):
        send_digest(spec, change_lines)

    print("\nDone.")


if __name__ == "__main__":
    main()
