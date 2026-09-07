"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { after, describe, it } = require("node:test");

const rc = require("./release-candidate.cjs");

const ROOT = path.resolve(__dirname, "..");
const SHA = "a".repeat(40);
const OTHER_SHA = "b".repeat(40);
const NOW = new Date().toISOString();

function catalog() {
  return JSON.parse(fs.readFileSync(path.join(__dirname, "release-platforms.json"), "ascii"));
}

function digestBytes(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function makeTemp() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "apm-release-candidate-"));
  after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

function writeRequiredConfig(root) {
  for (const relative of rc.CONFIG_HASH_FILES) {
    const file = path.join(root, relative);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const source = path.join(ROOT, relative);
    fs.writeFileSync(
      file,
      fs.existsSync(source) ? fs.readFileSync(source) : `test fixture for ${relative}\n`,
    );
  }
}

function writeCandidateFiles(root, rows = catalog(), sha = SHA, version = "1.2.3") {
  fs.mkdirSync(root, { recursive: true });
  for (const row of rows) {
    const archive = rc.archiveName(row.binary_name);
    const dir = path.join(root, row.binary_name, "release-assets");
    fs.mkdirSync(dir, { recursive: true });
    const archiveBytes = Buffer.from(`${row.binary_name}:${sha}`);
    const digest = digestBytes(archiveBytes);
    fs.writeFileSync(path.join(dir, archive), archiveBytes);
    fs.writeFileSync(path.join(dir, `${archive}.sha256`), `${digest}  ${archive}\n`, "ascii");
    fs.writeFileSync(
      path.join(dir, `${row.binary_name}.json`),
      `${JSON.stringify(
        {
          schema_version: 1,
          sha,
          version,
          binary_name: row.binary_name,
          archive,
          archive_sha256: digest,
          executable_sha256: digestBytes(Buffer.from(`exe:${row.binary_name}`)),
        },
        null,
        2,
      )}\n`,
      "ascii",
    );
  }
}

function artifactMetadata(rows = catalog(), attempt = 1, options = {}) {
  let id = options.startId || 100;
  const artifacts = [];
  const attemptNames = options.attemptNames !== false;
  if (options.includeEvidence !== false) {
    const evidenceName = attemptNames
      ? `release-candidate-evidence-${attempt}`
      : "release-candidate-evidence";
    artifacts.push({
      id: id++,
      name: evidenceName,
      size_in_bytes: 512,
      expired: options.expiredEvidence || false,
      digest: options.evidenceDigest || `sha256:${digestBytes(Buffer.from(evidenceName))}`,
    });
  }
  for (const row of rows) {
    const name = attemptNames ? `candidate-${attempt}-${row.binary_name}` : row.binary_name;
    artifacts.push({
      id: id++,
      name,
      size_in_bytes: 1000 + id,
      expired: options.expiredBinary === row.binary_name,
      digest:
        options.digestPrefix === null
          ? null
          : `sha256:${digestBytes(Buffer.from(`${options.digestPrefix || ""}${name}`))}`,
    });
  }
  return artifacts;
}

function requiredJobs(rows = catalog(), mutate = (jobs) => jobs) {
  const jobs = [
    ...rc.REQUIRED_SOURCE_JOB_FRAGMENTS.map((name) => ({
      name,
      status: "completed",
      conclusion: "success",
    })),
    ...rows.map((row) => ({
      name: `${row.binary_name} / Native Candidate Gate`,
      status: "completed",
      conclusion: "success",
    })),
  ];
  return mutate(jobs);
}

function context(overrides = {}) {
  return {
    eventName: "push",
    ref: "refs/tags/v1.2.3",
    sha: SHA,
    runId: 999,
    runAttempt: 1,
    repo: { owner: "microsoft", repo: "apm" },
    payload: { repository: { full_name: "microsoft/apm" } },
    ...overrides,
  };
}

