import { VoiceClient, type TranscriptMessage, type VoiceStatus } from '@cloudflare/voice/client';
import './style.css';

type Product = { name: string; quantity: number; max_price: number | null; preferred_name: string | null; brand: string | null; sku: string | null; alternatives: unknown[] };
type Action = { kind: string; name: string; quantity: number | null; max_price: number | null; location: string | null };
type State = {
  draft: string;
  preview: null | { token: string; status: 'preview'; actions: Action[]; items: Product[]; message: string; expiresAt: number };
  run: null | { token: string; status: string; message: string };
  busy: boolean;
  error: string | null;
  clarification?: string | null;
  muted: boolean;
};
type Config = { speechConfigured: boolean; modelConfigured: boolean; bridgeConfigured: boolean };

function isState(value: unknown): value is State {
  if (!value || typeof value !== 'object') return false;
  const data = value as Partial<State>;
  return typeof data.draft === 'string' && typeof data.busy === 'boolean' && 'preview' in data && 'run' in data && 'error' in data;
}

function stateErrorReason(error: string | null): string {
  if (!error) return '';
  const reasons: Record<string, string> = {
    'Preview changed, expired, or busy. Review a new preview.': 'Preview changed, expired, or busy. Wait for the current request, then generate a new preview.',
    'Shopping or interpretation is already running.': 'A request is already running. Wait for its result before changing the list.',
    'Use a request shorter than 4 KB.': 'Your request is too long or empty. Enter a nonempty request shorter than 4 KB.',
    'Bridge unavailable.': 'Shopping bridge unavailable. Check the local service, then reload state & settings.',
    'Invalid preview.': 'The preview could not be validated. Generate a new preview before running shop.',
  };
  return reasons[error] || 'The request could not be completed. Check names, quantities and price caps, then generate a new preview. If this continues, check the service and reload state & settings.';
}

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const draft = $<HTMLTextAreaElement>('draft');
const mic = $<HTMLButtonElement>('microphone');
const mute = $<HTMLButtonElement>('mute');
const generate = $<HTMLButtonElement>('preview-button');
const confirm = $<HTMLButtonElement>('confirm');
const retry = $<HTMLButtonElement>('retry');
const lookup = $<HTMLButtonElement>('lookup');
const readResult = $<HTMLButtonElement>('read-result');
let resultAudio: HTMLAudioElement | null = null;
let resultAudioURL = '';
let readInProgress = false;
let speechRequest: AbortController | null = null;
let speechSequence = 0;
let state: State = { draft: '', preview: null, run: null, busy: false, error: null, muted: false };
let config: Config | null = null;
let savedItems: Product[] = [];
let voice: VoiceClient | null = null;
let voiceStatus: VoiceStatus = 'idle';
let connected = false;
let ready = false;
let sessionStarted = false;
let loading = false;
let active = false;
let starting = false;
let pending = false;
let dirty = false;
let revision = 0;
let speechInvalidated = false;
let voiceRevision: number | null = null;
let voiceFinal = false;
let problem = '';
let voiceProblem = '';
let uncertain = false;
let pendingToken = '';
let pollTimer: ReturnType<typeof setTimeout> | undefined;
let mutationQueue: Promise<unknown> = Promise.resolve();
try { pendingToken = sessionStorage.getItem('shopping-pending-run') || ''; uncertain = !!pendingToken; } catch { /* State lookup still works without browser storage. */ }

function rememberToken(token: string) {
  pendingToken = token;
  try { token ? sessionStorage.setItem('shopping-pending-run', token) : sessionStorage.removeItem('shopping-pending-run'); } catch { /* Do not retry a run if storage is unavailable. */ }
}

async function api<T>(path: string, body?: unknown): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), path === '/api/preview' ? 90000 : 20000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal,
    });
    if (!response.ok && response.status !== 409) throw new Error(response.status === 401 || response.status === 403
      ? 'Session access expired. Reload this page to reconnect.'
      : `${path} failed (${response.status}). Check the service, then reload state & settings.`);
    if (response.status === 204) return undefined as T;
    let data: unknown;
    try { data = await response.json(); }
    catch { throw new Error('The service returned an unreadable response. Reload state & settings.'); }
    if (response.status === 409 && !isState(data)) throw new Error('The request was rejected. Reload state & settings before continuing.');
    return data as T;
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw new Error(`${path} timed out. Its outcome is not yet known.`);
    if (error instanceof TypeError) throw new Error(`${path} could not connect. Check your connection and reload state & settings.`);
    throw error;
  } finally { clearTimeout(timeout); }
}

