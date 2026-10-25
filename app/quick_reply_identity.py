"""Equality for saved reply distribution; independent owner edits remain private."""
from decimal import Decimal, ROUND_HALF_UP


def quick_reply_identity(row: dict) -> tuple | None:
    base = (row.get('title') or '', row.get('body') or '')
    if row.get('attachment_key'):
        digest = row.get('attachment_sha256')
        # A missing/unreadable legacy file is not proof of identical content.
        if not digest:
            return None
        return ('pdf', *base, row.get('attachment_filename'), digest)
    if row.get('location_latitude') is not None:
        def coordinate(value):
            return Decimal(str(value)).quantize(Decimal('0.0000001'), rounding=ROUND_HALF_UP)
        return ('location', *base, coordinate(row['location_latitude']),
                coordinate(row['location_longitude']), row.get('location_name') or '',
                row.get('location_address') or '')
    return ('text', *base)
