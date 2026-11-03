const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../chat_app/templates/index.html'), 'utf8');
const source = html.slice(html.indexOf('    function createChatScroll'), html.indexOf('    async function sendMessage'));

function setup() {
  const listeners = {};
  const frames = [];
  let resize;
  const container = {
    scrollHeight: 1000, clientHeight: 400, scrollTop: 600, children: [],
    addEventListener(name, handler) { listeners[name] = handler; }
  };
  const context = vm.createContext({
    messagesContainer: container,
    requestAnimationFrame(callback) { frames.push(callback); return frames.length; },
    ResizeObserver: class { constructor(callback) { resize = callback; } observe() {} unobserve() {} },
    MutationObserver: class { observe() {} },
    document: {
      createTextNode(data) { return { data, appendData(token) { this.data += token; } }; },
      createElement() { return {}; }
    },
    escapeHtml: text => text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
  });
  vm.runInContext(source, context);
  return {
    container, context, listeners,
    follow: () => vm.runInContext('chatScroll.follow()', context),
    resize: () => resize(),
    flush() { const pending = frames.splice(0); pending.forEach(fn => fn()); },
    scroll(top) { container.scrollTop = top; listeners.scroll(); }
  };
}

test('growth follows immediately; delayed scroll events do not disable follow', () => {
  const env = setup();
  env.container.scrollHeight += 500;
  env.listeners.scroll();
  env.resize(); env.flush();
  assert.equal(env.container.scrollTop, 1100);
  env.container.scrollHeight += 200;
  env.follow();
  assert.equal(env.container.scrollTop, 1300);
});

test('upward wheel pauses even close to bottom and cancels queued follow', () => {
  const env = setup();
  env.resize();
  env.listeners.wheel({ deltaY: -0.5 });
  env.listeners.scroll(); // delayed event from an earlier automatic write
  env.scroll(598);
  env.container.scrollHeight += 100;
  env.flush(); env.follow();
  assert.equal(env.container.scrollTop, 598);
  env.scroll(650); env.follow();
  assert.equal(env.container.scrollTop, 650);
  env.scroll(690); env.flush();
  assert.equal(env.container.scrollTop, 700);
});

test('bottom overscroll and elastic return never disable following', () => {
  const env = setup();
  env.listeners.wheel({ deltaY: 100 });
  env.scroll(680); env.scroll(600);
  env.container.scrollHeight += 80;
  env.resize(); env.flush();
  assert.equal(env.container.scrollTop, 680);
});

test('scrollbar, keyboard and touch can pause following', () => {
  for (const pause of [
    env => env.scroll(300),
    env => env.listeners.keydown({ key: 'PageUp' }),
    env => {
      env.listeners.touchstart({ touches: [{ clientY: 100 }] });
      env.listeners.touchmove({ touches: [{ clientY: 110 }] });
    }
  ]) {
    const env = setup(); pause(env);
    const before = env.container.scrollTop;
    env.container.scrollHeight += 100;
    env.resize(); env.flush();
    assert.equal(env.container.scrollTop, before);
  }
});

test('tokens preserve node identity and format escaped Markdown only at completion', () => {
  const env = setup();
  const content = { replacements: 0, htmlWrites: 0,
    replaceChildren(...nodes) { this.nodes = nodes; this.replacements++; },
    set innerHTML(value) { this.html = value; this.htmlWrites++; }
  };
  env.context.content = content;
  const stream = vm.runInContext('createStreamingMessage(content)', env.context);
  stream.append('## Title\n');
  const node = content.nodes[0];
  for (let i = 0; i < 100; i++) stream.append('**text** <script>\n');
  assert.equal(content.nodes[0], node);
  assert.equal(content.replacements, 1);
  assert.equal(content.htmlWrites, 0);
  stream.finish();
  assert.equal(content.htmlWrites, 1);
  assert.match(content.html, /class="md-h2"/);
  assert.match(content.html, /&lt;script&gt;/);
});

test('empty completion leaves a visible explanation', () => {
  const env = setup();
  env.context.content = {};
  vm.runInContext('createStreamingMessage(content).finish()', env.context);
  assert.equal(env.context.content.textContent, 'Keine Antwort erhalten.');
});

test('layout shrink clamps the previous position without pausing follow', () => {
  const env = setup();
  env.container.scrollHeight = 900;
  env.scroll(500);
  env.container.scrollHeight = 1100;
  env.resize(); env.flush();
  assert.equal(env.container.scrollTop, 700);
});
