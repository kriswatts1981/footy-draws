#!/usr/bin/env python3
"""Encrypt the sensitive parts of attendance-plain.json into attendance.json.

The plain file (attendance-plain.json) is gitignored. Public fields stay in
plaintext in attendance.json so the page works without a password; sensitive
fields (RPL list, votes, late-outs, DNP) are AES-GCM encrypted with a key
derived from COACH_PASSWORD via PBKDF2.

Usage:
  COACH_PASSWORD=tigers2026 python3 scripts/encrypt_attendance.py

The browser uses Web Crypto API to decrypt with the same parameters.
"""

import base64
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

ROOT = Path(__file__).resolve().parent.parent
PLAIN = ROOT / "attendance-plain.json"
OUT = ROOT / "attendance.json"

PBKDF2_ITERATIONS = 100_000
KEY_LEN = 32  # AES-256
SALT_LEN = 16
NONCE_LEN = 12

PASSWORD = os.environ.get("COACH_PASSWORD")
if not PASSWORD:
    print("ERROR: COACH_PASSWORD env var not set", file=sys.stderr)
    sys.exit(1)

if not PLAIN.exists():
    print(f"ERROR: {PLAIN} not found. Keep your editable copy here (it's gitignored).", file=sys.stderr)
    sys.exit(1)

plain = json.loads(PLAIN.read_text())

# Pull out sensitive fields. Anything else stays public.
sensitive = {
    "restricted_player_list": plain.pop("restricted_player_list", None),
    "votes": plain.pop("votes", None),
    # In each round, late_outs/dnp are coach-only; played list is public-OK.
    "round_extras": [
        {
            "round": r["round"],
            "div1": {"late_outs": r["div1"].get("late_outs", []), "dnp": r["div1"].get("dnp", [])},
            "div3": {"late_outs": r["div3"].get("late_outs", []), "dnp": r["div3"].get("dnp", [])},
        }
        for r in plain.get("rounds", [])
    ],
}

# Strip late_outs/dnp from public rounds.
for r in plain.get("rounds", []):
    for div in ("div1", "div3"):
        if div in r:
            r[div].pop("late_outs", None)
            r[div].pop("dnp", None)

# Derive key from password.
salt = os.urandom(SALT_LEN)
kdf = PBKDF2HMAC(
    algorithm=hashes.SHA256(),
    length=KEY_LEN,
    salt=salt,
    iterations=PBKDF2_ITERATIONS,
)
key = kdf.derive(PASSWORD.encode("utf-8"))

# Encrypt the sensitive blob.
nonce = os.urandom(NONCE_LEN)
plaintext = json.dumps(sensitive).encode("utf-8")
aes = AESGCM(key)
ciphertext = aes.encrypt(nonce, plaintext, associated_data=None)

plain["encrypted"] = {
    "v": 1,
    "alg": "AES-GCM",
    "kdf": "PBKDF2-HMAC-SHA256",
    "iter": PBKDF2_ITERATIONS,
    "salt": base64.b64encode(salt).decode("ascii"),
    "nonce": base64.b64encode(nonce).decode("ascii"),
    "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
}

OUT.write_text(json.dumps(plain, indent=2))
print(f"Wrote {OUT}")
print(f"  public fields kept: {[k for k in plain.keys() if k != 'encrypted']}")
print(f"  encrypted fields:   {list(sensitive.keys())}")
print(f"  ciphertext: {len(ciphertext)} bytes")
