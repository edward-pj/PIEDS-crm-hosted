#!/usr/bin/env python3
"""Generate a CSV of plus-addressed test contacts for a real send test.

Every address is `<you>+tNNN@<your domain>`, so all 300 mails land in ONE
inbox -- yours. That is the entire point. Sending a batch this size to invented
addresses produces a ~100% bounce rate, and Gmail answers a bounce rate like
that with "You have reached a limit for sending mail": a temporary block on the
whole mailbox, plus lasting reputation damage. It would break the exact thing
the test is meant to prove works.

Plus-addressing costs nothing and tests strictly more: the claim, the DRAFT row,
the real Gmail round trip, record_result, the cursor, the tick budgets, and the
rendering of {{ first_name }} and {{ company }} -- which you can only check by
reading mail that actually arrived.

    python3 tools/make_test_contacts.py                    # 300 rows, repo root
    python3 tools/make_test_contacts.py -n 20 -o small.csv

Two things to know before importing the result:

  - CampaignMailing.contact is on_delete=PROTECT, so once these have been
    mailed they can never be deleted -- only archived. They tag themselves
    `testdata` so you can always filter them out.
  - Import them under a SEPARATE root campaign. uniq_root_campaign_contact is
    scoped to the root, and you do not want 300 fixture sends inside a real
    campaign's numbers forever.
"""

import argparse
import csv
import random
import sys
from pathlib import Path

#: Real-sounding but plainly invented. A test contact that looks like a real
#: prospect is one somebody eventually mails for real.
FIRST = [
    "Rohan", "Ananya", "Vikram", "Sneha", "Arjun", "Meera", "Nikhil", "Priya",
    "Rahul", "Tara", "Kabir", "Ishita", "Aditya", "Nisha", "Karan", "Divya",
]
LAST = [
    "Sharma", "Menon", "Rao", "Nair", "Iyer", "Bose", "Kapoor", "Reddy",
    "Chawla", "Banerjee", "Pillai", "Joshi",
]
#: Deliberately fictional. Using real company names would make these rows
#: indistinguishable from prospects in a shared pool.
COMPANIES = [
    "Testworks", "Fixture Labs", "Sample Systems", "Dummy Dynamics",
    "Placeholder Co", "Stub Technologies", "Mock Industries", "Sandbox Analytics",
    "Trial Ventures", "Draft Digital", "Proto Partners", "Staging Studio",
]
DESIGNATIONS = [
    "Founder", "CTO", "VP Engineering", "Head of Partnerships",
    "Director, Strategy", "Product Lead",
]

COLUMNS = [
    "first_name", "last_name", "email", "company",
    "designation", "phone_no", "tags",
]


def rows(count, base, domain, tag, seed):
    rng = random.Random(seed)          # reproducible: re-running gives the
    for i in range(1, count + 1):      # same file, so a re-import is a no-op
        yield {
            "first_name": rng.choice(FIRST),
            "last_name": rng.choice(LAST),
            # The whole trick. Google Workspace routes anything after "+" to
            # the same mailbox, so these are 300 distinct contacts to the CRM
            # and one inbox to Gmail.
            "email": f"{base}+t{i:03d}@{domain}",
            "company": rng.choice(COMPANIES),
            "designation": rng.choice(DESIGNATIONS),
            # crm/validators.py: ^[6-9]\d{9}$ -- bare digits, no +91, no spaces.
            "phone_no": f"9{rng.randint(10**8, 10**9 - 1)}",
            # Semicolon-separated, because a comma would split the column.
            "tags": tag,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--count", type=int, default=300)
    parser.add_argument(
        "-o", "--out", default="test_contacts_300.csv",
        help="written relative to the repo root by default",
    )
    parser.add_argument(
        "--address", default="f20250882@pilani.bits-pilani.ac.in",
        help="the mailbox every test mail should land in",
    )
    parser.add_argument("--tag", default="testdata")
    parser.add_argument("--seed", type=int, default=26)
    opts = parser.parse_args(argv)

    if "@" not in opts.address or "+" in opts.address:
        parser.error("--address must be a plain address you own, with no '+'")

    base, domain = opts.address.split("@", 1)
    out = Path(opts.out)
    if not out.is_absolute():
        out = Path(__file__).resolve().parent.parent / out

    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows(opts.count, base, domain, opts.tag, opts.seed))

    print(f"wrote {opts.count} rows to {out}")
    print(f"every mail will arrive at {opts.address}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
