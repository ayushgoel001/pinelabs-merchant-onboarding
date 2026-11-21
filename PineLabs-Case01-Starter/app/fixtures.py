"""Synthetic fixture manifest (review I8).

A scenario is a property of the synthetic record, as in provider sandboxes: each provider looks up the
token it receives and replies according to the behaviour listed for that token. One run can therefore hold
every scenario at once. Behaviour lists are indexed by how many requests the provider has already seen for
the token, so later attempts can behave differently (transient error, then success). The last entry repeats.

Behaviour keys: outcome (PASS/FAIL/REFERRED/TRANSIENT), reason, delay (seconds before the callback, scaled
by the run's timing), registered_name (BANK), and faults. PAN and GST additionally bind the legal
name: a submitted name outside the CONSISTENT band of the shared name scorer fails as NAME_MISMATCH.
Faults:
    LATE          deliver at 1.5 x the run's attempt timeout
    SILENT        never deliver
    HOLD          deliver only when an operator releases it (demo controls)
    DUPLICATE     deliver the identical signed event a second time
    SECOND_EVENT  deliver the same result again under a new event ID
    CONFLICT      reuse the event ID with a different body
"""
import re
from copy import deepcopy

from .extraction import TOKEN_PATTERNS, name_band, name_score

UNKNOWN = [dict(outcome="FAIL", reason="NOT_IN_SYNTHETIC_REGISTRY", delay=2)]


def ok(delay, **extra):
    return [dict(outcome="PASS", reason="VERIFIED", delay=delay, **extra)]


REGISTRY = {
    "PAN": {
        "SYNPAN-0001": ok(2),
        "SYNPAN-0002": [dict(outcome="TRANSIENT", reason="PROVIDER_TEMPORARILY_UNAVAILABLE", delay=1),
                        dict(outcome="PASS", reason="VERIFIED", delay=2)],
        "SYNPAN-0003": ok(2),
        "SYNPAN-0004": ok(2, faults=["DUPLICATE", "SECOND_EVENT", "CONFLICT"]),
        "SYNPAN-0005": [dict(outcome="PASS", reason="VERIFIED", delay=2, faults=["LATE"]),
                        dict(outcome="PASS", reason="VERIFIED", delay=2)],
        "SYNPAN-0006": ok(2),
        "SYNPAN-0007": [dict(outcome="PASS", reason="VERIFIED", delay=2, faults=["SILENT"])],
        "SYNPAN-0008": ok(2),
        "SYNPAN-0009": ok(2),
        "SYNPAN-0010": ok(2),
    },
    "GST": {
        "SYNGST-27-SYNPAN-0001-1": ok(4),
        "SYNGST-29-SYNPAN-0002-1": ok(4),
        "SYNGST-27-SYNPAN-0003-1": [dict(outcome="FAIL", reason="GST_REGISTRATION_CANCELLED", delay=4)],
        "SYNGST-27-SYNPAN-0003-2": ok(4),
        "SYNGST-07-SYNPAN-0004-1": ok(4),
        "SYNGST-07-SYNPAN-0005-1": ok(4),
        "SYNGST-33-SYNPAN-0006-1": [dict(outcome="PASS", reason="VERIFIED", delay=4, faults=["HOLD"])],
        "SYNGST-33-SYNPAN-0007-1": ok(4),
        "SYNGST-19-SYNPAN-0008-1": ok(4),
        "SYNGST-19-SYNPAN-0009-1": ok(4),
        "SYNGST-24-SYNPAN-0010-1": ok(4),
    },
    "BANK": {
        "SYNACCT-0001": ok(6, registered_name="ACME TRADERS PRIVATE LIMITED"),
        "SYNACCT-0002": ok(6, registered_name="ASTER MEADOW STORES PVT LTD"),
        "SYNACCT-0003": ok(6, registered_name="RIVER CEDAR SUPPLIES PRIVATE LIMITED"),
        "SYNACCT-0004": ok(6, registered_name="VALLEY PAPER WORKS PRIVATE LIMITED"),
        "SYNACCT-0005": ok(6, registered_name="LANTERN GOODS PRIVATE LIMITED"),
        "SYNACCT-0006": ok(6, registered_name="CEDAR MARKET TRADING PRIVATE LIMITED"),
        "SYNACCT-0016": ok(6, registered_name="CEDAR MARKET TRADING PVT LTD"),
        "SYNACCT-0007": ok(6, registered_name="MEADOW GOODS TRADING PRIVATE LIMITED"),
        "SYNACCT-0008": ok(6, registered_name="ASTER RIVER SUPPLIES PRIVATE LIMITED"),
        "SYNACCT-0009": ok(6, registered_name="ASTER VALLEY STORES"),
        "SYNACCT-0010": ok(6, registered_name="CEDAR PAPER GOODS PRIVATE LIMITED"),
    },
    # RISK sees only category and postal zone, so its table is shared by every case with that profile.
    "RISK": {
        f"{category}/{zone}": behaviour
        for category in ("RETAIL", "SERVICES", "WHOLESALE")
        for zone, behaviour in (
            ("SYNZONE-01", ok(8)),
            ("SYNZONE-02", ok(8)),
            ("SYNZONE-03", [dict(outcome="FAIL", reason="PROHIBITED_PROFILE", delay=8)] if category == "WHOLESALE"
             else [dict(outcome="REFERRED", reason="HIGH_RISK_ZONE", delay=8)]),
        )
    },
}


