"""IMAP/SMTP tools for email management.

PATCHED 2026-06-25 (Michael / DataHub) — OOM fix for the Railway deployment.
Drop-in replacement for src/icloud_mcp/email.py in the mike-tih/icloud-mcp fork.

What changed (read paths only — no write/SMTP tools added, so apply_readonly_patch
still strips writes unchanged):

  * Chunked fetch  — never fetch more than ICLOUD_FETCH_CHUNK (default 25) messages
    per IMAP round-trip; each chunk is released before the next. Bounds peak memory
    no matter how many IDs the caller asks for. Kills the "60 in one call" /
    "200 in one call" blowups in get_messages() and the search() fallback.

  * Size gate      — for messages larger than ICLOUD_MAX_RAW_BYTES (default 2 MB)
    we fetch HEADERS ONLY (BODY.PEEK[HEADER]) and skip the body, so one
    attachment-laden message can't blow the heap. Those true up via the PST/IMAP
    path. Skipped bodies are flagged "body_truncated": true.

  * Body cap       — returned body_text/body_html truncated to ICLOUD_BODY_CAP
    (default 100 000 chars), matching the local ingest cap.

  * Search fallback capped at ICLOUD_SEARCH_SCAN (default 60, was max(limit*10,200))
    and routed through the same chunked/size-gated fetch.

All limits are env-tunable. Behaviour is otherwise identical (same fields returned,
plus harmless extra keys: "cc", "size", "body_truncated" — ignored by the stager).
"""

import imaplib
import smtplib
import email
import logging
import sys
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from typing import List, Dict, Any, Optional, Iterable, Tuple
from datetime import datetime
from fastmcp import Context
from imapclient import IMAPClient
from .auth import require_auth
from .config import config

# Configure minimal logging (only errors)
logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)

# Log errors to stderr
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.ERROR)
stderr_handler.setFormatter(formatter)
logger.addHandler(stderr_handler)

# ---------------------------------------------------------------------------
# Memory guardrails (env-tunable)
# ---------------------------------------------------------------------------
FETCH_CHUNK = int(os.getenv("ICLOUD_FETCH_CHUNK", "25"))            # msgs per IMAP fetch
BODY_CAP = int(os.getenv("ICLOUD_BODY_CAP", "100000"))             # chars of body returned
MAX_RAW_BYTES = int(os.getenv("ICLOUD_MAX_RAW_BYTES", "2000000"))  # >this -> headers only
SEARCH_FALLBACK_SCAN = int(os.getenv("ICLOUD_SEARCH_SCAN", "60"))  # local-filter scan cap


def _get_imap_client(username: str, password: str) -> IMAPClient:
    """Create IMAP client (stateless)."""
    client = IMAPClient(config.IMAP_SERVER, port=config.IMAP_PORT, ssl=True, use_uid=True)
    client.login(username, password)
    return client


def _close_imap_client(client: IMAPClient) -> None:
    """Safely close IMAP client connection."""
    try:
        # Don't call logout() - it causes "file property has no setter" error in Python 3.14+
        # Just close the underlying socket
        if hasattr(client, '_imap') and hasattr(client._imap, 'sock'):
            client._imap.sock.close()
    except Exception as _e:
        pass  # Silently ignore errors on close


def _get_smtp_client(username: str, password: str) -> smtplib.SMTP:
    """Create SMTP client (stateless)."""
    client = smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT)
    client.starttls()
    client.login(username, password)
    return client


def _decode_mime_header(header_value: str) -> str:
    """Decode MIME encoded email header."""
    if not header_value:
        return ""

    decoded_parts = decode_header(header_value)
    result = []

    for content, charset in decoded_parts:
        if isinstance(content, bytes):
            try:
                result.append(content.decode(charset or 'utf-8', errors='ignore'))
            except Exception as _e:
                result.append(content.decode('utf-8', errors='ignore'))
        else:
            result.append(str(content))

    return ' '.join(result)


# ---------------------------------------------------------------------------
# Bounded-memory fetch helpers (the OOM fix)
# ---------------------------------------------------------------------------
def _chunks(seq: List[Any], n: int) -> Iterable[List[Any]]:
    for i in range(0, len(seq), max(1, n)):
        yield seq[i:i + n]


def _extract_raw(data: Dict[Any, Any]) -> Optional[bytes]:
    """Pull the raw RFC822 (or header) bytes out of an imapclient fetch row."""
    for key in [b'BODY[]', 'BODY[]', b'RFC822', 'RFC822', b'BODY[HEADER]', b'BODY.PEEK[]']:
        if key in data:
            return data[key]
    return None


