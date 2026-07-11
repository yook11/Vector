import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendDirectory = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const repositoryDirectory = path.resolve(frontendDirectory, "..");

function run(command, args, cwd) {
  return spawnSync(command, args, {
    cwd,
    env: process.env,
    stdio: "inherit",
  }).status;
}

let exitCode = 1;
try {
  const userSeed = run(
    "docker",
    ["compose", "exec", "-T", "backend", "python", "scripts/seed_e2e_users.py"],
    repositoryDirectory,
  );
  if (userSeed !== 0) {
    throw new Error(`E2E user seed failed with exit code ${userSeed}`);
  }

  const researchSeed = run(
    "docker",
    [
      "compose",
      "exec",
      "-T",
      "backend",
      "python",
      "scripts/seed_e2e_research.py",
      "seed",
    ],
    repositoryDirectory,
  );
  if (researchSeed !== 0) {
    throw new Error(`Research seed failed with exit code ${researchSeed}`);
  }

  const authSetup = run(
    "npx",
    [
      "playwright",
      "test",
      "e2e/auth.setup.ts",
      "--project=setup",
      "--grep=authenticate user",
    ],
    frontendDirectory,
  );
  if (authSetup !== 0) {
    throw new Error(`User auth setup failed with exit code ${authSetup}`);
  }

  exitCode =
    run(
      "npx",
      [
        "playwright",
        "test",
        "e2e/research.spec.ts",
        "--project=user",
        "--no-deps",
      ],
      frontendDirectory,
    ) ?? 1;
} catch (error) {
  console.error(error instanceof Error ? error.message : error);
  exitCode = 1;
} finally {
  const cleanup = run(
    "docker",
    [
      "compose",
      "exec",
      "-T",
      "backend",
      "python",
      "scripts/seed_e2e_research.py",
      "cleanup",
    ],
    repositoryDirectory,
  );
  if (cleanup !== 0) {
    console.error(`Research cleanup failed with exit code ${cleanup}`);
    exitCode = cleanup ?? 1;
  }
}

process.exitCode = exitCode;
