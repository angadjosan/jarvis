# Jarvis

Jobs for the Slack Jarvis agent.

## #spotted image webhook

Every 5 minutes, GitHub Actions pulls the newest images from `#spotted` (`C06J25K5V1A`) and POSTs them to a webhook.

On the first run it sends the 20 most recent images. Later runs only send images newer than the saved Slack watermark.

### Webhook payload

Default `multipart/form-data`:

| Field | Contents |
| --- | --- |
| `images` | One file part per photo |
| `source` | `slack` |
| `channel` | `C06J25K5V1A` |
| `count` | Number of files in this POST |
| `payload` | JSON array of `file_id`, `filename`, `message_ts`, `user`, `text`, `permalink` |

Set repo variable `WEBHOOK_MODE=json` to send a single JSON body instead, with each image as `data_base64`.

### Setup

1. Create a Slack app with `channels:history` and `files:read`.
2. Invite the bot to `#spotted`.
3. Add repository secrets:
   - `SLACK_BOT_TOKEN`
   - `WEBHOOK_URL`
4. Run **spotted-images** from Actions, or wait for the 5-minute schedule.

### Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
export SLACK_BOT_TOKEN=xoxb-...
export WEBHOOK_URL=https://example.com/hook
python -m spotted_images.job
```