def _fetch_rows(
    client: IMAPClient,
    message_ids: Iterable[int],
    include_body: bool = True,
) -> Iterable[Tuple[int, Dict[Any, Any], bytes, bool]]:
    """Yield (msg_id, meta, raw_bytes, body_skipped) in bounded-memory chunks.

    Peak memory is capped at roughly FETCH_CHUNK * MAX_RAW_BYTES, regardless of how
    many ids are requested. Messages over MAX_RAW_BYTES yield header-only bytes with
    body_skipped=True so attachments are never pulled into RAM just to be discarded.
    """
    ids = list(message_ids)
    for chunk in _chunks(ids, FETCH_CHUNK):
        meta = client.fetch(chunk, [b'FLAGS', b'RFC822.SIZE'])
        if include_body:
            small, big = [], []
            for m in chunk:
                size = (meta.get(m, {}) or {}).get(b'RFC822.SIZE', 0) or 0
                (small if size <= MAX_RAW_BYTES else big).append(m)
            bodies = client.fetch(small, [b'BODY.PEEK[]']) if small else {}
            heads = client.fetch(big, [b'BODY.PEEK[HEADER]']) if big else {}
            for m in chunk:
                if m in bodies:
                    raw = _extract_raw(bodies[m])
                    if raw is not None:
                        yield m, meta.get(m, {}), raw, False
                        continue
                if m in heads:
                    raw = _extract_raw(heads[m])
                    if raw is not None:
                        yield m, meta.get(m, {}), raw, True
            bodies = heads = None
        else:
            heads = client.fetch(chunk, [b'BODY.PEEK[HEADER]'])
            for m in chunk:
                if m in heads:
                    raw = _extract_raw(heads[m])
                    if raw is not None:
                        yield m, meta.get(m, {}), raw, False
            heads = None
        meta = None


def _parse_message(
    msg_id: int,
    data: Dict[Any, Any],
    raw: bytes,
    folder: str,
    include_body: bool = True,
    full_html: bool = False,
    headers_only: bool = False,
) -> Dict[str, Any]:
    """Build the message dict from raw bytes + fetch metadata (flags/size)."""
    msg = email.message_from_bytes(raw)
    flags = (data or {}).get(b'FLAGS', (data or {}).get('FLAGS', []))
    size = (data or {}).get(b'RFC822.SIZE')

    result: Dict[str, Any] = {
        "id": str(msg_id),
        "subject": _decode_mime_header(msg.get('Subject', '')),
        "from": _decode_mime_header(msg.get('From', '')),
        "to": _decode_mime_header(msg.get('To', '')),
        "cc": _decode_mime_header(msg.get('Cc', '')),
        "date": msg.get('Date', ''),
        "flags": [f.decode() if isinstance(f, bytes) else f for f in flags],
        "folder": folder,
    }
    if size is not None:
        result["size"] = size

    if include_body and not headers_only:
        body_text = ""
        body_html = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain" and not body_text:
                    try:
                        body_text = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except Exception as _e:
                        pass
                elif content_type == "text/html" and full_html and not body_html:
                    try:
                        body_html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except Exception as _e:
                        pass
        else:
            try:
                body_text = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            except Exception as _e:
                pass
        result["body_text"] = body_text[:BODY_CAP]
        if full_html:
            result["body_html"] = body_html[:BODY_CAP]
    elif include_body and headers_only:
        # message exceeded MAX_RAW_BYTES -> body intentionally skipped
        result["body_text"] = ""
        result["body_truncated"] = True

    return result


async def list_folders(context: Context) -> List[Dict[str, Any]]:
    """
    List all email folders/mailboxes.

    Returns:
        List of folders with name and flags
    """
    try:
        username, password = require_auth(context)

        client = _get_imap_client(username, password)

        folders = client.list_folders()

        result = []
        for flags, delimiter, name in folders:
            result.append({
                "name": name,
                "flags": [flag.decode() if isinstance(flag, bytes) else flag for flag in flags],
                "delimiter": delimiter
            })

        return result
    except Exception as _e:
        raise
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass


async def list_messages(
    context: Context,
    folder: str = "INBOX",
    limit: int = 50,
    unread_only: bool = False
) -> List[Dict[str, Any]]:
    """
    List messages in a folder.

    Args:
        folder: Folder name (default: INBOX)
        limit: Maximum number of messages to return
        unread_only: Only return unread messages
    """
    try:
        username, password = require_auth(context)

        client = _get_imap_client(username, password)

        client.select_folder(folder)

        # Search for messages
        if unread_only:
            messages = client.search(['UNSEEN'])
        else:
            messages = client.search(['ALL'])

        # Get most recent messages
        message_ids = list(messages)[-limit:] if len(messages) > limit else list(messages)
        message_ids.reverse()  # Most recent first

        if not message_ids:
            return []

        # Bounded-memory chunked fetch (was: single client.fetch of all ids)
        result = []
        for msg_id, data, raw, skipped in _fetch_rows(client, message_ids, include_body=True):
            try:
                result.append(_parse_message(msg_id, data, raw, folder,
                                              include_body=True, full_html=False,
                                              headers_only=skipped))
            except Exception as _e:
                continue

        return result

    except Exception as _e:
        raise
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass


async def get_message(
    context: Context,
    message_id: str,
    folder: str = "INBOX",
    include_body: bool = True,
    full_html: bool = False
) -> Dict[str, Any]:
    """
    Get a specific message with full details.

    Args:
        message_id: Message ID
        folder: Folder name (default: INBOX)
        include_body: Include message body content
        full_html: Include full HTML body (default: False, only text body returned)

    Returns:
        Complete message details
    """
    try:
        username, password = require_auth(context)
        client = _get_imap_client(username, password)

        client.select_folder(folder)

        msg_id = int(message_id)

        rows = list(_fetch_rows(client, [msg_id], include_body=include_body))
        if not rows:
            raise ValueError(f"Message {message_id} not found")

        mid, data, raw, skipped = rows[0]
        return _parse_message(mid, data, raw, folder,
                              include_body=include_body, full_html=full_html,
                              headers_only=skipped)

    except Exception as _e:
        raise
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass


async def get_messages(
    context: Context,
    message_ids: List[str],
    folder: str = "INBOX",
    include_body: bool = True,
    full_html: bool = False
) -> List[Dict[str, Any]]:
    """
    Get multiple messages at once (bulk fetch) — bounded memory.

    Args:
        message_ids: List of message IDs to fetch
        folder: Folder name (default: INBOX)
        include_body: Include message body content
        full_html: Include full HTML body (default: False, only text body returned)

    Returns:
        List of message details
    """
    try:
        username, password = require_auth(context)
        client = _get_imap_client(username, password)

        client.select_folder(folder)

        # Convert string IDs to integers
        msg_ids = [int(mid) for mid in message_ids]

        # Bounded-memory chunked fetch (was: single client.fetch of ALL ids at once)
        results = []
        for mid, data, raw, skipped in _fetch_rows(client, msg_ids, include_body=include_body):
            try:
                results.append(_parse_message(mid, data, raw, folder,
                                               include_body=include_body, full_html=full_html,
                                               headers_only=skipped))
            except Exception as _e:
                continue

        return results

    except Exception as _e:
        raise
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass


async def search_messages(
    context: Context,
    query: str,
    folder: str = "INBOX",
    limit: int = 50
) -> List[Dict[str, Any]]:
    """
    Search for messages by text query.

    Args:
        query: Search text (searches subject and from fields)
        folder: Folder name (default: INBOX)
        limit: Maximum number of results

    Returns:
        List of matching messages
    """
    username, password = require_auth(context)
    client = _get_imap_client(username, password)

    try:
        client.select_folder(folder)

        # Try server-side search with UTF-8 charset (RFC 2978).
        try:
            messages = client.search(
                ['OR', ['SUBJECT', query], ['FROM', query]],
                charset='UTF-8'
            )

            message_ids = list(messages)[-limit:] if len(messages) > limit else list(messages)
            message_ids.reverse()

            if not message_ids:
                return []

            result = []
            for msg_id, data, raw, skipped in _fetch_rows(client, message_ids, include_body=True):
                try:
                    result.append(_parse_message(msg_id, data, raw, folder,
                                                  include_body=True, full_html=False,
                                                  headers_only=skipped))
                except Exception as _e:
                    continue

            return result

        except Exception as charset_error:
            # Fallback: server rejected CHARSET UTF-8 -> local filter.
            # CAPPED scan (was max(limit*10, 200)) + chunked/size-gated fetch so this
            # path can no longer pull hundreds of full messages into RAM at once.
            logger.error(f"Server-side UTF-8 search failed: {charset_error}. Falling back to local filtering.")

            fetch_limit = max(limit, SEARCH_FALLBACK_SCAN)

            all_msg_ids = client.search(['ALL'])
            message_ids = list(all_msg_ids)[-fetch_limit:] if len(all_msg_ids) > fetch_limit else list(all_msg_ids)
            message_ids.reverse()

            if not message_ids:
                return []

            all_messages = []
            for msg_id, data, raw, skipped in _fetch_rows(client, message_ids, include_body=True):
                try:
                    all_messages.append(_parse_message(msg_id, data, raw, folder,
                                                        include_body=True, full_html=False,
                                                        headers_only=skipped))
                except Exception as _e:
                    continue

            query_lower = query.lower()
            filtered_messages = [
                msg for msg in all_messages
                if query_lower in msg.get("subject", "").lower()
                or query_lower in msg.get("from", "").lower()
                or query_lower in msg.get("to", "").lower()
            ]

            return filtered_messages[:limit]

    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass


