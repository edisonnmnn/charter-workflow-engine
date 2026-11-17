"""The charter inquiry workflow and its command-line interface.

Steps: check aircraft availability, prepare a quote, wait for a person to
approve it, then send it. Every outside service is a mock; nothing is sent.

  python3 charter.py run REQ-1042 [--fail-availability N]
  python3 charter.py approve REQ-1042 --by NAME
  python3 charter.py reject REQ-1042 --by NAME --reason TEXT
  python3 charter.py status REQ-1042
"""

import argparse
import os
import sys

from engine import Engine, Step, WorkflowError, format_event

SAMPLE_INQUIRY = {
    "client": "Morgan Lee",
    "email": "morgan.lee@example.com",
    "origin": "KTEB",        # Teterboro, NJ
    "destination": "KPBI",   # Palm Beach, FL
    "date": "2026-12-18",
    "passengers": 4,
}


class MockFleetAPI:
    """Stands in for the operator's scheduling system. Times out on the first `fail_times` calls."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0

    def check_availability(self, inquiry: dict) -> dict:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("Temporary API timeout")
        return {"aircraft": "Citation XLS+", "tail_number": "N512AV", "seats": 8,
                "flight_hours": 2.6, "hourly_rate_usd": 5400}


class MockMailer:
    """Stands in for email. Keeps messages in a list instead of sending them."""

    def __init__(self):
        self.sent = []

    def send(self, to: str, subject: str, body: str) -> dict:
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"message_id": f"mock-{len(self.sent)}", "to": to}


def build_steps(fleet: MockFleetAPI, mailer: MockMailer) -> list:
    def check_availability(data):
        return fleet.check_availability(data["inquiry"])

    def prepare_quote(data):
        inquiry, plane = data["inquiry"], data["availability"]
        if inquiry["passengers"] > plane["seats"]:
            raise ValueError(f"{plane['aircraft']} has {plane['seats']} seats, "
                             f"but the inquiry has {inquiry['passengers']} passengers")
        flight_cost = round(plane["flight_hours"] * plane["hourly_rate_usd"])  # whole dollars
        fees = 950  # airport and handling fees
        return {
            "client": inquiry["client"],
            "email": inquiry["email"],
            "route": f"{inquiry['origin']} to {inquiry['destination']}",
            "date": inquiry["date"],
            "aircraft": f"{plane['aircraft']} ({plane['tail_number']})",
            "flight_cost_usd": flight_cost,
            "fees_usd": fees,
            "total_usd": flight_cost + fees,
        }

    def send_quote(data):
        quote = data["quote"]
        return mailer.send(
            to=quote["email"],
            subject=f"Your charter quote for {quote['date']}",
            body=f"{quote['aircraft']}, {quote['route']}: ${quote['total_usd']:,}",
        )

    return [
        Step("availability", "Check availability", check_availability,
             "Aircraft availability received", max_attempts=3),
        Step("quote", "Prepare quote", prepare_quote, "Draft prepared"),
        # No retries on the send: a timeout wouldn't tell us whether the quote went out.
        Step("send", "Send quote (mock)", send_quote, "Mock send completed",
             approval_of="quote"),
    ]


def describe_draft(quote: dict) -> str:
    return (f"Draft for {quote['client']}: {quote['aircraft']}, {quote['route']} "
            f"on {quote['date']}, total ${quote['total_usd']:,}")


def print_next_steps(state: dict) -> None:
    wid = state["id"]
    send_was_tried = state["approvals"].get("send", {}).get("used")
    if state["status"] == "PAUSED":
        print(f"\n{describe_draft(state['data']['quote'])}")
        if send_was_tried:
            print("An earlier send didn't finish and may have gone out. "
                  "Check before approving again.")
        print(f"Approve: python3 charter.py approve {wid} --by <name>")
        print(f"Reject:  python3 charter.py reject {wid} --by <name> --reason <text>")
    elif state["status"] == "FAILED":
        if send_was_tried:
            print("\nThe send didn't finish, so the quote may have gone out. "
                  "A rerun asks for approval again instead of resending.")
        print(f"\nRerun to continue from the failed step: python3 charter.py run {wid}")


def print_status(state: dict) -> None:
    print(f"Workflow {state['id']}: {state['status']}")
    for event in state["history"]:
        print(f"{event['at']}  {format_event(event['event'], event['message'])}")
    print_next_steps(state)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Charter inquiry workflow. All services are mocked.")
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="start a workflow, or continue one that failed")
    run_cmd.add_argument("workflow_id")
    run_cmd.add_argument("--fail-availability", type=int, default=0, metavar="N",
                         help="make the availability check time out N times")

    approve_cmd = commands.add_parser("approve", help="approve the draft, then send it")
    approve_cmd.add_argument("workflow_id")
    approve_cmd.add_argument("--by", required=True, help="who is approving")

    reject_cmd = commands.add_parser("reject", help="reject the draft; nothing is sent")
    reject_cmd.add_argument("workflow_id")
    reject_cmd.add_argument("--by", required=True, help="who is rejecting")
    reject_cmd.add_argument("--reason", required=True)

    status_cmd = commands.add_parser("status", help="show a workflow's status and history")
    status_cmd.add_argument("workflow_id")

    args = parser.parse_args(argv)
    fleet = MockFleetAPI(fail_times=getattr(args, "fail_availability", 0))
    engine = Engine(build_steps(fleet, MockMailer()), os.environ.get("CHARTER_STATE_DIR", "state"))

    try:
        if args.command == "run":
            state = engine.run(args.workflow_id, {"inquiry": SAMPLE_INQUIRY})
        elif args.command == "approve":
            state = engine.approve(args.workflow_id, args.by)
        elif args.command == "reject":
            state = engine.reject(args.workflow_id, args.by, args.reason)
        else:
            print_status(engine.load(args.workflow_id))
            return 0
    except WorkflowError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    print_next_steps(state)
    return 1 if state["status"] == "FAILED" else 0


if __name__ == "__main__":
    sys.exit(main())
