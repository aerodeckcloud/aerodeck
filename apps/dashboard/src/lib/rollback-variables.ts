/** Build the rollback confirm copy. Names only — never values (#273). */

export type RollbackVariableDiff = {
  added: string[];
  removed: string[];
  restored: string[];
};

export function rollbackVariableDiff(
  snapshotNames: string[] | null | undefined,
  currentNames: string[],
): RollbackVariableDiff | null {
  if (snapshotNames == null) {
    return null;
  }
  const snapshot = new Set(snapshotNames);
  const current = new Set(currentNames);
  const added = snapshotNames.filter((name) => !current.has(name)).sort();
  const removed = currentNames.filter((name) => !snapshot.has(name)).sort();
  const restored = snapshotNames.filter((name) => current.has(name)).sort();
  return { added, removed, restored };
}

export function rollbackConfirmMessage(diff: RollbackVariableDiff | null): string {
  if (diff === null) {
    return [
      "Roll back to this release?",
      "",
      "This release has no saved variables. The image will roll back; environment variables stay as they are.",
    ].join("\n");
  }

  const lines = ["Roll back to this release?", "", "Variables that would change (names only):"];
  if (diff.added.length > 0) {
    lines.push(`Add: ${diff.added.join(", ")}`);
  }
  if (diff.removed.length > 0) {
    lines.push(`Remove: ${diff.removed.join(", ")}`);
  }
  if (diff.restored.length > 0) {
    lines.push(`Restore values for: ${diff.restored.join(", ")}`);
  }
  if (diff.added.length === 0 && diff.removed.length === 0 && diff.restored.length === 0) {
    lines.push("None — the snapshot is empty, so every current variable will be removed.");
  }
  lines.push("", "Values are never shown.");
  return lines.join("\n");
}
