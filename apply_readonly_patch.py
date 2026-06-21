#!/usr/bin/env python3
"""
Read-only fork patch for mike-tih/icloud-mcp.

Neutralizes the @mcp.tool() decorator on every WRITE / SMTP tool so they are
never registered with FastMCP. The function bodies are left intact (so the file
stays valid and the diff is small/auditable); they simply become plain,
un-exposed functions.

Idempotent: re-running is a no-op once the decorators are already disabled.
Run during the Docker build BEFORE `pip install .` so the installed package
reflects the patched source.

Usage:
    python apply_readonly_patch.py            # patch
    python apply_readonly_patch.py --verify   # patch + fail build if anything is wrong

Exit non-zero if any write tool is still registered, or if an expected read
tool went missing (catches upstream drift in a future fork sync).
"""
import os
import re
import sys

# iCloud app-specific passwords are read+write with NO scope. Read-only MUST be
# enforced here, in server code — Apple gives no read-only credential.
WRITE_TOOLS = [
    # email writes + SMTP send
    "email_send", "email_move", "email_delete", "email_mark_read", "email_mark_unread",
    # calendar writes (CalDAV)
    "calendar_create_event", "calendar_update_event", "calendar_delete_event",
    # contacts writes (CardDAV)
    "contacts_create", "contacts_update", "contacts_delete",
]

# The 11 tools that must remain after patching.
READ_TOOLS = [
    "calendar_list_calendars", "calendar_list_events", "calendar_search_events",
    "contacts_list", "contacts_get", "contacts_search",
    "email_list_folders", "email_list_messages", "email_get_message",
    "email_get_messages", "email_search",
]

CANDIDATES = [
    "src/icloud_mcp/server.py",
    "icloud_mcp/server.py",
    "server.py",
]


def find_server():
    for p in CANDIDATES:
        if os.path.isfile(p):
            return p
    sys.exit("ERROR: could not locate server.py (looked in %s)" % CANDIDATES)


def patch(text):
    disabled = []
    for name in WRITE_TOOLS:
        # Match the @mcp.tool() decorator immediately preceding this specific
        # async def. Keyed to the unique function name so it can't hit the wrong
        # tool. \1 preserves any indentation (top-level => empty).
        pat = re.compile(r"@mcp\.tool\(\)\n(\s*)(async def %s\()" % re.escape(name))
        new, n = pat.subn(
            r"# [READ-ONLY FORK] write tool disabled: %s\n\1\2" % name, text
        )
        if n:
            text = new
            disabled.append(name)
    return text, disabled


def registered_tools(text):
    return re.findall(r"@mcp\.tool\(\)\n\s*async def (\w+)\(", text)


def main():
    verify = "--verify" in sys.argv
    path = find_server()

    with open(path, encoding="utf-8") as fh:
        original = fh.read()

    patched, disabled = patch(original)
    if patched != original:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(patched)

    remaining = set(registered_tools(patched))
    leaked = [t for t in WRITE_TOOLS if t in remaining]
    missing_reads = [t for t in READ_TOOLS if t not in remaining]

    print("Patched file      : %s" % path)
    print("Disabled this run : %s" % (", ".join(disabled) or "(none - already read-only)"))
    print("Tools registered  : %d -> %s" % (len(remaining), ", ".join(sorted(remaining))))

    ok = True
    if leaked:
        print("FAIL: write tools STILL registered: %s" % leaked)
        ok = False
    if missing_reads:
        print("FAIL: expected read tools missing (did upstream change?): %s" % missing_reads)
        ok = False

    if not ok:
        sys.exit(1)

    print("OK: read-only enforced - %d read tools exposed, 0 write/SMTP tools." % len(remaining))
    if verify and len(remaining) != len(READ_TOOLS):
        sys.exit("FAIL(--verify): expected exactly %d tools, found %d" % (len(READ_TOOLS), len(remaining)))


if __name__ == "__main__":
    main()
