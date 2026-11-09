const element = (id) => document.getElementById(id);
const currency = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const formatBalance = (cents) => currency.format(cents / 100);
const accountButtons = [...document.querySelectorAll('[data-account]')];
const actionButtons = [...document.querySelectorAll('[data-action]')];
const pinInput = element('pinInput');
const amountInput = element('amountInput');
const REQUEST_TIMEOUT_MS = 10_000;

const states = {
  IDLE: { node: 'S0', message: 'ATM ready\nPlease insert your card to begin.', actions: [] },
  CARD_INSERTED: { node: 'S1', actions: ['advance', 'eject'], delay: 1200 },
  ENTER_PIN: { node: 'S2', actions: ['submit_pin', 'eject'] },
  TRANSACTION_MENU: { node: 'S3', actions: ['withdrawal', 'balance_inquiry', 'eject'] },
  ENTER_AMOUNT: { node: 'S4', actions: ['submit_amount', 'eject'] },
  DISPENSE: { node: 'S5', actions: ['advance'], delay: 2800 },
  EJECT_CARD: { node: 'S6', actions: ['advance'], delay: 2500 },
  BALANCE_INQUIRY: { node: 'S7', actions: ['back_to_menu', 'eject'] },
  BALANCE_INQUIRY_CONFIRM: { node: 'S8', actions: ['confirm_inquiry', 'cancel_inquiry', 'eject'] },
  CARD_RETAINED: { node: 'S9', actions: ['advance'], delay: 3500 },
};

let selectedAccount = 1;
let account = null;
let session = null;
let message = '';
let transition = null;
let fieldError = '';
let requestError = '';
let pending = false;
let retryAdvance = false;
let advanceTimer;

function stateName() {
  return session?.state ?? 'IDLE';
}

function setFieldError(input, id, text) {
  const output = element(id);
  output.textContent = text;
  output.classList.toggle('show', Boolean(text));
  input.classList.toggle('error', Boolean(text));
  input.setAttribute('aria-invalid', String(Boolean(text)));
}

function renderControls() {
  const current = stateName();
  const isIdle = current === 'IDLE';
  for (const button of accountButtons) {
    const selected = Number(button.dataset.account) === selectedAccount;
    button.disabled = pending || !isIdle;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-pressed', String(selected));
  }
  for (const button of actionButtons) {
    const allowed = states[current].actions.includes(button.dataset.action);
    if (button.dataset.action === 'advance') button.hidden = !allowed || !retryAdvance;
    button.disabled = pending || !allowed || button.hidden;
  }
  element('insertCardBtn').disabled = pending || !account || !isIdle;
  element('restartBtn').disabled = pending || !account;
  pinInput.disabled = pending;
  amountInput.disabled = pending;
  element('atm-screen').setAttribute('aria-busy', String(pending));
}

function renderHistory() {
  const entries = account?.history ?? [];
  const historyList = element('historyList');
  if (!entries.length) {
    const empty = document.createElement('p');
    empty.className = 'history-empty';
    empty.textContent = account ? 'No transactions yet.' : 'Select an account to load its history.';
    historyList.replaceChildren(empty);
    return;
  }

  const items = entries.slice(0, 10).map((entry) => {
    const item = document.createElement('div');
    item.className = 'history-item';
    const securityEvent = entry.type === 'pin_failure' || entry.type === 'card_retained';
    item.classList.add(securityEvent ? 'error' : 'success');

    const description = document.createElement('span');
    description.textContent = entry.message;

    const time = document.createElement('time');
    time.className = 'history-time';
    const timestamp = new Date(entry.created_at);
    if (!Number.isNaN(timestamp.getTime())) {
      time.dateTime = entry.created_at;
      time.textContent = dateTime.format(timestamp);
    }

    item.append(description, time);
    return item;
  });
  historyList.replaceChildren(...items);
}

function render() {
  const current = stateName();
  const screen = element('atm-screen');
  const enteringPin = current === 'ENTER_PIN';
  const enteringAmount = current === 'ENTER_AMOUNT';
  const showFieldError = enteringPin || enteringAmount;

  element('currentUserDisplay').textContent = 'User ' + selectedAccount;
  element('balanceDisplay').textContent = account ? formatBalance(account.balance_cents) : '—';
  screen.textContent = message || (account ? states[current].message : 'Select an account to begin.') || '';
  screen.classList.toggle('error', current === 'CARD_RETAINED' || Boolean(fieldError && !showFieldError));
  screen.classList.toggle('success', current === 'DISPENSE' || current === 'BALANCE_INQUIRY');

  for (const node of document.querySelectorAll('.state')) {
    const active = node.id === 'moore-' + states[current].node;
    node.classList.toggle('active', active);
    if (active) {
      node.setAttribute('aria-current', 'step');
    } else {
      node.removeAttribute('aria-current');
    }
  }
  for (const edge of document.querySelectorAll('.transition')) {
    edge.classList.toggle('active', edge.id === transition);
  }

  element('pinInputGroup').classList.toggle('active', enteringPin);
  element('amountInputGroup').classList.toggle('active', enteringAmount);
  if (!enteringPin) pinInput.value = '';
  if (!enteringAmount) amountInput.value = '';
  setFieldError(pinInput, 'pinError', enteringPin ? fieldError : '');
  setFieldError(amountInput, 'amountError', enteringAmount ? fieldError : '');

  const attempts = session?.pin_attempts ?? 0;
  const attemptsNotice = element('pinAttempts');
  const retained = current === 'CARD_RETAINED';
  attemptsNotice.classList.toggle('show', attempts > 0 && (enteringPin || retained));
  attemptsNotice.classList.toggle('warning', !retained);
  attemptsNotice.classList.toggle('danger', retained);
  element('attemptCount').textContent = attempts;
  const remaining = 3 - attempts;
  element('attemptMessage').textContent = retained
    ? 'The card has been retained.'
    : remaining + ' attempt' + (remaining === 1 ? '' : 's') + ' remaining.';

  const alert = element('requestError');
  alert.textContent = requestError || (!showFieldError && current !== 'CARD_RETAINED' ? fieldError : '');
  alert.classList.toggle('show', Boolean(alert.textContent));
  renderHistory();
  renderControls();
}

