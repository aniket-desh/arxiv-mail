from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .http import tls_context


def check_gmail_login(*, sender: str, app_password: str) -> None:
    context = tls_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as smtp:
        smtp.login(sender, app_password)


def send_gmail(
    *,
    sender: str,
    recipient: str,
    app_password: str,
    subject: str,
    text_body: str,
    html_body: str,
    run_id: str,
) -> None:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["X-Arxivmail-Run-ID"] = run_id
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    context = tls_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=45) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(message)
