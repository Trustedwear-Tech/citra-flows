"""
Workflow Notifications
=====================
Email notifications for human-approval pauses, approval timeouts,
and workflow deployment events.

Uses the User Service email API (AWS SES) for the sending layer.
Deep links point into the Citra UI approval page.
"""

from __future__ import annotations
import logging
import os
from datetime import datetime
from typing import Optional

from .config import DEFAULT_APPROVAL_TIMEOUT_HOURS, MAX_EMAIL_PREVIEW_SIZE

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8081")


async def send_approval_notification(
    *,
    to_email: str,
    user_name: str,
    workflow_name: str,
    node_label: str,
    message: str,
    execution_id: str,
    approval_id: str,
    timeout_hours: float = DEFAULT_APPROVAL_TIMEOUT_HOURS,
    data_preview: Optional[str] = None,
) -> bool:
    """Send an email notifying the user that a workflow is waiting for approval.

    The email contains a deep link into the Citra UI approval queue.
    """
    approval_url = f"{FRONTEND_URL}?view=approval&execution={execution_id}"

    subject = f"⏳ Approval Required: {workflow_name} — {node_label}"

    preview_block = ""
    if data_preview:
        safe_preview = str(data_preview)
        truncated_notice = ""
        if len(safe_preview) > MAX_EMAIL_PREVIEW_SIZE:
            safe_preview = safe_preview[:MAX_EMAIL_PREVIEW_SIZE]
            truncated_notice = f"""
            <p style="color:#64748b;font-size:13px;font-style:italic;">Content truncated for email. <a href="{approval_url}" style="color:#4f46e5;">View full details in Citra</a></p>"""
        preview_block = f"""
            <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin:16px 0;font-family:monospace;font-size:13px;white-space:pre-wrap;max-height:300px;overflow:auto;">
{safe_preview}
            </div>{truncated_notice}"""

    timeout_text = f"This approval will expire in {int(timeout_hours)} hours." if timeout_hours else ""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;line-height:1.6;color:#333;max-width:600px;margin:0 auto;padding:20px;background:#f4f4f4;">
  <div style="background:#fff;border-radius:12px;padding:40px;box-shadow:0 4px 12px rgba(0,0,0,.1);">
    <div style="text-align:center;margin-bottom:24px;">
      <h1 style="color:#4f46e5;margin:0;">🧠 Citra AI</h1>
      <p style="color:#64748b;margin:4px 0 0;">Workflow Approval Required</p>
    </div>

    <p>Hi {user_name or 'there'},</p>

    <p>Your workflow <strong>{workflow_name}</strong> has reached a step that requires your review:</p>

    <div style="background:#fefce8;border-left:4px solid #f59e0b;padding:16px;border-radius:0 8px 8px 0;margin:16px 0;">
      <strong style="color:#92400e;">📋 {node_label}</strong><br>
      <span style="color:#78350f;">{message}</span>
    </div>

    {preview_block}

    <p style="color:#64748b;font-size:14px;">{timeout_text}</p>

    <div style="text-align:center;margin:32px 0;">
      <a href="{approval_url}" style="display:inline-block;background:#4f46e5;color:#fff;padding:14px 32px;border-radius:8px;text-decoration:none;font-weight:600;font-size:16px;">
        Review &amp; Approve
      </a>
    </div>

    <p style="color:#94a3b8;font-size:12px;text-align:center;">
      Or open Citra AI and go to Workflows → Approval Queue
    </p>
  </div>
  <div style="text-align:center;margin-top:16px;color:#94a3b8;font-size:12px;">
    © {datetime.utcnow().year} Citra AI · <a href="{FRONTEND_URL}" style="color:#94a3b8;">citra-ai.com</a>
  </div>
</body>
</html>"""

    text = (
        f"Approval Required: {workflow_name}\n\n"
        f"Hi {user_name or 'there'},\n\n"
        f"Your workflow \"{workflow_name}\" is waiting for approval at: {node_label}\n"
        f"Message: {message}\n\n"
        f"{timeout_text}\n\n"
        f"Review and approve here: {approval_url}\n"
    )

    return await _send_email(to_email, subject, text, html)


async def send_approval_timeout_notification(
    *,
    to_email: str,
    user_name: str,
    workflow_name: str,
    node_label: str,
    execution_id: str,
) -> bool:
    """Notify user that an approval timed out."""
    subject = f"⏰ Approval Timed Out: {workflow_name}"
    text = (
        f"Hi {user_name or 'there'},\n\n"
        f"The approval for workflow \"{workflow_name}\" at step \"{node_label}\" "
        f"has timed out and the execution has been marked as failed.\n\n"
        f"You can re-run the workflow from the Citra AI dashboard.\n"
    )
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:#fff;border-radius:12px;padding:32px;box-shadow:0 2px 8px rgba(0,0,0,.08);">
    <h2 style="color:#ef4444;">⏰ Approval Timed Out</h2>
    <p>Hi {user_name or 'there'},</p>
    <p>The approval for <strong>{workflow_name}</strong> at step <strong>{node_label}</strong> has expired.</p>
    <p>The execution has been automatically marked as <strong>failed</strong>.</p>
    <p><a href="{FRONTEND_URL}" style="color:#4f46e5;">Open Citra AI</a> to re-run the workflow.</p>
  </div>
</body>
</html>"""
    return await _send_email(to_email, subject, text, html)