const errorText = (error: unknown) => error instanceof Error ? error.message : 'Request failed. Reload state & settings.';
const serial = <T>(work: () => Promise<T>): Promise<T> => {
  const result = mutationQueue.then(work);
  mutationQueue = result.catch(() => undefined);
  return result;
};
const runActive = () => state.busy || state.run?.status === 'running';
const working = () => runActive() || pending || uncertain;
const voiceWorking = () => active || starting || voiceStatus === 'thinking' || voiceStatus === 'speaking';
const fresh = () => !!state.preview && state.preview.status === 'preview' && Number.isFinite(state.preview.expiresAt) && state.preview.expiresAt > Date.now() && !dirty;

function node(tag: string, text: unknown, className = '') {
  const element = document.createElement(tag);
  element.textContent = text == null ? '' : String(text);
  element.className = className;
  return element;
}

function announce(id: string, text: string) {
  if ($(id).textContent !== text) $(id).textContent = text;
}

function describeAlternative(value: unknown): string {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object') return 'Unspecified alternative';
  const item = value as Record<string, unknown>;
  return ['name', 'preferred_name', 'brand', 'sku', 'package_size', 'max_price'].flatMap(key =>
    item[key] == null ? [] : [`${key.replaceAll('_', ' ')}: ${String(item[key])}`]).join(' · ') || 'Unspecified alternative';
}

function renderList() {
  const preview = state.preview;
  const items = preview ? preview.items : savedItems;
  $('list-heading').textContent = preview ? 'Merged preview list' : 'Saved shopping list';
  $('count').textContent = `${items.length} ${items.length === 1 ? 'item' : 'items'}`;
  const list = $('items');
  list.replaceChildren();
  if (!items.length) list.append(node('li', preview ? 'This preview has no items. Edit your request to add groceries.' : 'Your list is empty. Speak or type the groceries you need.', 'empty'));
  for (const item of items) {
    const row = node('li', '', 'item');
    const main = node('div', '', 'item-main');
    const name = node('span', item.name, 'item-name');
    name.dir = 'auto';
    main.append(name, node('span', `${item.quantity} units`, 'quantity'));
    row.append(main, node('p', item.max_price == null ? 'No explicit price cap' : `Price cap: ${item.max_price} SAR per unit`, 'hint'));
    const preferences = [item.preferred_name && `Preferred product: ${item.preferred_name}`, item.brand && `Brand: ${item.brand}`, item.sku && `SKU: ${item.sku}`].filter(Boolean);
    row.append(node('p', preferences.length ? preferences.join(' · ') : 'No saved product preference', 'hint'));
    if (item.alternatives?.length) {
      row.append(node('p', 'Approved alternatives', 'hint'));
      const alternatives = node('ul', '', 'alternatives hint');
      for (const alternative of item.alternatives) alternatives.append(node('li', describeAlternative(alternative)));
      row.append(alternatives);
    }
    list.append(row);
  }
  $('changes').hidden = !preview;
  $('actions').replaceChildren();
  if (preview) {
    if (!preview.actions.length) $('actions').append(node('li', 'No list changes in this preview.'));
    for (const action of preview.actions) {
      const parts = [action.kind.replaceAll('_', ' '), action.name, action.quantity != null && `Quantity: ${action.quantity}`, action.max_price != null && `Cap: ${action.max_price} SAR`, action.location && `Location: ${action.location}`].filter(Boolean);
      const row = node('li', parts.join(' · '));
      row.dir = 'auto';
      $('actions').append(row);
    }
  }
  $('preview-message').textContent = preview ? preview.message || 'Review every item and change before running.' : dirty ? 'Request changed. Generate a new preview to see the merged list.' : 'Saved items stay in the list unless your request changes them.';
}

