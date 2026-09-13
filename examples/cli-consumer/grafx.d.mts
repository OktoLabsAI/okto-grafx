export interface GrafxDocument {
  command: string;
  exit_code: number;
  result: string;
  [key: string]: unknown;
}
export interface GrafxOptions {
  executable?: string;
  prefixArgs?: string[];
  timeoutMs?: number;
  maxBytes?: number;
}
export function runGrafx(args: string[], options?: GrafxOptions): Promise<GrafxDocument>;
