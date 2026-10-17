"""SMTP 메일 발송. 시크릿이 없으면 발송을 건너뛰고 HTML 파일만 남긴다."""
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


def send(subject, html_body, attachments=()):
    # 시크릿이 비어 있으면 빈 문자열로 들어오므로 'or'로 기본값 처리
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    to = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]
    if not (user and password and to):
        print("[mail] SMTP_USER / SMTP_PASSWORD / MAIL_TO 가 없어 발송을 건너뜁니다.")
        return False

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM", user)
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    for path in attachments:
        p = Path(path)
        part = MIMEApplication(p.read_bytes(), Name=p.name)
        part["Content-Disposition"] = f'attachment; filename="{p.name}"'
        msg.attach(part)

    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        s.login(user, password)
        s.sendmail(msg["From"], to, msg.as_string())
    print(f"[mail] {len(to)}명에게 발송 완료")
    return True
