from app.support_contacts import format_shared_contacts
from app.whatsapp_client import extract_incoming_message
from app.support_service import _serialize_support_messages, _safe_context_value


def test_cloud_contacts_are_readable_and_do_not_become_ai_text():
    cards = [{'name': {'formatted_name': 'Maya Example'}, 'phones': [{'phone': '+961 70 123456'}, {'wa_id': '905551234567'}],
              'emails': [{'email': 'maya@example.test'}], 'org': {'company': 'Example Co'}},
             {'name': {'first_name': 'Second', 'last_name': 'Person'}, 'phones': [{'phone': '+1 202 555 0100'}]}]
    incoming = extract_incoming_message({'entry': [{'changes': [{'value': {'messages': [
        {'from': '155500042', 'id': 'wamid.contact', 'type': 'contacts', 'contacts': cards}]
    }}]}]})
    assert incoming.contacts == cards and incoming.text is None
    body = format_shared_contacts(incoming.contacts)
    for value in ['Maya Example', '+961 70 123456', '+905551234567', 'maya@example.test', 'Example Co', 'Second Person']:
        assert value in body


def test_staff_authored_password_is_visible_but_context_and_customer_auth_remain_redacted():
    body = 'code:lb12953\npassword: sample-test-password'
    row = {'id': 5, 'ticket_id': 42, 'message_type': 'text', 'sender_type': 'AGENT', 'direction': 'OUTBOUND', 'body': body}
    assert _serialize_support_messages(42, [row])[0]['body'] == body
    assert 'sample-test-password' not in _serialize_support_messages(42, [{**row, 'sender_type': 'CUSTOMER', 'direction': 'INBOUND'}])[0]['body']
    assert _serialize_support_messages(42, [{**row, 'deleted_for_everyone_at': 'now'}])[0]['body'] is None
    assert _safe_context_value({'password': 'sample-test-password'}) == {'password': '[REDACTED]'}


def test_cloud_contact_enters_human_transcript_with_details():
    from types import SimpleNamespace
    from app.main import _human_inbound_body
    incoming = SimpleNamespace(message_type='contacts', contacts=[{'name': {'formatted_name': 'Example'}, 'phones': [{'phone': '+96170123456'}]}])
    assert _human_inbound_body(incoming, None) == 'Example\nPhone: +96170123456'


def test_shared_contact_and_staff_password_render_and_copy_in_browser(tmp_path):
    import os
    import shutil
    import subprocess
    from pathlib import Path
    from app.support_ui import _HTML
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install the configured test browser'
    add_message = _HTML[_HTML.index('function addMessage('):_HTML.index('// Compact message toolbar:')]
    links = _HTML[_HTML.index('function appendMessageTextWithLinks('):_HTML.index('function createVoiceNotePlayer(')]
    copy = _HTML[_HTML.index('function messageTextForCopy('):_HTML.index('async function copyMessageText(')]
    script = r'''
const state = {conversationSearchOpen: false};
const messageClass = m => m.sender_type === 'AGENT' ? 'agent' : 'customer';
const messageActor = m => m.sender_type === 'AGENT' ? 'Staff' : 'Customer';
const initials = () => 'EX', renderReplyQuote = () => {}, canChangeWhatsAppMessage = () => false;
const fmt = () => '12:00', messageOccurredAt = () => '2026-10-21T12:00:00Z';
const labelMessageAction = () => {}, messageReactionBar = () => null;
let copied;
const copyMessageText = text => {copied = text};
'''+add_message+links+copy+r'''
try {
  const c = document.getElementById('messages');
  const body = 'Maya Example\nPhone: +96170123456\nEmail: maya@example.test';
  addMessage(c, {sender_type:'CUSTOMER', direction:'INBOUND', message_type:'contacts', body}, 'history');
  if (!c.querySelector('.shared-contact-label') || !c.textContent.includes('+96170123456')) throw Error('Contact details missing');
  c.querySelector('button').click();
  if (copied !== body) throw Error('Contact copy changed details');
  const password = 'code:lb12953\npassword: sample-test-password';
  addMessage(c, {sender_type:'AGENT', direction:'OUTBOUND', message_type:'text', body:password}, 'history');
  if (!c.textContent.includes('password: sample-test-password')) throw Error('Staff password missing');
  addMessage(c, {sender_type:'CUSTOMER', direction:'INBOUND', message_type:'contacts', body:'<img src=x onerror=alert(1)>'}, 'history');
  if (c.querySelector('img')) throw Error('Contact executed HTML');
  document.body.dataset.result = 'passed';
} catch (error) {document.body.dataset.result = 'failed: ' + error.message}
'''
    css = (Path(__file__).resolve().parents[1] / 'app/static/support/support.css').read_text()
    page = tmp_path / 'shared-contacts.html'
    page.write_text('<html><head><style>'+css+'body{padding:32px;overflow:auto}#messages{max-width:800px}</style></head><body><div id="messages"></div><script>'+script+'</script></body></html>')
    result = subprocess.run([browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
        '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--user-data-dir='+str(tmp_path/'profile'), '--virtual-time-budget=1000',
        '--screenshot='+str(tmp_path/'shared-contacts.png'), '--window-size=1000,800', '--dump-dom', page.as_uri()],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'data-result="passed"' in result.stdout, result.stdout[-2500:]
