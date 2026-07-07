#!/usr/bin/env python3
"""
Convert a picks CSV into the draft-viewer JSON format.

CSV headers expected:  set,number,card_name,player,pack,pick
  set      abbreviated set code (e.g. SOR, TWI, ASH)
  number   collector number, not zero-padded
  card_name  ignored (viewer keys on the card id, not the name)
  player   the player's seat number in the pod (1..N)
  pack     which booster: 1 = first pack, etc.  -> becomes "round"
  pick     which pick within that pack: 1 = first pick, etc.

Usage:
  python3 csv_to_draft.py picks.csv            # writes draft-data.json
  python3 csv_to_draft.py picks.csv out.json

Everything the CSV cannot contain is set in CONFIG below. The only *essential*
one is PASS_DIRECTIONS — see the note there.
"""

import csv, json, sys
from collections import defaultdict, Counter

# ============================ CONFIG — EDIT ME ============================

# ESSENTIAL. Pass direction for each pack/round. The CSV has no way to record
# which pack a card came from once packs start moving, so direction is what
# lets us compute it.
#
# Convention (matches the viewer): "left" means a pack moves to the NEXT-HIGHER
# seat number each pass, so after the first pass seat S is drafting the pack
# that seat S+1 opened (wrapping N -> 1). "right" moves the other way.
# The script prints a one-line check after the first pass — if it says seat 1
# received the wrong neighbour's pack, flip that pack's value.
PASS_DIRECTIONS = {1: "left", 2: "right", 3: "left"}

# Optional. Which player number is the protagonist (default focus + star).
PROTAGONIST_PLAYER = 1

# Optional. Seat -> display name. Missing seats show as "Seat N".
PLAYER_NAMES = {
    # 1: "Alice", 2: "Bo", ...
}

# Optional. Set code + date shown in the header. DRAFT_SET=None auto-detects
# the most common set in the file (fine for a single-set draft).
DRAFT_SET = None
DRAFT_DATE = ""

# Optional. Leaders are a separate draft phase and usually aren't in this CSV.
# Add them here if you want them shown as badges: seat -> list of card ids.
LEADERS = {
    # 1: ["ASH-001"], 2: ["ASH-002"], ...
}

# =========================================================================


def card_id(set_code, number):
    return f"{set_code.strip().upper()}-{int(number):03d}"


