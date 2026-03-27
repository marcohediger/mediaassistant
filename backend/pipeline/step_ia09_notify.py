import smtplib
import ssl
from email.mime.text import MIMEText
from config import config_manager


async def execute(job, session) -> dict:
    """IA-09: Benachrichtigung — E-Mail bei Fehlern senden."""
    if not await config_manager.is_module_enabled("smtp"):
        return {"status": "skipped", "reason": "module disabled"}

    server = await config_manager.get("smtp.server")
    recipient = await config_manager.get("smtp.recipient")
    if not server or not recipient:
        return {"status": "skipped", "reason": "not configured"}

    # Collect errors from previous steps
    step_results = job.step_result or {}
    errors = []
    for step_code, result in step_results.items():
        if isinstance(result, dict) and result.get("status") == "error":
            errors.append(f"[{step_code}] {result.get('reason', 'unbekannt')}")

    if not errors:
        return {"status": "skipped", "reason": "no errors to report"}

    # Build email
    subject = f"MediaAssistant: {job.debug_key} — {len(errors)} Fehler"
    body = (
        f"Datei: {job.filename}\n"
        f"Debug-Key: {job.debug_key}\n"
        f"Pfad: {job.original_path}\n\n"
        f"Fehler:\n" + "\n".join(f"  {e}" for e in errors)
    )

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["To"] = recipient
    sender = await config_manager.get("smtp.user", recipient)
    msg["From"] = sender

    # Send
    port = int(await config_manager.get("smtp.port", 587))
    use_ssl = await config_manager.get("smtp.ssl", True)
    user = await config_manager.get("smtp.user", "")
    password = await config_manager.get("smtp.password", "")

    context = ssl.create_default_context()

    if use_ssl:
        with smtplib.SMTP_SSL(server, port, timeout=10, context=context) as smtp:
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(server, port, timeout=10) as smtp:
            smtp.starttls(context=context)
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)

    return {"sent": True, "recipient": recipient, "errors_reported": len(errors)}
