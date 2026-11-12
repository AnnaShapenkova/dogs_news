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