# ===========================================================================
# WRITE / DESTRUCTIVE TOOLS BELOW — unchanged from upstream.
# Your read-only build (apply_readonly_patch.py / server.py) does not register
# these, so Cowork cannot call them. Left in place for drop-in compatibility.
# ===========================================================================

async def send_message(
    context: Context,
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    html: bool = False
) -> Dict[str, str]:
    """Send an email message via SMTP."""
    username, password = require_auth(context)

    msg = MIMEMultipart('alternative') if html else MIMEText(body)

    msg['From'] = username
    msg['To'] = to
    msg['Subject'] = subject

    if cc:
        msg['Cc'] = cc
    if bcc:
        msg['Bcc'] = bcc

    if html:
        msg.attach(MIMEText(body, 'html'))

    with _get_smtp_client(username, password) as client:
        recipients = [to]
        if cc:
            recipients.extend([addr.strip() for addr in cc.split(',')])
        if bcc:
            recipients.extend([addr.strip() for addr in bcc.split(',')])

        client.send_message(msg, from_addr=username, to_addrs=recipients)

    imap_client = None
    try:
        imap_client = _get_imap_client(username, password)

        if 'Date' not in msg:
            from email.utils import formatdate
            msg['Date'] = formatdate(localtime=True)

        msg_bytes = msg.as_bytes()

        try:
            imap_client.append(config.SENT_FOLDER, msg_bytes, flags=['\\Seen'])
        except Exception as e:
            for folder_name in ['Sent', 'Sent Items', config.SENT_FOLDER]:
                try:
                    imap_client.append(folder_name, msg_bytes, flags=['\\Seen'])
                    break
                except Exception:
                    continue
            else:
                logger.error(f"Could not save to Sent folder: {e}")

    except Exception as e:
        logger.error(f"Error saving to Sent folder: {e}")

    finally:
        if imap_client:
            _close_imap_client(imap_client)

    return {
        "status": "success",
        "message": f"Email sent to {to}"
    }


async def move_message(
    context: Context,
    message_id: str,
    from_folder: str,
    to_folder: str
) -> Dict[str, str]:
    """Move a message to another folder."""
    username, password = require_auth(context)

    client = _get_imap_client(username, password)

    try:
        client.select_folder(from_folder)
        msg_id = int(message_id)

        client.copy([msg_id], to_folder)

        client.delete_messages([msg_id])
        client.expunge()

        return {
            "status": "success",
            "message": f"Message {message_id} moved from {from_folder} to {to_folder}"
        }
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass


async def delete_message(
    context: Context,
    message_id: str,
    folder: str = "INBOX",
    permanent: bool = False
) -> Dict[str, str]:
    """Delete a message."""
    username, password = require_auth(context)

    client = _get_imap_client(username, password)

    try:
        client.select_folder(folder)
        msg_id = int(message_id)

        if permanent:
            client.delete_messages([msg_id])
            client.expunge()
            message = f"Message {message_id} permanently deleted"
        else:
            try:
                client.copy([msg_id], 'Trash')
                client.delete_messages([msg_id])
                client.expunge()
                message = f"Message {message_id} moved to Trash"
            except Exception as _e:
                client.delete_messages([msg_id])
                client.expunge()
                message = f"Message {message_id} deleted"

        return {
            "status": "success",
            "message": message
        }
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass


async def mark_as_read(
    context: Context,
    message_id: str,
    folder: str = "INBOX"
) -> Dict[str, str]:
    """Mark a message as read."""
    username, password = require_auth(context)

    client = _get_imap_client(username, password)

    try:
        client.select_folder(folder)
        msg_id = int(message_id)
        client.add_flags([msg_id], ['\\Seen'])

        return {
            "status": "success",
            "message": f"Message {message_id} marked as read"
        }
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass


async def mark_as_unread(
    context: Context,
    message_id: str,
    folder: str = "INBOX"
) -> Dict[str, str]:
    """Mark a message as unread."""
    username, password = require_auth(context)

    client = _get_imap_client(username, password)

    try:
        client.select_folder(folder)
        msg_id = int(message_id)
        client.remove_flags([msg_id], ['\\Seen'])

        return {
            "status": "success",
            "message": f"Message {message_id} marked as unread"
        }
    finally:
        try:
            _close_imap_client(client)
        except Exception as _e:
            pass
