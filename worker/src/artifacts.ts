export type ArtifactState = "staging" | "sealed" | "published" | "abandoned";
const TRANSITIONS = {
  staging: ["sealed", "abandoned"],
  sealed: ["published", "abandoned"],
  published: [],
  abandoned: [],
} satisfies Record<ArtifactState, readonly ArtifactState[]>;

export function guardArtifactTransition(current: string, target: ArtifactState): void {
  if (!Object.entries(TRANSITIONS).some(([state, targets]) => state === current && targets.some((value) => value === target))) {
    throw new Error(`Illegal artifact transition: ${current} -> ${target}`);
  }
}

export type RetentionState = "retained" | "deletion_requested" | "deleting" | "delete_failed" | "deleted";
const RETENTION = {
  retained: ["deletion_requested"],
  deletion_requested: ["deleting"],
  deleting: ["deleted", "delete_failed", "deleting"],
  delete_failed: ["deleting"],
  deleted: [],
} satisfies Record<RetentionState, readonly RetentionState[]>;

export function guardRetentionTransition(current: string, target: RetentionState): void {
  if (!Object.entries(RETENTION).some(([state, targets]) => state === current && targets.some((value) => value === target))) {
    throw new Error(`Illegal retention transition: ${current} -> ${target}`);
  }
}


export type UploadState = "receiving" | "stored" | "admitted" | "abandoned";
const UPLOAD_TRANSITIONS = {
  receiving: ["stored", "abandoned"],
  stored: ["admitted", "abandoned"],
  admitted: [],
  abandoned: [],
} satisfies Record<UploadState, readonly UploadState[]>;

export function guardUploadTransition(current: string, target: UploadState): void {
  if (!Object.entries(UPLOAD_TRANSITIONS).some(([state, targets]) => state === current && targets.some((value) => value === target))) {
    throw new Error(`Illegal upload admission transition: ${current} -> ${target}`);
  }
}