function render() {
  const blocked = !ready || loading || working();
  const providerMissing = config && !config.modelConfigured;
  const bridgeMissing = config && !config.bridgeConfigured;
  mic.disabled = active ? starting : blocked || !connected || !config?.speechConfigured || !config?.modelConfigured || voiceStatus !== 'idle';
  $('microphone-label').textContent = starting ? 'Starting microphone…' : active ? 'Stop microphone' : 'Start microphone';
  mic.setAttribute('aria-pressed', String(active));
  mute.disabled = !ready || loading || pending;
  mute.textContent = state.muted ? 'Enable spoken replies' : 'Mute spoken replies';
  mute.setAttribute('aria-pressed', String(state.muted));
  draft.disabled = blocked || voiceWorking();
  generate.disabled = blocked || voiceWorking() || !draft.value.trim() || !!providerMissing;
  generate.textContent = pending && !state.run ? 'Working…' : 'Generate preview';
  confirm.disabled = blocked || voiceWorking() || !fresh() || !!problem || !!state.error || !!providerMissing || !!bridgeMissing;
  const feedback = [problem, state.clarification, stateErrorReason(state.error)].filter(Boolean).join(' ');
  announce('feedback', feedback);
  $('feedback').hidden = !feedback;
  retry.hidden = !sessionStarted || (!problem && !voiceProblem && connected);
  retry.disabled = loading || pending;
  const setup: string[] = [];
  if (config && !config.speechConfigured) setup.push('Speech provider missing. Type your request; microphone unavailable.');
  if (providerMissing) setup.push('Model provider missing. Preview generation unavailable.');
  if (bridgeMissing) setup.push('Shopping bridge is not configured. Cart preparation unavailable.');
  announce('connection', loading ? 'Loading session, state, settings and saved list…' : !sessionStarted ? 'Private session unavailable. Reload this page to start a session.' : !ready ? 'Could not load your shopping session. Use reload state & settings.' : setup.length ? setup.join(' ') : connected ? 'Connected to your private shopping session.' : 'Voice connection interrupted. Text requests remain available.');
  $('connection').parentElement!.classList.toggle('stale', !loading && (!ready || !connected || !!setup.length));
  announce('voice-status', voiceProblem || (starting ? 'Allow microphone access when your browser asks.' : voiceStatus === 'thinking' ? 'Transcribing / thinking. Approval is paused.' : voiceStatus === 'speaking' ? 'Assistant replying. Approval is paused.' : active ? 'Microphone on. Speak Arabic or English; stop it before running shop.' : 'Microphone off. You can also type below.'));
  const preview = state.preview;
  $('expiry').textContent = !preview ? '' : dirty ? 'Preview invalidated by a new request.' : preview.expiresAt <= Date.now() ? 'Preview expired. Generate a new preview.' : `Preview expires in ${Math.ceil((preview.expiresAt - Date.now()) / 1000)} seconds.`;
  $('approval-help').textContent = uncertain ? 'Run outcome unknown. Check run status before continuing.' : state.run?.status === 'running' ? 'Shopping is running. List editing and approval are locked.' : state.busy ? 'Interpreting your request. Editing and approval are paused.' : pending ? 'Waiting for the current request…' : voiceWorking() ? 'Stop the microphone and wait for the reply before running.' : problem || state.error ? 'Resolve the error before approving a preview.' : !fresh() ? 'Generate a fresh preview to review the exact list.' : providerMissing || bridgeMissing ? 'Service configuration must be restored before running.' : 'Ready to run this exact merged list and its changes.';
  const run = state.run;
  const status = $('run-status');
  let statusText: string;
  status.className = 'run-status';
  if (uncertain) { statusText = 'Interrupted · outcome unknown'; status.classList.add('warning'); }
  else if (run?.status === 'running') statusText = 'Preparing your cart…';
  else if (!run) statusText = 'No run yet';
  else {
    const labels: Record<string, string> = { completed: 'Cart preparation completed', failed: 'Cart preparation failed', error: 'Cart preparation failed', incomplete: 'Cart preparation incomplete', interrupted: 'Cart preparation interrupted', running: 'Preparing your cart…' };
    statusText = labels[run.status] || `Run status: ${run.status}`;
    if (['failed', 'error'].includes(run.status)) status.classList.add('error');
    else if (run.status !== 'completed') status.classList.add('warning');
  }
  announce('run-status', statusText);
  $('run-message').textContent = uncertain ? 'The request may have reached the server. It will not be sent again. Use Check run status to find out what happened.' : run?.message || (state.busy ? 'Interpreting your request. Review the preview when it is ready.' : 'Approve a fresh preview to prepare your cart. Checkout and purchase stay with you.');
  $('run-reference').hidden = !run && !pendingToken;
  $('run-reference').textContent = `Run reference: ${uncertain && pendingToken ? pendingToken : run?.token || pendingToken}`;
  lookup.hidden = !run && !uncertain;
  lookup.disabled = pending || loading || !ready;
  readResult.hidden = !run || run.status === 'running';
  readResult.disabled = readInProgress || pending || loading || state.muted || !config?.speechConfigured;
  readResult.textContent = readInProgress ? 'Generating speech…' : 'Read result aloud';
}

