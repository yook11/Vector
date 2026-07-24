import { spawn } from "node:child_process";
import path from "node:path";
import type { ResearchContinuityVariant } from "./research";

const repositoryDirectory = path.resolve(__dirname, "../../..");
const resetRateLimitsScript = [
  "local cursor = '0'",
  "repeat",
  "local result = redis.call('SCAN', cursor, 'MATCH', 'rl:*', 'COUNT', 1000)",
  "cursor = result[1]",
  "local keys = result[2]",
  "if #keys > 0 then redis.call('UNLINK', unpack(keys)) end",
  "until cursor == '0'",
  "return 1",
].join(" ");

const variantArguments: Record<ResearchContinuityVariant, string> = {
  closed: "closed",
  open: "open",
};
const researchUuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function runResearchContinuityCommand(
  command: "reset" | "fail" | "complete",
  variant: ResearchContinuityVariant,
): Promise<void> {
  const variantArgument = variantArguments[variant];
  if (variantArgument === undefined) {
    return Promise.reject(
      new Error(`Unsupported research continuity variant: ${String(variant)}`),
    );
  }

  return runDockerComposeCommand(
    [
      "-e",
      "CROSSREF_CONTACT_EMAIL=crossref-contact@example.invalid",
      "backend",
      "python",
      "scripts/seed_e2e_research.py",
      command,
      variantArgument,
    ],
    `Research continuity ${command} ${variantArgument}`,
  );
}

function runDockerComposeCommand(
  args: readonly string[],
  label: string,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn("docker", ["compose", "exec", "-T", ...args], {
      cwd: repositoryDirectory,
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });
    child.once("error", reject);
    child.once("close", (code, signal) => {
      if (code === 0) {
        resolve();
        return;
      }
      const detail = [stderr.trim(), stdout.trim()].filter(Boolean).join("\n");
      reject(
        new Error(
          `${label} failed ` +
            `(exit=${String(code)}, signal=${String(signal)})` +
            (detail.length > 0 ? `\n${detail}` : ""),
        ),
      );
    });
  });
}

export function resetResearchContinuity(
  variant: ResearchContinuityVariant,
): Promise<void> {
  return runResearchContinuityCommand("reset", variant);
}

export function failResearchContinuity(
  variant: ResearchContinuityVariant,
): Promise<void> {
  return runResearchContinuityCommand("fail", variant);
}

export function completeResearchContinuity(
  variant: ResearchContinuityVariant,
): Promise<void> {
  return runResearchContinuityCommand("complete", variant);
}

export function resetResearchRateLimits(): Promise<void> {
  return runDockerComposeCommand(
    ["redis-rl", "redis-cli", "--raw", "EVAL", resetRateLimitsScript, "0"],
    "Research E2E rate-limit reset",
  );
}

export function resetResearchDailyQuota(): Promise<void> {
  return runDockerComposeCommand(
    [
      "-e",
      "CROSSREF_CONTACT_EMAIL=crossref-contact@example.invalid",
      "backend",
      "python",
      "scripts/seed_e2e_research.py",
      "reset-quota",
    ],
    "Research E2E user daily quota reset",
  );
}

export function failResearchSubmission(runId: string): Promise<void> {
  if (!researchUuidPattern.test(runId)) {
    return Promise.reject(new Error(`Invalid Research run ID: ${runId}`));
  }
  return runDockerComposeCommand(
    [
      "-e",
      "CROSSREF_CONTACT_EMAIL=crossref-contact@example.invalid",
      "backend",
      "python",
      "scripts/seed_e2e_research.py",
      "fail-submission",
      runId,
    ],
    "Research E2E submission terminal transition",
  );
}