def provider_key(provider, inputs):
    if provider == "RISK":
        return f"{inputs['category']}/{inputs['zone']}"
    return inputs[{"PAN": "pan", "GST": "gst", "BANK": "account"}[provider]]


def behaviour(provider, inputs, ordinal):
    key = provider_key(provider, inputs)
    plan = REGISTRY[provider].get(key, UNKNOWN)
    reply = deepcopy(plan[min(ordinal, len(plan) - 1)])
    registered = registered_name(provider, key) if key in REGISTRY[provider] else None
    if registered and not names_match(registered, inputs.get("name", "")):
        # Identity registries bind the identifier to a legal name (review B2). A material mismatch is a
        # definitive result, independent of transient or delivery faults planned for the token.
        return dict(outcome="FAIL", reason="NAME_MISMATCH", delay=reply.get("delay", 2))
    return reply


def registered_name(provider, key):
    """PAN records carry the legal name they were issued to; a GST record inherits the name of its embedded PAN."""
    if provider == "PAN":
        return PAN_NAMES.get(key)
    if provider == "GST":
        found = re.fullmatch(TOKEN_PATTERNS["gst"], key)
        return PAN_NAMES.get(found.group(1)) if found else None
    return None


def names_match(registered, submitted):
    """Deterministic rule shared with document and BANK comparison: upper-case, keep letters, expand PVT and LTD,
    then difflib similarity of the normalized strings must be at least 95 (the CONSISTENT band)."""
    return name_band(name_score(registered, submitted)) == "CONSISTENT"


def merchant(n, name, gst, category="RETAIL", zone="SYNZONE-01"):
    return dict(name=name, pan=f"SYNPAN-{n:04d}", gst=gst, account=f"SYNACCT-{n:04d}",
                routing=f"SYNIFSC-{n:04d}", category=category, zone=zone)


def documents(m, omit=(), **override):
    printed = m["name"].upper().replace("PRIVATE LIMITED", "PVT LTD")
    docs = {"identity": {"name": printed, "pan": m["pan"]},
            "tax": {"name": printed, "pan": m["pan"], "gst": m["gst"]},
            "bank": {"beneficiary": m["name"], "account": m["account"], "routing": m["routing"]}}
    for kind in omit:
        del docs[kind]
    for kind, fields in override.items():
        docs[kind].update(fields)
    return docs


