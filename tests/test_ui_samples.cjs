// Execute the real UI functions with API/DOM boundaries stubbed; no provider calls.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const source = fs.readFileSync(require('node:path').join(__dirname, '../scripts/ui.html'), 'utf8');
const script = source.match(/<script>([\s\S]*)<\/script>/)[1];
new vm.Script(script);

function setup(response) {
  const box = {URL, URLSearchParams, encodeURIComponent,
    api: async () => response, esc: s => String(s ?? '').replaceAll('<', '&lt;'),
    SLUG: 'demo', D: {brand: {site: 'https://example.com'}},
    modal: h => {box.html = h;}, toast: m => {box.error = m;},
    head: () => '', render: () => {}, html: '', error: null};
  vm.createContext(box);
  vm.runInContext(source.slice(source.indexOf('let SMP=null'), source.indexOf('/* ===================== 框架')), box);
  return box;
}

for (const [platform, error] of [
  ['kimi', 'HTTP 400: invalid temperature: only 1 is allowed'],
  ['doubao', 'HTTP 401: AuthenticationError: The API key format is incorrect'],
]) {
  test(`${platform} stored failure opens detail instead of transport-error toast`, async () => {
    const box = setup({platform, ok: false, error, question: '问题', error_hint: '请检查配置'});
    await box.sampleModal('sample-key');
    assert.equal(box.error, null);
    assert.ok(box.html.includes(error));
    assert.ok(box.html.includes('采样失败'));
    assert.ok(!box.html.includes('id="sm-men"'));
  });
}
test('missing record still reports read error', async () => {
  const box = setup({error: '找不到该样本'});
  await box.sampleModal('missing');
  assert.equal(box.html, '');
  assert.ok(box.error.includes('找不到该样本'));
});
test('successful answer with nullable error opens review form', async () => {
  const box = setup({platform: 'kimi', ok: true, error: null, answer: '有效答案', citations: [null, 'https://example.com']});
  await box.sampleModal('key');
  assert.ok(box.html.includes('有效答案'));
  assert.ok(box.html.includes('id="sm-men"'));
});
test('failed list row is not presented as brand absent', () => {
  const box = setup(null);
  vm.runInContext('SMP={rows:[{key:"key",ok:false,platform:"doubao",round:2}],total:1}', box);
  const html = box.vSamples();
  assert.ok(html.includes('采样失败'));
  assert.ok(html.includes('第 2 轮'));
  assert.ok(!html.includes('>否</span>'));
});
test('overview does not interpret unmeasured citations as zero', () => {
  const box = setup(null);
  box.pct = n => `${n * 100}%`;
  vm.runInContext(source.slice(source.indexOf('function headline(){'), source.indexOf('function vOverview(){')), box);
  box.D.analytics = {health: {score: 2.4, subs: {mention: .053, cite: null}}, trend: []};
  assert.ok(box.headline()[1].includes('无法判断引用表现'));
  box.D.analytics.sample_quality = {provisional: true, warnings: ['采样失败']};
  assert.ok(box.headline()[0].includes('数据不完整'));
});
