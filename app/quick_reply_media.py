"""Immutable attachment files for owner-scoped quick replies, outside the public web root."""
import hashlib
import os
import re
import uuid
from pathlib import Path

from app.config import settings
from app.support_media import SupportMediaError, max_media_bytes, _safe_filename


class QuickReplyMediaStore:
    def _path(self, key: str, suffix: str = ".pdf") -> Path:
        if not re.fullmatch(r'[0-9a-f]{32}', key):
            raise SupportMediaError('Invalid saved attachment reference.')
        return Path(settings.support_media_directory).expanduser().resolve() / 'quick-replies' / (key + suffix)

    def save(self, content: bytes, filename: str | None, mime_type: str = "application/pdf") -> dict:
        extensions = {'application/pdf': '.pdf', 'image/jpeg': '.jpg', 'image/png': '.png'}
        if mime_type not in extensions:
            raise SupportMediaError('Choose a JPEG/PNG image or PDF file.')
        if mime_type == 'application/pdf':
            valid = content.startswith(b'%PDF-')
        elif mime_type == 'image/jpeg':
            valid = content.startswith(b'\xff\xd8\xff') and content.endswith(b'\xff\xd9')
        else:
            valid = (content.startswith(b'\x89PNG\r\n\x1a\n')
                     and content[12:16] == b'IHDR' and b'IEND' in content[-12:])
        if not valid:
            raise SupportMediaError('Choose a valid PDF file.' if mime_type == 'application/pdf' else 'Choose a valid JPEG/PNG image.')
        kind = 'document' if mime_type == 'application/pdf' else 'image'
        if len(content) > max_media_bytes(kind):
            raise SupportMediaError('The saved attachment is too large.')
        extension = extensions[mime_type]
        name = _safe_filename(str(filename or '').replace('\\', '/'), 'attachment' + extension)
        accepted_extensions = ('.jpg', '.jpeg') if mime_type == 'image/jpeg' else (extension,)
        if not name.lower().endswith(accepted_extensions):
            name = name[:176] + extension
        key = uuid.uuid4().hex
        path = self._path(key, '.pdf' if kind == 'document' else '.bin')
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
        return {'attachment_key': key, 'attachment_filename': name, 'attachment_size': len(content),
                'attachment_sha256': hashlib.sha256(content).hexdigest(), 'attachment_mime_type': mime_type}

    def read(self, key: str) -> bytes:
        try:
            path = self._path(key)
            return (path if path.exists() else self._path(key, '.bin')).read_bytes()
        except FileNotFoundError:
            raise SupportMediaError('The saved attachment is unavailable. Refresh quick replies or upload it again.') from None

    def delete(self, key: str | None) -> None:
        if key:
            self._path(key).unlink(missing_ok=True)
            self._path(key, '.bin').unlink(missing_ok=True)


quick_reply_media_store = QuickReplyMediaStore()
