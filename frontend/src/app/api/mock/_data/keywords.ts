import type { KeywordResponse } from "@/types";

const initialKeywords: KeywordResponse[] = [
  {
    id: 1,
    keyword: "Quantum Computing",
    category: "computing",
    isActive: true,
    articleCount: 6,
    createdAt: "2026-01-01T00:00:00Z",
  },
  {
    id: 2,
    keyword: "Materials Informatics",
    category: "materials",
    isActive: true,
    articleCount: 5,
    createdAt: "2026-01-01T00:00:00Z",
  },
  {
    id: 3,
    keyword: "Neuromorphic Computing",
    category: "computing",
    isActive: true,
    articleCount: 3,
    createdAt: "2026-01-05T00:00:00Z",
  },
  {
    id: 4,
    keyword: "Spintronics",
    category: "computing",
    isActive: true,
    articleCount: 2,
    createdAt: "2026-01-10T00:00:00Z",
  },
  {
    id: 5,
    keyword: "Photonic Computing",
    category: "computing",
    isActive: false,
    articleCount: 0,
    createdAt: "2026-01-15T00:00:00Z",
  },
  {
    id: 6,
    keyword: "Metamaterials",
    category: "materials",
    isActive: true,
    articleCount: 0,
    createdAt: "2026-01-20T00:00:00Z",
  },
];

// Mutable state for CRUD operations during development.
// Note: May reset on hot reload in Next.js dev mode.
let keywords = [...initialKeywords];
let nextId = initialKeywords.length + 1;

export function getAll(): KeywordResponse[] {
  return keywords;
}

export function findById(id: number): KeywordResponse | undefined {
  return keywords.find((k) => k.id === id);
}

export function findByKeyword(keyword: string): KeywordResponse | undefined {
  return keywords.find(
    (k) => k.keyword.toLowerCase() === keyword.toLowerCase(),
  );
}

export function create(keyword: string, category: string): KeywordResponse {
  const newKeyword: KeywordResponse = {
    id: nextId++,
    keyword,
    category,
    isActive: true,
    articleCount: 0,
    createdAt: new Date().toISOString(),
  };
  keywords.push(newKeyword);
  return newKeyword;
}

export function update(
  id: number,
  data: { isActive?: boolean | null },
): KeywordResponse | undefined {
  const kw = keywords.find((k) => k.id === id);
  if (!kw) return undefined;
  if (data.isActive !== undefined && data.isActive !== null) {
    kw.isActive = data.isActive;
  }
  return kw;
}

export function remove(id: number): boolean {
  const index = keywords.findIndex((k) => k.id === id);
  if (index === -1) return false;
  keywords.splice(index, 1);
  return true;
}
