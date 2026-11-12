"""보관된 메일 본문(HTML)을 다시 보낸다. 받는 사람은 MAIL_TO 시크릿.

사용: python tools/resend.py <html 경로> <제목> [첨부 경로...]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screener import mailer  # noqa: E402

html_path, subject, *attachments = sys.argv[1:]
attachments = [a for a in attachments if Path(a).exists()]
mailer.send(subject, Path(html_path).read_text(encoding="utf-8"), attachments)