def scenario(slug, label, story, m, **doc_options):
    return dict(slug=slug, label=label, story=story, merchant=m, documents=documents(m, **doc_options),
                samples=documents(m))


SCENARIOS = [
    scenario("happy", "All four checks pass",
             "Four delayed callbacks pass. Open the approval receipt and the simulated Merchant ID.",
             merchant(1, "Acme Traders Private Limited", "SYNGST-27-SYNPAN-0001-1")),
    scenario("missing-tax", "D01 · missing tax evidence, transient PAN",
             "Opens in NEEDS_INFO. After the tax document is added, PAN returns a transient error then passes.",
             merchant(2, "Aster Meadow Stores Private Limited", "SYNGST-29-SYNPAN-0002-1", zone="SYNZONE-02"),
             omit=("tax",)),
    scenario("gst-fail", "F01 · PAN passes, GST fails",
             "GST returns a definitive failure. Correcting the GST token to SYNGST-27-SYNPAN-0003-2 rechecks it.",
             merchant(3, "River Cedar Supplies Private Limited", "SYNGST-27-SYNPAN-0003-1", category="SERVICES")),
    scenario("duplicate-callback", "F02 · duplicate and conflicting callbacks",
             "PAN delivers the same event twice, a second event for the settled attempt, and a tampered reuse.",
             merchant(4, "Valley Paper Works Private Limited", "SYNGST-07-SYNPAN-0004-1", category="WHOLESALE")),
    scenario("late-callback", "F03 · callback after timeout",
             "The first PAN reply arrives after its deadline. The successor attempt decides the check.",
             merchant(5, "Lantern Goods Private Limited", "SYNGST-07-SYNPAN-0005-1")),
    scenario("bank-edit", "F04 · bank change while GST is held",
             "GST waits for an operator release. Change the account to SYNACCT-0016 in the form and bank document.",
             merchant(6, "Cedar Market Trading Private Limited", "SYNGST-33-SYNPAN-0006-1", zone="SYNZONE-02")),
    scenario("exhausted", "A05 · provider never replies",
             "PAN stays silent. Three attempts expire with 2 s and 5 s backoff, then an operator issue opens.",
             merchant(7, "Meadow Goods Trading Private Limited", "SYNGST-33-SYNPAN-0007-1")),
    scenario("risk-referral", "RISK referral",
             "SERVICES in SYNZONE-03 is referred. An operator must decide the current referral.",
             merchant(8, "Aster River Supplies Private Limited", "SYNGST-19-SYNPAN-0008-1",
                      category="SERVICES", zone="SYNZONE-03")),
    scenario("bank-name-review", "I7 · bank-registered beneficiary differs",
             "BANK passes but registers ASTER VALLEY STORES. The name score lands in the review band.",
             merchant(9, "Aster Valley Stores Private Limited", "SYNGST-19-SYNPAN-0009-1", zone="SYNZONE-02")),
    scenario("document-conflict", "F06 · bank document contradicts the form",
             "The bank document carries another account token, so no provider request is sent.",
             merchant(10, "Cedar Paper Goods Private Limited", "SYNGST-24-SYNPAN-0010-1"),
             bank={"account": "SYNACCT-0011"}),
]
# The issuer's fault sequence is counted per stable case key, including across restarts.
MID_FAULTS = {"SYNPAN-0011": ("LOSE_ACK",), "SYNPAN-0012": ("BEFORE_COMMIT",),
              "SYNPAN-0013": ("BEFORE_COMMIT",) * 3}
