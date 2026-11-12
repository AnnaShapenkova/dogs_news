// Execute the existing inline client script with empty server data. No browser/network dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const template = fs.readFileSync(path.join(__dirname, '..', 'bot', 'template.html'), 'utf8');
const source = template.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const nodes = new Map();
const node = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {
    style: {}, innerHTML: '', textContent: '', querySelectorAll: () => [],
  });
  return nodes.get(selector);
};
const context = vm.createContext({
  window: {},
  document: { querySelector: node, querySelectorAll: () => [], addEventListener() {} },
  setTimeout() { throw new Error('Unexpected animation in empty feed'); },
});
const data = {
  findings: [], run: null, sources: [],
  totals: { sources: 0, findings: 0 }, generated_at: '2026-11-12 12:00',
};
vm.runInContext(source.replace('/*DATA*/null/*END*/', JSON.stringify(data)), context);
assert.match(node('#view').innerHTML, /Опубликованных находок пока нет/);
assert.match(node('#view').innerHTML, /после допуска к публикации/);
assert.doesNotMatch(node('#view').innerHTML, /после ближайшего сбора/);
assert.equal(node('#filters').style.display, 'none');
console.log('PASS: Mini App empty state renders and filters are hidden');

assert.doesNotMatch(template, /\?{3,}/, 'Russian text must not be lost to shell encoding');
const published = {
  id: 1, summary: 'Как собаки выбирают изображения', title: 'Original title',
  date: '2026-11-12T12:00:00Z', source: 'Europe PMC', run_id: 1,
  url: 'https://europepmc.org/article/MED/999999991', reason: 'Обучение', topic: 'learning',
  article: { format_label: 'Объяснение исследования',
    paragraphs: [{ kind: 'result', text: '<script>unsafe()</script> Результат исследования.' },
                 { kind: 'interpretation', text: 'Осторожная интерпретация результата.' }],
    limitations: 'Разбор основан на аннотации; полный текст не проверен.',
    citations: [{ authors: 'Synthetic Author', title: 'Synthetic research', journal: 'Test', year: '2026',
                  url: 'https://europepmc.org/article/MED/999999991', doi: '10.9999/synthetic' }],
  },
};
const fullNodes = new Map();
const fullNode = selector => {
  if (!fullNodes.has(selector)) fullNodes.set(selector, { style: {}, innerHTML: '', textContent: '', querySelectorAll: () => [] });
  return fullNodes.get(selector);
};
const opened = [];
const fullContext = vm.createContext({
  window: { scrollY: 0, scrollTo() {}, open(url) { opened.push(url); } },
  document: { querySelector: fullNode, querySelectorAll: () => [], addEventListener() {} },
  setTimeout() {},
});
vm.runInContext(source.replace('/*DATA*/null/*END*/', JSON.stringify({ ...data, findings: [published] })), fullContext);
assert.match(fullNode('#sources').innerHTML, /Все темы/);
assert.match(fullNode('#view').innerHTML, /Как собаки выбирают изображения/);
vm.runInContext('openFinding(1)', fullContext);
const detail = fullNode('#view').innerHTML;
assert.match(detail, /Что важно учитывать/);
assert.match(detail, /Научный источник/);
assert.match(detail, /полный текст не проверен/);
assert.match(detail, /&lt;script&gt;/);
assert.doesNotMatch(detail, /<script>|entity =|material_id|source_id|prompt_version/);
vm.runInContext('openLink("javascript:alert(1)"); openLink("https://example.invalid")', fullContext);
assert.deepEqual(opened, ['https://example.invalid']);
console.log('PASS: published article, topic filter, citation, limitations and escaping');
