import type { RevisionMetadata } from "./matches/types";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

export function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

export function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

export function isIsoTimestamp(value: unknown): value is string {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

export function isRevisionMetadata(value: unknown): value is RevisionMetadata {
  if (!isRecord(value) || value.schema_version !== "vlytics.operator.v1") return false;
  if (
    !isStringArray(value.source_snapshot_ids) ||
    !isStringArray(value.schedule_revision_ids) ||
    !isStringArray(value.prediction_revision_ids) ||
    !isStringArray(value.evaluation_revision_ids) ||
    !isRecord(value.model_versions)
  ) {
    return false;
  }
  return Object.values(value.model_versions).every(isStringArray);
}