for number, slug, label, name, story in (
        (11, "mid-lost-ack", "F05 · issuer acknowledgement lost", "Aster Paper Goods Private Limited",
         "The issuer saves a MID, then loses the acknowledgement. The next call returns the original MID."),
        (12, "mid-before-commit", "F05 · issuer temporarily unavailable", "River Meadow Market Private Limited",
         "The first issuer call fails before creation. The worker retries the same key."),
        (13, "mid-dead-letter", "F05 · issuer outage until requeue", "Valley Cedar Works Private Limited",
         "Three issuer calls fail. An operator can requeue the existing dead letter after the outage.")):
    record = merchant(number, name, f"SYNGST-27-SYNPAN-{number:04d}-1")
    REGISTRY["PAN"][record["pan"]] = ok(2)
    REGISTRY["GST"][record["gst"]] = ok(4)
    REGISTRY["BANK"][record["account"]] = ok(6, registered_name=name.upper())
    SCENARIOS.append(scenario(slug, label, story, record))

# Duplicate-detection demos (M4-A). Seeded after the originals, so they are the later applications and carry the flag.
for number, slug, label, name, story in (
        (14, "dup-pan", "A02 · same PAN and GSTIN as case 4 (cannot be dismissed)", "Valley Paper Works Private Limited",
         "A second application for PAN SYNPAN-0004 with a different bank account. An exact PAN match is never dismissible."),
        (15, "dup-name-zone", "A02 · same legal name and zone as case 1 (dismissible)", "Acme Traders Private Limited",
         "A different PAN and bank account, but the same normalized name in the same zone. An operator can dismiss it with a reason.")):
    if number == 14:
        record = dict(merchant(4, name, "SYNGST-07-SYNPAN-0004-1", category="WHOLESALE"), account="SYNACCT-0014",
                      routing="SYNIFSC-0014")
    else:
        record = merchant(number, name, f"SYNGST-27-SYNPAN-{number:04d}-1")
        REGISTRY["PAN"][record["pan"]] = ok(2)
        REGISTRY["GST"][record["gst"]] = ok(4)
    REGISTRY["BANK"][record["account"]] = ok(6, registered_name=name.upper())
    SCENARIOS.append(scenario(slug, label, story, record))

# Load range: SYNPAN-0100 to SYNPAN-0199 are 100 distinct registered entities for the sustained-load tests. Each has
# a unique name (so name-and-zone never collides), a plain pass for every provider and short delays.
LOAD_WORDS = ["Aster", "Meadow", "Market", "Stores", "River", "Cedar", "Supplies", "Works", "Valley", "Paper",
              "Lantern", "Goods"]


def load_name(number):
    i = number - 100
    return f"{LOAD_WORDS[i % 12]} {LOAD_WORDS[(i // 12 + i % 12 + 1) % 12]} Private Limited"


def load_identity(number):
    return merchant(number, load_name(number), f"SYNGST-27-SYNPAN-{number:04d}-1")


LOAD_NAMES = {}
for _n in range(100, 200):
    _m = load_identity(_n)
    REGISTRY["PAN"][_m["pan"]] = ok(1)
    REGISTRY["GST"][_m["gst"]] = ok(2)
    REGISTRY["BANK"][_m["account"]] = ok(3, registered_name=_m["name"].upper())
    LOAD_NAMES[_m["pan"]] = _m["name"]

BY_KEY = {}
for _s in SCENARIOS:
    BY_KEY.setdefault((_s["merchant"]["pan"], _s["merchant"]["account"]), _s)
BY_PAN = {s["merchant"]["pan"]: s for s in reversed(SCENARIOS)}  # first scenario for a PAN wins
# Registered legal name for every synthetic PAN in the registry (and, through the embedded PAN, every GST).
PAN_NAMES = {s["merchant"]["pan"]: s["merchant"]["name"] for s in reversed(SCENARIOS)}
PAN_NAMES.update(LOAD_NAMES)
assert set(PAN_NAMES) == set(REGISTRY["PAN"]), "every registry PAN needs a registered legal name"


def label_for(merchant_record):
    found = BY_KEY.get((merchant_record.get("pan"), merchant_record.get("account")))
    if found and names_match(found["merchant"]["name"], merchant_record.get("name", "")):
        return found["label"]
    return "Custom synthetic input"
