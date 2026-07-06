#!/usr/bin/env python3
"""Flag squad members who are missing from the away-form Player Name dropdown.

Every time a player is added to the site squad AFTER the Google Form was built,
the form's Player Name dropdown goes stale and that player can't be marked away
(this is exactly what happened to Tosh Condon — added to the squad 28 Jun,
never added to the form, so no parent could select him).

The site can't do this check itself: the published form's `viewform` page has no
`access-control-allow-origin` header, so a browser fetch from GitHub Pages is
blocked by CORS (the responses CSV works only because Google sets `*` on it).
So it runs server-side, here, from a scheduled GitHub Action.

Detection mirrors the site's own name-matching (`canonicalPlayerName` in
index.html): a squad player counts as present if the form has an exact
(normalised) match OR a fuzzy one — same first name and a last name within one
edit, or a prefix. That keeps spelling variants from firing false alarms (e.g.
squad "Zhyal Blackburne" vs form "Zhyal Blackburn" is a match, exactly as the
live site treats a parent's submission) while still catching a truly absent
player (e.g. "Kade Brennan" when the form only lists "Kade O'Shea").

Writes a summary to $GITHUB_OUTPUT when run in Actions so the workflow can email.
Exit code is always 0 — a missing player is a data gap to report, not a failure.
"""

import html
import json
import os
import re
import sys
import urllib.request

FORM_URL = os.environ.get(
    "AWAY_FORM_URL",
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSenUqRm5mvqYXs4kja5fCLWtiO1x_tY9VHJY7o_nJJf_N7jnA/viewform",
)
ATTENDANCE_FILE = os.environ.get("ATTENDANCE_FILE", "attendance-plain.json")


def squad_names(path):
    with open(path) as f:
        data = json.load(f)
    squads = data.get("squads", {}) or {}
    names = []
    for dk in ("div1", "div3"):
        for p in (squads.get(dk, {}) or {}).get("players", []) or []:
            n = (p.get("name") or "").strip()
            if n:
                names.append(n)
    # de-dupe, keep order
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def fetch_form(url):
    req = urllib.request.Request(url, headers={"User-Agent": "footy-draws-form-check"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def form_options(page):
    """Pull dropdown/checkbox option strings out of the Google Form payload.

    Options are embedded as `["<label>",null,null,null,false]` inside the
    FB_PUBLIC_LOAD_DATA_ blob (HTML-escaped in the served page). Round-label
    options ("R5 - Fri 15 ...") come through too, but they never fuzzy-match a
    player name so they're harmless.
    """
    decoded = html.unescape(page)
    opts = re.findall(r'\["([^"]+)",null,null,null,(?:false|true)\]', decoded)
    return [o for o in opts if o.strip()]


def norm_name(s):
    # mirrors normName() in index.html
    s = re.sub(r"[.'’`]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def edit_distance(a, b):
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, n + 1):
            cur = dp[j]
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + (a[i - 1] != b[j - 1]))
            prev = cur
    return dp[n]


def matches(squad_name, options_norm):
    """True if the form has this player, using the site's matching rules."""
    target = norm_name(squad_name)
    if target in options_norm:
        return True
    tp = target.split(" ")
    t_first, t_last = tp[0], " ".join(tp[1:])
    if not t_last:
        return False
    for opt in options_norm:
        sp = opt.split(" ")
        s_first, s_last = sp[0], " ".join(sp[1:])
        if s_first != t_first or not s_last:
            continue
        if edit_distance(t_last, s_last) <= 1 or s_last.startswith(t_last) or t_last.startswith(s_last):
            return True
    return False


def main():
    names = squad_names(ATTENDANCE_FILE)
    if not names:
        print("No squad names found in", ATTENDANCE_FILE, "- nothing to check.")
        return 0

    options_norm = [norm_name(o) for o in form_options(fetch_form(FORM_URL))]

    missing = [n for n in names if not matches(n, options_norm)]

    print(f"Checked {len(names)} squad players against the away form.")
    if missing:
        print(f"MISSING from form dropdown ({len(missing)}):")
        for n in missing:
            print(f"  - {n}")
    else:
        print("All squad players are present in the form. Nothing to do.")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"missing_found={'true' if missing else 'false'}\n")
            f.write("summary<<EOF\n")
            f.write("\n".join(missing) if missing else "(none)")
            f.write("\nEOF\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
