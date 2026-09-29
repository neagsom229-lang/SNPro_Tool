import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import current_app, render_template
from celery import shared_task
import logging

logger = logging.getLogger(__name__)


def send_email_sync(to_email, subject, template_name, context):
    """Send an email synchronously using Gmail SMTP configuration from app config."""
    app = current_app._get_current_object()
    mail_server = app.config.get("MAIL_SERVER", "smtp.gmail.com")
    mail_port = app.config.get("MAIL_PORT", 587)
    mail_use_tls = app.config.get("MAIL_USE_TLS", True)
    mail_username = app.config.get("MAIL_USERNAME")
    mail_password = app.config.get("MAIL_PASSWORD")
    mail_default_sender = app.config.get("MAIL_DEFAULT_SENDER", mail_username)

    if not mail_username or not mail_password:
        logger.error("SMTP credentials (MAIL_USERNAME / MAIL_PASSWORD) are not configured!")
        raise RuntimeError("SMTP credentials not configured.")

    html_body = render_template(f"emails/{template_name}.html", **context)
    try:
        text_body = render_template(f"emails/{template_name}.txt", **context)
    except Exception:
        text_body = "Please view this email in an HTML-compatible client."

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = mail_default_sender
    msg["To"] = to_email

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if mail_port == 465:
            server = smtplib.SMTP_SSL(mail_server, mail_port, timeout=30)
        else:
            server = smtplib.SMTP(mail_server, mail_port, timeout=30)
            if mail_use_tls:
                server.starttls()
        server.login(mail_username, mail_password)
        server.sendmail(mail_default_sender, [to_email], msg.as_string())
        server.quit()
        logger.info("Successfully sent email '%s' to %s", subject, to_email)
    except Exception as e:
        logger.exception("Failed to send email '%s' to %s: %s", subject, to_email, e)
        raise


@shared_task(name="auth.send_async_email", bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def send_async_email(self, to_email, subject, template_name, context):
    """Celery task to send emails asynchronously so signup/forgot-password aren't blocked by SMTP latency."""
    send_email_sync(to_email, subject, template_name, context)
