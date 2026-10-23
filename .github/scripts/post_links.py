#!/usr/bin/env python3
"""Publish the next unposted row of a CSV queue as a Hypothesis annotation.

The CSV is the queue: each row is a link plus a description and a ``posted_at``
column that records when the row went out. Rows are published in file order,
so adding a row to the bottom of the file means "post this one next".

At most one row is published per UTC day: a manual run, a retried job or a
re-run after a failed commit is a no-op once that day's post went out.

The destination is selected with ``POST_TARGET`` (default: ``hypothesis``):

    hypothesis HYPOTHESIS_API_TOKEN, optionally HYPOTHESIS_GROUP_ID and
               HYPOTHESIS_API_URL. Annotates the link itself.
    discord    DISCORD_WEBHOOK_URL
    slack      SLACK_WEBHOOK_URL
    telegram   TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
    generic    WEBHOOK_URL, receives {"link": ..., "description": ...}
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FIELDNAMES = ["link", "description", "posted_at"]


def _required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set to post with this target")
    return value


def _request_json(url, payload=None, headers=None, method=None):
    request = urllib.request.Request(
        url,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{url} returned {exc.code}: {detail}") from exc
    return json.loads(body) if body else {}


def _send_json(url, payload, headers=None):
    _request_json(url, payload=payload, headers=headers, method="POST")


def _hypothesis_api_url():
    return os.environ.get("HYPOTHESIS_API_URL", "").strip().rstrip("/") or "https://api.hypothes.is"


def _hypothesis_headers():
    return {"Authorization": f"Bearer {_required('HYPOTHESIS_API_TOKEN')}"}


def _page_title(link):
    """Return the <title> of the page at link, or "" if it can't be read.

    The API displays the *document's* title next to an annotation, not the
    annotation text, so a page that has never been visited by the client shows
    up as "Untitled" unless the title is sent with the annotation.
    """
    request = urllib.request.Request(link, headers={"User-Agent": "h-daily-post"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            head = response.read(200_000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - any fetch failure just means "no title"
        return ""

    match = re.search(r"<title[^>]*>(.*?)</title>", head, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return " ".join(html.unescape(match.group(1)).split())


def _message(link, description):
    return f"{description}\n\n{link}" if description else link


def post_hypothesis(link, description):
    # A page note: the annotation's uri is the link itself, so the description
    # becomes the note body rather than a second copy of the URL. The document
    # title is what the client shows above the annotation, so always send one.
    payload = {"uri": link, "text": description or link}
    title = _page_title(link) or description
    if title:
        payload["document"] = {"title": [title]}
    # Without a group the API files the annotation in the public __world__ group.
    group = os.environ.get("HYPOTHESIS_GROUP_ID", "").strip()
    if group:
        payload["group"] = group
    _send_json(
        f"{_hypothesis_api_url()}/api/annotations", payload, headers=_hypothesis_headers()
    )


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
    "hypothesis": post_hypothesis,
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


def repair_titles(rows, dry_run=False):
    """Give the annotations of already-posted rows a document title.

    An annotation created without document metadata leaves its page showing up
    as "Untitled" until something claims a title for that URI, so rows that went
    out before titles were sent need an update.
    """
    api_url = _hypothesis_api_url()
    headers = _hypothesis_headers()
    userid = _request_json(f"{api_url}/api/profile", headers=headers)["userid"]

    repaired = 0
    for row in rows:
        link = (row.get("link") or "").strip()
        if not link or not (row.get("posted_at") or "").strip():
            continue

        title = _page_title(link) or (row.get("description") or "").strip()
        if not title:
            print(f"No title available for {link}, skipping.")
            continue

        query = urllib.parse.urlencode({"uri": link, "user": userid, "limit": 50})
        found = _request_json(f"{api_url}/api/search?{query}", headers=headers).get("rows", [])
        if not found:
            print(f"No annotation of ours on {link}, skipping.")
            continue

        for annotation in found:
            if dry_run:
                print(f"[dry-run] Would title annotation {annotation['id']} on {link}: {title!r}")
                continue
            _request_json(
                f"{api_url}/api/annotations/{annotation['id']}",
                payload={"document": {"title": [title]}},
                headers=headers,
                method="PATCH",
            )
            repaired += 1
            print(f"Titled annotation {annotation['id']} on {link}: {title!r}")

    if not dry_run:
        print(f"Retitled {repaired} annotation(s).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=Path("data/posts.csv"))
    parser.add_argument("--limit", type=int, default=1, help="maximum rows to post (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="print the next row without posting")
    parser.add_argument(
        "--repair-titles",
        action="store_true",
        help="title the annotations of rows that are already marked posted",
    )
    args = parser.parse_args()

    fieldnames, rows = read_queue(args.csv)

    if args.repair_titles:
        try:
            repair_titles(rows, dry_run=args.dry_run)
        except RuntimeError as exc:
            raise SystemExit(f"error: {exc}") from exc
        return

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

    target = os.environ.get("POST_TARGET", "").strip().lower() or "hypothesis"
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
