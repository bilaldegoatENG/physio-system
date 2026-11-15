#!/usr/bin/env python3
"""PhysioCoach sync agent for the UNO Q kiosk (Option B: the patient's own login).

DOWN  copies the paired patient's approved prescriptions from the cloud into the board's
      local backend (so the patient can exercise with no internet).
UP    uploads finished local sessions to the cloud (POST /sessions/sync, idempotent).

When the cloud cannot be reached it does nothing and exits cleanly; run it again later.
Standard library only. Secrets live in a 0600 config file, never in the repository.

  python3 sync_agent.py --init      one-time setup (asks for the passwords)
  python3 sync_agent.py             one sync pass
  python3 sync_agent.py --loop 10   a pass every 10 minutes
  python3 sync_agent.py --status    show what has been synced
"""
import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

CONFIG_PATH = os.environ.get("PHYSIO_SYNC_CONFIG", "/home/arduino/sync-config.json")
STATE_PATH = os.environ.get("PHYSIO_SYNC_STATE", "/home/arduino/sync-state.json")
DEFAULT_CLOUD = "https://physio-backend-b59p.onrender.com/api"
DEFAULT_LOCAL = "http://127.0.0.1:8080/api"
MAX_CLOCK_AHEAD_SECONDS = 240  # the cloud rejects sessions that start >5 min in its future
COPY_STATUSES = ("APPROVED", "ACTIVE", "PAUSED")  # pending prescriptions stay on the cloud


class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__("HTTP %s: %s" % (status, body))
        self.status = status
        self.body = body


def log(message):
    print("%s  %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"), message), flush=True)


def call(base, method, path, token=None, body=None, timeout=20):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode(errors="replace")[:500])


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def parse_iso(text):
    text = re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00"))
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def exercise_key(ex):
    """Stable identity of an exercise across databases (numeric ids differ)."""
    key = ex.get("analyzerKey")
    if key and key != "reference_dtw":
        return "k:" + key
    return "n:" + (ex.get("name") or "").strip().lower()


