"""Runs sync_agent against fake 'cloud' and 'local' backends (no network, no real data)."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import sync_agent

NOW = "2099-01-01T00:00:00Z"


def make_server(routes):
    """routes: {(method, path): fn(body) -> (status, obj)}; also records every call."""
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def handle_any(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            path = self.path.split("?")[0]
            calls.append((self.command, path, body, self.headers.get("Authorization")))
            fn = routes.get((self.command, path))
            status, obj = fn(body) if fn else (404, {"error": "no route " + path})
            raw = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = handle_any

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, calls


SHOULDER = {"id": 3, "name": "Shoulder Raise", "analyzerKey": "shoulder_abduction_bilateral"}
LOCAL_SHOULDER = {"id": 1, "name": "Shoulder Raise", "analyzerKey": "shoulder_abduction_bilateral"}
CLOUD_ASSIGNMENT = {"id": 7, "exercise": SHOULDER, "status": "ACTIVE", "targetReps": 8, "setsPerSession": 1,
                    "difficulty": "EASY", "startDate": "2026-11-01"}
LOCAL_SESSION = {"id": 5, "exerciseId": 1, "assignmentId": 100, "status": "COMPLETED", "roundsRequested": 8,
                 "roundsCompleted": 8, "correctReps": 6, "durationSeconds": 90, "clientSessionId": "abc-123",
                 "startedAt": "2026-11-14T10:00:00", "endedAt": "2026-11-14T10:01:30"}


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        sync_agent.STATE_PATH = os.path.join(self.tmp, "state.json")
        self.cloud_assignments = [CLOUD_ASSIGNMENT]
        self.cloud_srv, self.cloud_calls = make_server({
            ("GET", "/health"): lambda b: (200, {"status": "ok", "time": NOW}),
            ("POST", "/auth/login"): lambda b: (200, {"token": "cloud-token"}),
            ("GET", "/users/me"): lambda b: (200, {"id": 9, "firstName": "Pat", "lastName": "Patient"}),
            ("GET", "/assignments"): lambda b: (200, self.cloud_assignments),
            ("GET", "/exercises"): lambda b: (200, [SHOULDER]),
            ("POST", "/sessions/sync"): lambda b: (200, {"sessionId": 55}),
        })
        self.local_logins = []

        def local_login(body):
            self.local_logins.append(body["email"])
            if body["email"] == "pat@example.com" and len(self.local_logins) <= 2 and "registered" not in self.__dict__:
                return 401, {"error": "bad credentials"}
            return 200, {"token": "local-token"}

        def register(body):
            self.registered = True
            return 201, {"token": "local-token"}

        self.local_srv, self.local_calls = make_server({
            ("POST", "/auth/login"): local_login,
            ("POST", "/auth/register"): register,
            ("GET", "/users/me"): lambda b: (200, {"id": 20}),
            ("GET", "/exercises"): lambda b: (200, [LOCAL_SHOULDER]),
            ("POST", "/therapist/patients"): lambda b: (200, {"id": 20}),
            ("POST", "/therapist/patients/20/assignments"): lambda b: (201, {"id": 100}),
            ("POST", "/therapist/assignments/100/approve"): lambda b: (200, {"id": 100}),
            ("PUT", "/therapist/assignments/100/status"): lambda b: (200, {"id": 100}),
            ("GET", "/sessions"): lambda b: (200, [LOCAL_SESSION]),
            ("GET", "/sessions/5/report"): lambda b: (200, {
                "totalReps": 8, "correctReps": 6, "durationSeconds": 90,
                "commonMistakes": [{"code": "KNEE", "message": "Keep your knee straight", "count": 2}],
                "metrics": {"peakAngle": 88}}),
            ("GET", "/sessions/5/events"): lambda b: (200, [
                {"sequenceNo": 1, "eventTimestamp": "2026-11-14T10:00:10", "eventType": "REP", "errorCode": None,
                 "jointName": None, "value": 1.5, "message": None, "repNumber": 1}]),
        })
        self.cfg = {"cloud_url": "http://127.0.0.1:%d" % self.cloud_srv.server_port,
                    "local_url": "http://127.0.0.1:%d" % self.local_srv.server_port,
                    "cloud_email": "pat@example.com", "cloud_password": "Secret123",
                    "local_therapist_email": "therapist@demo.local", "local_therapist_password": "Secret456"}

    def tearDown(self):
        self.cloud_srv.shutdown()
        self.local_srv.shutdown()

    def run_agent(self, cfg=None, state=None):
        return sync_agent.Agent(cfg or self.cfg, state if state is not None else {}).run()

    def posted(self, calls, path):
        return [c for c in calls if c[0] == "POST" and c[1] == path]

    def test_full_pass_copies_prescription_and_uploads_session(self):
        state = {}
        self.assertEqual(self.run_agent(state=state), 0)
        # DOWN: prescription copied, approved
        created = self.posted(self.local_calls, "/therapist/patients/20/assignments")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][2]["exerciseId"], 1)  # mapped by analyzerKey, not by cloud id 3
        self.assertEqual(len(self.posted(self.local_calls, "/therapist/assignments/100/approve")), 1)
        self.assertEqual(state["assignments"], {"7": 100})
        # a local account was created for the paired patient
        self.assertEqual(len(self.posted(self.local_calls, "/auth/register")), 1)
        # UP: session uploaded with cloud ids
        sent = self.posted(self.cloud_calls, "/sessions/sync")
        self.assertEqual(len(sent), 1)
        body = sent[0][2]
        self.assertEqual((body["exerciseId"], body["assignmentId"], body["clientSessionId"]), (3, 7, "abc-123"))
        self.assertEqual(body["summary"]["errorCounts"], {"KNEE": 2})
        self.assertEqual(body["summary"]["events"][0]["sequenceNo"], 1)
        self.assertEqual(len(body["summary"]["events"][0]["eventUid"]), 40)
        self.assertEqual(sent[0][3], "Bearer cloud-token")

    def test_second_pass_does_not_duplicate(self):
        state = {}
        self.run_agent(state=state)
        self.run_agent(state=state)
        self.assertEqual(len(self.posted(self.local_calls, "/therapist/patients/20/assignments")), 1)
        self.assertEqual(len(self.posted(self.cloud_calls, "/sessions/sync")), 1)

    def test_cloud_unreachable_does_nothing(self):
        cfg = dict(self.cfg, cloud_url="http://127.0.0.1:1")  # nothing listens here
        self.assertEqual(self.run_agent(cfg=cfg), 0)
        self.assertEqual(self.local_calls, [])

    def test_prescription_removed_in_cloud_is_stopped_locally(self):
        state = {}
        self.run_agent(state=state)
        self.cloud_assignments.clear()
        self.run_agent(state=state)
        puts = [c for c in self.local_calls if c[0] == "PUT"]
        self.assertEqual(puts[-1][2], {"status": "DISCONTINUED"})

    def test_board_clock_ahead_skips_uploads(self):
        cloud_time = {"v": "2000-01-01T00:00:00Z"}  # board clock (2026+) is far ahead of this
        self.cloud_srv.RequestHandlerClass  # keep reference
        routes_health = (200, {"status": "ok", "time": cloud_time["v"]})
        # replace the health route by a new fake cloud
        srv, calls = make_server({
            ("GET", "/health"): lambda b: routes_health,
            ("POST", "/auth/login"): lambda b: (200, {"token": "t"}),
            ("GET", "/users/me"): lambda b: (200, {"id": 9, "firstName": "Pat", "lastName": "Patient"}),
            ("GET", "/assignments"): lambda b: (200, []),
            ("GET", "/exercises"): lambda b: (200, [SHOULDER]),
            ("POST", "/sessions/sync"): lambda b: (200, {}),
        })
        try:
            cfg = dict(self.cfg, cloud_url="http://127.0.0.1:%d" % srv.server_port)
            self.assertEqual(self.run_agent(cfg=cfg), 0)  # reached the end, not an early failure
            self.assertTrue(any(c[1] == "/sessions" for c in self.local_calls) is False)  # upload step never listed sessions
            self.assertEqual(self.posted(calls, "/sessions/sync"), [])
        finally:
            srv.shutdown()

    def test_cloud_rejection_is_recorded_not_retried_forever(self):
        self.cloud_srv.shutdown()
        srv, calls = make_server({
            ("GET", "/health"): lambda b: (200, {"time": NOW}),
            ("POST", "/auth/login"): lambda b: (200, {"token": "t"}),
            ("GET", "/users/me"): lambda b: (200, {"id": 9}),
            ("GET", "/assignments"): lambda b: (200, [CLOUD_ASSIGNMENT]),
            ("GET", "/exercises"): lambda b: (200, [SHOULDER]),
            ("POST", "/sessions/sync"): lambda b: (409, {"message": "not part of an approved prescription"}),
        })
        try:
            cfg = dict(self.cfg, cloud_url="http://127.0.0.1:%d" % srv.server_port)
            state = {}
            self.run_agent(cfg=cfg, state=state)
            self.run_agent(cfg=cfg, state=state)
            self.assertEqual(len(self.posted(calls, "/sessions/sync")), 1)
            self.assertTrue(state["uploaded"]["abc-123"].startswith("rejected"))
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