async def send_execution_failure_notification(
    *,
    to_email: str,
    user_name: str,
    workflow_name: str,
    execution_id: str,
    failed_node_label: str,
    error_message: str,
    retry_count: int = 0,
    retry_errors: Optional[list] = None,
) -> bool:
    """Notify user that a workflow execution failed.

    Sent when a node exhausts all retry attempts (or has no retry configured)
    and the entire execution is marked FAILED.
    """
    execution_url = f"{FRONTEND_URL}?view=execution&execution={execution_id}"

    retry_info = ""
    if retry_count > 0:
        retry_info = f"<p style='color:#64748b;font-size:14px;'>The node retried <strong>{retry_count}</strong> time(s) before failing.</p>"
        if retry_errors:
            attempts_html = "".join(
                f"<li style='margin:4px 0;color:#64748b;font-size:13px;'><strong>Attempt {i + 1}:</strong> {err}</li>"
                for i, err in enumerate(retry_errors)
            )
            retry_info += f"<ul style='margin:8px 0;padding-left:20px;'>{attempts_html}</ul>"

    subject = f"❌ Workflow Failed: {workflow_name}"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;line-height:1.6;color:#333;max-width:600px;margin:0 auto;padding:20px;background:#f4f4f4;">
  <div style="background:#fff;border-radius:12px;padding:40px;box-shadow:0 4px 12px rgba(0,0,0,.1);">
    <div style="text-align:center;margin-bottom:24px;">
      <h1 style="color:#4f46e5;margin:0;">🧠 Citra AI</h1>
      <p style="color:#64748b;margin:4px 0 0;">Workflow Execution Failed</p>
    </div>

    <p>Hi {user_name or 'there'},</p>

    <p>Your workflow <strong>{workflow_name}</strong> has failed during execution.</p>

    <div style="background:#fef2f2;border-left:4px solid #ef4444;padding:16px;border-radius:0 8px 8px 0;margin:16px 0;">
      <strong style="color:#991b1b;">❌ Failed Node: {failed_node_label}</strong><br>
      <span style="color:#7f1d1d;">{error_message}</span>
    </div>

    <p style="color:#64748b;font-size:13px;"><a href="{execution_url}" style="color:#4f46e5;">View full execution details in Citra</a></p>

    {retry_info}

    <div style="text-align:center;margin:32px 0;">
      <a href="{execution_url}" style="display:inline-block;background:#4f46e5;color:#fff;padding:14px 32px;border-radius:8px;text-decoration:none;font-weight:600;font-size:16px;">
        View Execution Details
      </a>
    </div>

    <p style="color:#94a3b8;font-size:12px;text-align:center;">
      Execution ID: {execution_id}
    </p>
  </div>
  <div style="text-align:center;margin-top:16px;color:#94a3b8;font-size:12px;">
    © {datetime.utcnow().year} Citra AI · <a href="{FRONTEND_URL}" style="color:#94a3b8;">citra-ai.com</a>
  </div>
</body>
</html>"""

    text = (
        f"Workflow Failed: {workflow_name}\n\n"
        f"Hi {user_name or 'there'},\n\n"
        f"Your workflow \"{workflow_name}\" failed at step \"{failed_node_label}\".\n"
        f"Error: {error_message}\n"
        + (f"Retries: {retry_count}\n" if retry_count else "")
        + f"\nView details: {execution_url}\n"
    )

    return await _send_email(to_email, subject, text, html)


async def _send_email(to_addr: str, subject: str, text: str, html: str) -> bool:
    """Send via User Service API (uses AWS SES)."""
    import httpx

    user_service_url = os.getenv("USER_SERVICE_URL", "http://localhost:7004")
    payload = {
        "to": to_addr,
        "subject": subject,
        "body": text,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{user_service_url}/api/send-workflow-email",
                json=payload,
            )
        if resp.status_code == 200:
            logger.info("Workflow notification sent to %s: %s", to_addr, subject)
            return True
        else:
            # A failure alert that can't be DELIVERED is itself a failure the
            # BA would never see. Log at ERROR so the observability stack
            # (GlitchTip → support email) catches it and someone fixes the
            # delivery path. Don't let it stay a quiet warning.
            logger.error(
                "ALERT DELIVERY FAILED — could not email workflow failure to %s "
                "(User Service returned %d: %s). The BA will not be notified by "
                "email; the run is still marked FAILED in the Workflows tab.",
                to_addr, resp.status_code, resp.text[:200],
            )
            return False
    except Exception as e:
        logger.error(
            "ALERT DELIVERY FAILED — exception emailing workflow failure to %s: %s. "
            "The BA will not be notified by email; the run is still marked FAILED.",
            to_addr, e,
        )
        return False
