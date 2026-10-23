#!/usr/bin/env python3
"""Publish the next unposted row of a CSV queue to a chat or webhook target.

The CSV is the queue: each row is a link plus a description and a ``posted_at``
column that records when the row went out. Rows are published in file order,
so adding a row to the bottom of the file means "post this one next".

At most one row is published per UTC day: a manual run, a retried job or a
re-run after a failed commit is a no-op once that day's post went out.

The destination is selected with ``POST_TARGET`` (default: ``discord``):

    discord   DISCORD_WEBHOOK_URL
    slack     SLACK_WEBHOOK_URL
    telegram  TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
    generic   WEBHOOK_URL, receives {"link": ..., "description": ...}
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FIELDNAMES = ["link", "description", "posted_at"]


def _required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set to post with this target")
    return value


def _send_json(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{url} returned {exc.code}: {detail}") from exc


def _message(link, description):
    return f"{description}\n\n{link}" if description else link


def post_discord(link, description):
    # Discord rejects messages longer than 2000 characters.
    _send_json(_required("DISCORD_WEBHOOK_URL"), {"content": _message(link, description)[:2000]})


def post_slack(link, description):
    _send_json(_required("SLACK_WEBHOOK_URL"), {"text": _message(link, description)})


def post_telegram(link, description):
    token = _required("TELEGRAM_BOT_TOKEN")
    _send_json(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": _required("TELEGRAM_CHAT_ID"), "text": _message(link, description)},
    )


def post_generic(link, description):
    _send_json(_required("WEBHOOK_URL"), {"link": link, "description": description})


TARGETS = {
    "discord": post_discord,
    "generic": post_generic,
    "slack": post_slack,
    "telegram": post_telegram,
}


def read_queue(path):
    """Return the CSV field names and rows, adding ``posted_at`` if it is absent.

    Both the canonical ``link,description[,posted_at]`` file and a headerless
    ``url,description`` export straight from a spreadsheet are accepted.
    """
    # utf-8-sig so a byte order mark from a spreadsheet export is not parsed as
    # part of the first link.
    with path.open(newline="", encoding="utf-8-sig") as handle:
        lines = [row for row in csv.reader(handle) if any(cell.strip() for cell in row)]

    if not lines:
        return list(FIELDNAMES), []

    if lines[0][0].strip().lower().startswith(("http://", "https://")):
        # Headerless export: every row is data, in link then description order.
        fieldnames, body = list(FIELDNAMES), lines
    else:
        fieldnames, body = [cell.strip() for cell in lines[0]], lines[1:]

    for column in ("link", "description"):
        if column not in fieldnames:
            raise SystemExit(f"{path} needs a '{column}' column, found: {fieldnames}")

    if "posted_at" not in fieldnames:
        fieldnames.append("posted_at")

    return fieldnames, [dict(zip(fieldnames, row)) for row in body]


def write_queue(path, fieldnames, rows):
    # lineterminator="\n" keeps the file byte-identical on Windows and in CI.
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=Path("data/posts.csv"))
    parser.add_argument("--limit", type=int, default=1, help="maximum rows to post (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="print the next row without posting")
    args = parser.parse_args()

    fieldnames, rows = read_queue(args.csv)

    # One post per UTC day, whatever triggered this run.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    posted_today = [row for row in rows if (row.get("posted_at") or "").startswith(today)]
    if posted_today and not args.dry_run:
        print(f"Already posted {len(posted_today)} link(s) on {today} UTC, skipping until tomorrow.")
        return

    pending = [row for row in rows if not (row.get("posted_at") or "").strip()]

    if not pending:
        print(f"{args.csv} has no unposted rows.")
        return

    target = os.environ.get("POST_TARGET", "").strip().lower() or "discord"
    if target not in TARGETS:
        raise SystemExit(f"Unknown POST_TARGET {target!r}, expected one of: {', '.join(sorted(TARGETS))}")
    publish = TARGETS[target]

    published = 0
    for row in pending:
        if published >= args.limit:
            break

        link = (row.get("link") or "").strip()
        description = (row.get("description") or "").strip()

        if not link:
            print(f"Skipping a row with no link: {description!r}")
            continue

        if args.dry_run:
            print(f"[dry-run] Would post to {target}: {link} ({description!r})")
            published += 1
            continue

        try:
            publish(link, description)
        except RuntimeError as exc:
            raise SystemExit(f"error: {exc}") from exc

        row["posted_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        published += 1
        print(f"Posted to {target}: {link}")

    if published and not args.dry_run:
        write_queue(args.csv, fieldnames, rows)
        print(f"Marked {published} row(s) as posted in {args.csv}.")
    elif not published:
        print(f"No pending rows were published (limit: {args.limit}).")


if __name__ == "__main__":
    main()