class RequestError extends Error {
  constructor(status, body) {
    super(body.error || 'The server could not complete this request.');
    this.status = status;
    this.body = body;
  }
}

async function request(path, data) {
  const controller = new AbortController();
  const options = {
    cache: 'no-store',
    headers: { Accept: 'application/json' },
    signal: controller.signal,
  };
  if (data !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(data);
  }
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(path, options);
    const body = await response.json().catch((error) => {
      if (error instanceof SyntaxError) return {};
      throw error;
    });
    if (!response.ok || !body?.account) {
      throw new RequestError(response.status, body ?? {});
    }
    return body;
  } catch (error) {
    if (controller.signal.aborted) {
      throw new RequestError(0, { error: 'The ATM server took too long to respond. Try the action again.' });
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function applySnapshot(snapshot) {
  account = snapshot.account;
  selectedAccount = account.id;
  session = snapshot.session?.state !== 'IDLE' ? (snapshot.session ?? null) : null;
  transition = snapshot.transition ?? null;
  message = snapshot.message ?? '';
  fieldError = snapshot.error ?? '';
}

function scheduleAdvance() {
  const delay = states[stateName()].delay;
  if (!delay || !session) return;

  const { token, revision } = session;
  advanceTimer = window.setTimeout(() => {
    if (session?.token === token && session.revision === revision) {
      submitAction('advance');
    }
  }, delay);
}

async function runRequest(operation, { focusAfter = false, advancing = false } = {}) {
  if (pending) return;
  window.clearTimeout(advanceTimer);
  pending = true;
  requestError = '';
  renderControls();

  try {
    applySnapshot(await operation());
    retryAdvance = false;
  } catch (error) {
    const hasSnapshot = Boolean(error.body?.account);
    if (hasSnapshot) {
      applySnapshot(error.body);
      fieldError = '';
    }
    retryAdvance = advancing && !hasSnapshot;
    requestError = error instanceof RequestError
      ? error.message
      : 'Could not reach the ATM server. Check that it is running and try again.';
  } finally {
    pending = false;
    render();
    if (focusAfter && stateName() === 'ENTER_PIN') pinInput.focus();
    if (focusAfter && stateName() === 'ENTER_AMOUNT') amountInput.focus();
    if (!retryAdvance) scheduleAdvance();
  }
}

function selectAccount(id) {
  if (stateName() === 'IDLE') {
    return runRequest(() => request('/api/accounts/' + id));
  }
}

function startCardSession() {
  if (!account || stateName() !== 'IDLE') return;
  return runRequest(() => request('/api/accounts/' + selectedAccount + '/sessions', {}));
}

function submitAction(action) {
  if (!session || pending || !states[stateName()].actions.includes(action)) return;

  const { token, revision } = session;
  const data = { action, revision };
  if (action === 'submit_pin') {
    data.pin = pinInput.value.trim();
    pinInput.value = '';
  }
  if (action === 'submit_amount') data.amount = amountInput.value.trim();

  return runRequest(async () => {
    try {
      return await request('/api/sessions/' + encodeURIComponent(token) + '/actions', data);
    } catch (error) {
      if (!(error instanceof RequestError) || error.status !== 404) throw error;

      session = null;
      transition = null;
      fieldError = '';
      message = 'The card session ended. Insert a card to start again.';
      const freshAccount = await request('/api/accounts/' + selectedAccount);
      return { ...freshAccount, message };
    }
  }, { focusAfter: true, advancing: action === 'advance' });
}

function resetAccount() {
  if (!account || pending) return;
  if (window.confirm('Reset User ' + selectedAccount + ' to $1,000.00 and clear its transaction history?')) {
    return runRequest(() => request('/api/accounts/' + selectedAccount + '/reset', {}));
  }
}

for (const button of accountButtons) {
  button.addEventListener('click', () => selectAccount(Number(button.dataset.account)));
}
for (const button of actionButtons) {
  button.addEventListener('click', () => submitAction(button.dataset.action));
}
element('insertCardBtn').addEventListener('click', startCardSession);
element('restartBtn').addEventListener('click', resetAccount);

for (const [input, errorId, action] of [
  [pinInput, 'pinError', 'submit_pin'],
  [amountInput, 'amountError', 'submit_amount'],
]) {
  input.addEventListener('input', () => {
    if (input === pinInput) input.value = input.value.replace(/\D/g, '');
    fieldError = '';
    setFieldError(input, errorId, '');
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      submitAction(action);
    }
  });
}

if (window.location.protocol === 'file:') {
  const screen = element('atm-screen');
  const link = document.createElement('a');
  link.href = 'http://127.0.0.1:8000/';
  link.textContent = 'Open the local ATM';
  screen.replaceChildren('Double-click Start ATM.cmd in the project folder to start the server. ', link);
  element('historyList').textContent = 'Transaction history loads after the server starts.';
} else {
  selectAccount(selectedAccount);
}