function acceptState(next: State, expectedRevision = revision) {
  if (!isState(next)) throw new Error('Invalid shopping state. Reload state & settings.');
  if (expectedRevision !== revision) {
    state = { ...state, run: next.run, busy: next.busy, muted: next.muted, error: next.error, clarification: next.clarification };
  } else {
    state = next;
    if (!dirty) draft.value = next.draft;
  }
  if (state.run?.status === 'running' && (active || starting)) stopMicrophone();
  renderList();
  render();
  schedulePoll();
}

function invalidate(body: { speech: true } | { transcript: string }) {
  dirty = true;
  revision++;
  state.preview = null;
  renderList();
  render();
  const version = revision;
  return serial(async () => {
    try {
      const result = await api<State | undefined>('/api/invalidate', body);
      if (result && typeof result.draft === 'string' && version === revision) acceptState(result, version);
    } catch (error) { problem = `Preview invalidation failed. ${errorText(error)}`; render(); }
  });
}

function stopMicrophone() {
  voice?.endCall();
  active = false;
  starting = false;
  $('interim').hidden = true;
  render();
}

function invalidateSpeech() {
  if (speechInvalidated) return;
  speechInvalidated = true;
  voiceFinal = false;
  void invalidate({ speech: true });
  voiceRevision = revision;
}

