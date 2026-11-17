// Reader contract in a Node DOM stub; this is not a visual/browser acceptance test.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const template = fs.readFileSync(path.join(__dirname, '..', 'bot', 'template.html'), 'utf8');
const source = template.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const data = {
  findings: [{id: 1, run_id: 1, summary: 'Как читать этикетку корма', title: 'Original',
    source: 'Русский источник', reason: 'Питание', topic: 'nutrition', date: '2026-11-16T10:00:00Z',
    url: 'https://vetandlife.ru/pets/source/', article: {
      version: 2, format_label: 'Полезное руководство', material_type: 'guide', evidence_scope: 'page_excerpt',
      source_language: 'ru', original_url: 'https://vetandlife.ru/pets/source/', attribution: 'Тестовый эксперт',
      paragraphs: [{kind: 'guidance', text: 'Краткое содержание для владельца. <script>unsafe()</script>'}],
      limitations: '', citations: [{title: 'Оригинал', url: 'https://vetandlife.ru/pets/source/'}]
    }}], totals: {sources: 6, findings: 1}, sources: [], run: null, generated_at: '2026-11-16 10:00'
};
const nodes = new Map();
function node(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {style:{}, innerHTML:'', textContent:'', querySelectorAll:()=>[]});
  return nodes.get(selector);
}
const context = vm.createContext({window:{scrollY:0,scrollTo(){},open(){}},
  document:{querySelector:node,querySelectorAll:()=>[],addEventListener(){}},setTimeout(){}});
vm.runInContext(source.replace('/*DATA*/null/*END*/', JSON.stringify(data)), context);
assert.match(node('#view').innerHTML, /Питание/);
assert.match(node('#view').innerHTML, /🦴/);
vm.runInContext('openFinding(1)', context);
let detail=node('#view').innerHTML;
assert.match(detail,/Краткое содержание/);
assert.match(detail,/Читать оригинал/);
assert.match(detail,/Полезное руководство/);
assert.match(detail,/По материалу источника/);
assert.doesNotMatch(detail,/Что важно учитывать|Результат исследования|Открыть аннотацию|Научный источник/);
assert.doesNotMatch(detail,/<script>unsafe/);
assert.match(detail,/&lt;script&gt;/);
vm.runInContext('findings[0].article.source_language="en"; openFinding(1)',context);
assert.match(node('#view').innerHTML,/на английском/);
vm.runInContext('closeDetail(); state.topic="Питание"; render()',context);
assert.match(node('#view').innerHTML,/Как читать этикетку корма/);
console.log('PASS: T17 nutrition, guide summary, original/language, no mandatory warnings, escaping and filters/back');
