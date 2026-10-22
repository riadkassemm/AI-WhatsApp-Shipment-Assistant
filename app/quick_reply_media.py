"""Immutable PDF files for owner-scoped quick replies, outside the public web root."""
import os
import re
import uuid
from pathlib import Path

from app.config import settings
from app.support_media import SupportMediaError, max_media_bytes, _safe_filename


class QuickReplyMediaStore:
    def _path(self, key: str) -> Path:
        if not re.fullmatch(r'[0-9a-f]{32}', key):
            raise SupportMediaError('Invalid saved PDF reference.')
        return Path(settings.support_media_directory).expanduser().resolve() / 'quick-replies' / (key + '.pdf')

    def save(self, content: bytes, filename: str | None) -> dict:
        if not content.startswith(b'%PDF-'):
            raise SupportMediaError('Choose a valid PDF file.')
        if len(content) > max_media_bytes('document'):
            raise SupportMediaError('The PDF document is too large.')
        name = _safe_filename(str(filename or '').replace('\\', '/'), 'document.pdf')
        if not name.lower().endswith('.pdf'):
            name = name[:176] + '.pdf'
        key = uuid.uuid4().hex
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with path.open('xb') as output:
                os.chmod(path, 0o600)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {'attachment_key': key, 'attachment_filename': name, 'attachment_size': len(content)}

    def read(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise SupportMediaError('The saved PDF is unavailable. Refresh quick replies or upload it again.') from None

    def delete(self, key: str | None) -> None:
        if key:
            self._path(key).unlink(missing_ok=True)


quick_reply_media_store = QuickReplyMediaStore()
