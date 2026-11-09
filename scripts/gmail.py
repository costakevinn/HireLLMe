"""Gmail over IMAP: read a label, move messages, create messages directly in a label.

Uses GMAIL_USER and GMAIL_APP_PASSWORD (a Google app password, never in the code).
The server is imap.gmail.com unless config.json says otherwise ("gmail": {"server": ...}).
Rules: reading never changes anything (PEEK); moving only swaps labels (nothing goes to
the trash unless the target is the trash); creating never sends anything (messages do not
go through the inbox or Sent, and trigger no notification).
"""
import imaplib
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import common


def connect():
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        sys.exit("Missing GMAIL_USER and GMAIL_APP_PASSWORD in .env.")
    server = common.config().get("gmail", {}).get("server", "imap.gmail.com")
    try:
        imap = imaplib.IMAP4_SSL(server)
    except (socket.gaierror, OSError) as e:
        sys.exit(f"Could not reach the email server {server}: {e}")
    try:
        imap.login(user, password)
    except imaplib.IMAP4.error as e:
        sys.exit(f"The email server refused the login for {user} ({e}). Usual causes: the app password "
                 "was revoked, or the account password was changed (that revokes every app password). "
                 "Create a new app password and update GMAIL_APP_PASSWORD.")
    return imap


def _search(imap, criteria, senders):
    """UID SEARCH, limited to messages from any of 'senders' (if given)."""
    if senders:
        tree = f'FROM "{senders[-1]}"'
        for s in reversed(senders[:-1]):
            tree = f'OR FROM "{s}" {tree}'
        criteria = f"({criteria}) ({tree})"
    return imap.uid("SEARCH", None, criteria)[1][0].split()


def _folder(label):
    return f'"{label}"'


def _received(response):
    header = response[0] if isinstance(response, tuple) else response
    return datetime.fromtimestamp(time.mktime(imaplib.Internaldate2tuple(header)), timezone.utc)


def read(label, cutoff=None, senders=None):
    """Every message in the label (only those from 'senders', if given): [{'uid', 'raw', 'received'}].
    Read-only.

    Gmail filters on the server (SEARCH) and the messages come in one request.
    Messages received before 'cutoff' (if given) are not downloaded (raw=None, received=None).
    """
    imap = connect()
    try:
        if imap.select(_folder(label), readonly=True)[0] != "OK":
            sys.exit(f"Label {label} not found in Gmail.")
        everything = _search(imap, "ALL", senders)
        recent = set(_search(imap, f"SINCE {cutoff.strftime('%d-%b-%Y')}", senders) if cutoff else everything)
        print(f"  {len(everything)} message(s) in {label}: {len(recent)} recent to read", flush=True)
        messages = [{"uid": u, "raw": None, "received": None} for u in everything if u not in recent]
        if recent:
            uid_set = b",".join(sorted(recent, key=int)).decode()
            for part in imap.uid("FETCH", uid_set, "(INTERNALDATE BODY.PEEK[])")[1]:
                if isinstance(part, tuple):
                    uid = re.search(rb"UID (\d+)", part[0]).group(1)
                    messages.append({"uid": uid, "raw": part[1], "received": _received(part)})
        return messages
    finally:
        imap.logout()


def uids(label, before=None):
    """UIDs in the label (all, or only those received before the day 'before'). Nothing is downloaded.
    A label that does not exist returns an empty list."""
    imap = connect()
    try:
        if imap.select(_folder(label), readonly=True)[0] != "OK":
            return []
        criteria = ("BEFORE", before.strftime("%d-%b-%Y")) if before else ("ALL",)
        return imap.uid("SEARCH", None, *criteria)[1][0].split()
    finally:
        imap.logout()


def trash():
    """Name of the Gmail trash folder (it depends on the account language: Trash, Lixeira...)."""
    imap = connect()
    try:
        for line in imap.list()[1]:
            line = line.decode()
            if "\\Trash" in line:
                return line.rsplit(' "/" ', 1)[-1].strip().strip('"')
    finally:
        imap.logout()
    raise SystemExit("Gmail trash folder not found.")


def move(uid_list, source, target, seen):
    """Moves messages from 'source' to 'target' (swaps the label) and marks them read or unread."""
    if not uid_list:
        return
    imap = connect()
    try:
        imap.create(_folder(target))                     # if it already exists, Gmail just answers NO
        if imap.select(_folder(source))[0] != "OK":
            raise SystemExit(f"Label {source} not found in Gmail.")
        uid_set = ",".join(u.decode() for u in uid_list)
        imap.uid("STORE", uid_set, "+FLAGS" if seen else "-FLAGS", "(\\Seen)")
        if imap.uid("COPY", uid_set, _folder(target))[0] != "OK":
            raise SystemExit(f"Could not copy the messages to {target}.")
        imap.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")   # in 'source' this only removes the label
        imap.expunge()
    finally:
        imap.logout()


def create(label, messages):
    """Creates unread messages directly in the label. messages = [(subject, body), ...]."""
    if not messages:
        return
    user = os.environ.get("GMAIL_USER")
    imap = connect()
    try:
        imap.create(_folder(label))
        for subject, body in messages:
            msg = EmailMessage()
            msg["From"] = f"HireLLMe <{user}>"
            msg["To"] = user
            msg["Subject"] = subject
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid(domain="hirellme.local")
            msg.set_content(body)
            if imap.append(_folder(label), None, imaplib.Time2Internaldate(time.time()), msg.as_bytes())[0] != "OK":
                raise SystemExit(f"Could not create the message '{subject}' in {label}.")
    finally:
        imap.logout()