"""Render WhatsApp's structured shared contacts as searchable, copyable text."""
from typing import Any


def format_shared_contacts(contacts: Any) -> str:
    if not isinstance(contacts, list):
        return 'Shared contact (no readable details)'

    def clean(value: Any) -> str:
        return ' '.join(str(value or '').split())[:512]

    cards = []
    for contact in contacts[:100]:
        if not isinstance(contact, dict):
            continue
        name = contact.get('name') if isinstance(contact.get('name'), dict) else {}
        title = clean(name.get('formatted_name')) or ' '.join(filter(None, (
            clean(name.get('first_name')), clean(name.get('last_name'))))) or 'Shared contact'
        lines = [title]
        for kind, field, label in [('phones', 'phone', 'Phone'), ('emails', 'email', 'Email')]:
            entries = contact.get(kind)
            if not isinstance(entries, list):
                continue
            for entry in entries[:20]:
                if not isinstance(entry, dict):
                    continue
                value = clean(entry.get(field))
                if kind == 'phones' and not value and str(entry.get('wa_id') or '').isdigit():
                    value = '+' + str(entry['wa_id'])
                if value and f'{label}: {value}' not in lines:
                    lines.append(f'{label}: {value}')
        org = contact.get('org')
        if isinstance(org, dict) and clean(org.get('company')):
            lines.append('Company: ' + clean(org['company']))
        cards.append('\n'.join(lines))
    body = '\n\n'.join(cards) or 'Shared contact (no readable details)'
    return body if len(body) <= 16000 else body[:15950] + '\n[Additional contact details omitted]'
