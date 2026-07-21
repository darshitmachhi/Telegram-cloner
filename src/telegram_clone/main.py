"""
entrypoint — handles telegram login, channel selection, and kicks off the cloner.
"""

import asyncio
import logging
import signal
import sys

from telethon import TelegramClient
from telethon.tl.types import Channel, ForumTopic
from telethon.tl.functions.messages import GetForumTopicsRequest
from tqdm import tqdm

from config import (
    API_ID, API_HASH, PHONE, SOURCE_CHANNEL, DEST_CHANNEL, SESSION_FILE,
    SOURCE_TOPIC_ID, DEST_TOPIC_ID, CLONE_MODE, DROP_AUTHOR,
)
from tracker import create_tracker
from cloner import clone_channel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("main")


async def list_channels(client: TelegramClient) -> list:
    """grab all channels/groups the user is in."""
    channels = []
    async for dialog in client.iter_dialogs():
        if isinstance(dialog.entity, Channel):
            channels.append(dialog)
    return channels


async def pick_channel(client: TelegramClient, prompt: str, default: str = "") -> str:
    """interactive channel picker — shows a numbered list, user picks one."""

    if default:
        use_default = input(f"\n{prompt} [default: {default}] — press enter to use default, or 'l' to list: ").strip()
        if not use_default:
            return default
        if use_default.lower() != "l":
            return use_default

    channels = await list_channels(client)
    if not channels:
        log.error("no channels found. are you in any channels?")
        sys.exit(1)

    print(f"\n{'=' * 50}")
    print(f" {prompt}")
    print(f"{'=' * 50}")
    for i, dialog in enumerate(channels, 1):
        entity = dialog.entity
        member_info = f" ({entity.participants_count} members)" if entity.participants_count else ""
        print(f"  [{i:3d}] {dialog.name}{member_info}")
    print(f"{'=' * 50}")

    while True:
        choice = input("\npick a number (or type channel username): ").strip()
        if not choice:
            continue
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(channels):
                selected = channels[idx]
                log.info(f"selected: {selected.name}")
                return selected.entity.id
        else:
            return choice

    return default


async def pick_topic(client: TelegramClient, channel_identifier, prompt: str, default_topic_id: int | None = None) -> int | None:
    """interactive topic picker for forum supergroups."""
    try:
        entity = await client.get_entity(channel_identifier)
    except Exception:
        return default_topic_id

    if not getattr(entity, "forum", False):
        return default_topic_id

    try:
        res = await client(GetForumTopicsRequest(
            peer=entity,
            q='',
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100,
        ))
        topics = [t for t in getattr(res, "topics", []) if isinstance(t, ForumTopic)]
    except Exception as exc:
        log.warning(f"could not fetch topics for {getattr(entity, 'title', channel_identifier)}: {exc}")
        return default_topic_id

    if not topics:
        return default_topic_id

    title = getattr(entity, "title", str(channel_identifier))
    print(f"\n{'=' * 50}")
    print(f" {prompt} for '{title}' (Forum Supergroup)")
    print(f"{'=' * 50}")
    print(f"  [  0] All topics / Default topic")
    for i, topic in enumerate(topics, 1):
        print(f"  [{i:3d}] {topic.title} (ID: {topic.id})")
    print(f"{'=' * 50}")

    default_str = f" [default: {default_topic_id}]" if default_topic_id else " [default: 0 (All/Default)]"
    while True:
        choice = input(f"\npick a topic number or topic ID{default_str}: ").strip()
        if not choice:
            return default_topic_id
        if choice.isdigit():
            val = int(choice)
            if val == 0:
                return None
            if 1 <= val <= len(topics):
                selected = topics[val - 1]
                log.info(f"selected topic: {selected.title} (ID: {selected.id})")
                return selected.id
            return val


def _make_cli_progress():
    state = {
        "bar": None,
        "phase": None,
        "filename": None,
        "total": None,
        "last": 0,
    }

    def _close_bar():
        if state["bar"] is not None:
            state["bar"].close()
            state["bar"] = None

    def cb(stats: dict):
        fp = stats.get("file_progress")
        if not fp:
            _close_bar()
            return

        phase = fp.get("phase")
        filename = fp.get("filename") or "media"
        total = fp.get("total") or 0
        current = fp.get("current") or 0

        if (
            state["bar"] is None
            or phase != state["phase"]
            or filename != state["filename"]
            or total != state["total"]
        ):
            _close_bar()
            label = "DL" if phase == "downloading" else "UL"
            state["bar"] = tqdm(
                total=total,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=f"{label} {filename}",
                leave=False,
                dynamic_ncols=True,
            )
            state["phase"] = phase
            state["filename"] = filename
            state["total"] = total
            state["last"] = 0

        delta = current - state["last"]
        if delta > 0 and state["bar"] is not None:
            state["bar"].update(delta)
            state["last"] = current

        if total and current >= total:
            _close_bar()

    return cb


