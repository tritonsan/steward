"""SES receipt metadata travels alongside the S3 object through a trusted SQS queue."""

import json

from steward.domain.clock import parse_datetime
from steward.store import WorkflowArtifact
from steward.store.workflow import stable_id


class ReceiptQueue:
    def __init__(self, runtime, *, queue_url, client=None):
        import boto3

        self.runtime, self.url = runtime, queue_url
        self.client = client or boto3.Session(
            profile_name=runtime._settings.aws_profile, region_name=runtime._settings.aws_region
        ).client("sqs")

    def poll(self):
        response = self.client.receive_message(
            QueueUrl=self.url, MaxNumberOfMessages=5, WaitTimeSeconds=1, VisibilityTimeout=300
        )
        count = 0
        for message in response.get("Messages", []):
            envelope = json.loads(message["Body"])
            event = (
                json.loads(envelope["Message"])
                if envelope.get("Type") == "Notification"
                else envelope
            )
            self.ingest(event)
            # Model inference runs later from the durable job, after this acknowledgement.
            self.client.delete_message(QueueUrl=self.url, ReceiptHandle=message["ReceiptHandle"])
            count += 1
        return count

    def ingest(self, event):
        runtime = self.runtime
        if event.get("notificationType") != "Received":
            raise ValueError("expected SES receipt notification")
        mail, receipt = event["mail"], event["receipt"]
        mid = mail["messageId"]
        source = stable_id("ses-receipt", mid)
        prior = runtime.store.artifact("ses.receipt.completed.v1", source)
        if prior:
            return prior.payload
        runtime.store.put_configuration(
            WorkflowArtifact(
                artifact_id=source,
                kind="ses.receipt.v1",
                created_at=parse_datetime(mail["timestamp"].replace("Z", "+00:00")),
                payload=event,
            )
        )
        verdicts = {
            k: receipt.get(k, {}).get("status")
            for k in ("dmarcVerdict", "spamVerdict", "virusVerdict")
        }
        if any(v != "PASS" for v in verdicts.values()):
            result = {
                "status": "quarantined",
                "reason": "sender_authentication_or_content_check",
                "verdicts": verdicts,
            }
        else:
            if runtime._inbound_reader is None:
                raise ValueError("SES inbound reader is not configured")
            inbound = runtime._inbound_reader.read(
                "mail/" + mid,
                received_at=parse_datetime(mail["timestamp"].replace("Z", "+00:00")),
            )
            from dataclasses import asdict

            from steward.procurement.replies import (
                VendorReplyAuthorizationError,
                VendorReplyRouteError,
            )

            try:
                # A verified attachment-only reply still needs a routed review when
                # the PDF cannot be read. It must never enter quote extraction.
                route = runtime._vendor_replies._route(inbound, allow_unreadable_attachment=True)
            except (VendorReplyAuthorizationError, VendorReplyRouteError) as exc:
                result = {
                    "status": "quarantined",
                    "reason": "unknown_sender"
                    if isinstance(exc, VendorReplyAuthorizationError)
                    else "case_routing",
                    "verdicts": verdicts,
                }
            else:
                raw = asdict(inbound)
                raw["received_at"] = inbound.received_at.isoformat()
                with runtime.store.atomic():
                    if not runtime.store.artifact("ses.raw.v1", source):
                        runtime.store.put_configuration(
                            WorkflowArtifact(
                                artifact_id=source,
                                kind="ses.raw.v1",
                                case_id=route.case.case_id,
                                created_at=runtime._clock.now(),
                                payload={"inbound": raw, "verdicts": verdicts},
                            )
                        )
                    if inbound.unreadable_attachments:
                        result = {
                            "status": "quarantined",
                            "reason": "unreadable_attachment",
                            "attachments": list(inbound.unreadable_attachments),
                            "case_id": route.case.case_id,
                            "verdicts": verdicts,
                        }
                    else:
                        runtime.store.enqueue_job(
                            runtime._maintenance.job(
                                route.case,
                                "mail.process",
                                due_at=runtime._clock.now(),
                                source_id=source,
                            )
                        )
                        result = {
                            "status": "queued",
                            "case_id": route.case.case_id,
                            "verdicts": verdicts,
                        }
        runtime.store.put_configuration(
            WorkflowArtifact(
                artifact_id=source,
                kind="ses.receipt.completed.v1",
                created_at=runtime._clock.now(),
                payload=result,
            )
        )
        return result
