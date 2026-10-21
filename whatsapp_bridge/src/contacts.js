// Convert WhatsApp vCards to the same contact shape used by Cloud API webhooks.
// No raw HTML, images, or arbitrary vCard extensions are sent to the dashboard.
export function sharedContacts(content) {
  const cards = content.contactMessage ? [content.contactMessage] : content.contactsArrayMessage?.contacts;
  if (!Array.isArray(cards)) return null;
  const normalized = cards.slice(0, 100).map(card => {
    const contact = { name: { formatted_name: String(card.displayName ?? '').slice(0, 512) }, phones: [], emails: [] };
    const text = String(card.vcard ?? '').replace(/=\r?\n/g, '').replace(/\r?\n[ \t]/g, '');
    for (const line of text.split(/\r?\n/)) {
      const colon = line.indexOf(':');
      if (colon < 0) continue;
      const header = line.slice(0, colon);
      const field = header.split(';')[0].split('.').at(-1).toUpperCase();
      let value = line.slice(colon + 1);
      if (/;ENCODING=QUOTED-PRINTABLE/i.test(header)) {
        const bytes = [];
        for (let i = 0; i < value.length; i++) {
          if (value[i] === '=' && /^[0-9A-F]{2}$/i.test(value.slice(i + 1, i + 3))) {
            bytes.push(parseInt(value.slice(i + 1, i + 3), 16)); i += 2;
          } else bytes.push(...Buffer.from(value[i]));
        }
        const charset = /;CHARSET=([^;]+)/i.exec(header)?.[1] ?? 'utf-8';
        try { value = new TextDecoder(charset).decode(new Uint8Array(bytes)); } catch { value = Buffer.from(bytes).toString('utf8'); }
      }
      value = value.replace(/\\n/gi, ' ').replace(/\\([,;\\])/g, '$1').replace(/[\r\n\u0000-\u001f]/g, ' ').trim().slice(0, 512);
      if (field === 'FN' && value) contact.name.formatted_name = value;
      else if (field === 'N' && !contact.name.formatted_name) contact.name.formatted_name = value.split(';').filter(Boolean).reverse().join(' ');
      else if (field === 'TEL' && contact.phones.length < 20) {
        const waId = /;WAID=(\d+)/i.exec(header)?.[1];
        contact.phones.push({ phone: value.replace(/^tel:/i, ''), ...(waId ? { wa_id: waId } : {}) });
      } else if (field === 'EMAIL' && contact.emails.length < 20) contact.emails.push({ email: value });
      else if (field === 'ORG') contact.org = { company: value.split(';')[0] };
    }
    return contact;
  });
  const bounded = [];
  let bytes = 0;
  for (const card of normalized) {
    bytes += Buffer.byteLength(JSON.stringify(card));
    if (bytes > 64 * 1024) break;
    bounded.push(card);
  }
  if (bounded.length < cards.length) bounded.push({ name: { formatted_name: '[Additional contact details omitted]' } });
  return bounded;
}
