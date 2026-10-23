#!/usr/bin/env python3
"""One-shot migration: wger routine + workout logs -> Lyftr program + workouts.

Env vars:
  WGER_URL       default https://wger.lan.home
  WGER_TOKEN     wger API token
  LYFTR_URL      default https://lyftr.lan.home
  LYFTR_TOKEN    Lyftr bearer token (from /api/v1/auth/login)
  WGER_INSECURE / LYFTR_INSECURE  "1" (default) skips TLS verification

Usage:
  python3 scripts/migrate_from_wger.py --dry-run
  python3 scripts/migrate_from_wger.py
"""
import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

PROGRAM_YAML = Path(__file__).resolve().parent.parent.parent / "wger" / "program.yaml"
CELL_RE = re.compile(r"^(\d+)x([\d.]+)(?:-([\d.]+))?\s*@([\d.]+)$")

# wger exercise id (the "exercise" field, not the translation id) -> Lyftr exercise id.
# Resolved by hand against both catalogs' /exercise(-translation) and /exercises search.
WGER_TO_LYFTR = {
    475: 560,   # Pull ups -> Pullups
    566: 59,    # Shoulder Press, Barbell -> Barbell Shoulder Press (historical only, no longer in program)
    1273: 796,  # Pullover -> Straight-Arm Dumbbell Pullover
    91: 45,     # Biceps Curls With Barbell -> Barbell Curl
    1380: 40,   # Band pull-aparts -> Band Pull Apart
    73: 73,     # Bench Press -> Bench Press - Powerlifting (same numeric id, different catalogs)
    1510: 860,  # Neutral Grip Lat Pulldown -> Wide-Grip Lat Pulldown
    348: 664,   # Lateral Raises -> Side Lateral Raise
    1519: 777,  # Overhead Triceps Extension -> Standing Overhead Barbell Triceps Extension
    257: 285,   # Front Squats -> Front Barbell Squat
    1388: 376,  # Single Leg RDL -> Kettlebell One-Legged Deadlift
    154: 161,   # Chin-ups -> Chin-Up
    537: 343,   # Incline Bench Press - Dumbbell -> Incline Dumbbell Press
    1932: 309,  # Hammer Curl -> Hammer Curls
    507: 604,   # Romanian Deadlift -> Romanian Deadlift
    1903: 96,   # Walking Lunges -> Bodyweight Walking Lunge
    194: 532,   # Dips -> Parallel Bar Dip
    1637: 495,  # Single arm row -> One-Arm Dumbbell Row
    1185: 825,  # Triceps Pushdown -> Triceps Pushdown
}


def parse_cell(cell):
    m = CELL_RE.match(cell.strip())
    if not m:
        raise ValueError(f"bad cell: {cell!r}")
    sets, reps_lo, _reps_hi, rpe = m.groups()
    return int(sets), int(float(reps_lo)), float(rpe)


class Api:
    def __init__(self, base_url, auth_header, insecure):
        self.base_url = base_url.rstrip("/")
        self.auth_header = auth_header
        self.ctx = ssl._create_unverified_context() if insecure else None

    def request(self, method, path, payload=None, params=None):
        url = f"{self.base_url}{path}"
        if params:
            from urllib.parse import urlencode
            url += "?" + urlencode(params)
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", self.auth_header)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            print(f"FAILED {method} {path}: {e.code} {e.read().decode()[:500]}", file=sys.stderr)
            raise


def build_program_sets(cell):
    sets, reps, _rpe = parse_cell(cell)
    return [{"set_number": i + 1, "target_reps": reps, "target_weight": 0} for i in range(sets)]


def build_program_sets_weighted(cell, weight):
    sets, reps, _rpe = parse_cell(cell)
    w = weight if weight is not None else 0
    return [{"set_number": i + 1, "target_reps": reps, "target_weight": w} for i in range(sets)]


def build_program(program_yaml):
    days = []
    for day in program_yaml["days"]:
        exercises = []
        for ex in day["exercises"]:
            lyftr_id = WGER_TO_LYFTR[ex["wger_exercise_id"]]
            if ex["kind"] == "main":
                b1 = ex["blocks"]["B1"]
                top_w = (b1.get("top_set_weight") or {}).get("W1")
                back_w = (b1.get("backoff_weight") or {}).get("W1")
                exercises.append({
                    "exercise_id": lyftr_id,
                    "notes": f"{ex['name']} - top set",
                    "rest_seconds": 180,
                    "sets": build_program_sets_weighted(b1["top_set"]["W1"], top_w),
                })
                exercises.append({
                    "exercise_id": lyftr_id,
                    "notes": f"{ex['name']} - backoff",
                    "rest_seconds": 120,
                    "sets": build_program_sets_weighted(b1["backoff"]["W1"], back_w),
                })
            elif ex["kind"] == "work":
                exercises.append({
                    "exercise_id": lyftr_id,
                    "notes": ex["name"],
                    "rest_seconds": 90,
                    "sets": build_program_sets(ex["blocks"]["B1"]["work"]["W1"]),
                })
            else:
                exercises.append({
                    "exercise_id": lyftr_id,
                    "notes": ex["name"],
                    "rest_seconds": 75,
                    "sets": build_program_sets(ex["flat"]),
                })
        days.append({"is_rest_day": False, "name": day["label"], "exercises": exercises})

    return {
        "name": program_yaml["routine"]["name"],
        "notes": program_yaml["routine"]["description"] + " (migrated from wger, B1W1 targets)",
        "days": days,
    }