function setupVoice() {
  if (voice) return;
  voice = new VoiceClient({ agent: 'ShoppingAgent', name: 'local' });
  voice.addEventListener('connectionchange', value => {
    const reconnect = !connected && value;
    connected = value;
    if (!value && (active || starting)) { stopMicrophone(); voiceProblem = 'Voice connection interrupted. Reconnect, then start the microphone again, or type your request.'; }
    render();
    if (reconnect && ready && !loading) void refresh();
  });
  voice.addEventListener('statuschange', status => {
    if (status === 'listening' && voiceStatus !== 'listening') speechInvalidated = false;
    voiceStatus = status;
    if (status === 'thinking' && active) invalidateSpeech();
    render();
  });
  voice.addEventListener('interimtranscript', text => {
    $('interim').textContent = text || '';
    $('interim').hidden = !text;
    if (text) invalidateSpeech();
    render();
  });
  let lastUser = '';
  voice.addEventListener('transcriptchange', (messages: TranscriptMessage[]) => {
    const transcripts = $('transcripts');
    transcripts.replaceChildren();
    $('conversation-empty').hidden = !!messages.length;
    for (const message of messages) {
      const row = node('li', '');
      const text = node('p', message.text, 'content');
      text.dir = 'auto';
      row.append(node('p', message.role === 'user' ? 'You' : 'Assistant', 'role'), text);
      transcripts.append(row);
    }
    const user = messages.filter(message => message.role === 'user').at(-1);
    const key = user ? `${user.timestamp}:${user.text}` : '';
    if (user && key !== lastUser) {
      lastUser = key;
      if (!active && !starting && voiceRevision === null) return;
      draft.value = user.text;
      invalidateSpeech();
      voiceFinal = true;
      // shopping_state custom messages carry the server's final merged preview.
      render();
    }
  });
  voice.addEventListener('custommessage', async message => {
    try {
      const data = typeof message === 'string' ? JSON.parse(message) : message;
      if (data?.type !== 'shopping_state') return;
      // Do not allow delayed voice replies to replace an edit or a text preview request.
      const version = voiceRevision;
      await mutationQueue;
      if (voiceFinal && version !== null && version === revision) {
        if (data.state?.busy === false && (data.state.preview || data.state.error || data.state.clarification)) {
          dirty = false;
          voiceRevision = null;
          voiceFinal = false;
        }
        acceptState(data.state, version);
      } else if (data.state?.run || data.state?.busy === false) acceptState(data.state, -1);
    } catch { problem = 'Voice returned unreadable shopping state. Stop the microphone and reload state & settings.'; render(); }
  });
  voice.addEventListener('error', message => {
    if (!message) return;
    voiceProblem ||= 'Voice unavailable. Stop the microphone and type your request, or reload state & settings.';
    stopMicrophone();
    voiceRevision = null;
    voiceFinal = false;
    void invalidate({ speech: true });
  });
  voice.addEventListener('voiceerror', error => {
    voiceProblem = error.stage === 'stt'
      ? 'Speech transcription unavailable. Stop the microphone and type your request, or reload state & settings.'
      : 'Voice unavailable. Stop the microphone and type your request, or reload state & settings.';
    render();
  });
  voice.addEventListener('completionoutcome', () => {
    voiceProblem = 'The assistant could not finish its reply. Review the list and request a new preview if needed.';
    render();
  });
  voice.addEventListener('turnmetrics', metrics => {
    if (metrics.outcome === 'tts_error') {
      voiceProblem = 'Spoken reply unavailable. Read the full reply in Conversation.';
      render();
    }
  });
  voice.addEventListener('outputdeviceerror', message => {
    if (message) { voiceProblem = 'Spoken replies could not play. Full replies remain in Conversation.'; render(); }
  });
  voice.connect();
}

async function refresh() {
  if (loading || pending) return;
  loading = true;
  render();
  const version = revision;
  const results = await Promise.allSettled([
    api<State>('/api/state'), api<Config>('/api/config'), api<{ items: Product[] }>('/api/list'),
  ]);
  const failures = results.flatMap((result, index) => result.status === 'rejected' ? [`${['State', 'Settings', 'Saved list'][index]}: ${errorText(result.reason)}`] : []);
  if (failures.length) { problem = failures.join(' '); ready = false; }
  else {
    try {
      const next = (results[0] as PromiseFulfilledResult<State>).value;
      config = (results[1] as PromiseFulfilledResult<Config>).value;
      savedItems = (results[2] as PromiseFulfilledResult<{ items: Product[] }>).value.items;
      problem = '';
      voiceProblem = '';
      ready = true;
      acceptState(next, version);
      if (pendingToken && next.run?.token === pendingToken) { uncertain = false; rememberToken(''); }
    } catch (error) { problem = errorText(error); ready = false; }
  }
  loading = false;
  render();
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (ready && !uncertain && state.run?.status === 'running') {
    pollTimer = setTimeout(() => { if (!pending && !loading) void checkStatus(); else schedulePoll(); }, 3000);
  }
}

async function checkStatus() {
  if (pending || loading || !ready) return;
  pending = true;
  render();
  try {
    const next = await serial(() => api<State>('/api/status', {}));
    if (pendingToken && next.run?.token !== pendingToken) {
      uncertain = true;
      problem = 'The interrupted run is not identified yet. Check run status again; do not send another approval.';
    } else {
      uncertain = false;
      rememberToken('');
      problem = '';
    }
    acceptState(next);
  } catch (error) { problem = `Run status unavailable. ${errorText(error)}`; uncertain = true; }
  finally { pending = false; render(); schedulePoll(); }
}

