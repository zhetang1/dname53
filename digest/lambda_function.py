"""Daily digest: emails new dname53 searches since the last run.

Reads/writes the last-run cursor at SSM /dname53/last-digest-run, scans the
dname53-searches DynamoDB table for rows newer than the cursor, and sends an
HTML summary via SES to EMAIL_TO. Skips email when there are no new searches.
"""
import json
import os
from datetime import datetime, timezone, timedelta
from html import escape

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
TABLE = "dname53-searches"
SSM_LAST_RUN = "/dname53/last-digest-run"
EMAIL_FROM = os.environ.get("EMAIL_FROM", "zhetang1@gmail.com")
EMAIL_TO = os.environ.get("EMAIL_TO", "zhetang1@gmail.com")

ddb = boto3.resource("dynamodb", region_name=REGION)
ssm = boto3.client("ssm", region_name=REGION)
ses = boto3.client("ses", region_name=REGION)


def get_last_run() -> str:
    try:
        return ssm.get_parameter(Name=SSM_LAST_RUN)["Parameter"]["Value"]
    except ClientError:
        return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")


def set_last_run(value: str) -> None:
    ssm.put_parameter(Name=SSM_LAST_RUN, Value=value, Type="String", Overwrite=True)


def fetch_new_searches(since: str) -> list[dict]:
    table = ddb.Table(TABLE)
    items, kwargs = [], {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    new = [it for it in items if it.get("created_at", "") > since]
    new.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return new


def fmt_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y · %H:%M UTC")
    except (ValueError, AttributeError):
        return iso or ""


def fmt_location(s: dict) -> str:
    parts = [s.get("city"), s.get("region"), s.get("country")]
    return ", ".join(p for p in parts if p) or "—"


def short_ua(ua: str) -> str:
    if not ua:
        return ""
    return ua if len(ua) <= 80 else ua[:80] + "…"


def render_results_block(results: list[dict]) -> str:
    if not results:
        return ""
    available = [r for r in results if r.get("available")]
    unavailable = [r for r in results if not r.get("available")]

    avail_html = ""
    if available:
        rows = []
        for r in available:
            p = r.get("price") or {}
            reg = p.get("registration")
            ren = p.get("renewal")
            price_str = ""
            if reg is not None:
                price_str = f"${float(reg):.2f} reg · ${float(ren):.2f}/yr" if ren is not None else f"${float(reg):.2f}"
            rows.append(
                f'<tr><td style="padding:4px 12px 4px 0;font-family:ui-monospace,Menlo,monospace;font-size:13px;color:#0f172a">{escape(r.get("domain", ""))}</td>'
                f'<td style="padding:4px 0;font-size:12px;color:#475569;text-align:right">{escape(price_str)}</td></tr>'
            )
        avail_html = (
            '<div style="margin:8px 0 12px 0">'
            f'<div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:#64748b;margin-bottom:6px">Available ({len(available)})</div>'
            f'<table style="border-collapse:collapse;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;padding:8px;width:100%"><tbody>{"".join(rows)}</tbody></table>'
            "</div>"
        )

    unavail_html = ""
    if unavailable:
        chips = "".join(
            f'<span style="display:inline-block;font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#64748b;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:4px;padding:2px 6px;margin:2px 4px 2px 0">{escape(r.get("domain", ""))}</span>'
            for r in unavailable
        )
        unavail_html = (
            '<div style="margin:8px 0">'
            f'<div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:#64748b;margin-bottom:6px">Unavailable ({len(unavailable)})</div>'
            f'<div>{chips}</div>'
            "</div>"
        )

    return avail_html + unavail_html


def render_html(searches: list[dict], since: str) -> str:
    cards = []
    for s in searches:
        avail = int(s.get("available_count", 0))
        total = int(s.get("total_count", 0))
        summary = f'<span style="color:#15803d;font-weight:600">{avail}</span> / {total} available' if total else "—"
        cards.append(f"""
        <div style="background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px;margin-bottom:14px">
          <div style="font-size:12px;color:#64748b;margin-bottom:4px">{escape(fmt_time(s.get("created_at", "")))}</div>
          <div style="font-size:15px;color:#0f172a;font-weight:600;margin-bottom:10px">{escape(s.get("description", ""))}</div>
          <div style="font-size:13px;color:#475569;margin-bottom:6px">Results: {summary}</div>
          <div style="font-size:12px;color:#64748b;margin-bottom:2px">📍 {escape(fmt_location(s))} · <span style="font-family:ui-monospace,Menlo,monospace">{escape(s.get("ip", ""))}</span></div>
          <div style="font-size:11px;color:#94a3b8;margin-bottom:8px">{escape(short_ua(s.get("user_agent", "")))}</div>
          {render_results_block(s.get("results") or _parse_results(s))}
        </div>
        """)

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#0f172a">
  <div style="max-width:680px;margin:0 auto">
    <h1 style="font-size:22px;margin:0 0 4px 0">dname53 — daily digest</h1>
    <p style="font-size:13px;color:#64748b;margin:0 0 20px 0">
      {len(searches)} new {"search" if len(searches) == 1 else "searches"} since {escape(fmt_time(since))}
    </p>
    {"".join(cards)}
    <p style="font-size:11px;color:#94a3b8;text-align:center;margin-top:24px">
      Sent by the dname53 daily digest job.
    </p>
  </div>
</body></html>"""


def _parse_results(s: dict) -> list[dict]:
    raw = s.get("results_json")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def render_text(searches: list[dict], since: str) -> str:
    lines = [f"dname53 daily digest — {len(searches)} new searches since {fmt_time(since)}", ""]
    for s in searches:
        avail = int(s.get("available_count", 0))
        total = int(s.get("total_count", 0))
        lines.append(f"[{fmt_time(s.get('created_at', ''))}] {s.get('description', '')}")
        lines.append(f"  results: {avail}/{total} available")
        lines.append(f"  from: {fmt_location(s)} ({s.get('ip', '')})")
        lines.append("")
    return "\n".join(lines)


def lambda_handler(event, context):
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    since = get_last_run()

    new = fetch_new_searches(since)
    print(f"Found {len(new)} new searches since {since}")

    if not new:
        set_last_run(now_iso)
        return {"status": "no_new_searches", "since": since, "checked_at": now_iso}

    for s in new:
        if "results_json" in s and "results" not in s:
            s["results"] = _parse_results(s)

    subject = f"dname53 digest — {len(new)} new {'search' if len(new) == 1 else 'searches'}"
    html = render_html(new, since)
    text = render_text(new, since)

    ses.send_email(
        Source=EMAIL_FROM,
        Destination={"ToAddresses": [EMAIL_TO]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text, "Charset": "UTF-8"},
                "Html": {"Data": html, "Charset": "UTF-8"},
            },
        },
    )

    set_last_run(now_iso)
    return {"status": "sent", "count": len(new), "since": since, "checked_at": now_iso}
