"""
the actual cloning engine — iterates source channel messages,
downloads media, re-uploads to dest, deletes local files, tracks everything.
uses FastTelethon for parallel downloads/uploads on big files.
"""

import asyncio
import os
import shutil
import logging
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from telethon import TelegramClient, utils
from telethon.errors.rpcerrorlist import (
    FileReferenceExpiredError, FloodWaitError,
    MessageIdInvalidError, ChatForwardsRestrictedError,
)
from telethon.tl.types import (
    MessageService,
    MessageMediaPhoto, MessageMediaDocument,
    InputMediaUploadedDocument, InputMediaUploadedPhoto,
)

from config import DOWNLOAD_DIR
from tracker import CloneTracker
from fast_telethon import download_file, upload_file

log = logging.getLogger("cloner")

# files above this size use parallel transfer (5 MB)
FAST_TRANSFER_THRESHOLD = 5 * 1024 * 1024
MAX_RETRY_DELAY = 300.0  # seconds, cap for exponential backoff
RETRY_JITTER_PCT = 0.2   # +/- 20%
FREE_UPLOAD_LIMIT = 2 * 1024 * 1024 * 1024
PREMIUM_UPLOAD_LIMIT = 4 * 1024 * 1024 * 1024


async def _tracker_call(tracker: CloneTracker, method: str, *args, **kwargs):
    async_method = getattr(tracker, f"a{method}", None)
    if async_method is not None:
        return await async_method(*args, **kwargs)
    return getattr(tracker, method)(*args, **kwargs)


def _media_type(message) -> str | None:
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.audio:
        return "audio"
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.sticker:
        return "sticker"
    if message.gif:
        return "gif"
    if message.document:
        return "document"
    return None


def _file_size_from_message(message) -> int:
    """try to get file size from message media."""
    if message.document:
        return message.document.size or 0
    if message.photo:
        # photos are usually small, pick the largest size
        if hasattr(message.photo, "sizes") and message.photo.sizes:
            for size in reversed(message.photo.sizes):
                if hasattr(size, "size"):
                    return size.size
    return 0


