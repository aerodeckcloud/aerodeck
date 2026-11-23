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

export type RollbackConfirmInput = {
  snapshotNames: string[] | null | undefined;
  /** Current keys, or null when the list could not be loaded. */
  currentNames: string[] | null;
};

export function rollbackConfirmMessage(input: RollbackConfirmInput): string {
  const { snapshotNames, currentNames } = input;
  if (snapshotNames == null) {
    return [
      "Roll back to this release?",
      "",
      "This release has no saved variables. The image will roll back; environment variables stay as they are.",
    ].join("\n");
  }

  if (currentNames === null) {
    const saved =
      snapshotNames.length > 0 ? snapshotNames.join(", ") : "(none — empty snapshot)";
    return [
      "Roll back to this release?",
      "",
      `Saved variable names: ${saved}`,
      "Could not load the current environment variables, so the add/remove list is unavailable.",
      "Confirming will still restore the saved variables when the snapshot is readable.",
      "",
      "Values are never shown.",
    ].join("\n");
  }

  const diff = rollbackVariableDiff(snapshotNames, currentNames);
  const lines = ["Roll back to this release?", "", "Variables that would change (names only):"];
  if (diff && diff.added.length > 0) {
    lines.push(`Add: ${diff.added.join(", ")}`);
  }
  if (diff && diff.removed.length > 0) {
    lines.push(`Remove: ${diff.removed.join(", ")}`);
  }
  if (diff && diff.restored.length > 0) {
    lines.push(`Restore values for: ${diff.restored.join(", ")}`);
  }
  if (
    diff &&
    diff.added.length === 0 &&
    diff.removed.length === 0 &&
    diff.restored.length === 0
  ) {
    lines.push("None — the snapshot is empty, so every current variable will be removed.");
  }
  lines.push("", "Values are never shown.");
  return lines.join("\n");
}