async def run(
    source_topic_id: int | None = None,
    dest_topic_id: int | None = None,
    mode: str | None = None,
    drop_author: bool | None = None,
    max_messages: int | None = None,
    batch_delay: float = 5.0,
):
    if not API_ID or not API_HASH:
        log.error("API_ID and API_HASH are required. get them from https://my.telegram.org")
        log.error("copy .env.example to .env and fill in your credentials")
        sys.exit(1)

    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await client.start(phone=PHONE if PHONE else lambda: input("phone number: "))

    me = await client.get_me()
    log.info(f"logged in as {me.first_name} (@{me.username})")

    source = await pick_channel(client, "source channel (clone FROM)", SOURCE_CHANNEL)
    dest = await pick_channel(client, "destination channel (clone TO)", DEST_CHANNEL)

    if str(source) == str(dest):
        log.error("source and dest are the same channel, that's a loop my guy")
        sys.exit(1)

    if source_topic_id is None:
        source_topic = await pick_topic(client, source, "source topic filter", SOURCE_TOPIC_ID)
    else:
        source_topic = source_topic_id

    if dest_topic_id is None:
        dest_topic = await pick_topic(client, dest, "destination target topic", DEST_TOPIC_ID)
    else:
        dest_topic = dest_topic_id

    if mode is None:
        print("\n[1] Server-side plain forward (Instant, no media download)")
        print("[2] Download media & re-upload to destination")
        while True:
            m_choice = input("pick method [1/2] [default: 1]: ").strip()
            if not m_choice or m_choice == "1":
                mode = "forward"
                break
            if m_choice == "2":
                mode = "reupload"
                break

    if drop_author is None:
        drop_author = DROP_AUTHOR

    print("\n[1] clone entire history (oldest to newest)")
    print("[2] skip history, listen for new messages only")
    print("[3] largest files first")
    while True:
        hist_choice = input("pick mode [1/2/3]: ").strip()
        if hist_choice in ("1", "2", "3"):
            break
    skip_history = (hist_choice == "2")
    largest_first = (hist_choice == "3")

    tracker = create_tracker()
    existing = tracker.get_stats()
    if existing["total_cloned"] > 0 and not skip_history:
        log.info(f"resuming — {existing['total_cloned']} messages already cloned (last run: {existing['last_run']})")

    print(f"\nstarting clone...")
    print(f"{'—' * 40}")

    stop_event = asyncio.Event()
    sigint_count = 0

    def _handle_sigint():
        nonlocal sigint_count
        sigint_count += 1
        if sigint_count == 1:
            print(f"\n[!] SIGINT received, stopping current clone gracefully... (press again to force quit)")
            stop_event.set()
            return
        print(f"\n[!] SIGINT received again, exiting now.")
        sys.exit(1)

    try:
        client.loop.add_signal_handler(signal.SIGINT, _handle_sigint)
    except NotImplementedError:
        # Windows event loops don't implement add_signal_handler
        def _sigint_handler(signum, frame):
            client.loop.call_soon_threadsafe(_handle_sigint)

        signal.signal(signal.SIGINT, _sigint_handler)

    stats = {"cloned": 0, "skipped": 0, "failed": 0, "total": 0}
    try:
        stats = await clone_channel(
            client,
            source,
            dest,
            tracker,
            stop_event=stop_event,
            progress_callback=_make_cli_progress(),
            skip_history=skip_history,
            largest_first=largest_first,
            source_topic_id=source_topic,
            dest_topic_id=dest_topic,
            mode=mode,
            drop_author=drop_author,
            max_messages=max_messages,
            batch_delay=batch_delay,
        )
    finally:
        print(f"\n{'—' * 40}")
        print(f"done.")
        print(f"  cloned:  {stats['cloned']}")
        print(f"  skipped: {stats['skipped']} (already cloned)")
        print(f"  failed:  {stats['failed']}")
        print(f"  total:   {stats['total']}")

        await client.disconnect()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="telegram channel cloner")
    parser.add_argument("--cli", action="store_true", help="run in cli mode instead of web interface")
    parser.add_argument("--source-topic", type=int, default=None, help="ID of specific source topic to clone from")
    parser.add_argument("--dest-topic", type=int, default=None, help="ID of target destination topic to clone to")
    parser.add_argument("--mode", choices=["forward", "reupload"], default=None, help="cloning mode: 'forward' (instant, no download) or 'reupload'")
    parser.add_argument("--keep-author", action="store_true", help="keep 'Forwarded from' header when forwarding")
    parser.add_argument("--max-messages", type=int, default=None, help="maximum number of messages to clone before stopping")
    parser.add_argument("--batch-delay", type=float, default=5.0, help="delay in seconds between batches to prevent bans (default: 5.0)")
    args = parser.parse_args()

    if args.cli:
        drop_auth = False if args.keep_author else None
        asyncio.run(run(
            source_topic_id=args.source_topic,
            dest_topic_id=args.dest_topic,
            mode=args.mode,
            drop_author=drop_auth,
            max_messages=args.max_messages,
            batch_delay=args.batch_delay,
        ))
    else:
        from web import app, WEB_HOST, WEB_PORT
        app.run(host=WEB_HOST, port=WEB_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()