def _human_size(nbytes: int) -> str:
    """turn byte count into something readable."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


async def _get_upload_limit(client: TelegramClient) -> tuple[int, bool]:
    me = await client.get_me()
    is_premium = bool(getattr(me, "premium", False))
    return (PREMIUM_UPLOAD_LIMIT if is_premium else FREE_UPLOAD_LIMIT), is_premium


def _guess_filename(message) -> str:
    pre_name = "media"
    if message.document and hasattr(message.document, "attributes"):
        for attr in message.document.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                pre_name = attr.file_name
                break
    return pre_name


def _should_skip_over_limit(message, limit_bytes: int) -> tuple[bool, int, str]:
    size_bytes = _file_size_from_message(message)
    if size_bytes and limit_bytes and size_bytes > limit_bytes:
        return True, size_bytes, _guess_filename(message)
    return False, size_bytes, _guess_filename(message)


def _cleanup_download_dir(dir_path: str) -> int:
    """remove leftover files from a previous run. returns number removed."""
    if not os.path.isdir(dir_path):
        return 0
    removed = 0
    for name in os.listdir(dir_path):
        path = os.path.join(dir_path, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
        except Exception as exc:
            log.warning(f"couldn't remove leftover download {path!r}: {exc}")
    return removed


def _cleanup_new_downloads(before: set[str], dir_path: str) -> None:
    """remove any new files created during a failed download."""
    if not os.path.isdir(dir_path):
        return
    for name in os.listdir(dir_path):
        if name in before:
            continue
        path = os.path.join(dir_path, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            log.debug(f"deleted partial file: {name}")
        except Exception as exc:
            log.warning(f"couldn't remove partial download {path!r}: {exc}")


def _clean_channel_id(cid):
    if isinstance(cid, str):
        s = cid.strip()
        if s.startswith("-100"):
            s = s[4:]
        try:
            return int(s)
        except ValueError:
            return cid
    return cid

async def clone_channel(
    client: TelegramClient,
    source,
    dest,
    tracker: CloneTracker,
    rate_limit_delay: float = 2.0,
    progress_callback: Callable[[dict], None] | None = None,
    stop_event: asyncio.Event | None = None,
    max_retries: int = 3,
    retry_delay: float = 5.0,
    follow: bool = True,
    follow_poll_interval: float = 5.0,
    skip_history: bool = False,
    largest_first: bool = True,
    source_topic_id: int | None = None,
    dest_topic_id: int | None = None,
    mode: str = "forward",
    drop_author: bool = True,
    max_messages: int | None = None,
    batch_delay: float = 5.0,
):
    """clone all messages from source channel to dest channel."""

    if mode == "reupload":
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        removed = _cleanup_download_dir(DOWNLOAD_DIR)
        if removed:
            log.info(f"cleared {removed} leftover downloads before starting")

    source_entity = await client.get_entity(_clean_channel_id(source))
    dest_entity = await client.get_entity(_clean_channel_id(dest))
    source_id = source_entity.id

    upload_limit, is_premium = await _get_upload_limit(client)
    log.info(
        "account tier: %s | upload limit: %s",
        "premium" if is_premium else "free",
        _human_size(upload_limit),
    )

    log.info(f"source: {getattr(source_entity, 'title', source)} (id: {source_id})")
    log.info(f"dest:   {getattr(dest_entity, 'title', dest)}")
    log.info(f"cloning mode: {mode} (drop_author={drop_author}, batch_delay={batch_delay}s)")
    if source_topic_id is not None:
        log.info(f"source topic ID filter: {source_topic_id}")
    if dest_topic_id is not None:
        log.info(f"dest topic ID target: {dest_topic_id}")
    if max_messages is not None:
        log.info(f"batch message limit: {max_messages}")

    # grab total message count before iterating
    get_msg_kwargs = {}
    if source_topic_id is not None:
        get_msg_kwargs["reply_to"] = source_topic_id

    history = await client.get_messages(source_entity, limit=0, **get_msg_kwargs)
    total_messages = history.total or 0
    log.info(f"total messages in source: {total_messages}")

    stats = {
        "cloned": 0,
        "skipped": 0,
        "skipped_over_limit": 0,
        "failed": 0,
        "processed": 0,
        "total": total_messages,
        "current_msg": None,
        "file_progress": None,
        "status": "running",
        "failed_ids": [],
        "last_error": None,
        "last_error_msg_id": None,
        "last_error_at": None,
        "last_error_attempt": None,
        "last_error_wait": None,
        "last_skip": None,
        "last_skip_at": None,
        "last_skip_msg_id": None,
        "last_skip_reason": None,
        "last_skip_filename": None,
        "last_skip_size": None,
        "last_skip_size_human": None,
        "last_skip_limit": None,
        "last_skip_limit_human": None,
        "upload_limit": upload_limit,
        "upload_limit_human": _human_size(upload_limit),
        "is_premium": is_premium,
    }

    last_processed_id = 0
    BATCH_SIZE = 100
    pending_batch = []

    async def _flush_batch():
        nonlocal pending_batch
        if not pending_batch:
            return True
        to_send = pending_batch[:]
        pending_batch.clear()

        await _forward_batch(
            client, to_send, source_entity, dest_entity, tracker, source_id,
            stats, progress_callback, dest_topic_id=dest_topic_id, drop_author=drop_author
        )
        if batch_delay > 0:
            log.info(f"batch sent ({stats['cloned']} cloned) — sleeping {batch_delay}s to prevent rate limits & ban...")
            await asyncio.sleep(batch_delay)
        return True

    async def _process_message(message):
        nonlocal last_processed_id

        if stop_event and stop_event.is_set():
            if pending_batch:
                await _flush_batch()
            stats["status"] = "stopped"
            log.info("clone stopped by user")
            return False

        stats["status"] = "running"
        msg_id = getattr(message, "id", None)
        if not msg_id or isinstance(message, MessageService):
            stats["skipped"] += 1
            log.info(f"msg #{msg_id or 'service'} skipped (service message / system notification)")
            if progress_callback:
                progress_callback(stats.copy())
            return True

        last_processed_id = max(last_processed_id, msg_id)
        stats["current_msg"] = msg_id
        stats["processed"] += 1
        stats["file_progress"] = None

        if await _tracker_call(tracker, "is_cloned", source_id, msg_id):
            stats["skipped"] += 1
            if progress_callback:
                progress_callback(stats.copy())
            return True

        if mode == "forward":
            pending_batch.append(message)
            if len(pending_batch) >= BATCH_SIZE:
                await _flush_batch()

            if max_messages is not None and (stats["cloned"] + len(pending_batch)) >= max_messages:
                await _flush_batch()
                log.info(f"reached maximum batch limit of {max_messages} messages — stopping run")
                stats["status"] = "completed"
                return False

            return True

        if mode == "reupload":
            should_skip, size_bytes, pre_name = _should_skip_over_limit(message, upload_limit)
            if should_skip:
                stats["skipped_over_limit"] += 1
                stats["last_skip"] = "over_limit"
                stats["last_skip_at"] = datetime.now(timezone.utc).isoformat()
                stats["last_skip_msg_id"] = msg_id
                stats["last_skip_reason"] = "file too large"
                stats["last_skip_filename"] = pre_name
                stats["last_skip_size"] = size_bytes
                stats["last_skip_size_human"] = _human_size(size_bytes)
                stats["last_skip_limit"] = upload_limit
                stats["last_skip_limit_human"] = _human_size(upload_limit)
                try:
                    await _tracker_call(
                        tracker,
                        "mark_skipped",
                        source_id,
                        msg_id,
                        reason="file too large",
                        file_size=size_bytes,
                        limit_bytes=upload_limit,
                        filename=pre_name,
                        media_type=_media_type(message),
                    )
                except Exception as mark_exc:
                    log.warning(f"failed to record skipped msg #{msg_id}: {mark_exc}")
                log.warning(
                    "msg #%s skipped: %s exceeds limit %s",
                    msg_id,
                    _human_size(size_bytes),
                    _human_size(upload_limit),
                )
                if progress_callback:
                    progress_callback(stats.copy())
                return True

            if max_messages is not None and stats["cloned"] >= max_messages:
                log.info(f"reached maximum batch limit of {max_messages} messages — stopping run")
                stats["status"] = "completed"
                return False

        success, error_reason = await _try_clone_with_retry(
            client, message, source_entity, dest_entity, tracker, source_id,
            stats, progress_callback, max_retries, retry_delay, stop_event,
            dest_topic_id=dest_topic_id, mode=mode, drop_author=drop_author,
        )

        if not success:
            if error_reason == "stopped":
                stats["status"] = "stopped"
                return False
            if msg_id not in stats["failed_ids"]:
                stats["failed_ids"].append(msg_id)
            stats["failed"] = len(stats["failed_ids"])

        stats["file_progress"] = None
        if progress_callback:
            progress_callback(stats.copy())

        jittered_delay = random.uniform(rate_limit_delay * 0.6, rate_limit_delay * 1.4)
        await asyncio.sleep(jittered_delay)
        return True

    iter_kwargs = {}
    if source_topic_id is not None:
        iter_kwargs["reply_to"] = source_topic_id

    if skip_history:
        latest = await client.get_messages(source_entity, limit=1, **iter_kwargs)
        if latest:
            last_processed_id = latest[0].id
        log.info(f"skipping history, starting from message ID {last_processed_id}...")
    elif largest_first:
        log.info("collecting messages for largest-first ordering...")
        all_msgs = []
        async for message in client.iter_messages(source_entity, **iter_kwargs):
            all_msgs.append(message)
        all_msgs.sort(key=lambda m: _file_size_from_message(m), reverse=True)
        log.info(f"sorted {len(all_msgs)} messages by file size (largest first)")
        for message in all_msgs:
            ok = await _process_message(message)
            if not ok:
                break
        if pending_batch:
            await _flush_batch()
    else:
        async for message in client.iter_messages(source_entity, reverse=True, **iter_kwargs):
            ok = await _process_message(message)
            if not ok:
                break
        if pending_batch:
            await _flush_batch()

    if follow and stats["status"] != "stopped":
        stats["status"] = "watching"
        if progress_callback:
            progress_callback(stats.copy())

        while not (stop_event and stop_event.is_set()):
            new_count = 0
            async for message in client.iter_messages(source_entity, min_id=last_processed_id, reverse=True, **iter_kwargs):
                new_count += 1
                ok = await _process_message(message)
                if not ok:
                    break
            if pending_batch:
                await _flush_batch()

            if new_count > 0:
                stats["total"] += new_count
                stats["status"] = "watching"
                if progress_callback:
                    progress_callback(stats.copy())

            await asyncio.sleep(follow_poll_interval)

        stats["status"] = "stopped"

    if stats["status"] != "stopped":
        stats["status"] = "completed"

    if progress_callback:
        progress_callback(stats.copy())

    return stats


async def _try_clone_with_retry(
    client, message, source_entity, dest_entity, tracker, source_id,
    stats, progress_callback, max_retries, retry_delay,
    stop_event: asyncio.Event | None = None,
    dest_topic_id: int | None = None,
    mode: str = "forward",
    drop_author: bool = True,
) -> tuple[bool, str]:
    """attempt to clone a message with exponential backoff retries."""
    msg_id = message.id

    attempt = 0
    file_ref_attempts = 0
    flood_hits = 0
    retry_forever = max_retries <= 0
    while True:
        if stop_event and stop_event.is_set():
            return False, "stopped"
        attempt += 1
        try:
            await _clone_message(
                client, message, source_entity, dest_entity, tracker,
                source_id, stats, progress_callback,
                dest_topic_id=dest_topic_id,
                mode=mode,
                drop_author=drop_author,
            )
            stats["cloned"] += 1
            if msg_id in stats["failed_ids"]:
                stats["failed_ids"].remove(msg_id)
            stats["failed"] = len(stats["failed_ids"])
            stats["last_error"] = None
            stats["last_error_msg_id"] = None
            stats["last_error_at"] = None
            stats["last_error_attempt"] = None
            stats["last_error_wait"] = None
            log.info(
                f"[{stats['processed']}/{stats['total']}] "
                f"msg #{msg_id} cloned"
            )
            return True, ""
        except FloodWaitError as e:
            # telegram rate limit — wait it out, don't count as a retry attempt
            flood_hits += 1
            multiplier = min(flood_hits, 3) * 0.5 + 1.0  # 1.5x, 2x, 2.5x, 2.5x...
            if flood_hits >= 3:
                multiplier = 3.0
            base_wait = e.seconds * multiplier
            jitter = base_wait * RETRY_JITTER_PCT
            wait = min(base_wait + random.uniform(-jitter, jitter), MAX_RETRY_DELAY * 6)
            wait = max(wait, e.seconds)  # never sleep less than what telegram asked

            stats["status"] = "flood_wait"
            stats["last_error"] = f"FloodWait {e.seconds}s (sleeping {wait:.0f}s, x{multiplier:.1f})"
            stats["last_error_msg_id"] = msg_id
            stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
            stats["last_error_attempt"] = attempt
            stats["last_error_wait"] = wait
            if progress_callback:
                progress_callback(stats.copy())

            log.warning(
                f"flood wait hit on msg #{msg_id}, sleeping {wait:.0f}s "
                f"(telegram asked {e.seconds}s, multiplier x{multiplier:.1f}, hit #{flood_hits})"
            )
            await asyncio.sleep(wait)
            attempt -= 1  # don't burn a retry attempt on flood waits
            continue

        except FileReferenceExpiredError as e:
            file_ref_attempts += 1
            ref_delay = random.uniform(0.5, 1.5)
            log.info(f"file reference expired for msg #{msg_id}, refreshing (attempt {file_ref_attempts}/3, delay {ref_delay:.1f}s)")
            await asyncio.sleep(ref_delay)
            try:
                refreshed = await client.get_messages(message.chat_id or message.peer_id, ids=msg_id)
            except Exception as refresh_exc:
                log.warning(f"failed to refresh msg #{msg_id} after FileReferenceExpiredError: {refresh_exc}")
            else:
                if refreshed:
                    message = refreshed
            if file_ref_attempts >= 3:
                error_reason = str(e)
                stats["status"] = "failed"
                stats["last_error"] = error_reason
                stats["last_error_msg_id"] = msg_id
                stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
                stats["last_error_attempt"] = attempt
                stats["last_error_wait"] = None
                if msg_id not in stats["failed_ids"]:
                    stats["failed_ids"].append(msg_id)
                stats["failed"] = len(stats["failed_ids"])
                if progress_callback:
                    progress_callback(stats.copy())
                try:
                    await _tracker_call(tracker, "mark_failed", source_id, msg_id, error_reason)
                except Exception as mark_exc:
                    log.warning(f"failed to record failed msg #{msg_id}: {mark_exc}")
                log.error(
                    f"msg #{msg_id} permanently failed after {file_ref_attempts} FileReferenceExpiredError "
                    f"refresh attempts: {error_reason}"
                )
                return False, error_reason
            attempt -= 1  # ref refresh isn't a real retry either
            continue

        except (MessageIdInvalidError, ChatForwardsRestrictedError) as e:
            error_reason = str(e)
            log.warning(f"msg #{msg_id} unforwardable ({e.__class__.__name__}): {error_reason} — skipping retries")
            stats["status"] = "running"
            stats["last_error"] = f"Unforwardable: {error_reason}"
            stats["last_error_msg_id"] = msg_id
            stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
            stats["last_error_attempt"] = attempt
            stats["last_error_wait"] = None
            if msg_id not in stats["failed_ids"]:
                stats["failed_ids"].append(msg_id)
            stats["failed"] = len(stats["failed_ids"])
            if progress_callback:
                progress_callback(stats.copy())
            try:
                await _tracker_call(tracker, "mark_failed", source_id, msg_id, error_reason)
            except Exception as mark_exc:
                log.warning(f"failed to record failed msg #{msg_id}: {mark_exc}")
            return False, error_reason

        except Exception as e:
            error_reason = str(e)
            is_invalid_msg = any(
                phrase in error_reason.lower()
                for phrase in ["specified message id is invalid", "messageidinvalid", "can't do that operation", "chatforwardsrestricted"]
            )
            if is_invalid_msg:
                log.warning(f"msg #{msg_id} unforwardable: {error_reason} — skipping retries")
                stats["status"] = "running"
                stats["last_error"] = f"Unforwardable: {error_reason}"
                stats["last_error_msg_id"] = msg_id
                stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
                stats["last_error_attempt"] = attempt
                stats["last_error_wait"] = None
                if msg_id not in stats["failed_ids"]:
                    stats["failed_ids"].append(msg_id)
                stats["failed"] = len(stats["failed_ids"])
                if progress_callback:
                    progress_callback(stats.copy())
                try:
                    await _tracker_call(tracker, "mark_failed", source_id, msg_id, error_reason)
                except Exception as mark_exc:
                    log.warning(f"failed to record failed msg #{msg_id}: {mark_exc}")
                return False, error_reason

            wait = retry_delay * (2 ** (attempt - 1))
            wait = min(wait, MAX_RETRY_DELAY)
            jitter = wait * RETRY_JITTER_PCT
            wait = max(1.0, wait + random.uniform(-jitter, jitter))
            stats["status"] = "retrying"
            stats["last_error"] = error_reason
            stats["last_error_msg_id"] = msg_id
            stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
            stats["last_error_attempt"] = attempt
            stats["last_error_wait"] = wait
            if msg_id not in stats["failed_ids"]:
                stats["failed_ids"].append(msg_id)
            stats["failed"] = len(stats["failed_ids"])
            if progress_callback:
                progress_callback(stats.copy())
            try:
                await _tracker_call(tracker, "mark_failed", source_id, msg_id, error_reason)
            except Exception as mark_exc:
                log.warning(f"failed to record failed msg #{msg_id}: {mark_exc}")

            if not retry_forever and attempt >= max_retries:
                log.error(f"msg #{msg_id} permanently failed after {max_retries} attempts: {error_reason}")
                return False, error_reason

            log.warning(
                f"msg #{msg_id} attempt {attempt}{'' if retry_forever else f'/{max_retries}'} failed: {e} "
                f"— retrying in {wait:.0f}s"
            )
            await asyncio.sleep(wait)

    return False, "max retries exceeded"


async def _forward_batch(
    client: TelegramClient,
    messages: list,
    source_entity,
    dest_entity,
    tracker: CloneTracker,
    source_id: int,
    stats: dict,
    progress_callback: Callable[[dict], None] | None = None,
    dest_topic_id: int | None = None,
    drop_author: bool = True,
):
    """batch forward up to 50-100 messages in a single Telegram API request."""
    if not messages:
        return

    from telethon.tl.functions.messages import ForwardMessagesRequest

    msg_ids = [m.id for m in messages if hasattr(m, "id")]
    if not msg_ids:
        return

    rnd_ids = [random.randint(-2**63, 2**63 - 1) for _ in msg_ids]

    kwargs = {
        "from_peer": source_entity,
        "id": msg_ids,
        "to_peer": dest_entity,
        "drop_author": drop_author,
        "random_id": rnd_ids,
    }
    if dest_topic_id is not None:
        kwargs["top_msg_id"] = dest_topic_id

    try:
        await client(ForwardMessagesRequest(**kwargs))
        for msg in messages:
            stats["cloned"] += 1
            media_type = _media_type(msg)
            await _tracker_call(
                tracker,
                "mark_cloned",
                source_id,
                msg.id,
                filename=None,
                media_type=media_type,
            )
        log.info(
            f"[{stats['processed']}/{stats['total']}] "
            f"batch forwarded {len(messages)} messages (IDs: #{msg_ids[0]}..#{msg_ids[-1]})"
        )
        if progress_callback:
            progress_callback(stats.copy())
    except FloodWaitError as e:
        log.warning(f"flood wait hit during batch forward: sleeping {e.seconds}s")
        await asyncio.sleep(e.seconds + 1)
        for msg in messages:
            await _try_clone_with_retry(
                client, msg, source_entity, dest_entity, tracker, source_id,
                stats, progress_callback, 3, 2.0, None,
                dest_topic_id=dest_topic_id, mode="forward", drop_author=drop_author
            )
    except Exception as exc:
        log.warning(f"batch forward exception ({exc}) — falling back to single-message mode for this batch")
        for msg in messages:
            await _try_clone_with_retry(
                client, msg, source_entity, dest_entity, tracker, source_id,
                stats, progress_callback, 3, 2.0, None,
                dest_topic_id=dest_topic_id, mode="forward", drop_author=drop_author
            )


async def _forward_message(
    client: TelegramClient,
    message,
    source_entity,
    dest_entity,
    tracker: CloneTracker,
    source_id: int,
    stats: dict,
    dest_topic_id: int | None = None,
    drop_author: bool = True,
):
    """server-side forward message directly on Telegram without local download."""
    from telethon.tl.functions.messages import ForwardMessagesRequest
    rnd_id = [random.randint(-2**63, 2**63 - 1)]
    kwargs = {
        "from_peer": source_entity,
        "id": [message.id],
        "to_peer": dest_entity,
        "drop_author": drop_author,
        "random_id": rnd_id,
    }
    if dest_topic_id is not None:
        kwargs["top_msg_id"] = dest_topic_id

    await client(ForwardMessagesRequest(**kwargs))

    media_type = _media_type(message)
    await _tracker_call(
        tracker,
        "mark_cloned",
        source_id,
        message.id,
        filename=None,
        media_type=media_type,
    )


async def _clone_message(
    client: TelegramClient,
    message,
    source_entity,
    dest_entity,
    tracker: CloneTracker,
    source_id: int,
    stats: dict,
    progress_callback: Callable[[dict], None] | None = None,
    dest_topic_id: int | None = None,
    mode: str = "forward",
    drop_author: bool = True,
):
    """handle a single message — server-side forward or download & re-upload."""
    if mode == "forward":
        await _forward_message(
            client, message, source_entity, dest_entity, tracker,
            source_id, stats, dest_topic_id=dest_topic_id, drop_author=drop_author,
        )
        return

    caption = message.text or ""
    media_type = _media_type(message)
    filename = None

    has_media = isinstance(message.media, (MessageMediaPhoto, MessageMediaDocument))

    if has_media:
        filename = await _download_and_reupload(
            client, message, dest_entity, caption, stats, progress_callback,
            dest_topic_id=dest_topic_id,
        )
    elif caption:
        send_kwargs = {"formatting_entities": message.entities}
        if dest_topic_id is not None:
            send_kwargs["reply_to"] = dest_topic_id
        await client.send_message(dest_entity, caption, **send_kwargs)

    await _tracker_call(
        tracker,
        "mark_cloned",
        source_id,
        message.id,
        filename=filename,
        media_type=media_type,
    )


async def _download_and_reupload(
    client: TelegramClient,
    message,
    dest_entity,
    caption: str,
    stats: dict,
    progress_callback: Callable[[dict], None] | None = None,
    dest_topic_id: int | None = None,
) -> str | None:
    """download media to disk, send to dest, nuke the local copy.
    uses FastTelethon parallel transfer for files above the threshold."""

    file_size = _file_size_from_message(message)
    use_fast = file_size > FAST_TRANSFER_THRESHOLD and message.document is not None

    # guess filename before download
    pre_name = "media"
    if message.document and hasattr(message.document, "attributes"):
        for attr in message.document.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                pre_name = attr.file_name
                break

    def _make_progress_cb(phase: str, fname: str):
        start_time = time.time()
        
        def cb(current, total):
            elapsed = time.time() - start_time
            speed = current / elapsed if elapsed > 0 else 0

            stats["file_progress"] = {
                "phase": phase,
                "filename": fname,
                "current": current,
                "total": total,
                "current_human": _human_size(current),
                "total_human": _human_size(total),
                "speed_human": _human_size(speed) + "/s",
            }
            if progress_callback:
                progress_callback(stats.copy())
        return cb

    if use_fast:
        try:
            return await _fast_transfer(
                client, message, dest_entity, caption, pre_name,
                stats, progress_callback, _make_progress_cb,
                dest_topic_id=dest_topic_id,
            )
        except Exception as e:
            log.warning(f"fast transfer failed for {pre_name}, falling back to standard: {e}")
            stats["file_progress"] = None
            return await _standard_transfer(
                client, message, dest_entity, caption, pre_name,
                stats, _make_progress_cb,
                dest_topic_id=dest_topic_id,
            )

    return await _standard_transfer(
        client, message, dest_entity, caption, pre_name,
        stats, _make_progress_cb,
        dest_topic_id=dest_topic_id,
    )


async def _fast_transfer(
    client, message, dest_entity, caption, pre_name,
    stats, progress_callback, make_cb,
    dest_topic_id: int | None = None,
):
    """parallel download + upload via FastTelethon for big files."""

    dl_path = os.path.join(DOWNLOAD_DIR, pre_name)
    # avoid collisions
    base, ext = os.path.splitext(dl_path)
    counter = 0
    while os.path.exists(dl_path):
        counter += 1
        dl_path = f"{base}_{counter}{ext}"

    filename = Path(dl_path).name

    try:
        # parallel download
        with open(dl_path, "wb") as f:
            await download_file(client, message.document, f, progress_callback=make_cb("downloading", pre_name))

        # parallel upload
        with open(dl_path, "rb") as f:
            uploaded = await upload_file(client, f, progress_callback=make_cb("uploading", filename))

        # build the proper media with attributes so it shows correctly
        attributes, mime_type = utils.get_attributes(dl_path)
        if message.document and message.document.attributes:
            attributes = list(message.document.attributes)
        mime_type = message.document.mime_type if message.document else mime_type

        media = InputMediaUploadedDocument(
            file=uploaded,
            mime_type=mime_type,
            attributes=attributes,
            force_file=False,
        )
        send_file_kwargs = {
            "file": media,
            "caption": caption,
            "formatting_entities": message.entities,
        }
        if dest_topic_id is not None:
            send_file_kwargs["reply_to"] = dest_topic_id
        await client.send_file(
            dest_entity,
            **send_file_kwargs,
        )
        return filename
    finally:
        if os.path.exists(dl_path):
            os.remove(dl_path)
            log.debug(f"deleted local file: {filename}")


async def _standard_transfer(
    client, message, dest_entity, caption, pre_name,
    stats, make_cb,
    dest_topic_id: int | None = None,
):
    """regular telethon download + upload for smaller files and photos."""

    pre_existing = set(os.listdir(DOWNLOAD_DIR)) if os.path.isdir(DOWNLOAD_DIR) else set()
    try:
        file_path = await client.download_media(
            message,
            file=DOWNLOAD_DIR,
            progress_callback=make_cb("downloading", pre_name),
        )
    except Exception:
        _cleanup_new_downloads(pre_existing, DOWNLOAD_DIR)
        raise
    if not file_path:
        _cleanup_new_downloads(pre_existing, DOWNLOAD_DIR)
        if caption:
            send_kwargs = {"formatting_entities": message.entities}
            if dest_topic_id is not None:
                send_kwargs["reply_to"] = dest_topic_id
            await client.send_message(dest_entity, caption, **send_kwargs)
        return None

    file_path = str(file_path)
    filename = Path(file_path).name

    try:
        send_file_kwargs = {
            "file": file_path,
            "caption": caption,
            "formatting_entities": message.entities,
            "force_document": message.document is not None and not any([
                message.video, message.audio, message.voice,
                message.video_note, message.sticker, message.gif,
            ]),
            "progress_callback": make_cb("uploading", filename),
        }
        if dest_topic_id is not None:
            send_file_kwargs["reply_to"] = dest_topic_id
        await client.send_file(
            dest_entity,
            **send_file_kwargs,
        )
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
            log.debug(f"deleted local file: {filename}")

    return filename
