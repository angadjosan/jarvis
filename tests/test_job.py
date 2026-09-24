import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from spotted_images.job import (
    SlackImage,
    images_from_messages,
    newest_images,
    post_images_json,
    post_images_multipart,
    run_job,
    validate_webhook_url,
)


CHANNEL = "C06J25K5V1A"


def image_message(ts: str, file_id: str, *, mimetype: str = "image/jpeg", name: str = "spot.jpg") -> dict:
    return {
        "ts": ts,
        "user": "U123",
        "text": f"spot {file_id}",
        "files": [
            {
                "id": file_id,
                "name": name,
                "mimetype": mimetype,
                "url_private": f"https://files.slack.com/{file_id}",
                "url_private_download": f"https://files.slack.com/{file_id}/download",
            }
        ],
    }


def test_images_from_messages_skips_non_images_and_duplicates() -> None:
    messages = [
        image_message("1.0", "F1"),
        {
            "ts": "2.0",
            "user": "U123",
            "text": "notes",
            "files": [
                {
                    "id": "FDOC",
                    "name": "notes.pdf",
                    "mimetype": "application/pdf",
                    "url_private": "https://files.slack.com/FDOC",
                }
            ],
        },
        image_message("3.0", "F1"),
        image_message("4.0", "F2"),
    ]
    images = images_from_messages(messages, channel=CHANNEL, seen_file_ids=set())
    assert [image.file_id for image in images] == ["F1", "F2"]
    assert images[0].permalink.endswith("/p10")


def test_newest_images_keeps_latest() -> None:
    images = [
        SlackImage("F1", "a.jpg", "image/jpeg", "u", "1.0", "U", "a"),
        SlackImage("F2", "b.jpg", "image/jpeg", "u", "2.0", "U", "b"),
        SlackImage("F3", "c.jpg", "image/jpeg", "u", "3.0", "U", "c"),
    ]
    assert [image.file_id for image in newest_images(images, 2)] == ["F2", "F3"]


def test_validate_webhook_url_rejects_relative() -> None:
    with pytest.raises(ValueError):
        validate_webhook_url("/hooks/local")


def test_post_images_multipart_sends_files_and_payload() -> None:
    session = Mock()
    session.post.return_value = Mock(raise_for_status=Mock())
    image = SlackImage(
        "F1",
        "spot.jpg",
        "image/jpeg",
        "https://files.slack.com/F1",
        "1.2",
        "U123",
        "spot",
        content=b"jpeg-bytes",
        channel=CHANNEL,
    )
    post_images_multipart("https://example.test/hook", [image], session=session)
    args, kwargs = session.post.call_args
    assert args[0] == "https://example.test/hook"
    assert kwargs["data"]["count"] == "1"
    payload = json.loads(kwargs["data"]["payload"])
    assert payload["images"][0]["file_id"] == "F1"
    filename, buffer, mimetype = kwargs["files"][0][1]
    assert filename == "spot.jpg"
    assert mimetype == "image/jpeg"
    assert buffer.getvalue() == b"jpeg-bytes"


def test_post_images_json_includes_base64() -> None:
    session = Mock()
    session.post.return_value = Mock(raise_for_status=Mock())
    image = SlackImage("F1", "spot.jpg", "image/jpeg", "u", "1.2", "U123", "spot", content=b"abc")
    post_images_json("https://example.test/hook", [image], session=session)
    body = session.post.call_args.kwargs["json"]
    assert body["count"] == 1
    assert body["images"][0]["data_base64"] == "YWJj"


def test_run_job_first_run_posts_newest_and_advances_watermark(tmp_path: Path) -> None:
    session = Mock()
    history = Mock()
    history.json.return_value = {
        "ok": True,
        "messages": [
            image_message("10.0", "FOLD"),
            image_message("20.0", "FNEW"),
            image_message("30.0", "FLAT"),
        ],
    }
    downloads = {
        "https://files.slack.com/FNEW/download": Mock(content=b"new", raise_for_status=Mock()),
        "https://files.slack.com/FLAT/download": Mock(content=b"lat", raise_for_status=Mock()),
    }
    webhook = Mock()
    webhook.raise_for_status = Mock()

    def get(url: str, **kwargs):
        if url.endswith("conversations.history"):
            return history
        return downloads[url]

    session.get.side_effect = get
    session.post.return_value = webhook

    state_path = tmp_path / "state.json"
    result = run_job(
        slack_token="xoxb-test",
        webhook_url="https://example.test/hook",
        channel=CHANNEL,
        state_path=state_path,
        first_run_images=2,
        session=session,
    )
    assert result["posted"] == 2
    assert result["file_ids"] == ["FNEW", "FLAT"]
    assert result["last_ts"] == "30.0"
    saved = json.loads(state_path.read_text())
    assert saved["posted_file_ids"] == ["FNEW", "FLAT"]


def test_run_job_skips_failed_downloads(tmp_path: Path) -> None:
    session = Mock()
    history = Mock()
    history.json.return_value = {
        "ok": True,
        "messages": [image_message("11.0", "F1"), image_message("12.0", "F2")],
    }

    def get(url: str, **kwargs):
        if url.endswith("conversations.history"):
            return history
        if url.endswith("F1/download"):
            raise requests.RequestException("nope")
        response = Mock(content=b"ok", raise_for_status=Mock())
        return response

    session.get.side_effect = get
    session.post.return_value = Mock(raise_for_status=Mock())
    result = run_job(
        slack_token="xoxb-test",
        webhook_url="https://example.test/hook",
        state_path=tmp_path / "state.json",
        session=session,
    )
    assert result["posted"] == 1
    assert result["skipped"] == 1
    assert result["file_ids"] == ["F2"]
    assert float(result["last_ts"]) < 11.0
