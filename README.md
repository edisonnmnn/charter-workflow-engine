# Charter workflow engine

A small execution engine that runs a four-step workflow for a charter inquiry: check aircraft availability, prepare a quote, wait for human approval, then send the quote. Every outside service is a mock, so nothing is actually sent.

## Run it

Python 3.9 or later, standard library only (tested on 3.9 and 3.12).

```
python3 charter.py run REQ-1042 --fail-availability 1   # one timeout, a retry, then a pause for approval
python3 charter.py approve REQ-1042 --by dana           # record the approval, then send the quote (mock)
python3 charter.py status REQ-1042                      # status and the full timestamped history
```

Instead of approving, `python3 charter.py reject REQ-1042 --by dana --reason "..."` ends the workflow without sending.

- `bash demo.sh` runs every scenario from a clean state. Its output is in [sample-output.txt](sample-output.txt).
- `python3 -m unittest -v` runs the tests.
- Workflows are saved in `state/`. Delete that folder to start over. Running a finished workflow again prints `error: REQ-1042 is already COMPLETE`.

## Sample run

```
$ python3 charter.py run REQ-1042 --fail-availability 1
[START]      Workflow REQ-1042
[RUNNING]    Check availability
[FAILED]     Temporary API timeout
[RETRY]      Attempt 2
[SUCCESS]    Aircraft availability received
[RUNNING]    Prepare quote
[SUCCESS]    Draft prepared
[PAUSED]     Waiting for human approval

Draft for Morgan Lee: Citation XLS+ (N512AV), KTEB to KPBI on 2026-12-18, total $14,990
Approve: python3 charter.py approve REQ-1042 --by <name>
Reject:  python3 charter.py reject REQ-1042 --by <name> --reason <text>

$ python3 charter.py approve REQ-1042 --by dana
[APPROVED]   Human approves draft (by dana)
[RUNNING]    Send quote (mock)
[SUCCESS]    Mock send completed
[COMPLETE]   Workflow finished
```

This matches the example output in the brief, except that the run is split into two commands at the approval point (see the key decision below) and the APPROVED line names who approved.

## Architecture

- `engine.py` is generic and knows nothing about charters. It runs a list of steps in order, retries timeouts, pauses before any step that needs approval, and saves the workflow to `state/<id>.json` after every event.
- `charter.py` defines the charter workflow (three step functions plus mock fleet and email services) and the CLI.
- Steps share a `data` dict. Each result is stored under its step's name (`availability`, `quote`, `send`), so the quote step reads the aircraft and hourly rate from `data["availability"]`.
- The state file holds the status (RUNNING, PAUSED, COMPLETE, FAILED or REJECTED), the finished steps, the data, the approvals and the history. Each event is saved before it is printed, so the console output is the saved history.
- Waiting for approval is not a step function. The send step is marked `approval_of="quote"`, and the engine won't run it until a person approves that exact quote. The approval stores a hash of the quote, so a changed quote needs a new approval.

## Key decision: pausing means saving and exiting

At the approval point the engine saves the workflow and the process exits. A separate `approve` command loads it and continues. I chose this over a blocking `input()` prompt because a real approver answers on Slack or email, possibly hours later, and an in-memory wait loses the draft and the history if the process restarts. With the state on disk, one path ("load the file, continue from the first unfinished step") handles approval, reruns after a failure, and recovery from a crash. While the workflow is paused no process is running, so nothing can be sent.

In production I would use a durable workflow service, such as AWS Step Functions (wait for a task-token callback) or Temporal (signals). This is the smallest version of the same idea.

## Failure handling

- Only timeouts are retried: up to 3 attempts at the availability check, waiting 0.5s and then 1s.
- Any other error stops the workflow right away (FAILED), since retrying would fail the same way.
- The send is never retried automatically, because a timeout on a send doesn't tell you whether it went out. An approval covers one try: it's used up, and saved, just before the send runs. If the send fails or the process dies partway through, a rerun pauses for a new approval instead of sending again.
- After a failure or a crash, `run` continues from the step that didn't finish. A run killed with `kill -9` during a retry picks up at the availability check when rerun.

## Limitation

The engine never resends on its own, but it can't tell whether a send that timed out actually went out. The person approving again has to check the email system first. Also, two `approve` commands at the same moment could both send, because a JSON file has no locking. The fix for both is an idempotency key (workflow ID plus quote hash) that the email service uses to drop duplicates, and a store with locking, such as a database row with a conditional update.
