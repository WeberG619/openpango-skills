#!/usr/bin/env python3
"""
cli.py - Command-line interface for the OpenPango email skill.

All output goes to stdout as JSON. Logs go to stderr.

Usage:
    python3 skills/email/cli.py read [--account NAME] [--folder INBOX] [--limit 50]
    python3 skills/email/cli.py send --to ADDR --subject SUBJ --body BODY [--cc] [--bcc] [--attach FILE]
    python3 skills/email/cli.py reply --message-id ID --body BODY [--reply-all]
    python3 skills/email/cli.py threads [--limit 20]
    python3 skills/email/cli.py thread MESSAGE_ID
    python3 skills/email/cli.py search QUERY [--account NAME] [--folder INBOX]
    python3 skills/email/cli.py accounts
    python3 skills/email/cli.py accounts add --name NAME --imap-host H --smtp-host H --username U --password P
    python3 skills/email/cli.py accounts delete --name NAME
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Allow running from project root via: python3 skills/email/cli.py
_SKILLS_ROOT = Path(__file__).parent.parent.parent
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from skills.email.credentials import CredentialStore
from skills.email.email_handler import EmailHandler
from skills.email.thread_tracker import ThreadTracker

# Logs to stderr so stdout stays clean JSON
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s [%(name)s]: %(message)s",
    stream=sys.stderr,
)


def _out(data):
    """Print *data* as pretty JSON to stdout."""
    print(json.dumps(data, indent=2, default=str))


def _load_handler(account_name: str) -> EmailHandler:
    """Load credentials and return a connected EmailHandler."""
    store = CredentialStore()
    config = store.account_to_handler_config(account_name)
    if not config:
        # Fall back to environment variables
        config = _config_from_env()
    if not config:
        print(
            json.dumps({"error": f"Account '{account_name}' not found and no env vars set."}),
            file=sys.stderr,
        )
        sys.exit(1)
    return EmailHandler(config)


def _config_from_env() -> dict:
    imap_host = os.getenv("IMAP_HOST", "")
    smtp_host = os.getenv("SMTP_HOST", "")
    username = os.getenv("IMAP_USER") or os.getenv("SMTP_USER", "")
    password = os.getenv("IMAP_PASS") or os.getenv("SMTP_PASS", "")
    if not imap_host or not smtp_host or not username:
        return {}
    return {
        "imap": {
            "host": imap_host,
            "port": int(os.getenv("IMAP_PORT", "993")),
            "use_ssl": True,
        },
        "smtp": {
            "host": smtp_host,
            "port": int(os.getenv("SMTP_PORT", "587")),
            "use_tls": True,
        },
        "username": username,
        "password": password,
    }


# ------------------------------------------------------------------ #
#  Sub-command handlers                                                 #
# ------------------------------------------------------------------ #

def cmd_read(args):
    with _load_handler(args.account) as handler:
        messages = handler.fetch_unread(folder=args.folder, limit=args.limit)
    _out({"count": len(messages), "messages": messages})


def cmd_send(args):
    attachments = [Path(p) for p in args.attach] if args.attach else []
    cc = args.cc or []
    bcc = args.bcc or []
    with _load_handler(args.account) as handler:
        result = handler.send(
            to=args.to,
            subject=args.subject,
            body=args.body,
            cc=cc,
            bcc=bcc,
            attachments=attachments,
            html=args.html,
        )
    _out(result)


def cmd_reply(args):
    with _load_handler(args.account) as handler:
        result = handler.reply(
            original_message_id=args.message_id,
            body=args.body,
            reply_all=args.reply_all,
        )
    _out(result)


def cmd_threads(args):
    tracker = ThreadTracker()
    threads = tracker.get_threads(limit=args.limit)
    _out({"count": len(threads), "threads": threads})


def cmd_thread(args):
    tracker = ThreadTracker()
    messages = tracker.get_thread(args.message_id)
    _out({"count": len(messages), "messages": messages})


def cmd_search(args):
    with _load_handler(args.account) as handler:
        results = handler.search(args.query, folder=args.folder)
    _out({"count": len(results), "messages": results})


def cmd_accounts_list(args):
    store = CredentialStore()
    accounts = store.list_accounts()
    _out({"accounts": accounts})


def cmd_accounts_add(args):
    store = CredentialStore()
    store.save_account(
        name=args.name,
        imap_host=args.imap_host,
        imap_port=args.imap_port,
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        username=args.username,
        password=args.password,
    )
    _out({"status": "saved", "account": args.name})


def cmd_accounts_delete(args):
    store = CredentialStore()
    deleted = store.delete_account(args.name)
    if deleted:
        _out({"status": "deleted", "account": args.name})
    else:
        _out({"status": "error", "error": f"Account '{args.name}' not found"})
        sys.exit(1)


# ------------------------------------------------------------------ #
#  Argument parser                                                      #
# ------------------------------------------------------------------ #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openpango email",
        description="OpenPango native email management (IMAP/SMTP)",
    )
    parser.add_argument(
        "--account", "-a",
        default="default",
        metavar="NAME",
        help="Credential account name (default: 'default')",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # --- read ---
    p_read = sub.add_parser("read", help="Fetch unread messages")
    p_read.add_argument("--folder", default="INBOX")
    p_read.add_argument("--limit", type=int, default=50)
    p_read.set_defaults(func=cmd_read)

    # --- send ---
    p_send = sub.add_parser("send", help="Send a message")
    p_send.add_argument("--to", required=True)
    p_send.add_argument("--subject", required=True)
    p_send.add_argument("--body", required=True)
    p_send.add_argument("--cc", nargs="*", default=[])
    p_send.add_argument("--bcc", nargs="*", default=[])
    p_send.add_argument("--attach", nargs="*", default=[], metavar="FILE")
    p_send.add_argument("--html", action="store_true", help="Send body as HTML")
    p_send.set_defaults(func=cmd_send)

    # --- reply ---
    p_reply = sub.add_parser("reply", help="Reply to a message")
    p_reply.add_argument("--message-id", required=True, dest="message_id")
    p_reply.add_argument("--body", required=True)
    p_reply.add_argument("--reply-all", action="store_true", dest="reply_all")
    p_reply.set_defaults(func=cmd_reply)

    # --- threads ---
    p_threads = sub.add_parser("threads", help="List recent thread summaries")
    p_threads.add_argument("--limit", type=int, default=20)
    p_threads.set_defaults(func=cmd_threads)

    # --- thread ---
    p_thread = sub.add_parser("thread", help="Show full thread by Message-ID")
    p_thread.add_argument("message_id", metavar="MESSAGE_ID")
    p_thread.set_defaults(func=cmd_thread)

    # --- search ---
    p_search = sub.add_parser("search", help="Search messages")
    p_search.add_argument("query", metavar="QUERY")
    p_search.add_argument("--folder", default="INBOX")
    p_search.set_defaults(func=cmd_search)

    # --- accounts ---
    p_accounts = sub.add_parser("accounts", help="Manage saved accounts")
    acc_sub = p_accounts.add_subparsers(dest="accounts_cmd")

    # accounts list (default)
    p_acc_list = acc_sub.add_parser("list", help="List saved accounts")
    p_acc_list.set_defaults(func=cmd_accounts_list)

    # accounts add
    p_acc_add = acc_sub.add_parser("add", help="Add or update an account")
    p_acc_add.add_argument("--name", required=True)
    p_acc_add.add_argument("--imap-host", required=True, dest="imap_host")
    p_acc_add.add_argument("--imap-port", type=int, default=993, dest="imap_port")
    p_acc_add.add_argument("--smtp-host", required=True, dest="smtp_host")
    p_acc_add.add_argument("--smtp-port", type=int, default=587, dest="smtp_port")
    p_acc_add.add_argument("--username", required=True)
    p_acc_add.add_argument("--password", required=True)
    p_acc_add.set_defaults(func=cmd_accounts_add)

    # accounts delete
    p_acc_del = acc_sub.add_parser("delete", help="Delete an account")
    p_acc_del.add_argument("--name", required=True)
    p_acc_del.set_defaults(func=cmd_accounts_delete)

    # Default 'accounts' with no sub-command → list
    p_accounts.set_defaults(func=lambda a: (
        cmd_accounts_list(a) if not a.accounts_cmd else None
    ))

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
