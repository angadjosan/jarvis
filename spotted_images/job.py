"""Fetch recent images from a Slack channel and POST them to a webhook."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import mimetypes
import os
import sys
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests

DEFAULT_CHANNEL_ID = "C06J25K5V1A"
DEFAULT_HISTORY_LIMIT = 100
DEFAULT_FIRST_RUN_IMAGES = 20
DEFAULT_STATE_PATH = ".spotted_images_state.json"
SLACK_API = "https://slack.com/api"
IMAGE_PREFIXES = ("image/",)

log = logging.getLogger("spotted_images")


@dataclass
class SlackImage:
    file_id: str
    filename: str
    mimetype: str
    url: str
    message_ts: str
    user: str
    text: str
    permalink: str = ""
    channel: str = DEFAULT_CHANNEL_ID
    content: bytes = field(default=b"", repr=False)


@dataclass
class JobState:
    last_ts: str = "0"
    posted_file_ids: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "JobState":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        posted = data.get("posted_file_ids") or []
        return cls(last_ts=str(data.get("last_ts") or "0"), posted_file_ids=list(posted))

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {"last_ts": self.last_ts, "posted_file_ids": self.posted_file_ids[-500:]},
                indent=2,
            )
            + "\n"
        )


class SlackClient:
    def __init__(self, token: str, session: requests.Session | None = None) -> None:
        self.token = token
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def conversations_history(
        self,
        channel: str,
        *,
        oldest: str | None = None,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"channel": channel, "limit": limit}
        if oldest and oldest != "0":
            params["oldest"] = oldest
        response = self.session.get(
            f"{SLACK_API}/conversations.history",
            headers=self._headers(),
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"conversations.history failed: {payload.get('error')}")
        return list(payload.get("messages") or [])

    def download(self, url: str) -> bytes:
        response = self.session.get(url, headers=self._headers(), timeout=60)
        response.raise_for_status()
        return response.content


def is_image_file(file_obj: dict[str, Any]) -> bool:
    mimetype = str(file_obj.get("mimetype") or "")
    filetype = str(file_obj.get("filetype") or "").lower()
    if any(mimetype.startswith(prefix) for prefix in IMAGE_PREFIXES):
        return True
    return filetype in {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif"}


def slack_permalink(channel: str, ts: str) -> str:
    compact = ts.replace(".", "")
    return f"https://slack.com/archives/{channel}/p{compact}"


def images_from_messages(
    messages: Iterable[dict[str, Any]],
    *,
    channel: str,
    seen_file_ids: set[str],
) -> list[SlackImage]:
    images: list[SlackImage] = []
    seen = set(seen_file_ids)
    for message in messages:
        ts = str(message.get("ts") or "")
        if not ts:
            continue
        user = str(message.get("user") or message.get("username") or "")
        text = str(message.get("text") or "")
        permalink = str(message.get("permalink") or slack_permalink(channel, ts))
        for file_obj in message.get("files") or []:
            if not is_image_file(file_obj):
                continue
            file_id = str(file_obj.get("id") or "")
            if not file_id or file_id in seen:
                continue
            seen.add(file_id)
            url = str(file_obj.get("url_private_download") or file_obj.get("url_private") or "")
            if not url:
                continue
            filename = str(file_obj.get("name") or f"{file_id}.jpg")
            mimetype = str(file_obj.get("mimetype") or mimetypes.guess_type(filename)[0] or "image/jpeg")
            images.append(
                SlackImage(
                    file_id=file_id,
                    filename=filename,
                    mimetype=mimetype,
                    url=url,
                    message_ts=ts,
                    user=user,
                    text=text,
                    permalink=permalink,
                    channel=channel,
                )
            )
    images.sort(key=lambda item: float(item.message_ts))
    return images


def newest_images(images: list[SlackImage], limit: int) -> list[SlackImage]:
    if limit <= 0 or len(images) <= limit:
        return images
    return images[-limit:]


def image_metadata(image: SlackImage) -> dict[str, str]:
    return {
        "file_id": image.file_id,
        "filename": image.filename,
        "mimetype": image.mimetype,
        "message_ts": image.message_ts,
        "user": image.user,
        "text": image.text,
        "permalink": image.permalink,
        "channel": image.channel,
    }


def post_images_multipart(
    webhook_url: str,
    images: list[SlackImage],
    *,
    session: requests.Session | None = None,
) -> requests.Response:
    client = session or requests.Session()
    files = [
        ("images", (image.filename, BytesIO(image.content), image.mimetype))
        for image in images
    ]
    data = {
        "source": "slack",
        "channel": images[0].channel if images else DEFAULT_CHANNEL_ID,
        "count": str(len(images)),
        "payload": json.dumps({"images": [image_metadata(image) for image in images]}),
    }
    response = client.post(webhook_url, data=data, files=files, timeout=60)
    response.raise_for_status()
    return response


def post_images_json(
    webhook_url: str,
    images: list[SlackImage],
    *,
    session: requests.Session | None = None,
) -> requests.Response:
    client = session or requests.Session()
    body = {
        "source": "slack",
        "channel": images[0].channel if images else DEFAULT_CHANNEL_ID,
        "count": len(images),
        "images": [
            {
                **image_metadata(image),
                "data_base64": base64.b64encode(image.content).decode("ascii"),
            }
            for image in images
        ],
    }
    response = client.post(webhook_url, json=body, timeout=60)
    response.raise_for_status()
    return response


def validate_webhook_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("WEBHOOK_URL must be an absolute http(s) URL")
    return url


def just_before(ts: str) -> str:
    """Return a Slack timestamp strictly earlier than ts so oldest= stays inclusive."""
    return f"{float(ts) - 1e-6:.6f}"


def run_job(
    *,
    slack_token: str,
    webhook_url: str,
    channel: str = DEFAULT_CHANNEL_ID,
    state_path: Path = Path(DEFAULT_STATE_PATH),
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    first_run_images: int = DEFAULT_FIRST_RUN_IMAGES,
    mode: str = "multipart",
    session: requests.Session | None = None,
) -> dict[str, Any]:
    webhook_url = validate_webhook_url(webhook_url)
    state = JobState.load(state_path)
    slack = SlackClient(slack_token, session=session)
    first_run = state.last_ts == "0"
    oldest = None if first_run else state.last_ts
    messages = slack.conversations_history(channel, oldest=oldest, limit=history_limit)
    images = images_from_messages(messages, channel=channel, seen_file_ids=set(state.posted_file_ids))
    if first_run:
        images = newest_images(images, first_run_images)

    downloaded: list[SlackImage] = []
    failed_ts: list[str] = []
    for image in images:
        try:
            image.content = slack.download(image.url)
        except requests.RequestException as exc:
            log.warning("skipping %s: download failed: %s", image.file_id, exc)
            failed_ts.append(image.message_ts)
            continue
        if not image.content:
            log.warning("skipping %s: empty download", image.file_id)
            failed_ts.append(image.message_ts)
            continue
        downloaded.append(image)

    if downloaded:
        if mode == "json":
            post_images_json(webhook_url, downloaded, session=session)
        else:
            post_images_multipart(webhook_url, downloaded, session=session)
        state.posted_file_ids.extend(image.file_id for image in downloaded)

    if failed_ts:
        retry_ts = just_before(min(failed_ts, key=float))
        if float(retry_ts) < float(state.last_ts) or state.last_ts == "0":
            state.last_ts = retry_ts
    elif messages:
        newest_ts = max(str(message.get("ts") or "0") for message in messages)
        if float(newest_ts) > float(state.last_ts):
            state.last_ts = newest_ts
    state.save(state_path)
    return {
        "posted": len(downloaded),
        "skipped": len(images) - len(downloaded),
        "last_ts": state.last_ts,
        "file_ids": [image.file_id for image in downloaded],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=os.environ.get("SLACK_CHANNEL_ID", DEFAULT_CHANNEL_ID))
    parser.add_argument("--webhook-url", default=os.environ.get("WEBHOOK_URL", ""))
    parser.add_argument("--slack-token", default=os.environ.get("SLACK_BOT_TOKEN", ""))
    parser.add_argument("--state-path", default=os.environ.get("STATE_PATH", DEFAULT_STATE_PATH))
    parser.add_argument("--history-limit", type=int, default=int(os.environ.get("HISTORY_LIMIT", DEFAULT_HISTORY_LIMIT)))
    parser.add_argument(
        "--first-run-images",
        type=int,
        default=int(os.environ.get("FIRST_RUN_IMAGES", DEFAULT_FIRST_RUN_IMAGES)),
    )
    parser.add_argument("--mode", choices=("multipart", "json"), default=os.environ.get("WEBHOOK_MODE", "multipart"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    if not args.slack_token:
        log.error("SLACK_BOT_TOKEN is required")
        return 2
    if not args.webhook_url:
        log.error("WEBHOOK_URL is required")
        return 2
    result = run_job(
        slack_token=args.slack_token,
        webhook_url=args.webhook_url,
        channel=args.channel,
        state_path=Path(args.state_path),
        history_limit=args.history_limit,
        first_run_images=args.first_run_images,
        mode=args.mode,
    )
    log.info("posted %s image(s); last_ts=%s", result["posted"], result["last_ts"])
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