def opener_seat(seat, pick, seats, rank, direction):
    """Which seat opened the pack this seat holds at this pick."""
    step = 1 if direction == "left" else -1
    j = (rank[seat] + step * (pick - 1)) % len(seats)
    return seats[j]


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "picks.csv"
    out = sys.argv[2] if len(sys.argv) > 2 else "draft-data.json"

    # ---- read + auto-detect delimiter (Swedish/EU Excel often uses ';') ----
    with open(src, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delim = ","
        reader = csv.DictReader(f, delimiter=delim)
        header = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
        # re-key each row with normalised (stripped, lowercased) column names;
        # any surplus cells from an unquoted delimiter collect under key "".
        rows = [{(k or "").strip().lower(): v for k, v in r.items()} for r in reader]

    if not rows:
        sys.exit("No rows found in CSV.")

    required = {"set", "number", "player", "pack", "pick"}   # card_name optional
    missing = required - set(header)
    if missing:
        sys.exit(f"Missing column(s) {sorted(missing)}. Detected delimiter {delim!r} "
                 f"and columns {header}. If those columns look merged into one string, "
                 f"the delimiter guess is wrong — re-save the file as comma-separated.")
    if any(r.get("") for r in rows):
        print(f"Note: some rows have more cells than columns (delimiter {delim!r}). "
              f"A field — usually card_name — probably contains an unquoted {delim!r}. "
              f"Re-export with quoting if the parse below looks shifted.", file=sys.stderr)

    def geti(r, col, lineno):
        v = (r.get(col) or "").strip()
        try:
            return int(v)
        except ValueError:
            sys.exit(f"Column '{col}' on data row {lineno} is {v!r}, not a number.\n"
                     f"  Full row: {r}\n"
                     f"  Detected delimiter: {delim!r}. If the row looks shifted, a field "
                     f"(often card_name) contains the delimiter unquoted; if everything is "
                     f"in one cell, the delimiter guess is wrong.")

    # ---- normalise rows ----
    recs = []
    for i, r in enumerate(rows, 2):  # line 2 = first data row
        setc = (r.get("set") or "").strip()
        num = geti(r, "number", i)
        recs.append({
            "round": geti(r, "pack", i),
            "pick":  geti(r, "pick", i),
            "seat":  geti(r, "player", i),
            "set":   setc,
            "num":   num,
            "card":  card_id(setc, num),
        })

    seats = sorted({r["seat"] for r in recs})
    rank = {s: i for i, s in enumerate(seats)}
    N = len(seats)
    rounds = sorted({r["round"] for r in recs})

    # ---- validation (warnings to stderr; these catch transcription gaps) ----
    warn = []
    seen = set()
    for r in recs:
        key = (r["round"], r["pick"], r["seat"])
        if key in seen:
            warn.append(f"duplicate pick row for round {r['round']}, pick {r['pick']}, seat {r['seat']}")
        seen.add(key)

    pack_size = {}
    for rd in rounds:
        rrecs = [r for r in recs if r["round"] == rd]
        per_player = Counter(r["seat"] for r in rrecs)
        ps = max(per_player.values())
        pack_size[rd] = ps
        for s in seats:
            if per_player.get(s, 0) != ps:
                warn.append(f"round {rd}: seat {s} has {per_player.get(s,0)} picks, expected {ps}")
        if rd not in PASS_DIRECTIONS:
            sys.exit(f"PASS_DIRECTIONS is missing an entry for pack/round {rd}.")

    # ---- assign tokens (which pack each card came from) ----
    for r in recs:
        d = PASS_DIRECTIONS[r["round"]]
        r["token"] = opener_seat(r["seat"], r["pick"], seats, rank, d)

    # ---- reconstruct pack opening contents from the picks ----
    packs = []
    grouped = defaultdict(list)
    for r in recs:
        grouped[(r["round"], r["token"])].append(r)
    for (rd, tok), group in sorted(grouped.items()):
        cards = [g["card"] for g in sorted(group, key=lambda x: (x["set"], x["num"]))]
        if len(cards) != pack_size[rd]:
            warn.append(f"round {rd}, pack opened by seat {tok}: {len(cards)} cards, "
                        f"expected {pack_size[rd]} (incomplete CSV?)")
        packs.append({
            "pack_id": f"R{rd}-S{tok}",
            "round": rd,
            "opened_by_seat": tok,
            "token": tok,
            "cards": cards,
        })

    # ---- assemble picks ----
    picks = []
    for s, ids in LEADERS.items():
        for i, cid in enumerate(ids, 1):
            picks.append({"phase": "leader", "round": 1, "pick": i, "seat": int(s), "card": cid})
    for r in sorted(recs, key=lambda x: (x["round"], x["pick"], x["seat"])):
        picks.append({"phase": "booster", "round": r["round"], "pick": r["pick"],
                      "seat": r["seat"], "card": r["card"], "token": r["token"]})

    set_code = DRAFT_SET or Counter(r["set"] for r in recs).most_common(1)[0][0]
    draft = {
        "set": set_code.upper(),
        "date": DRAFT_DATE,
        "seats": [{"seat": s, "player": PLAYER_NAMES.get(s, f"Seat {s}")} for s in seats],
        "config": {
            "pack_size": pack_size[rounds[0]],
            "protagonist_seat": PROTAGONIST_PLAYER,
            "pass_directions": {str(rd): PASS_DIRECTIONS[rd] for rd in rounds},
        },
    }
    data = {"draft": draft, "packs": packs, "picks": picks}

    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # ---- report ----
    print(f"Wrote {out}: {N} seats, {len(rounds)} pack(s), {len(picks)} picks "
          f"({len(packs)} pack objects).", file=sys.stderr)
    d1 = PASS_DIRECTIONS[rounds[0]]
    got = opener_seat(seats[0], 2, seats, rank, d1) if pack_size[rounds[0]] > 1 else None
    if got is not None:
        print(f"Pass check (pack {rounds[0]}, '{d1}'): after the first pass, seat {seats[0]} "
              f"is drafting the pack seat {got} opened. If that's the wrong neighbour, "
              f"flip PASS_DIRECTIONS[{rounds[0]}].", file=sys.stderr)
    if warn:
        print("\nWarnings:", file=sys.stderr)
        for w in warn:
            print("  - " + w, file=sys.stderr)
    else:
        print("Validation: all picks present, pack sizes consistent.", file=sys.stderr)


if __name__ == "__main__":
    main()
