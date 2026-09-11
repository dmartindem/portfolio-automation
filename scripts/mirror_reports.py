#!/usr/bin/env python3
"""
Mirror the emailed Daily Brief / Weekly Review into data/reports/ so the
public pages (daily.html, weekly.html) can show them.

How it works
------------
The Claude scheduled tasks send each report to a Gmail address. This script
logs into that mailbox over IMAP (app password), pulls every message whose
subject starts with "Daily Brief" or "Weekly Review" from the last LOOKBACK_DAYS,
keeps the HTML body, strips it down to a safe allow-list of tags, runs a privacy
guard over the text, and writes one fragment per report:

    data/reports/daily-2026-09-14.html
    data/reports/weekly-2026-09-13.html
    data/reports/index.json          <- what the pages read

Only messages sent FROM the mailbox's own address are accepted. Anything else
that happens to carry the subject is ignored, so a third party cannot publish
to the site by emailing it.

Privacy guard
-------------
The repo is public. The reports are written under a rule that forbids share
counts, cost basis, account value and dollar amounts, but this script does not
trust that: amount-shaped patterns are replaced with "[redacted]" before
anything is written, and a report that needs more than MAX_REDACTIONS is
skipped entirely rather than published. Nothing from a message body is ever
printed to the workflow log — logs on a public repo are world-readable.

Secrets (repo Settings -> Secrets and variables -> Actions):
    GMAIL_USER          the mailbox address, e.g. someone@gmail.com
    GMAIL_APP_PASSWORD  a 16-character Google app password (needs 2-Step Verification)
"""
from __future__ import annotations

import email
import email.policy
import html
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from dateutil import parser as dtparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'data', 'reports')
INDEX = os.path.join(OUT_DIR, 'index.json')

IMAP_HOST = 'imap.gmail.com'
MAILBOX = '"[Gmail]/All Mail"'        # sent-to-self lands here exactly once
LOOKBACK_DAYS = int(os.environ.get('LOOKBACK_DAYS', '21'))
LOCAL_TZ = ZoneInfo('America/Chicago')
MAX_REDACTIONS = 25

KINDS = {
    'daily':  re.compile(r'^\s*Daily Brief\s*[—–\-:]\s*(?P<rest>.+?)\s*$', re.I),
    'weekly': re.compile(r'^\s*Weekly Review\s*[—–\-:]\s*(?P<rest>.+?)\s*$', re.I),
}
SUBJECT_SEARCH = {'daily': 'Daily Brief', 'weekly': 'Weekly Review'}

# --------------------------------------------------------------------------
# Privacy guard
# --------------------------------------------------------------------------
_NUM = r'\d[\d,]*(?:\.\d+)?'
REDACT_PATTERNS = [
    # $12,345  $12,345.67  — any comma-grouped dollar figure
    re.compile(r'\$\s?\d{1,3}(?:,\d{3})+(?:\.\d+)?'),
    # $1234 and larger without commas (option strikes are three digits at most here)
    re.compile(r'\$\s?\d{4,}(?:\.\d+)?'),
    # $6k  $6K/month  $1.2M  $2bn
    re.compile(r'\$\s?\d+(?:\.\d+)?\s?(?:k|K|m|M|bn|BN|B)\b'),
    # 12,345 dollars / USD 12,345 / 12,345 USD
    re.compile(r'\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\s?(?:dollars|USD)\b', re.I),
    re.compile(r'\bUSD\s?' + _NUM, re.I),
    # 150 shares / 3 contracts — quantities
    re.compile(r'\b' + _NUM + r'\s+(?:shares?|contracts?)\b', re.I),
    # cost basis 102.40 / average cost of $98 — per-share basis is still basis
    re.compile(r'\b(?:cost basis|average cost|avg\.? cost|basis)\b(?:\s*(?:of|at|is|was|:|=|~|≈))*\s*\$?\s?' + _NUM, re.I),
    # account value / total value / net worth followed by a number
    re.compile(r'\b(?:account value|total value|portfolio value|net worth|balance)\b(?:\s*(?:of|at|is|was|:|=|~|≈))*\s*\$?\s?' + _NUM, re.I),
]


def redact(text: str) -> tuple[str, int]:
    n = 0
    for pat in REDACT_PATTERNS:
        text, k = pat.subn('[redacted]', text)
        n += k
    return text, n