class Agent:
    def __init__(self, cfg, state):
        self.cfg = cfg
        self.state = state
        self.cloud = cfg["cloud_url"].rstrip("/")
        self.local = cfg["local_url"].rstrip("/")
        for k in ("assignments", "assignment_status", "uploaded"):
            self.state.setdefault(k, {})

    # ------------------------------------------------------------------ auth
    def login(self, base, email, password):
        return call(base, "POST", "/auth/login", body={"email": email, "password": password})["token"]

    def local_patient_token(self):
        email, pw = self.cfg["cloud_email"], self.cfg["cloud_password"]
        try:
            return self.login(self.local, email, pw)
        except HttpError as e:
            if e.status not in (400, 401, 403):
                raise
        me = call(self.cloud, "GET", "/users/me", self.ctok)
        log("creating the local account for the paired patient")
        call(self.local, "POST", "/auth/register", body={
            "firstName": me.get("firstName") or "Patient", "lastName": me.get("lastName") or "Board",
            "email": email, "password": pw})
        return self.login(self.local, email, pw)

    # ------------------------------------------------------------------ one pass
    def run(self):
        try:
            health = call(self.cloud, "GET", "/health", timeout=60)  # free instance may be waking up
        except (OSError, HttpError) as e:
            log("cloud not reachable, nothing to do (%s)" % e)
            return 0
        skew = None
        try:
            skew = (datetime.now(timezone.utc) - parse_iso(health["time"])).total_seconds()
        except (KeyError, ValueError, TypeError):
            log("could not read the cloud time; uploads will be skipped this pass")
        if skew is not None and abs(skew) > 300:
            log("WARNING: board clock differs from the cloud by %.0f s" % skew)

        try:
            self.ctok = self.login(self.cloud, self.cfg["cloud_email"], self.cfg["cloud_password"])
        except HttpError as e:
            log("cloud login failed (%s). Was the password changed on the website? Run --init again." % e.status)
            return 2
        try:
            self.ttok = self.login(self.local, self.cfg["local_therapist_email"], self.cfg["local_therapist_password"])
            self.ptok = self.local_patient_token()
        except HttpError as e:
            log("local login failed: %s" % e)
            return 2
        except OSError as e:
            log("local backend not reachable: %s" % e)
            return 1

        code = 0
        try:
            self.sync_down()
        except (OSError, HttpError) as e:
            log("download step stopped: %s" % e)
            code = 1
        if skew is not None and skew <= MAX_CLOCK_AHEAD_SECONDS:
            try:
                self.sync_up()
            except (OSError, HttpError) as e:
                log("upload step stopped: %s" % e)
                code = 1
        elif skew is not None:
            log("board clock is %.0f s ahead of the cloud: uploads skipped until the clock is right" % skew)
        save_json(STATE_PATH, self.state)
        return code

    # ------------------------------------------------------------------ DOWN
    def local_exercises(self):
        return {exercise_key(e): e for e in call(self.local, "GET", "/exercises", self.ptok) or []}

    def ensure_local_exercise(self, cloud_ex, index):
        key = exercise_key(cloud_ex)
        if key in index:
            return index[key]["id"]
        if cloud_ex.get("analyzerKey") != "reference_dtw":
            log("exercise '%s' does not exist on the board; skipping its prescription" % cloud_ex.get("name"))
            return None
        full = call(self.cloud, "GET", "/exercises/%s" % cloud_ex["id"], self.ctok)
        body = {k: full.get(k) for k in ("name", "description", "instructions", "cameraSetup", "bodyRegion", "defaultReps", "analysisConfig")}
        created = call(self.local, "POST", "/therapist/exercises", self.ttok, body)
        index[key] = created
        log("copied exercise '%s' to the board" % full.get("name"))
        return created["id"]

    def sync_down(self):
        me = call(self.local, "GET", "/users/me", self.ptok)
        pid = me["id"]
        if not self.state.get("linked"):
            try:
                call(self.local, "POST", "/therapist/patients", self.ttok, {"email": self.cfg["cloud_email"]})
            except HttpError as e:
                if e.status != 409:
                    log("could not link the patient to the local therapist: %s" % e)
            self.state["linked"] = True

        cloud_list = call(self.cloud, "GET", "/assignments", self.ctok) or []
        index = self.local_exercises()
        amap, astat = self.state["assignments"], self.state["assignment_status"]
        seen = set()
        for a in cloud_list:
            cid, status = str(a["id"]), a.get("status")
            seen.add(cid)
            if cid in amap:
                self.propagate_status(cid, status, amap[cid], astat)
                continue
            if status not in COPY_STATUSES:
                continue
            local_ex = self.ensure_local_exercise(a["exercise"], index)
            if local_ex is None:
                continue
            body = {"exerciseId": local_ex, "targetReps": a.get("targetReps") or 1}
            for k in ("setsPerSession", "sessionsPerWeek", "notes", "durationSeconds", "restSeconds", "startDate",
                      "endDate", "difficulty", "instructions", "safetyInstructions", "expectedProgression"):
                if a.get(k) is not None:
                    body[k] = a[k]
            created = call(self.local, "POST", "/therapist/patients/%s/assignments" % pid, self.ttok, body)
            call(self.local, "POST", "/therapist/assignments/%s/approve" % created["id"], self.ttok,
                 {"readinessConfirmed": True, "note": "Copied from the cloud prescription %s" % cid})
            if status == "PAUSED":
                call(self.local, "PUT", "/therapist/assignments/%s/status" % created["id"], self.ttok, {"status": "PAUSED"})
            amap[cid], astat[cid] = created["id"], status
            log("prescription %s copied to the board as %s" % (cid, created["id"]))
        for cid in [c for c in amap if c not in seen and astat.get(c) != "RETIRED"]:
            call(self.local, "PUT", "/therapist/assignments/%s/status" % amap[cid], self.ttok, {"status": "DISCONTINUED"})
            astat[cid] = "RETIRED"
            log("prescription %s is no longer active in the cloud; stopped on the board" % cid)

    def propagate_status(self, cid, status, local_id, astat):
        old = astat.get(cid)
        if status == old or status not in ("ACTIVE", "APPROVED", "PAUSED") or old == "RETIRED":
            return
        target = "PAUSED" if status == "PAUSED" else "ACTIVE"
        call(self.local, "PUT", "/therapist/assignments/%s/status" % local_id, self.ttok, {"status": target})
        astat[cid] = status
        log("prescription %s is now %s on the board" % (cid, target))

    # ------------------------------------------------------------------ UP
    def sync_up(self):
        local_by_id = {e["id"]: exercise_key(e) for e in self.local_exercises().values()}
        cloud_by_key = {exercise_key(e): e["id"] for e in call(self.cloud, "GET", "/exercises", self.ctok) or []}
        reverse = {v: int(k) for k, v in self.state["assignments"].items()}
        uploaded = self.state["uploaded"]
        for s in call(self.local, "GET", "/sessions", self.ptok) or []:
            if s.get("status") != "COMPLETED" or not s.get("startedAt"):
                continue
            csid = s.get("clientSessionId") or "uno-%s-%s" % (s["id"], hashlib.sha1(s["startedAt"].encode()).hexdigest()[:10])
            csid = csid[:64]
            if csid in uploaded:
                continue
            cloud_ex = cloud_by_key.get(local_by_id.get(s["exerciseId"]))
            if cloud_ex is None:
                uploaded[csid] = "rejected: exercise unknown in the cloud"
                log("session %s skipped: exercise unknown in the cloud" % s["id"])
                continue
            cloud_assignment = None
            if s.get("assignmentId") is not None:
                cloud_assignment = reverse.get(s["assignmentId"])
                if cloud_assignment is None:
                    uploaded[csid] = "rejected: prescription not from the cloud"
                    log("session %s skipped: its prescription is not a cloud prescription" % s["id"])
                    continue
            body = self.build_upload(s, csid, cloud_ex, cloud_assignment)
            try:
                call(self.cloud, "POST", "/sessions/sync", self.ctok, body)
                uploaded[csid] = "ok"
                log("session %s uploaded" % s["id"])
            except HttpError as e:
                if 400 <= e.status < 500 and e.status not in (401, 408, 429):
                    uploaded[csid] = "rejected: %s" % e.body[:120]
                    log("session %s rejected by the cloud: %s" % (s["id"], e.body[:160]))
                else:
                    raise

    def build_upload(self, s, csid, cloud_ex, cloud_assignment):
        report = call(self.local, "GET", "/sessions/%s/report" % s["id"], self.ptok) or {}
        events = call(self.local, "GET", "/sessions/%s/events" % s["id"], self.ptok) or []
        mistakes = report.get("commonMistakes") or []
        payload_events = [{
            "eventUid": hashlib.sha1(("%s:%s" % (csid, e["sequenceNo"])).encode()).hexdigest(),
            "sequenceNo": e["sequenceNo"], "eventTimestamp": e["eventTimestamp"], "eventType": e["eventType"],
            "errorCode": e.get("errorCode"), "jointName": e.get("jointName"), "value": e.get("value"),
            "message": e.get("message"), "repNumber": e.get("repNumber")} for e in events[:2000]]
        rounds = report.get("totalReps") if report.get("totalReps") is not None else (s.get("roundsCompleted") or 0)
        return {
            "clientSessionId": csid, "exerciseId": cloud_ex, "assignmentId": cloud_assignment,
            "roundsRequested": max(1, min(200, s.get("roundsRequested") or 1)),
            "startedAt": s["startedAt"], "endedAt": s.get("endedAt"),
            "summary": {
                "roundsCompleted": rounds, "correctReps": report.get("correctReps"),
                "durationSeconds": min(86400, report.get("durationSeconds") or s.get("durationSeconds") or 0),
                "errorCounts": {m["code"]: m["count"] for m in mistakes if m.get("code")},
                "errorMessages": {m["code"]: m["message"] for m in mistakes if m.get("code") and m.get("message")},
                "metrics": report.get("metrics"), "events": payload_events}}


