"""Operational metrics derived only from persisted data (A06). Nothing is stored or estimated.

Sources: `applications` (submission time and current state), `events` (every state entry with its time),
`attempts` (send and settle times per provider check), `approvals`, `merchants`, `review_items` and `jobs`.

Definitions (also returned in `definitions` so the page can print them):
- Cohort: applications submitted in [start, end) and no later than the observation time. Every rate below divides
  by the size of that cohort unless it says otherwise. An empty cohort gives None, never zero and never a guess.
- Approval rate: cohort cases that have an approval / cohort. Decided approval share: approved / (approved + rejected).
- Exception rate: cohort cases that entered NEEDS_INFO, IN_REVIEW or PROVISIONING_FAILED at least once by the
  observation time / cohort. Each case counts once however many times it was held.
- Time in a state: the sum, over the cohort, of the gaps between a state entry and the next state entry. A case
  that is still in a non-terminal state has an open interval that is closed at the observation time and is
  reported separately. Intervals stop at ACTIVE or REJECTED.
"""
import statistics
import time

EXCEPTION_STATES = ("NEEDS_INFO", "IN_REVIEW", "PROVISIONING_FAILED")
TERMINAL = ("ACTIVE", "REJECTED")
STATE_ORDER = ("NEEDS_INFO", "IN_REVIEW", "VERIFYING", "APPROVED_PENDING_MID", "PROVISIONING_FAILED", "ACTIVE", "REJECTED")
SETTLED = ("PASS", "FAIL", "REFERRED", "TRANSIENT")
WINDOWS = {"all": None, "1h": 3600.0, "24h": 86400.0}
DEFINITIONS = (
    "Cohort: applications submitted in the window, up to the observation time. Rates divide by the cohort size, and an empty cohort shows no rate.",
    "Approval rate: cases with an approval divided by the cohort. Decided share: approved divided by approved plus rejected.",
    "Exception rate: cases that entered Needs info, In review or Provisioning failed at least once, divided by the cohort. Each case counts once.",
    "Time in state: the gaps between successive state entries, summed over repeated visits. Cases still in a state are counted up to the observation time and shown as open.",
)


def ratio(numerator, denominator):
    return None if not denominator else numerator / denominator


