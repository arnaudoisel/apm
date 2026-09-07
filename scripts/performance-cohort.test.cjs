'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {waitForCohort} = require('./performance-cohort.cjs');

const cohort = 'integration-2-apm-darwin-arm64';
const names = ['baseline-1', 'proposed-1', 'proposed-2']
  .map(member => `performance-cohort-${cohort}-${member}`);

function harness(responses) {
  let clock = 0;
  let calls = 0;
  const github = {
    rest: {actions: {listWorkflowRunArtifacts: 'list'}},
    paginate: async (method, params) => {
      assert.equal(method, 'list');
      assert.deepEqual(params, {owner: 'owner', repo: 'repo', run_id: 123, per_page: 100});
      return responses[Math.min(calls++, responses.length - 1)];
    },
  };
  return {
    options: {
      github, context: {repo: {owner: 'owner', repo: 'repo'}, runId: 123},
      cohort, attempt: 2, timeoutMs: 100, intervalMs: 20,
      now: () => clock, sleep: async ms => { clock += ms; },
    },
    calls: () => calls,
  };
}

test('waits for the exact current-attempt trio, ignoring old attempts', async () => {
  const current = names.map(name => ({name, expired: false}));
  const state = harness([
    [{name: names[0].replace('-2-', '-1-'), expired: false}, current[0]],
    current,
  ]);
  await waitForCohort(state.options);
  assert.equal(state.calls(), 2);
});

test('missing members time out instead of allowing incomparable tests', async () => {
  const state = harness([[{name: names[0], expired: false}]]);
  await assert.rejects(waitForCohort(state.options), /timed out/);
  assert.equal(state.calls(), 5);
});

for (const fault of ['duplicate', 'expired']) {
  test(`rejects ${fault} artifact evidence`, async () => {
    const members = names.map(name => ({name, expired: fault === 'expired'}));
    if (fault === 'duplicate') members.push(members[0]);
    const state = harness([members]);
    await assert.rejects(waitForCohort(state.options), /Expired or duplicate/);
  });
}

test('API failures propagate rather than pretending a cohort is ready', async () => {
  const state = harness([]);
  state.options.github.paginate = async () => { throw new Error('API unavailable'); };
  await assert.rejects(waitForCohort(state.options), /API unavailable/);
});

for (const change of [
  {attempt: 1}, {cohort: '../../wrong'}, {timeoutMs: Infinity},
  {timeoutMs: 900001}, {intervalMs: 0},
]) {
  test(`rejects invalid wait input ${JSON.stringify(change)}`, async () => {
    const state = harness([]);
    await assert.rejects(waitForCohort({...state.options, ...change}), /Invalid bounded/);
    assert.equal(state.calls(), 0);
  });
}
