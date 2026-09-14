"""Configure the dedicated demo bot without printing credentials or sending messages."""

import argparse
import json
import secrets
from pathlib import Path

import boto3
import httpx
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["configure", "register", "status"])
    args = parser.parse_args()
    session = boto3.Session(region_name="us-east-1")
    config = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1})
    stack = session.client("cloudformation", config=config).describe_stacks(
        StackName="StewardNorthgateLive"
    )["Stacks"][0]
    outputs = {row["OutputKey"]: row["OutputValue"] for row in stack["Outputs"]}
    sm = session.client("secretsmanager", config=config)
    cfg = json.loads(
        sm.get_secret_value(SecretId=outputs["ChannelConfigurationSecret"])["SecretString"]
    )
    if args.action == "configure":
        local = json.loads((ROOT / ".scratch/telegram.local.json").read_text())
        cfg.update(
            telegram_enabled=True,
            telegram_bot_token=local["telegram_bot_token"],
            telegram_bot_username=local["telegram_bot_username"],
            telegram_webhook_secret=cfg.get("telegram_webhook_secret") or secrets.token_urlsafe(32),
        )
    token = cfg["telegram_bot_token"]
    with httpx.Client(timeout=20, trust_env=False) as client:

        def call(method, payload=None):
            try:
                response = client.post(
                    "https://api.telegram.org/bot" + token + "/" + method, json=payload or {}
                )
                result = response.json()
            except (httpx.HTTPError, ValueError):
                raise RuntimeError(
                    "Telegram request failed; credential-bearing URL suppressed"
                ) from None
            if not result.get("ok"):
                raise RuntimeError(
                    "Telegram rejected " + method + ": " + str(result.get("error_code"))
                )
            return result["result"]

        bot = call("getMe")
        if bot["username"].lower() != cfg["telegram_bot_username"].lower():
            raise RuntimeError("Saved bot identity does not match configuration")
        if args.action == "configure":
            sm.put_secret_value(
                SecretId=outputs["ChannelConfigurationSecret"], SecretString=json.dumps(cfg)
            )
            print("Bot configuration saved. Refresh API and worker before webhook registration.")
            return
        endpoint = outputs["ConsoleUrl"] + "/api/channels/telegram"
        if args.action == "register":
            # Existing queued updates are retained. Polling must be stopped before this action.
            call(
                "setWebhook",
                {
                    "url": endpoint,
                    "secret_token": cfg["telegram_webhook_secret"],
                    "allowed_updates": ["message", "my_chat_member"],
                    "drop_pending_updates": False,
                    "max_connections": 5,
                },
            )
        info = call("getWebhookInfo")
        report = {
            "bot": bot["username"],
            "configured": bool(cfg.get("telegram_enabled")),
            "webhook_matches": info.get("url") == endpoint,
            "pending_updates": info.get("pending_update_count"),
            "last_error": info.get("last_error_message"),
        }
        (ROOT / "artifacts/validation/cloud-telegram.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))


if __name__ == "__main__":
    main()
