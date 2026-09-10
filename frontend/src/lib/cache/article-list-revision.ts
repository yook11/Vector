import "server-only";

import { randomUUID } from "node:crypto";

declare global {
  namespace NodeJS {
    interface Process {
      vectorArticleListRevision: string | undefined;
    }
  }
}

function createArticleListRevision(): string {
  return randomUUID();
}

export function getArticleListRevision(): string {
  globalThis.process.vectorArticleListRevision ??= createArticleListRevision();
  return globalThis.process.vectorArticleListRevision;
}

export function renewArticleListRevision(): string {
  const revision = createArticleListRevision();
  globalThis.process.vectorArticleListRevision = revision;
  return revision;
}