draft.addEventListener('input', () => {
  voiceRevision = null;
  voiceFinal = false;
  void invalidate({ transcript: draft.value });
});
mic.addEventListener('click', async () => {
  if (active) { stopMicrophone(); return; }
  if (mic.disabled || !voice) return;
  starting = true;
  speechInvalidated = false;
  voiceProblem = '';
  render();
  try {
    await voice.startCall();
    if (connected && starting && state.run?.status !== 'running') active = true;
    else voice.endCall();
  }
  catch (error) {
    voice?.endCall();
    voiceProblem = error instanceof DOMException && ['NotAllowedError', 'PermissionDeniedError'].includes(error.name)
      ? 'Microphone permission denied. Allow access in browser settings and try again, or type your request.'
      : 'Microphone could not start. Check browser microphone access, then try again, or type your request instead.';
  } finally { starting = false; render(); }
});
function stopReadAloud() {
  speechSequence++;
  speechRequest?.abort();
  speechRequest = null;
  readInProgress = false;
  resultAudio?.pause();
}

mute.addEventListener('click', async () => {
  if (mute.disabled) return;
  stopReadAloud();
  if (!state.muted) voice?.sendJSON({ type: 'interrupt' });
  pending = true;
  render();
  try { acceptState(await serial(() => api<State>('/api/mute', { muted: !state.muted }))); }
  catch (error) { problem = `Reply setting was not confirmed. ${errorText(error)}`; }
  finally { pending = false; render(); }
});
$('draft-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (generate.disabled) return;
  const version = revision;
  voiceRevision = null;
  const transcript = draft.value.trim();
  pending = true;
  render();
  try {
    const next = await serial(() => api<State>('/api/preview', { transcript }));
    if (version === revision) { dirty = false; problem = ''; acceptState(next, version); }
  } catch (error) { dirty = true; problem = `Preview could not be generated. ${errorText(error)}`; }
  finally { pending = false; render(); }
});
confirm.addEventListener('click', async () => {
  if (confirm.disabled || !fresh() || !state.preview) return;
  const token = state.preview.token;
  const approvedItems = state.preview.items;
  rememberToken(token);
  pending = true;
  render();
  try {
    const next = await serial(() => api<State>('/api/confirm', { token }));
    uncertain = false;
    rememberToken('');
    if (!next.error && next.run?.token === token) savedItems = approvedItems;
    acceptState(next);
  } catch (error) { uncertain = true; problem = `Approval outcome unknown. ${errorText(error)} Check run status; approval will not be retried.`; }
  finally { pending = false; render(); schedulePoll(); }
});
retry.addEventListener('click', () => { void refresh(); });
lookup.addEventListener('click', () => { void checkStatus(); });
readResult.addEventListener('click', async () => {
  if (readResult.disabled) return;
  stopReadAloud();
  const sequence = speechSequence;
  const token = state.run?.token;
  const controller = new AbortController();
  speechRequest = controller;
  const timeout = setTimeout(() => controller.abort(), 30000);
  readInProgress = true;
  render();
  try {
    const response = await fetch('/api/speech', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subject: 'result' }), signal: controller.signal,
    });
    if (!response.ok) throw new Error('Speech unavailable. Read the displayed result or check speech configuration.');
    const audio = await response.blob();
    if (sequence !== speechSequence || controller.signal.aborted || state.muted || token !== state.run?.token) return;
    resultAudio?.pause();
    if (resultAudioURL) URL.revokeObjectURL(resultAudioURL);
    resultAudioURL = URL.createObjectURL(audio);
    resultAudio = new Audio(resultAudioURL);
    await resultAudio.play();
  } catch { if (sequence === speechSequence) problem = 'Spoken result unavailable. Read the displayed result or check speech configuration.'; }
  finally {
    clearTimeout(timeout);
    if (sequence === speechSequence) { readInProgress = false; speechRequest = null; }
    render();
  }
});
window.addEventListener('online', () => { void refresh(); });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') void refresh();
});
window.addEventListener('pagehide', () => { stopReadAloud(); voice?.endCall(); voice?.disconnect(); clearTimeout(pollTimer); });
setInterval(() => { if (state.preview) render(); }, 1000);

async function bootstrap() {
  loading = true;
  render();
  try {
    await api('/session', {});
    sessionStarted = true;
    loading = false;
    await refresh();
    setupVoice();
  } catch (error) {
    loading = false;
    problem = `Session could not start. ${errorText(error)} Reload this page to start a session.`;
    $('connection').textContent = 'Private session unavailable. Reload this page to try again.';
    render();
  }
}
void bootstrap();
