"""Exercise the existing dashboard functions without external services."""
import subprocess

from app.support_ui import _HTML


def run(script):
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_only_inactive_meta_entries_offer_saved_number_removal():
    source = _HTML[_HTML.index('function renderWhatsAppRegistry('):_HTML.index('function openWhatsAppNumberNameEditor(')]
    run(r"""
const assert = require('node:assert/strict');
const element = () => ({children: [], append(...nodes) {this.children.push(...nodes)}, replaceChildren() {this.children=[]}, setAttribute() {}});
const document = {createElement: element, createTextNode: value => ({textContent: value})};
const elements = {whatsappAccounts: element(), whatsappNumbers: element()};
const $ = id => elements[id];
const state = {whatsappBusy: false, linkedDeviceBusy: false};
const emptyMarkup = () => ({outer: element()});
const createSupportNumberLabel = element;
const supportNumberRaw = item => item.phone_number_id;
const createMetaChip = element;
const createIcon = element;
const calls = [];
const removeSavedWhatsAppNumber = id => calls.push(id);
""" + source + r"""
const entries = [
 {phone_number_id:'11111', source:'META_SYNC', is_enabled:false, provider_present:true},
 {phone_number_id:'22222', source:'WEBHOOK', is_enabled:false, provider_present:false},
 {phone_number_id:'33333', source:'META_SYNC', is_enabled:true, provider_present:true},
 {phone_number_id:'90005', source:'LINKED_DEVICE', is_enabled:false, provider_present:true}
];
renderWhatsAppRegistry({accounts:[], numbers:entries});
let rows = elements.whatsappNumbers.children;
const removal = row => row.children[1].children.find(node => node.textContent === 'Remove saved number');
assert.ok(removal(rows[0])); assert.ok(removal(rows[1]));
assert.equal(removal(rows[2]), undefined); assert.equal(removal(rows[3]), undefined);
removal(rows[1]).onclick();
assert.deepEqual(calls, ['22222']);
assert.ok(rows[3].children[1].children.some(node => node.textContent === 'Remove and unlink'));
state.whatsappBusy = true;
renderWhatsAppRegistry({accounts:[], numbers:entries});
assert.equal(removal(elements.whatsappNumbers.children[0]).disabled, true);
""")


def test_removal_waits_for_confirmation_backend_success_and_preserves_list_on_failure():
    source = _HTML[_HTML.index('async function removeSavedWhatsAppNumber('):_HTML.index('async function refreshWhatsAppRegistry(')]
    run(r"""
const assert = require('node:assert/strict');
const state = {whatsappBusy:false,linkedDeviceBusy:false,whatsappRegistry:{numbers:[{phone_number_id:'11111'}]}};
let manager = false, consent = false, fail = false;
const canManageWhatsApp = () => manager;
const confirmDialog = async options => {assert.match(options.message, /history|conversations/); assert.match(options.message, /will not add this entry back/); return consent};
const clearWhatsAppFeedback = () => {};
const renderWhatsAppRegistry = payload => {state.whatsappRegistry=payload};
const feedback = [];
const showWhatsAppFeedback = (...args) => feedback.push(args);
const calls = [];
let entered, release;
const requested = new Promise(resolve => {entered=resolve});
const api = async (path,options) => {
  calls.push([path, options.method]);
  if (fail) throw Error('Active conversations remain');
  entered(); return await new Promise(resolve => {release=resolve});
};
""" + source + r"""
(async () => {
 await removeSavedWhatsAppNumber('11111'); assert.equal(calls.length,0);
 manager=true;
 await removeSavedWhatsAppNumber('11111'); assert.equal(calls.length,0);
 consent=true;
 const pending=removeSavedWhatsAppNumber('11111');
 await requested;
 assert.equal(state.whatsappBusy,true);
 assert.equal(state.whatsappRegistry.numbers.length,1);
 await removeSavedWhatsAppNumber('11111');
 assert.equal(calls.length,1);
 release({numbers:[]}); await pending;
 assert.deepEqual(calls,[['/api/support/whatsapp-numbers/11111','DELETE']]);
 assert.equal(state.whatsappBusy,false); assert.equal(state.whatsappRegistry.numbers.length,0);
 assert.equal(feedback[0][0],'whatsappSuccess');
 state.whatsappRegistry={numbers:[{phone_number_id:'22222'}]}; fail=true;
 await removeSavedWhatsAppNumber('22222');
 assert.equal(state.whatsappRegistry.numbers[0].phone_number_id,'22222');
 assert.equal(feedback.at(-1)[0],'whatsappError'); assert.equal(state.whatsappBusy,false);
})().catch(error => {console.error(error);process.exitCode=1});
""")