# ---------------------------------------------------------------------- command line
def init_config():
    print("Pairing the board with a cloud patient account. Passwords are saved only in %s (mode 600)." % CONFIG_PATH)
    cfg = {
        "cloud_url": input("Cloud API [%s]: " % DEFAULT_CLOUD).strip() or DEFAULT_CLOUD,
        "local_url": DEFAULT_LOCAL,
        "cloud_email": input("Patient email (the website account): ").strip(),
        "cloud_password": getpass.getpass("Patient password: "),
        "local_therapist_email": input("Local therapist email on the board [therapist@demo.local]: ").strip() or "therapist@demo.local",
        "local_therapist_password": getpass.getpass("Local therapist password: "),
    }
    save_json(CONFIG_PATH, cfg)
    print("Saved.")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--init", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--loop", type=int, metavar="MINUTES")
    args = p.parse_args(argv)
    if args.init:
        init_config()
        return 0
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        print("No configuration. Run:  python3 %s --init" % sys.argv[0])
        return 2
    state = load_json(STATE_PATH, {})
    if args.status:
        done = sum(1 for v in state.get("uploaded", {}).values() if v == "ok")
        bad = [v for v in state.get("uploaded", {}).values() if v != "ok"]
        print("prescriptions copied: %d, sessions uploaded: %d, rejected: %d" % (len(state.get("assignments", {})), done, len(bad)))
        for v in bad:
            print("  -", v)
        return 0
    while True:
        code = Agent(cfg, state).run()
        if not args.loop:
            return code
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    sys.exit(main())
