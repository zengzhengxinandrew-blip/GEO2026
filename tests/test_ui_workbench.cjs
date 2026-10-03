const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../scripts/ui.html'), 'utf8');
const start = source.indexOf('/* ===================== 内容工作台 ===================== */');
const end = source.indexOf('/* ===================== 效果验收 ===================== */');
const script = source.slice(start, end);

function deferred() {
  let resolve;
  const promise = new Promise(r => {resolve = r});
  return {promise, resolve};
}

function setup() {
  const box = {
    SLUG: 'demo', ST: {}, D: {},
    render: () => {}, toast: () => {}, confirm: () => true,
    api: async () => ({}), post: async () => ({}),
    $: () => ({value: '正文'}),
    encodeURIComponent,
  };
  vm.createContext(box);
  vm.runInContext(script, box);
  return box;
}

test('stale outline is not automatically opened', async () => {
  const box = setup();
  const calls = [];
  box.api = async url => {
    calls.push(url);
    return {question: {id: 'q016', text: '当前风险问题'}, sources: [],
            stale: [{kind: 'outline', path: 'outlines/q016.md'}]};
  };
  await box.loadWB('q016');
  assert.equal(calls.length, 1);
  assert.equal(vm.runInContext('WB.cur', box), null);
  assert.equal(vm.runInContext('WB.stale.length', box), 1);
});

test('older source response cannot replace a newer selected source', async () => {
  const box = setup();
  vm.runInContext(`WB.sources=[
    {kind:'outline',path:'outlines/q016.md'},
    {kind:'draft',path:'drafts/q016.md'}]`, box);
  const old = deferred(), newer = deferred();
  box.api = url => url.includes('outlines/') ? old.promise : newer.promise;
  box.post = async () => ({wc: 100});
  const first = box.loadWBFile(0);
  const second = box.loadWBFile(1);
  newer.resolve({text: '新选中的初稿'});
  await second;
  old.resolve({text: '过期的大纲'});
  await first;
  assert.equal(vm.runInContext('WB.text', box), '新选中的初稿');
  assert.equal(vm.runInContext('WB.cur.kind', box), 'draft');
});

test('switching question during precheck cannot publish to the wrong question', async () => {
  const box = setup();
  vm.runInContext("WB.q={id:'q016',text:'第一题'};WB.qid='q016'", box);
  const check = deferred();
  const calls = [];
  box.post = (url, body) => {
    calls.push({url, body});
    return url === '/api/precheck' ? check.promise : Promise.resolve({ok: true});
  };
  const pending = box.wbPublish();
  vm.runInContext("wbRequest++;WB.q={id:'q017',text:'第二题'};WB.qid='q017'", box);
  check.resolve({wc: 1000});
  await pending;
  assert.deepEqual(calls.map(x => x.url), ['/api/precheck']);
});