# --------------------------------------------------------------------------
# HTML sanitiser — allow-list, attributes dropped except safe href
# --------------------------------------------------------------------------
ALLOWED = {
    'h1', 'h2', 'h3', 'h4', 'p', 'br', 'hr', 'strong', 'b', 'em', 'i', 'u', 's',
    'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption',
    'a', 'blockquote', 'code', 'pre', 'span', 'div', 'small', 'sup', 'sub',
}
VOID = {'br', 'hr'}
DROP_WITH_CONTENT = {'script', 'style', 'head', 'title', 'iframe', 'object', 'embed', 'svg', 'math', 'template', 'noscript'}
RENAME = {'h1': 'h2'}                  # the page owns its h1


class Sanitiser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0                  # depth inside a dropped element
        self.open: list[str] = []
        self.redactions = 0
        self.words = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip:
            if tag in DROP_WITH_CONTENT and tag not in VOID:
                self.skip += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.skip = 1
            return
        if tag not in ALLOWED:
            return                     # unwrap: keep children, drop the tag
        tag = RENAME.get(tag, tag)
        keep = ''
        if tag == 'a':
            href = dict(attrs).get('href') or ''
            if re.match(r'^https?://', href.strip(), re.I):
                keep = f' href="{html.escape(href.strip(), quote=True)}" rel="noopener noreferrer" target="_blank"'
        elif tag in ('td', 'th'):
            a = dict(attrs)
            for k in ('colspan', 'rowspan'):
                v = a.get(k)
                if v and v.isdigit():
                    keep += f' {k}="{v}"'
        self.out.append(f'<{tag}{keep}>')
        if tag not in VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip:
            if tag in DROP_WITH_CONTENT:
                self.skip -= 1
            return
        if tag not in ALLOWED or tag in VOID:
            return
        tag = RENAME.get(tag, tag)
        if tag in self.open:
            # close anything left open inside it, then it
            while self.open:
                t = self.open.pop()
                self.out.append(f'</{t}>')
                if t == tag:
                    break

    def handle_data(self, data):
        if self.skip:
            return
        text, n = redact(data)
        self.redactions += n
        self.words += len(text.split())
        self.out.append(html.escape(text, quote=False))

    def close(self):
        super().close()
        while self.open:
            self.out.append(f'</{self.open.pop()}>')

    def result(self) -> str:
        s = ''.join(self.out)
        s = re.sub(r'\n{3,}', '\n\n', s)
        return s.strip()


def sanitise(fragment: str) -> tuple[str, int, int]:
    p = Sanitiser()
    p.feed(fragment)
    p.close()
    return p.result(), p.redactions, p.words


def plain_to_html(text: str) -> str:
    paras = re.split(r'\n\s*\n', text.strip())
    return ''.join('<p>' + html.escape(p, quote=False).replace('\n', '<br>') + '</p>' for p in paras if p.strip())


# --------------------------------------------------------------------------
# Mail handling
# --------------------------------------------------------------------------
def decode_subject(msg) -> str:
    raw = msg.get('Subject', '')
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def sender_address(msg) -> str:
    _, addr = email.utils.parseaddr(msg.get('From', ''))
    return addr.lower()


def body_html(msg) -> str | None:
    """The HTML part, or the plain part wrapped in <p>, whichever actually carries the report."""
    html_part = msg.get_body(preferencelist=('html',))
    plain_part = msg.get_body(preferencelist=('plain',))
    if html_part is not None:
        content = html_part.get_content()
        if len(re.sub(r'<[^>]+>', ' ', content).split()) >= 20 or plain_part is None:
            return content
    if plain_part is not None:
        return plain_to_html(plain_part.get_content())
    return None


_HAS_DATE = re.compile(r'\d{4}-\d{2}-\d{2}|\b\d{1,2}\b.*\b[A-Za-z]{3,9}\b|\b[A-Za-z]{3,9}\b.*\b\d{1,2}\b')


def report_date(subject_rest: str, msg) -> str:
    """ISO date for the file name: the date in the subject if it really has one, else the send date in Chicago."""
    if _HAS_DATE.search(subject_rest):
        try:
            return dtparse.parse(subject_rest, fuzzy=True, dayfirst=True).date().isoformat()
        except (ValueError, OverflowError):
            pass
    d = email.utils.parsedate_to_datetime(msg.get('Date'))
    return d.astimezone(LOCAL_TZ).date().isoformat()