function fakeGithub({ runs = [], artifactsByRun = {}, jobsByRun = {}, runById = {}, calls = [] } = {}) {
  return {
    rest: {
      actions: {
        listWorkflowRuns: async (params) => {
          calls.push(["listWorkflowRuns", params]);
          return { data: { workflow_runs: runs } };
        },
        listWorkflowRunArtifacts: async (params) => {
          calls.push(["listWorkflowRunArtifacts", params]);
          return { data: { artifacts: artifactsByRun[Number(params.run_id)] || [] } };
        },
        listJobsForWorkflowRun: async (params) => {
          calls.push(["listJobsForWorkflowRun", params]);
          return { data: { jobs: jobsByRun[Number(params.run_id)] || [] } };
        },
        listJobsForWorkflowRunAttempt: async (params) => {
          calls.push(["listJobsForWorkflowRunAttempt", params]);
          return { data: { jobs: jobsByRun[Number(params.run_id)] || [] } };
        },
        getWorkflowRun: async (params) => {
          calls.push(["getWorkflowRun", params]);
          return { data: runById[Number(params.run_id)] };
        },
      },
    },
  };
}

function withCwd(dir, fn) {
  const old = process.cwd();
  process.chdir(dir);
  return Promise.resolve()
    .then(fn)
    .finally(() => process.chdir(old));
}

describe("release candidate planning", () => {
  for (const unavailable of ["missing evidence", "missing platform", "expired evidence", "expired platform", "old run"]) {
    it(`builds fresh when automatic discovery finds ${unavailable}`, async () => {
      const outputs = {};
      const messages = [];
      const artifacts = artifactMetadata(catalog(), 1, {
        includeEvidence: unavailable !== "missing evidence",
        expiredEvidence: unavailable === "expired evidence",
        expiredBinary: unavailable === "expired platform" ? "apm-linux-x86_64" : undefined,
      });
      if (unavailable === "missing platform") artifacts.pop();
      const run = {
        id: 123, event: "schedule", path: rc.WORKFLOW_PATH, head_sha: SHA,
        head_branch: "main", status: "completed", conclusion: "success", run_attempt: 1,
        run_started_at: unavailable === "old run"
          ? new Date(Date.now() - 31 * 86400000).toISOString() : NOW,
      };
      await rc.plan({
        github: fakeGithub({ runs: [run], artifactsByRun: { 123: artifacts } }),
        context: context(),
        core: {
          setOutput(name, value) { outputs[name] = value; },
          info(message) { messages.push(message); },
        },
      });
      assert.equal(outputs.full_validation, "true");
      assert.equal(JSON.parse(outputs.matrix).include.length, 5);
      assert.equal(outputs.candidate_run_id, "");
      assert.equal(outputs.candidate_artifact_ids, "");
      assert.equal(outputs.candidate_evidence_artifact_id, "");
      assert.match(messages[0], /building a fresh full candidate/);
    });
  }

  it("keeps main push matrix partial and non-promotable", () => {
    const { matrix, fullValidation } = rc.matrixForContext(
      context({ eventName: "push", ref: "refs/heads/main" }),
      catalog(),
    );

    assert.equal(fullValidation, false);
    assert.equal(matrix.include.length, 4);
    assert.deepEqual(
      matrix.include.filter((row) => row.on_main === false).map((row) => row.binary_name),
      [],
    );
  });

  it("uses all platforms for schedule, dispatch, and tag full validation", () => {
    for (const ctx of [
      context({ eventName: "schedule", ref: "refs/heads/main" }),
      context({ eventName: "repository_dispatch", ref: "refs/heads/main" }),
      context({ eventName: "push", ref: "refs/tags/v1.2.3" }),
    ]) {
      const { matrix, fullValidation } = rc.matrixForContext(ctx, catalog());
      assert.equal(fullValidation, true);
      assert.equal(matrix.include.length, 5);
    }
  });

  it("outputs prior candidate run and artifact IDs for exact-SHA tag promotion", async () => {
    const outputs = {};
    const run = {
      id: 123,
      event: "schedule",
      path: rc.WORKFLOW_PATH,
      head_sha: SHA,
      head_branch: "main",
      status: "completed",
      conclusion: "success",
      run_attempt: 1,
      run_started_at: NOW,
    };
    const github = fakeGithub({
      runs: [run],
      artifactsByRun: { 123: artifactMetadata(catalog()) },
    });

    await rc.plan({
      github,
      context: context(),
      core: { setOutput(name, value) { outputs[name] = value; } },
    });

    assert.equal(outputs.candidate_run_id, "123");
    assert.match(outputs.candidate_evidence_artifact_id, /^[0-9]+$/);
    assert.equal(outputs.candidate_artifact_ids.split(",").length, 5);
  });
});