def summary(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return dict(n=0, mean=None, median=None, p90=None, max=None)
    rank = max(0, -(-90 * len(values) // 100) - 1)  # nearest-rank 90th percentile
    return dict(n=len(values), mean=statistics.fmean(values), median=statistics.median(values), p90=values[rank], max=values[-1])


def window_bounds(name, now):
    span = WINDOWS[name]
    return (None if span is None else now - span), now + 1e-6


def compute(conn, start=None, end=None, now=None, window_label="all time"):
    now = time.time() if now is None else now
    start = float("-inf") if start is None else start
    end = (now + 1e-6) if end is None else min(end, now + 1e-6)
    lo, hi = start if start != float("-inf") else -1e18, end
    cohort_rows = conn.execute("SELECT id,state,created FROM applications WHERE created>=? AND created<?", (lo, hi)).fetchall()
    ids = {r["id"] for r in cohort_rows}
    cohort = len(ids)
    result = dict(window=dict(label=window_label, start=None if start == float("-inf") else start, end=end, observed_at=now),
                  cohort=cohort, empty=cohort == 0, definitions=list(DEFINITIONS))
    if not cohort:
        result.update(status_distribution={}, outcomes=dict(approved=0, rejected=0, active=0, approval_rate=None,
                                                           decided_approval_share=None),
                      exceptions=dict(cases=0, rate=None, by_state={}, by_cause={}), time_in_state={},
                      workload=workload(conn, set(), now, {}), durations={}, reliability={}, bottleneck=None)
        return result

    # --- one pass over state entries, closed at the observation time
    entries = conn.execute("""WITH e AS (SELECT case_id,detail AS state,at AS entered,seq FROM events
            WHERE event='STATE_ENTERED' AND at<=?)
        SELECT case_id,state,entered,LEAD(entered) OVER (PARTITION BY case_id ORDER BY seq) AS left_at FROM e
        ORDER BY case_id,seq""", (now,)).fetchall()
    # --- status distribution: the state each case was in AT the observation time (final audit I4). Reading the
    # current state would mix later outcomes into an earlier observation. A case with no state entry yet is counted
    # in its current state, which is the only one it has.
    state_entered_at = {}
    for r in entries:
        state_entered_at[r["case_id"]] = (r["state"], r["entered"])
    state_at = {r["id"]: state_entered_at.get(r["id"], (r["state"], None))[0] for r in cohort_rows}
    distribution = {s: 0 for s in STATE_ORDER}
    for state in state_at.values():
        distribution[state] = distribution.get(state, 0) + 1

    # --- approvals and outcomes
    approvals = {r["case_id"]: r for r in conn.execute("SELECT case_id,approved_at FROM approvals") if r["case_id"] in ids
                 and r["approved_at"] <= now}
    approved, rejected = len(approvals), distribution["REJECTED"]
    outcomes = dict(approved=approved, rejected=rejected, active=distribution["ACTIVE"],
                    approval_rate=ratio(approved, cohort), decided_approval_share=ratio(approved, approved + rejected))

    dwell, visited, visits, open_cases, intervals = {}, {}, {}, {}, {}
    entered_exception, last_entry = {}, {}
    for r in entries:
        if r["case_id"] not in ids:
            continue
        state = r["state"]
        last_entry[r["case_id"]] = (state, r["entered"])
        if state in EXCEPTION_STATES:
            entered_exception.setdefault(r["case_id"], set()).add(state)
        if r["left_at"] is None and state in TERMINAL:
            continue
        is_open = r["left_at"] is None
        length = max(0.0, (now if is_open else r["left_at"]) - r["entered"])
        dwell[state] = dwell.get(state, 0.0) + length
        visits[state] = visits.get(state, 0) + 1
        visited.setdefault(state, set()).add(r["case_id"])
        intervals.setdefault(state, []).append(length)
        if is_open:
            open_cases.setdefault(state, set()).add(r["case_id"])
    time_in_state = {}
    for state in STATE_ORDER:
        if state not in dwell:
            continue
        cases = len(visited[state])
        time_in_state[state] = dict(cases=cases, visits=visits[state], total=dwell[state], mean_per_case=dwell[state] / cases,
                                    median_per_visit=statistics.median(intervals[state]), open_cases=len(open_cases.get(state, ())),
                                    share=None)
    non_terminal_total = sum(v["total"] for s, v in time_in_state.items() if s not in TERMINAL)
    for s, v in time_in_state.items():
        v["share"] = None if s in TERMINAL else ratio(v["total"], non_terminal_total)

    # --- exceptions
    by_state = {s: sum(1 for states in entered_exception.values() if s in states) for s in EXCEPTION_STATES}
    placeholders = ",".join("?" * len(ids))
    # A declared upload that is still arriving is the save in progress, not an exception (final release audit A06).
    # A refused or interrupted upload is recorded as UPLOAD_FAILED and does count.
    by_cause = {r["cause"]: r["n"] for r in conn.execute(
        f"""SELECT cause,count(DISTINCT case_id) AS n FROM review_items WHERE case_id IN ({placeholders})
            AND cause<>'UPLOAD_PENDING' GROUP BY cause ORDER BY n DESC, cause""", list(ids))}
    exceptions = dict(cases=len(entered_exception), rate=ratio(len(entered_exception), cohort), by_state=by_state, by_cause=by_cause)

    # --- durations from persisted timestamps
    created = {r["id"]: r["created"] for r in cohort_rows}
    mids = {r["case_id"]: r["received_at"] for r in conn.execute("SELECT case_id,received_at FROM merchants")
            if r["case_id"] in ids and r["received_at"] <= now}
    latency = {}
    for r in conn.execute(f"""SELECT kind,settled_at-sent_at AS d FROM attempts WHERE case_id IN ({placeholders})
            AND status IN ('PASS','FAIL','REFERRED','TRANSIENT') AND sent_at IS NOT NULL AND settled_at IS NOT NULL
            AND settled_at<=?""", [*ids, now]):
        latency.setdefault(r["kind"], []).append(max(0.0, r["d"]))
    durations = dict(
        provider_latency={kind: summary(values) for kind, values in sorted(latency.items())},
        submission_to_approval=summary([max(0.0, a["approved_at"] - created[c]) for c, a in approvals.items()]),
        approval_to_mid=summary([max(0.0, mids[c] - approvals[c]["approved_at"]) for c in mids if c in approvals]),
        submission_to_active=summary([max(0.0, mids[c] - created[c]) for c in mids]))

    # --- reliability
    status_counts = {r["status"]: r["n"] for r in conn.execute(
        f"SELECT status,count(*) AS n FROM attempts WHERE case_id IN ({placeholders}) GROUP BY status", list(ids))}
    left_sent = sum(status_counts.get(s, 0) for s in (*SETTLED, "EXPIRED"))
    reliability = dict(
        attempts=status_counts, timeout_rate=ratio(status_counts.get("EXPIRED", 0), left_sent),
        retries=conn.execute(f"SELECT count(*) FROM attempts WHERE case_id IN ({placeholders}) AND generation>1", list(ids)).fetchone()[0],
        duplicate_receipts=conn.execute(f"""SELECT coalesce(sum(receipts-1),0) FROM callback_inbox
            WHERE case_id IN ({placeholders})""", list(ids)).fetchone()[0],
        rejected_callbacks=conn.execute("SELECT coalesce(sum(count),0) FROM rejected_callbacks").fetchone()[0])

    # --- bottleneck: the state where cases spend the most time, and the slowest provider
    machine = [(s, v) for s, v in time_in_state.items() if s not in TERMINAL]
    top = max(machine, key=lambda item: item[1]["total"]) if machine else None
    slow = max(((k, v) for k, v in durations["provider_latency"].items() if v["n"]), key=lambda item: item[1]["mean"], default=None)
    bottleneck = dict(
        state=top[0] if top else None, share=top[1]["share"] if top else None, total=top[1]["total"] if top else None,
        human_wait=bool(top and top[0] in ("NEEDS_INFO", "IN_REVIEW")),
        slowest_provider=slow[0] if slow else None, slowest_provider_mean=slow[1]["mean"] if slow else None)

    result.update(status_distribution=distribution, outcomes=outcomes, exceptions=exceptions, time_in_state=time_in_state,
                  workload=workload(conn, ids, now, state_entered_at), durations=durations, reliability=reliability, bottleneck=bottleneck)
    return result


def workload(conn, ids, now, state_entered_at):
    """What is waiting at the observation time, for the cohort's cases. Counts by state use the state each case was in
    then. Provider attempts still open then include those settled only afterwards. Issues and jobs carry no history of
    their own, so their counts describe the current records of the cohort's cases, never another cohort's."""
    marks = ",".join("?" * len(ids)) or "NULL"
    args = list(ids)
    at = {case: state for case, (state, _) in state_entered_at.items() if case in ids}
    pending = {r["kind"]: r["n"] for r in conn.execute(f"""SELECT a.kind,count(*) AS n FROM attempts a
        JOIN checks c USING(case_id,kind,input_revision) WHERE (a.status IN ('SCHEDULED','SENT') OR a.settled_at>?)
        AND a.case_id IN ({marks}) GROUP BY a.kind ORDER BY a.kind""", [now, *args])}
    owners = {r["owner"]: r["n"] for r in conn.execute(f"""SELECT owner,count(DISTINCT case_id) AS n FROM review_items
        WHERE status<>'RESOLVED' AND case_id IN ({marks}) GROUP BY owner""", args)}
    open_by_cause = {r["cause"]: r["n"] for r in conn.execute(f"""SELECT cause,count(*) AS n FROM review_items
        WHERE status<>'RESOLVED' AND case_id IN ({marks}) GROUP BY cause ORDER BY n DESC, cause""", args)}
    queues = {}
    for state, label in (("IN_REVIEW", "operator"), ("NEEDS_INFO", "merchant")):
        waits = [now - state_entered_at[case][1] for case, current in at.items() if current == state]
        queues[label] = dict(cases=len(waits), oldest=max(waits) if waits else None,
                             median=statistics.median(waits) if waits else None)
    count = lambda state: sum(1 for current in at.values() if current == state)
    # A dispatch job belongs to a case through its attempt, an issuance job directly.
    dead = conn.execute(f"""SELECT count(*) FROM jobs j LEFT JOIN attempts a ON a.id=j.attempt_id
        WHERE j.status='DEAD' AND coalesce(j.case_id,a.case_id) IN ({marks})""", args).fetchone()[0]
    return dict(
        verifying_cases=count("VERIFYING"), pending_attempts=pending, open_issue_cases_by_owner=owners,
        open_issues_by_cause=open_by_cause, queues=queues, approved_awaiting_mid=count("APPROVED_PENDING_MID"),
        provisioning_failed=count("PROVISIONING_FAILED"), dead_lettered=dead)
