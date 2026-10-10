const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../scripts/ui.html'), 'utf8');
const script = source.slice(source.indexOf('async function editQuestions(){'),
  source.indexOf('/* ===================== 差距诊断 ===================== */'));

function setup(value, questions = [{id: 'q001', group: '场景', market: 'cn', text: '旧问题'}], market = 'cn') {
  const calls = [];
  const notices = [];
  const box = {
    SLUG: 'demo', ST: {qRevision: 'revision-1'},
    api: async () => ({_revision: 'revision-1', questions, market}),
    post: async (url, body) => {calls.push({url, body}); return {ok: true}},
    $: selector => ({value: selector === '#qnew-market' ? market : value}),
    toast: (message, level) => notices.push({message, level}),
    modal: html => {box.html = html}, esc: text => text,
    closeModal: () => {}, load: async () => {},
  };
  vm.createContext(box);
  vm.runInContext(script, box);
  return {box, calls, notices};
}

test('plain Chinese question is saved as a manual visibility question', async () => {
  const {box, calls} = setup('q001|场景|cn|auto|旧问题\n洁净室项目选哪个品牌？');
  await box.saveQuestions();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/config/demo');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body.questions[1])), {
    id: 'q002', group: '推荐', market: 'cn', text: '洁净室项目选哪个品牌？',
    scope: 'visibility', source: 'manual',
  });
});

test('multiple plain questions receive distinct IDs and retain the original rows', async () => {
  const {box, calls} = setup('q001|场景|cn|旧问题\n洁净室项目选哪个品牌？\n洁净室品牌怎么比较？');
  await box.saveQuestions();
  assert.deepEqual(Array.from(calls[0].body.questions, q => q.id), ['q001', 'q002', 'q003']);
  assert.equal(calls[0].body.questions[0].text, '旧问题');
});

test('overseas plain question is saved as a global visibility question', async () => {
  const {box, calls} = setup('q001|场景|cn|旧问题\nWhich cleanroom brand should I choose?',
    [{id: 'q001', group: '场景', market: 'cn', text: '旧问题'}], 'global');
  await box.saveQuestions();
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body.questions[1])), {
    id: 'q101', group: '推荐', market: 'global',
    text: 'Which cleanroom brand should I choose?', scope: 'visibility', source: 'manual',
  });
});

test('overseas projects preselect the overseas market in the editor', async () => {
  const {box} = setup('', [], 'global');
  await box.editQuestions();
  assert.match(box.html, /<option value="global" selected>海外<\/option>/);
});

test('shared-market plain question is saved for both markets', async () => {
  const {box, calls} = setup('Which cleanroom brand should I choose?', [], 'both');
  await box.saveQuestions();
  assert.equal(calls[0].body.questions[0].id, 'q901');
  assert.equal(calls[0].body.questions[0].market, 'both');
});

test('saving an existing manual question keeps its source', async () => {
  const question = {id: 'q002', group: '推荐', market: 'cn',
    text: '洁净室项目选哪个品牌？', scope: 'visibility', source: 'manual'};
  const {box, calls} = setup('q002|推荐|cn|visibility|洁净室项目选哪个品牌？', [question]);
  await box.saveQuestions();
  assert.equal(calls[0].body.questions[0].source, 'manual');
});

test('a duplicate plain question is rejected before saving', async () => {
  const {box, calls, notices} = setup('q001|场景|cn|auto|旧问题\n旧问题');
  await box.saveQuestions();
  assert.equal(calls.length, 0);
  assert.match(notices[0].message, /问题重复/);
});

test('incomplete structured rows are reported without replacing the question bank', async () => {
  const {box, calls, notices} = setup('q001|场景|cn|auto|旧问题\nq002|推荐|cn|');
  await box.saveQuestions();
  assert.equal(calls.length, 0);
  assert.match(notices[0].message, /第 2 行/);
  assert.equal(notices[0].level, 'err');
});