describe("trusted candidate discovery", () => {
  it("discovers exact-SHA schedule candidates with complete immutable artifact metadata", async () => {
    const run = {
      id: 123,
      event: "schedule",
      path: rc.WORKFLOW_PATH,
      head_sha: SHA,
      head_branch: "main",
      status: "completed",
      conclusion: "success",
      run_attempt: 1,
      run_started_at: NOW,
    };
    const github = fakeGithub({
      runs: [run],
      artifactsByRun: { 123: artifactMetadata(catalog()) },
    });

    const selected = await rc.discoverCandidateRun({ github, context: context(), sha: SHA });

    assert.equal(selected.run.id, 123);
  });

  it("narrows candidate lookup by exact SHA and bounded creation window", async () => {
    const calls = [];
    const github = fakeGithub({ calls });

    await rc.discoverCandidateRun({ github, context: context(), sha: SHA });

    const [, params] = calls.find(([name]) => name === "listWorkflowRuns");
    assert.equal(params.workflow_id, "build-release.yml");
    assert.equal(params.branch, "main");
    assert.equal(params.head_sha, SHA);
    assert.match(params.created, /^>=\d{4}-\d{2}-\d{2}$/);
  });

  it("ignores ordinary push-main artifacts even when they share the tag SHA", async () => {
    const run = {
      id: 123,
      event: "push",
      path: rc.WORKFLOW_PATH,
      head_sha: SHA,
      head_branch: "main",
      status: "completed",
      conclusion: "success",
      run_attempt: 1,
      run_started_at: NOW,
    };
    const github = fakeGithub({
      runs: [run],
      artifactsByRun: { 123: artifactMetadata(catalog()) },
    });

    const selected = await rc.discoverCandidateRun({ github, context: context(), sha: SHA });

    assert.equal(selected, null);
  });

  it("hard-fails when an explicitly selected candidate is older than the allowed window", async () => {
    const run = {
      id: 123,
      event: "schedule",
      path: rc.WORKFLOW_PATH,
      head_sha: SHA,
      head_branch: "main",
      status: "completed",
      conclusion: "success",
      run_attempt: 1,
      run_started_at: "2000-01-01T00:00:00Z",
    };
    const github = fakeGithub({
      runById: { 123: run },
      artifactsByRun: { 123: artifactMetadata(catalog()) },
    });

    await assert.rejects(
      () => rc.resolveExplicitCandidateRun({ github, context: context(), runId: 123, sha: SHA }),
      /older than/,
    );
  });

  for (const invalid of ["missing timestamp", "duplicate evidence", "duplicate platform", "missing digest", "API error"]) {
    it(`never silently rebuilds after ${invalid}`, async () => {
      const run = {
        id: 123, event: "schedule", path: rc.WORKFLOW_PATH, head_sha: SHA,
        head_branch: "main", status: "completed", conclusion: "success", run_attempt: 1,
        run_started_at: NOW,
      };
      const artifacts = artifactMetadata(catalog());
      if (invalid === "missing timestamp") delete run.run_started_at;
      if (invalid === "duplicate evidence") artifacts.push({ ...artifacts[0], id: 200 });
      if (invalid === "duplicate platform") artifacts.push({ ...artifacts[1], id: 200 });
      if (invalid === "missing digest") delete artifacts[0].digest;
      const github = fakeGithub({ runs: [run], artifactsByRun: { 123: artifacts } });
      if (invalid === "API error") {
        github.rest.actions.listWorkflowRunArtifacts = async () => {
          throw new Error("API request denied");
        };
      }
      const outputs = {};
      await assert.rejects(() => rc.plan({
        github, context: context(),
        core: { setOutput(name, value) { outputs[name] = value; } },
      }));
      assert.deepEqual(outputs, {});
    });
  }

  it("rejects workflow_dispatch as a release-promotion source", () => {
    assert.throws(
      () =>
        rc.validateTrustedReusableRun(
          {
            id: 123,
            event: "workflow_dispatch",
            path: rc.WORKFLOW_PATH,
            head_sha: SHA,
            head_branch: "main",
            status: "completed",
            conclusion: "success",
            run_attempt: 1,
          },
          SHA,
        ),
      /not trusted/,
    );
  });

  it("rejects same-SHA candidates from the wrong workflow path", () => {
    assert.throws(
      () =>
        rc.validateTrustedReusableRun(
          {
            id: 123,
            event: "schedule",
            path: ".github/workflows/other.yml",
            head_sha: SHA,
            head_branch: "main",
            status: "completed",
            conclusion: "success",
            run_attempt: 1,
            run_started_at: NOW,
          },
          SHA,
        ),
      /workflow path/,
    );
  });

  it("rejects missing reusable run identity fields", () => {
    for (const [field, pattern] of [
      ["path", /workflow path/],
      ["head_branch", /head_branch/],
      ["run_attempt", /attempt/],
    ]) {
      const run = {
        id: 123,
        event: "schedule",
        path: rc.WORKFLOW_PATH,
        head_sha: SHA,
        head_branch: "main",
        status: "completed",
        conclusion: "success",
        run_attempt: 1,
        run_started_at: NOW,
      };
      delete run[field];
      assert.throws(() => rc.validateTrustedReusableRun(run, SHA), pattern);
    }
  });

  it("rejects expired candidate artifacts for explicit candidate handling", async () => {
    const run = {
      id: 123,
      event: "schedule",
      path: rc.WORKFLOW_PATH,
      head_sha: SHA,
      head_branch: "main",
      status: "completed",
      conclusion: "success",
      run_attempt: 1,
      run_started_at: NOW,
    };
    const github = fakeGithub({
      runById: { 123: run },
      artifactsByRun: {
        123: artifactMetadata(catalog(), 1, { expiredBinary: "apm-linux-x86_64" }),
      },
    });

    await assert.rejects(
      () => rc.resolveExplicitCandidateRun({ github, context: context(), runId: 123, sha: SHA }),
      /expired/,
    );
  });

  it("hard-fails invalid explicit candidate_run_id instead of falling back", async () => {
    const github = fakeGithub({
      runById: {
        777: {
          id: 777,
          event: "workflow_dispatch",
          path: rc.WORKFLOW_PATH,
          head_sha: SHA,
          head_branch: "main",
          status: "completed",
          conclusion: "success",
          run_attempt: 1,
        },
      },
      artifactsByRun: { 777: artifactMetadata(catalog()) },
    });

    await assert.rejects(
      () => rc.resolveExplicitCandidateRun({ github, context: context(), runId: 777, sha: SHA }),
      /not trusted/,
    );
  });

  it("chooses newest same-SHA trusted candidate without requiring cross-run byte equality", async () => {
    const runs = [
      {
        id: 123,
        event: "schedule",
        path: rc.WORKFLOW_PATH,
        head_sha: SHA,
        head_branch: "main",
        status: "completed",
        conclusion: "success",
        run_attempt: 1,
        run_started_at: "2026-09-07T10:00:00Z",
      },
      {
        id: 124,
        event: "repository_dispatch",
        path: rc.WORKFLOW_PATH,
        head_sha: SHA,
        head_branch: "main",
        status: "completed",
        conclusion: "success",
        run_attempt: 1,
        run_started_at: "2026-09-07T11:00:00Z",
      },
    ];
    const github = fakeGithub({
      runs,
      artifactsByRun: {
        123: artifactMetadata(catalog(), 1, { digestPrefix: "old-" }),
        124: artifactMetadata(catalog(), 1, { digestPrefix: "new-" }),
      },
    });

    const selected = await rc.discoverCandidateRun({ github, context: context(), sha: SHA });

    assert.equal(selected.run.id, 124);
  });

  it("does not fall back to an older same-SHA candidate when the newest candidate is invalid", async () => {
    const runs = [
      {
        id: 123,
        event: "schedule",
        path: rc.WORKFLOW_PATH,
        head_sha: SHA,
        head_branch: "main",
        status: "completed",
        conclusion: "success",
        run_attempt: 1,
        run_started_at: "2026-09-07T10:00:00Z",
      },
      {
        id: 124,
        event: "repository_dispatch",
        path: rc.WORKFLOW_PATH,
        head_sha: SHA,
        head_branch: "main",
        status: "completed",
        conclusion: "success",
        run_attempt: 1,
        run_started_at: "2026-09-07T11:00:00Z",
      },
    ];
    const github = fakeGithub({
      runs,
      artifactsByRun: {
        123: artifactMetadata(catalog(), 1, { digestPrefix: "old-" }),
        124: artifactMetadata(catalog(), 1, { evidenceDigest: "malformed" }),
      },
    });

    await assert.rejects(
      () => rc.discoverCandidateRun({ github, context: context(), sha: SHA }),
      /valid immutable sha256 digest/,
    );
  });
});

