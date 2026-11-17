"""Tests for the engine's guarantees, using the charter workflow and its mocks."""

import json
import tempfile
import unittest
from pathlib import Path

from charter import SAMPLE_INQUIRY, MockFleetAPI, MockMailer, build_steps
from engine import Engine, WorkflowError, fingerprint

WID = "REQ-1042"


class ProcessKilled(BaseException):
    """Not caught by the engine, like a process being killed partway through a step."""


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = tmp.name
        self.mailer = MockMailer()  # shared across engines, so tests can see every send
        self.lines = []

    def engine(self, fleet=None):
        """A new engine is what a restarted process sees: only the state file carries over."""
        steps = build_steps(fleet or MockFleetAPI(), self.mailer)
        return Engine(steps, self.state_dir, sleep=lambda seconds: None, out=self.lines.append)

    def start(self, fail_times=0, inquiry=SAMPLE_INQUIRY):
        return self.engine(MockFleetAPI(fail_times)).run(WID, {"inquiry": inquiry})

    @staticmethod
    def events(state):
        return [entry["event"] for entry in state["history"]]

    def test_output_matches_the_expected_run(self):
        self.start(fail_times=1)
        self.engine().approve(WID, by="dana")
        self.assertEqual(self.lines, [
            "[START]      Workflow REQ-1042",
            "[RUNNING]    Check availability",
            "[FAILED]     Temporary API timeout",
            "[RETRY]      Attempt 2",
            "[SUCCESS]    Aircraft availability received",
            "[RUNNING]    Prepare quote",
            "[SUCCESS]    Draft prepared",
            "[PAUSED]     Waiting for human approval",
            "[APPROVED]   Human approves draft (by dana)",
            "[RUNNING]    Send quote (mock)",
            "[SUCCESS]    Mock send completed",
            "[COMPLETE]   Workflow finished",
        ])
        self.assertEqual(len(self.mailer.sent), 1)

    def test_data_flows_between_steps(self):
        self.start()
        state = self.engine().approve(WID, by="dana")
        self.assertEqual(state["data"]["quote"]["aircraft"], "Citation XLS+ (N512AV)")
        self.assertEqual(state["data"]["quote"]["total_usd"], 14990)  # 2.6 h x $5,400 + $950
        self.assertEqual(self.mailer.sent[0]["to"], SAMPLE_INQUIRY["email"])
        self.assertEqual(state["data"]["send"]["message_id"], "mock-1")

    def test_nothing_is_sent_while_paused_even_after_a_restart(self):
        self.assertEqual(self.start()["status"], "PAUSED")
        with self.assertRaises(WorkflowError):
            self.engine().run(WID)
        self.assertEqual(self.engine().load(WID)["status"], "PAUSED")
        self.assertEqual(self.mailer.sent, [])

    def test_retries_run_out_then_a_rerun_continues(self):
        state = self.start(fail_times=3)
        self.assertEqual(state["status"], "FAILED")
        self.assertEqual(self.events(state).count("FAILED"), 3)
        self.assertEqual(self.events(state)[-1], "STOPPED")

        state = self.engine().run(WID)  # the fleet API is healthy again
        self.assertEqual(state["status"], "PAUSED")
        self.assertIn("RESUMED", self.events(state))
        self.assertEqual(self.mailer.sent, [])

    def test_other_errors_are_not_retried(self):
        state = self.start(inquiry=dict(SAMPLE_INQUIRY, passengers=12))
        self.assertEqual(state["status"], "FAILED")
        self.assertNotIn("RETRY", self.events(state))
        self.assertEqual(self.mailer.sent, [])

    def test_a_crash_mid_step_resumes_at_that_step(self):
        class KilledFleet(MockFleetAPI):
            def check_availability(self, inquiry):
                raise ProcessKilled()

        with self.assertRaises(ProcessKilled):
            self.engine(KilledFleet()).run(WID, {"inquiry": SAMPLE_INQUIRY})
        self.assertEqual(self.engine().load(WID)["status"], "RUNNING")
        self.assertEqual(self.engine().run(WID)["status"], "PAUSED")

    def test_approval_only_covers_the_draft_that_was_approved(self):
        self.start()
        path = Path(self.state_dir, f"{WID}.json")
        state = json.loads(path.read_text())
        # Approved, then the quote changed before the send ran.
        state["approvals"]["send"] = {"by": "dana", "at": "",
                                      "fingerprint": fingerprint(state["data"]["quote"])}
        state["data"]["quote"]["total_usd"] = 1
        state["status"] = "RUNNING"
        path.write_text(json.dumps(state))

        self.assertEqual(self.engine().run(WID)["status"], "PAUSED")
        self.assertEqual(self.mailer.sent, [])

    def test_reject_ends_without_sending(self):
        self.start()
        state = self.engine().reject(WID, by="dana", reason="Client wants a larger cabin")
        self.assertEqual(state["status"], "REJECTED")
        with self.assertRaises(WorkflowError):
            self.engine().approve(WID, by="dana")
        self.assertEqual(self.mailer.sent, [])

    def test_approving_twice_sends_once(self):
        self.start()
        self.engine().approve(WID, by="dana")
        with self.assertRaises(WorkflowError):
            self.engine().approve(WID, by="dana")
        self.assertEqual(len(self.mailer.sent), 1)

    def test_a_failed_send_waits_for_a_new_approval(self):
        class FlakyMailer(MockMailer):
            def send(self, to, subject, body):
                result = super().send(to, subject, body)
                if len(self.sent) == 1:  # the first email goes out, but we never hear back
                    raise TimeoutError("Mail server timeout")
                return result

        self.mailer = FlakyMailer()
        self.start()
        state = self.engine().approve(WID, by="dana")
        self.assertEqual(state["status"], "FAILED")
        self.assertNotIn("RETRY", self.events(state))

        state = self.engine().run(WID)  # the rerun asks again instead of resending
        self.assertEqual(state["status"], "PAUSED")
        self.assertEqual(len(self.mailer.sent), 1)

        state = self.engine().approve(WID, by="dana")  # a person decides to resend
        self.assertEqual(state["status"], "COMPLETE")
        self.assertEqual(len(self.mailer.sent), 2)

    def test_a_crash_mid_send_waits_for_a_new_approval(self):
        class KilledMailer(MockMailer):
            def send(self, to, subject, body):
                super().send(to, subject, body)
                raise ProcessKilled()  # killed after the send, before the result was saved

        self.mailer = KilledMailer()
        self.start()
        with self.assertRaises(ProcessKilled):
            self.engine().approve(WID, by="dana")
        self.assertEqual(self.engine().run(WID)["status"], "PAUSED")
        self.assertEqual(len(self.mailer.sent), 1)

    def test_unsafe_workflow_ids_are_refused(self):
        with self.assertRaises(WorkflowError):
            self.engine().run("../outside", {"inquiry": SAMPLE_INQUIRY})


if __name__ == "__main__":
    unittest.main()
