"""Email delivery via Resend.

Reads three env vars:
  RESEND_API_KEY   — API key from resend.com (free tier: 100 emails/day)
  NOTIFY_EMAIL     — destination address
  FROM_EMAIL       — verified sender on Resend

If any are missing, this module no-ops with a clear log message rather than
failing the run — the report file in the repo is still the canonical
delivery, the email is convenience.

The body is rendered both as Markdown source (the `text` field, used by
clients that prefer plain text) and as HTML (the `html` field, used by
graphical clients like Gmail). The HTML renders tables, bold text, and
links properly. Resend accepts both fields together — clients pick based
on user preference.

CSS strategy: styles are applied INLINE on each element via `style="..."`
attributes. Embedded <style> blocks don't survive Gmail's sanitizer — even
when placed in the body — so anything that needs to render reliably must
be inline. We post-process the markdown output with BeautifulSoup to walk
the HTML tree and inject styles by tag name. Gmail-compatible properties
only (background-color, color, font-*, padding, border, text-align).
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import markdown
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"

# Conservative attachment-size ceiling. Resend's documented limit is ~40MB
# per email; the underlying email infrastructure (most receivers) caps
# closer to 25MB. We refuse to attach anything over this and just log —
# better than getting silently dropped at delivery time.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MB

# Markdown extensions:
#   - "tables"     — pipe tables (the new/still-open/closed sections rely on this)
#   - "fenced_code" — ``` blocks (we don't use them today, future-proofing)
#   - "nl2br"      — single newlines render as <br>, matching how the body reads
#                    in raw form. Without this, the markdown summary line and
#                    the alerting callout collapse together visually.
_MD_EXTENSIONS = ["tables", "fenced_code", "nl2br"]

# Per-tag inline styles. Gmail strips <style> blocks entirely and only
# preserves a whitelist of inline CSS properties, so each style here uses
# only Gmail-safe properties. text-align: left on <th> is the specific
# fix for browser defaults centering header cells.
_TAG_STYLES: dict[str, str] = {
    "body": (
        'font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; '
        "font-size: 14px; line-height: 1.5; color: #222;"
    ),
    "h1": (
        "font-size: 20px; font-weight: 600; "
        "border-bottom: 1px solid #ddd; padding-bottom: 4px;"
    ),
    "h2": "font-size: 16px; font-weight: 600; margin-top: 24px;",
    "h3": "font-size: 14px; font-weight: 600; margin-top: 16px;",
    "table": "border-collapse: collapse; margin: 8px 0; font-size: 13px;",
    "th": (
        "border: 1px solid #ddd; padding: 4px 8px; text-align: left; "
        "vertical-align: top; background-color: #f5f5f5; font-weight: 600;"
    ),
    "td": (
        "border: 1px solid #ddd; padding: 4px 8px; text-align: left; "
        "vertical-align: top;"
    ),
    "blockquote": (
        "border-left: 4px solid #f0a000; background-color: #fff7e0; "
        "margin: 12px 0; padding: 8px 12px;"
    ),
    "code": (
        "background-color: #f5f5f5; padding: 1px 4px; border-radius: 3px; "
        "font-size: 12px; font-family: monospace;"
    ),
    "a": "color: #1a73e8; text-decoration: none;",
    "hr": "border: none; border-top: 1px solid #ddd; margin: 16px 0;",
}


def _inline_styles(html: str) -> str:
    """Walk the rendered HTML and apply inline styles from _TAG_STYLES.

    This bypasses Gmail's <style>-block sanitizer entirely. Each <th>,
    <td>, <a>, etc., gets a style attribute directly so the rendering is
    deterministic regardless of which email client receives it.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag_name, style in _TAG_STYLES.items():
        for element in soup.find_all(tag_name):
            existing = element.get("style", "")
            # If the element already has inline styles (markdown rarely
            # adds them, but defensive), append rather than overwrite.
            element["style"] = f"{existing} {style}".strip() if existing else style
    return str(soup)


def _markdown_to_html(markdown_body: str) -> str:
    """Render Markdown to inline-styled HTML for email."""
    body_html = markdown.markdown(markdown_body, extensions=_MD_EXTENSIONS)
    return _inline_styles(body_html)


def send(subject: str, markdown_body: str, attachment_path: Path | None = None) -> bool:
    """Send the email. Returns True on success, False if skipped or failed.

    Does NOT raise — email is best-effort. The report file in the repo is
    the canonical delivery channel.

    If `attachment_path` is provided and the file exists, it's attached to
    the email as base64. Failures to read or attach are logged but don't
    prevent the email from being sent (the body still goes out).
    """
    api_key = os.environ.get("RESEND_API_KEY")
    to_addr = os.environ.get("NOTIFY_EMAIL")
    from_addr = os.environ.get("FROM_EMAIL")

    missing = [
        name for name, val in [
            ("RESEND_API_KEY", api_key),
            ("NOTIFY_EMAIL", to_addr),
            ("FROM_EMAIL", from_addr),
        ] if not val
    ]
    if missing:
        log.warning(
            "Email delivery skipped — missing env vars: %s. "
            "Report is still committed to the repo.",
            ", ".join(missing),
        )
        return False

    # Resend accepts `text` and `html` fields. Providing both lets the
    # recipient's client pick — graphical clients render the HTML version
    # (tables, bold, links); plain-text clients fall back to the Markdown
    # source, which is readable on its own.
    payload: dict = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "text": markdown_body,
        "html": _markdown_to_html(markdown_body),
    }

    # Attach the CSV if provided. Failure to attach is non-fatal: we'd
    # rather get the email body through than block on attachment trouble.
    if attachment_path is not None:
        attachment = _build_attachment(attachment_path)
        if attachment is not None:
            payload["attachments"] = [attachment]

    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Resend email send failed: %s", exc)
        return False

    log.info("Email sent to %s", to_addr)
    return True


def _build_attachment(path: Path) -> dict | None:
    """Build a Resend attachment dict from a file path, or None on failure.

    Resend's attachment schema requires:
      - filename: the name shown in the email
      - content:  base64-encoded file contents (str)

    `content_type` is optional — Resend infers from the filename — but we
    set it explicitly for CSV to make sure mail clients open it in Excel/
    Sheets rather than as plain text. We also tag it 'utf-8' so the BOM is
    interpreted correctly.
    """
    if not path.exists():
        log.warning("Attachment skipped — file not found: %s", path)
        return None

    try:
        data = path.read_bytes()
    except OSError as exc:
        log.warning("Attachment skipped — could not read %s: %s", path, exc)
        return None

    size = len(data)
    if size > MAX_ATTACHMENT_BYTES:
        log.warning(
            "Attachment skipped — %s is %d bytes, exceeds %d byte limit.",
            path, size, MAX_ATTACHMENT_BYTES,
        )
        return None

    content_type = (
        "text/csv; charset=utf-8"
        if path.suffix.lower() == ".csv"
        else "application/octet-stream"
    )

    return {
        "filename": path.name,
        "content": base64.b64encode(data).decode("ascii"),
        "content_type": content_type,
    }
