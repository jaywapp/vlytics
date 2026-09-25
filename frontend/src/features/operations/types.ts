import type { Availability, RevisionMetadata } from "../matches/types";

export type OperationSummary = {
  id: string;
  job_type: string;
  state: string;
  due_at: string;
  deadline_at: string | null;
  attempt_no: number;
  error_code: string | null;
  retryable: boolean;
};

export type CoverageSummary = {
  data_kind: string;
  availability: Availability;
  count: number;
  latest_observed_at: string;
  evidence_codes: string[];
};

export type ProviderBudgetSummary = {
  provider: string;
  currency: string;
  period: "day" | "month";
  period_start: string;
  period_end: string;
  as_of: string;
  reserved_amount: string;
  actual_amount: string;
  effective_amount: string;
  reservation_count: number;
  settled_count: number;
  outstanding_count: number;
  conservative_charge_count: number;
};

export type OperationsResponse = {
  metadata: RevisionMetadata;
  data: { items: OperationSummary[]; budgets: ProviderBudgetSummary[]; next_cursor: string | null };
};

export type CoverageResponse = {
  metadata: RevisionMetadata;
  data: { items: CoverageSummary[] };
};

export type RetryJobResponse = {
  metadata: RevisionMetadata;
  data: {
    job_id: string;
    state: "retry_wait";
    due_at: string;
    deadline_at: string | null;
    idempotent_replay: boolean;
  };
};

export interface OperationsApiClient {
  getOperations: (query: { state?: string; jobType?: string; cursor?: string; limit?: number }, signal?: AbortSignal) => Promise<OperationsResponse>;
  getCoverage: (signal?: AbortSignal) => Promise<CoverageResponse>;
  retryJob: (jobId: string, idempotencyKey: string, signal?: AbortSignal) => Promise<RetryJobResponse>;
}