def migrate_logs(wger, lyftr, dry_run):
    sessions = wger.request("GET", "/api/v2/workoutsession/", params={"limit": 100})["results"]
    logs = wger.request("GET", "/api/v2/workoutlog/", params={"limit": 200})["results"]
    logs_by_session = {}
    for log in logs:
        logs_by_session.setdefault(log["session"], []).append(log)

    day_name_cache = {}

    def day_name(day_id):
        if day_id not in day_name_cache:
            day_name_cache[day_id] = wger.request("GET", f"/api/v2/day/{day_id}/")["name"]
        return day_name_cache[day_id]

    for session in sessions:
        session_logs = logs_by_session.get(session["id"], [])
        if not session_logs:
            continue
        session_logs.sort(key=lambda x: x["date"])

        by_slot = {}
        slot_order = []
        for log in session_logs:
            se = log["slot_entry"]
            if se not in by_slot:
                by_slot[se] = []
                slot_order.append(se)
            by_slot[se].append(log)

        exercises = []
        for order_index, se in enumerate(slot_order):
            slot_logs = by_slot[se]
            wger_ex_id = slot_logs[0]["exercise"]
            lyftr_ex_id = WGER_TO_LYFTR.get(wger_ex_id)
            if lyftr_ex_id is None:
                print(f"WARN: no Lyftr mapping for wger exercise {wger_ex_id}, skipping slot {se}", file=sys.stderr)
                continue
            sets = []
            for i, log in enumerate(slot_logs):
                rir = float(log["rir"]) if log["rir"] is not None else None
                rpe = (10 - rir) if rir is not None else 0
                sets.append({
                    "set_number": i + 1,
                    "reps": int(float(log["repetitions"])),
                    "weight": float(log["weight"]) if log["weight"] is not None else 0,
                    "rpe": rpe,
                    "is_warmup": False,
                })
            exercises.append({
                "exercise_id": lyftr_ex_id,
                "order_index": order_index,
                "sets": sets,
            })

        start = session["datetime_start"]
        end = session.get("datetime_end")
        duration = 0
        if end:
            from datetime import datetime
            fmt = "%Y-%m-%dT%H:%M:%S.%f%z"
            try:
                t0 = datetime.strptime(start, fmt)
                t1 = datetime.strptime(end, fmt)
                duration = int((t1 - t0).total_seconds())
            except ValueError:
                duration = 0

        payload = {
            "name": day_name(session["day"]),
            "notes": session.get("notes", ""),
            "duration": duration,
            "started_at": start,
            "exercises": exercises,
        }

        if dry_run:
            print(f"POST /api/v1/workouts  {json.dumps(payload)[:300]}...")
            continue
        lyftr.request("POST", "/api/v1/workouts", payload)
        print(f"migrated workout: {payload['name']} ({start}) - {len(exercises)} exercises, "
              f"{sum(len(e['sets']) for e in exercises)} sets")


def migrate_weights(wger, lyftr, dry_run):
    entries = wger.request("GET", "/api/v2/weightentry/", params={"limit": 200})["results"]
    if not entries:
        return

    if dry_run:
        for e in entries:
            print(f"POST /api/v1/weight  {{weight: {e['weight']}, logged_at: {e['date']}}}")
        return

    lyftr.request("PUT", "/api/v1/settings", {"weight_unit": "kg"})
    for e in entries:
        payload = {
            "weight": float(e["weight"]),
            "logged_at": e["date"],
            "logged_on": e["date"][:10],
        }
        lyftr.request("POST", "/api/v1/weight", payload)
        print(f"migrated weigh-in: {payload['weight']}kg ({payload['logged_on']})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-program", action="store_true")
    ap.add_argument("--skip-logs", action="store_true")
    ap.add_argument("--skip-weights", action="store_true")
    args = ap.parse_args()

    wger_token = os.environ.get("WGER_TOKEN")
    lyftr_token = os.environ.get("LYFTR_TOKEN")
    if not args.dry_run and not wger_token:
        sys.exit("WGER_TOKEN required")
    if not args.dry_run and not lyftr_token:
        sys.exit("LYFTR_TOKEN required")

    wger = Api(
        os.environ.get("WGER_URL", "https://wger.lan.home"),
        f"Token {wger_token}",
        os.environ.get("WGER_INSECURE", "1") == "1",
    )
    lyftr = Api(
        os.environ.get("LYFTR_URL", "https://lyftr.lan.home"),
        f"Bearer {lyftr_token}",
        os.environ.get("LYFTR_INSECURE", "1") == "1",
    )

    program_yaml = yaml.safe_load(PROGRAM_YAML.read_text())

    if not args.skip_program:
        program = build_program(program_yaml)
        if args.dry_run:
            print(json.dumps(program, indent=2)[:2000])
        else:
            r = lyftr.request("POST", "/api/v1/programs", program)
            print(f"created program id={r['data']['id']}")

    if not args.skip_logs:
        migrate_logs(wger, lyftr, args.dry_run)

    if not args.skip_weights:
        migrate_weights(wger, lyftr, args.dry_run)


if __name__ == "__main__":
    main()
