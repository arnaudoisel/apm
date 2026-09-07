'use strict';

const MEMBERS = ['baseline-1', 'proposed-1', 'proposed-2'];

async function waitForCohort({
  github, context, cohort, attempt,
  timeoutMs = 900000, intervalMs = 20000,
  now = Date.now, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
}) {
  if (!Number.isSafeInteger(attempt) || attempt < 1 ||
      !new RegExp(`^(unit|integration)-${attempt}-apm-[a-z0-9]+-[a-z0-9_]+$`).test(cohort) ||
      !Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 900000 ||
      !Number.isFinite(intervalMs) || intervalMs <= 0) {
    throw new Error('Invalid bounded performance cohort');
  }
  const expected = new Set(MEMBERS.map(member => `performance-cohort-${cohort}-${member}`));
  const deadline = now() + timeoutMs;
  while (now() < deadline) {
    const artifacts = await github.paginate(github.rest.actions.listWorkflowRunArtifacts, {
      ...context.repo, run_id: context.runId, per_page: 100,
    });
    const members = artifacts.filter(artifact => expected.has(artifact.name));
    if (members.some(artifact => artifact.expired) ||
        new Set(members.map(artifact => artifact.name)).size !== members.length) {
      throw new Error('Expired or duplicate performance cohort artifact');
    }
    if (members.length === expected.size) {
      return;
    }
    await sleep(Math.min(intervalMs, Math.max(0, deadline - now())));
  }
  throw new Error('Performance cohort timed out before all actual test runners were ready');
}

module.exports = {waitForCohort};