def sent_utc(msg) -> str:
    try:
        return email.utils.parsedate_to_datetime(msg.get('Date')).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        return ''


def fetch_candidates(user: str, password: str):
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime('%d-%b-%Y')
    M = imaplib.IMAP4_SSL(IMAP_HOST)
    M.login(user, password)
    typ, _ = M.select(MAILBOX, readonly=True)
    if typ != 'OK':
        M.select('INBOX', readonly=True)
    seen_uids: set[bytes] = set()
    for kind, needle in SUBJECT_SEARCH.items():
        typ, data = M.uid('SEARCH', None, 'SINCE', since, 'SUBJECT', f'"{needle}"')
        if typ != 'OK' or not data or not data[0]:
            continue
        for uid in data[0].split():
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            typ, raw = M.uid('FETCH', uid, '(BODY.PEEK[])')
            if typ != 'OK' or not raw or not isinstance(raw[0], tuple):
                continue
            yield email.message_from_bytes(raw[0][1], policy=email.policy.default)
    try:
        M.logout()
    except Exception:
        pass


# --------------------------------------------------------------------------
def main() -> int:
    user = os.environ.get('GMAIL_USER', '').strip().lower()
    pw = os.environ.get('GMAIL_APP_PASSWORD', '').replace(' ', '')
    if not user or not pw:
        print('::error::GMAIL_USER / GMAIL_APP_PASSWORD secrets are not set. '
              'Add them under Settings -> Secrets and variables -> Actions.')
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    index: dict[str, dict] = {}
    if os.path.exists(INDEX):
        with open(INDEX, encoding='utf-8') as f:
            for e in json.load(f).get('reports', []):
                index[e['file']] = e

    picked: dict[tuple[str, str], tuple[str, object]] = {}   # (kind, date) -> (sent, msg)
    scanned = rejected_sender = 0
    for msg in fetch_candidates(user, pw):
        scanned += 1
        if sender_address(msg) != user:
            rejected_sender += 1
            continue
        subject = decode_subject(msg)
        for kind, rx in KINDS.items():
            m = rx.match(subject)
            if not m:
                continue
            date = report_date(m.group('rest'), msg)
            key = (kind, date)
            sent = sent_utc(msg)
            if key not in picked or sent > picked[key][0]:
                picked[key] = (sent, msg)
            break

    written = skipped = unchanged = 0
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    for (kind, date), (sent, msg) in sorted(picked.items()):
        raw = body_html(msg)
        if not raw:
            skipped += 1
            continue
        frag, n_red, words = sanitise(raw)
        if n_red > MAX_REDACTIONS or words < 20:
            print(f'::warning::{kind} {date}: skipped ({n_red} redactions, {words} words)')
            skipped += 1
            continue
        fname = f'{kind}-{date}.html'
        path = os.path.join(OUT_DIR, fname)
        old = open(path, encoding='utf-8').read() if os.path.exists(path) else None
        if old == frag and fname in index:
            unchanged += 1
            continue
        with open(path, 'w', encoding='utf-8') as f:
            f.write(frag)
        index[fname] = {
            'kind': kind, 'date': date, 'file': fname,
            'subject': redact(decode_subject(msg))[0],
            'sent_utc': sent, 'mirrored_utc': now,
            'words': words, 'redactions': n_red,
        }
        written += 1

    reports = sorted(index.values(), key=lambda e: (e['date'], e['sent_utc']), reverse=True)
    payload = {
        'schema': 1,
        'note': 'Mirrored from the emailed reports. Derived ratios only; amount-shaped text is redacted before publishing. Not investment or tax advice.',
        'kinds': ['daily', 'weekly'],
        'reports': reports,
    }
    new_index = json.dumps(payload, indent=2, ensure_ascii=False) + '\n'
    old_index = open(INDEX, encoding='utf-8').read() if os.path.exists(INDEX) else None
    if new_index != old_index:
        with open(INDEX, 'w', encoding='utf-8') as f:
            f.write(new_index)

    print(f'scanned={scanned} rejected_sender={rejected_sender} matched={len(picked)} '
          f'written={written} unchanged={unchanged} skipped={skipped} indexed={len(reports)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
