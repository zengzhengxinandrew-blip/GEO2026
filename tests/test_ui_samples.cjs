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

test('sample list invalidation discards an older in-flight response', async () => {
  const box = setup(null);
  let finish;
  box.api = () => new Promise(resolve => {finish = resolve});
  const pending = box.loadSamples();
  box.invalidateSamples();
  finish({rows: [{key: 'old-failure', ok: false}], total: 1});
  await pending;
  assert.equal(vm.runInContext('SMP', box), null);
});

test('sample list fetch bypasses browser cache and keeps fresh success metadata', async () => {
  const box = setup(null);
  let options;
  box.api = async (_url, o) => {
    options = o;
    return {rows: [{key: 'latest', ok: true, brand_mentioned: true,
                    brand_rank: 1, competitors: ['竞品A']}], total: 1};
  };
  await box.loadSamples();
  const row = vm.runInContext('SMP.rows[0]', box);
  assert.equal(options.cache, 'no-store');
  assert.equal(row.ok, true);
  assert.equal(row.brand_rank, 1);
  assert.deepEqual(Array.from(row.competitors), ['竞品A']);
});

test('date delete button shows full-day count even when list has filters', () => {
  const box = setup(null);
  box.ME = {role: 'admin'};
  vm.runInContext("SMPF.date='2026-10-01';SMPF.platform='kimi';SMP={rows:[],total:1,dates:['2026-10-01'],date_counts:{'2026-10-01':12},platforms:['kimi']}", box);
  assert.ok(box.vSamples().includes('删除 2026-10-01 全部 12 条'));
});

test('date delete requires typed date and sends exact day with expected count', async () => {
  const box = setup(null);
  box.ME = {role: 'admin'};
  vm.runInContext("SMPF.date='2026-10-01';SMP={date_counts:{'2026-10-01':12}}", box);
  box.confirm = () => true;
  box.prompt = () => 'wrong';
  let sent;
  box.post = async (url, body) => {sent = {url, body};return {ok: true, deleted_count: 12}};
  box.load = async () => {};
  box.loadSamples = async () => {};
  await box.deleteSampleDate();
  assert.equal(sent, undefined);
  box.prompt = () => '2026-10-01';
  await box.deleteSampleDate();
  assert.equal(sent.url, '/api/sample-date/demo');
  assert.equal(sent.body.date, '2026-10-01');
  assert.equal(sent.body.expected_count, 12);
  assert.equal(vm.runInContext('SMPF.date', box), '');
});