describe("candidate manifest and publication verification", () => {
  it("qualifies a manifest only with full source and five-platform native gate evidence", async () => {
    const root = makeTemp();
    writeRequiredConfig(root);
    const artifactRoot = path.join(root, "candidate-artifacts");
    writeCandidateFiles(artifactRoot);
    const github = fakeGithub({
      artifactsByRun: { 999: artifactMetadata(catalog()) },
      jobsByRun: { 999: requiredJobs() },
    });

    await withCwd(root, async () => {
      const evidence = await rc.qualify({
        github,
        context: context({ eventName: "schedule", ref: "refs/heads/main" }),
        core: { info() {} },
        artifactRoot,
      });

      assert.equal(evidence.full_validation, true);
      assert.equal(Object.keys(evidence.platforms).length, 5);
      assert.equal(fs.existsSync(path.join(root, "release-candidate-evidence.json")), true);
    });
  });

  it("rejects legacy bare artifact names and tells operators to rerun all jobs", async () => {
    const root = makeTemp();
    writeRequiredConfig(root);
    const artifactRoot = path.join(root, "candidate-artifacts");
    writeCandidateFiles(artifactRoot);
    const github = fakeGithub({
      artifactsByRun: { 999: artifactMetadata(catalog(), 1, { attemptNames: false }) },
      jobsByRun: { 999: requiredJobs() },
    });

    await withCwd(root, async () => {
      await assert.rejects(
        () => rc.qualify({ github, context: context(), core: { info() {} }, artifactRoot }),
        /Use Re-run all jobs to regenerate complete candidate evidence for this attempt/,
      );
    });
  });

  it("rejects manifests missing source gate evidence", () => {
    assert.throws(
      () =>
        rc.assertRequiredJobs(
          requiredJobs(catalog(), (jobs) =>
            jobs.filter((job) => !job.name.includes("Test Architecture Ratchets")),
          ),
          catalog(),
        ),
      /Test Architecture Ratchets/,
    );
  });

  it("rejects shadow and duplicate jobs instead of substring-matching a success", () => {
    assert.throws(
      () =>
        rc.selectRequiredJobs(
          requiredJobs(catalog(), (jobs) => [
            ...jobs.filter((job) => job.name !== "Candidate Source Checks / Lint"),
            {
              name: "Candidate Source Checks / Not Lint",
              status: "completed",
              conclusion: "success",
            },
          ]),
          catalog(),
        ),
      /exactly one Candidate Source Checks \/ Lint/,
    );

    assert.throws(
      () =>
        rc.selectRequiredJobs(
          requiredJobs(catalog(), (jobs) => [
            ...jobs,
            {
              name: "Candidate Source Checks / Windows Compatibility Gate",
              status: "completed",
              conclusion: "success",
            },
          ]),
          catalog(),
        ),
      /exactly one Candidate Source Checks \/ Windows Compatibility Gate/,
    );
  });

  it("rejects candidate archives whose version does not match the tag", async () => {
    const root = makeTemp();
    writeRequiredConfig(root);
    const artifactRoot = path.join(root, "candidate-artifacts");
    writeCandidateFiles(artifactRoot, catalog(), SHA, "1.2.4");
    const github = fakeGithub({
      artifactsByRun: { 999: artifactMetadata(catalog()) },
      jobsByRun: { 999: requiredJobs() },
    });

    await withCwd(root, async () => {
      await assert.rejects(
        () => rc.qualify({ github, context: context(), core: { info() {} }, artifactRoot }),
        /does not match tag v1\.2\.3/,
      );
    });
  });

  it("rejects a reusable manifest whose version does not match the release tag", async () => {
    const root = makeTemp();
    writeRequiredConfig(root);
    const artifactRoot = path.join(root, "candidate-artifacts");
    const outputRoot = path.join(root, "release-assets");
    writeCandidateFiles(artifactRoot);
    const github = fakeGithub({
      artifactsByRun: { 999: artifactMetadata(catalog()) },
      jobsByRun: { 999: requiredJobs() },
      runById: {
        999: {
          id: 999,
          event: "schedule",
          path: rc.WORKFLOW_PATH,
          head_sha: SHA,
          head_branch: "main",
          status: "completed",
          conclusion: "success",
          run_attempt: 1,
          run_started_at: NOW,
        },
      },
    });

    await withCwd(root, async () => {
      await rc.qualify({
        github,
        context: context({ eventName: "schedule", ref: "refs/heads/main" }),
        core: { info() {} },
        artifactRoot,
      });

      await assert.rejects(
        () =>
          rc.verify({
            github,
            context: context({ ref: "refs/tags/v1.2.4" }),
            core: { info() {} },
            runId: 999,
            artifactRoot,
            evidencePath: path.join(root, "release-candidate-evidence.json"),
            outputRoot,
          }),
        /version 1\.2\.3 does not match tag v1\.2\.4/,
      );
    });
  });

  it("rejects malformed API artifact digests", async () => {
    const run = {
      id: 123,
      event: "schedule",
      path: rc.WORKFLOW_PATH,
      head_sha: SHA,
      head_branch: "main",
      status: "completed",
      conclusion: "success",
      run_attempt: 1,
      run_started_at: NOW,
    };
    const github = fakeGithub({
      runById: { 123: run },
      artifactsByRun: { 123: artifactMetadata(catalog(), 1, { digestPrefix: null }) },
    });

    await assert.rejects(
      () => rc.resolveExplicitCandidateRun({ github, context: context(), runId: 123, sha: SHA }),
      /valid immutable sha256 digest/,
    );
  });

  it("verifies and copies exact archive bytes without repacking", async () => {
    const root = makeTemp();
    writeRequiredConfig(root);
    const artifactRoot = path.join(root, "candidate-artifacts");
    const outputRoot = path.join(root, "release-assets");
    writeCandidateFiles(artifactRoot);
    const github = fakeGithub({
      artifactsByRun: { 999: artifactMetadata(catalog()) },
      jobsByRun: { 999: requiredJobs() },
      runById: {
        999: {
          id: 999,
          event: "push",
          path: rc.WORKFLOW_PATH,
          head_sha: SHA,
          head_branch: "v1.2.3",
          status: "in_progress",
          conclusion: null,
          run_attempt: 1,
        },
      },
    });

    await withCwd(root, async () => {
      await rc.qualify({ github, context: context(), core: { info() {} }, artifactRoot });
      await rc.verify({
        github,
        context: context(),
        core: { info() {} },
        runId: 999,
        artifactRoot,
        evidencePath: path.join(root, "release-candidate-evidence.json"),
        outputRoot,
      });

      for (const row of catalog()) {
        const archive = rc.archiveName(row.binary_name);
        assert.equal(
          fs.readFileSync(path.join(outputRoot, archive), "utf8"),
          fs.readFileSync(path.join(artifactRoot, row.binary_name, "release-assets", archive), "utf8"),
        );
      }
    });
  });

  it("hard-fails when archive bytes differ from manifest digest", async () => {
    const root = makeTemp();
    writeRequiredConfig(root);
    const artifactRoot = path.join(root, "candidate-artifacts");
    const outputRoot = path.join(root, "release-assets");
    writeCandidateFiles(artifactRoot);
    const github = fakeGithub({
      artifactsByRun: { 999: artifactMetadata(catalog()) },
      jobsByRun: { 999: requiredJobs() },
      runById: {
        999: {
          id: 999,
          event: "push",
          path: rc.WORKFLOW_PATH,
          head_sha: SHA,
          head_branch: "v1.2.3",
          status: "in_progress",
          conclusion: null,
          run_attempt: 1,
        },
      },
    });

    await withCwd(root, async () => {
      await rc.qualify({ github, context: context(), core: { info() {} }, artifactRoot });
      const tampered = path.join(
        artifactRoot,
        "apm-linux-x86_64",
        "release-assets",
        "apm-linux-x86_64.tar.gz",
      );
      fs.writeFileSync(tampered, "tampered");

      await assert.rejects(
        () =>
          rc.verify({
            github,
            context: context(),
            core: { info() {} },
            runId: 999,
            artifactRoot,
            evidencePath: path.join(root, "release-candidate-evidence.json"),
            outputRoot,
          }),
        /archive digest mismatch/,
      );
    });
  });

  it("hard-fails when downloaded candidate artifacts contain an unexpected release archive", async () => {
    const root = makeTemp();
    writeRequiredConfig(root);
    const artifactRoot = path.join(root, "candidate-artifacts");
    const outputRoot = path.join(root, "release-assets");
    writeCandidateFiles(artifactRoot);
    const github = fakeGithub({
      artifactsByRun: { 999: artifactMetadata(catalog()) },
      jobsByRun: { 999: requiredJobs() },
      runById: {
        999: {
          id: 999,
          event: "push",
          path: rc.WORKFLOW_PATH,
          head_sha: SHA,
          head_branch: "v1.2.3",
          status: "in_progress",
          conclusion: null,
          run_attempt: 1,
        },
      },
    });

    await withCwd(root, async () => {
      await rc.qualify({ github, context: context(), core: { info() {} }, artifactRoot });
      fs.writeFileSync(path.join(artifactRoot, "apm-surprise.tar.gz"), "unexpected");

      await assert.rejects(
        () =>
          rc.verify({
            github,
            context: context(),
            core: { info() {} },
            runId: 999,
            artifactRoot,
            evidencePath: path.join(root, "release-candidate-evidence.json"),
            outputRoot,
          }),
        /unexpected release files/,
      );
    });
  });

  it("hard-fails when live Actions job state no longer proves a required gate", async () => {
    const root = makeTemp();
    writeRequiredConfig(root);
    const artifactRoot = path.join(root, "candidate-artifacts");
    const outputRoot = path.join(root, "release-assets");
    writeCandidateFiles(artifactRoot);
    const githubForQualify = fakeGithub({
      artifactsByRun: { 999: artifactMetadata(catalog()) },
      jobsByRun: { 999: requiredJobs() },
    });
    const githubForVerify = fakeGithub({
      artifactsByRun: { 999: artifactMetadata(catalog()) },
      jobsByRun: {
        999: requiredJobs(catalog(), (jobs) =>
          jobs.map((job) =>
            job.name.includes("Lint") ? { ...job, conclusion: "failure" } : job,
          ),
        ),
      },
      runById: {
        999: {
          id: 999,
          event: "push",
          path: rc.WORKFLOW_PATH,
          head_sha: SHA,
          head_branch: "v1.2.3",
          status: "in_progress",
          conclusion: null,
          run_attempt: 1,
        },
      },
    });

    await withCwd(root, async () => {
      await rc.qualify({
        github: githubForQualify,
        context: context(),
        core: { info() {} },
        artifactRoot,
      });

      await assert.rejects(
        () =>
          rc.verify({
            github: githubForVerify,
            context: context(),
            core: { info() {} },
            runId: 999,
            artifactRoot,
            evidencePath: path.join(root, "release-candidate-evidence.json"),
            outputRoot,
          }),
        /Candidate Source Checks \/ Lint was completed\/failure/,
      );
    });
  });

  it("hard-fails explicit candidate_run_id verification when SHA mismatches", async () => {
    const root = makeTemp();
    writeRequiredConfig(root);
    const artifactRoot = path.join(root, "candidate-artifacts");
    const outputRoot = path.join(root, "release-assets");
    writeCandidateFiles(artifactRoot);
    const github = fakeGithub({
      artifactsByRun: { 999: artifactMetadata(catalog()) },
      jobsByRun: { 999: requiredJobs() },
      runById: {
        999: {
          id: 999,
          event: "schedule",
          path: rc.WORKFLOW_PATH,
          head_sha: OTHER_SHA,
          head_branch: "main",
          status: "completed",
          conclusion: "success",
          run_attempt: 1,
        },
      },
    });

    await withCwd(root, async () => {
      const evidence = {
        schema_version: 1,
        repo: "microsoft/apm",
        workflow_path: rc.WORKFLOW_PATH,
        event: "schedule",
        ref: "refs/heads/main",
        head_branch: "main",
        head_sha: OTHER_SHA,
        run_id: 999,
        run_attempt: 1,
        full_validation: true,
        config_hashes: rc.configHashes(root),
        required_jobs: requiredJobs(),
        platforms: rc.collectArchiveInventory({ artifactRoot, catalog: catalog(), sha: SHA }),
      };
      fs.writeFileSync(
        path.join(root, "release-candidate-evidence.json"),
        `${JSON.stringify(evidence, null, 2)}\n`,
      );

      await assert.rejects(
        () =>
          rc.verify({
            github,
            context: context(),
            core: { info() {} },
            runId: 999,
            artifactRoot,
            evidencePath: path.join(root, "release-candidate-evidence.json"),
            outputRoot,
          }),
        /SHA does not match tag SHA/,
      );
    });
  });
});
