"""Optional live connector: pull reservation emails from Gmail into landing/email.

Uses IMAP with a Gmail *app password* read from environment variables
(GMAIL_USER, GMAIL_APP_PASSWORD) — never hard-code credentials. Only messages
under the configured label (default "Reservations") are fetched, and the last
seen UID is kept in landing/.gmail_state so each run is incremental.

Set up a Gmail filter that applies the "Reservations" label to mail from
resy.com, opentable.com and your website form sender.
"""
import imaplib
import os

STATE_FILE = ".gmail_state"


def fetch_new(landing_email_dir, label=None):
    user, pw = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        return 0
    label = label or os.environ.get("GMAIL_LABEL", "Reservations")
    os.makedirs(landing_email_dir, exist_ok=True)
    state_path = os.path.join(os.path.dirname(landing_email_dir), STATE_FILE)
    last_uid = int(open(state_path).read().strip()) if os.path.exists(state_path) else 0

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(user, pw)
    imap.select(f'"{label}"', readonly=True)
    _, data = imap.uid("search", None, f"UID {last_uid + 1}:*")
    uids = [int(u) for u in data[0].split() if int(u) > last_uid]
    for uid in uids:
        _, msg = imap.uid("fetch", str(uid), "(RFC822)")
        with open(os.path.join(landing_email_dir, f"gmail_{uid}.eml"), "wb") as f:
            f.write(msg[0][1])
    imap.logout()
    if uids:
        with open(state_path, "w") as f:
            f.write(str(max(uids)))
    return len(uids)
