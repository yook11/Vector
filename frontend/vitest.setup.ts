import "@testing-library/jest-dom/vitest";

// Phase 1 では純関数のみだが、jest-dom matcher を初期から有効化することで
// Phase 2 の component test 追加時に setup 修正を不要にする。
// MSW の setupServer は Phase 2 で追加する。
