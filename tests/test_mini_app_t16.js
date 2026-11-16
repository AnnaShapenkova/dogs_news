// Reader behaviour and colour contracts without real Telegram or network.
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const template = fs.readFileSync(path.join(__dirname, '..', 'bot', 'template.html'), 'utf8');
const source = template.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
assert.match(template, /data-theme="paper"/);
assert.doesNotMatch(template, /#0b0b0c|#BFFF00/);
assert.match(template, /prefers-reduced-motion/);
assert.match(template, /min-height: 44px/);
assert.match(template, /aria-hidden="true">🐕/);
assert.match(template, /🎾/);
assert.match(template, /🦴/);

const data = {
  findings: [{ id: 1, summary: 'Как собаки выбирают знакомые изображения', date: '2026-11-16T10:00:00Z',
    title: 'Synthetic source', source: 'Europe PMC: Dogs pilot', run_id: 1, reason: 'Обучение', topic: 'learning',
    url: 'https://example.invalid/source', article: { format_label: 'Разбор исследования',
      paragraphs: [{kind: 'result', text: 'Синтетический проверяемый результат.'}],
      limitations: 'Основано на аннотации. Полный текст не проверен.',
      citations: [{title: 'Synthetic source', url: 'https://example.invalid/source'}] } }],
  totals: {sources: 1, findings: 1}, sources: [], run: null, generated_at: '2026-11-16 10:00'
};
function mount(telegram) {
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {style: {}, innerHTML: '', textContent: '', querySelectorAll: () => []});
    return nodes.get(selector);
  };
  const context = vm.createContext({
    window: {Telegram: telegram ? {WebApp: telegram} : undefined, scrollY: 0, scrollTo() {}, open() {}},
    document: {querySelector: node, querySelectorAll: () => [], addEventListener() {}}, setTimeout() {}
  });
  vm.runInContext(source.replace('/*DATA*/null/*END*/', JSON.stringify(data)), context);
  return {context, nodes, node};
}
const colours = [];
const buttons = [];
const telegram = {initData: 'synthetic', colorScheme: 'dark', ready() {}, expand() {},
  setHeaderColor(c) { colours.push(c); }, setBackgroundColor(c) { colours.push(c); },
  setBottomBarColor(c) { colours.push(c); },
  MainButton: {setParams(p) { buttons.push(p); }, hide() {}, onClick() {}},
  BackButton: {show() {}, hide() {}, onClick() {}}
};
const mounted = mount(telegram);
assert.deepEqual(colours, ['#f3efe6', '#f3efe6', '#f3efe6']);
const feed = mounted.node('#view').innerHTML;
assert.match(feed, /story-title/);
assert.match(feed, /🎾/);
assert.doesNotMatch(feed, /Dogs pilot/);
vm.runInContext('openFinding(1)', mounted.context);
assert.match(mounted.node('#view').innerHTML, /Научный источник/);
assert.match(mounted.node('#view').innerHTML, /Что важно учитывать/);
assert.match(mounted.node('#view').innerHTML, /data-back/);
assert.equal(buttons[0].color, '#315644');
assert.equal(buttons[0].text_color, '#faf8f3');
vm.runInContext('closeDetail(); state.topic="missing"; render()', mounted.context);
assert.match(mounted.node('#view').innerHTML, /Сбросить фильтры/);
vm.runInContext('state.topic="all"; render()', mounted.context);
assert.match(mounted.node('#view').innerHTML, /Как собаки выбирают/);

// Partial/old WebApp APIs still allow browsing and detail navigation.
const partial = mount({initData: 'synthetic', colorScheme: 'dark'});
vm.runInContext('openFinding(1); closeDetail()', partial.context);
assert.match(partial.node('#view').innerHTML, /story-title/);

function luminance(hex) {
  const channels = hex.match(/[a-f\d]{2}/gi).map(v => parseInt(v, 16) / 255)
    .map(v => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}
for (const [fg, bg] of [['1a1712', 'f3efe6'], ['5c574a', 'faf8f3'], ['6b665a', 'f3efe6'],
                         ['faf8f3', '315644'], ['5c574a', 'e6ecdf'], ['315644', 'e0e9db']]) {
  const values = [luminance(fg), luminance(bg)].sort((a,b) => b-a);
  const ratio = (values[0] + 0.05) / (values[1] + 0.05);
  assert.ok(ratio >= 4.5, `${fg}/${bg} contrast=${ratio}`);
}
console.log('PASS: paper theme, dog accents, dark Telegram chrome, filters/back, missing APIs, text contrast');
