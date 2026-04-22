/**
 * Shared Gemini API client with retry-with-backoff and model fallback.
 *
 * Model priority (configurable via GEMINI_PRIMARY_MODEL env var):
 *   1. gemini-2.5-flash   (primary — fastest, cheapest)
 *   2. gemini-flash-latest (fallback)
 *   3. gemini-2.5-pro     (last resort)
 *
 * Retry policy per model:
 *   - RETRY_STATUSES: [429, 500, 502, 503, 504]
 *   - Up to maxRetries attempts before moving to the next model
 *   - Backoffs: 2s → 5s → 10s → 20s
 *   - Network errors (ECONNRESET, ETIMEDOUT, TimeoutError) are also retried
 */

const GEMINI_API_KEY = process.env.GEMINI_API_KEY ?? "";

export const GEMINI_MODELS: string[] = [
  process.env.GEMINI_PRIMARY_MODEL ?? "gemini-2.5-flash",
  "gemini-flash-latest",
  "gemini-2.5-pro",
];

export const geminiUrl = (model: string) =>
  `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${GEMINI_API_KEY}`;

const RETRY_STATUSES = [429, 500, 502, 503, 504];
const BACKOFFS_MS = [2_000, 5_000, 10_000, 20_000];

/**
 * Call a single Gemini model URL with retry-on-transient-error.
 * Throws on non-retryable errors or after exhausting maxRetries.
 * Returns the parsed JSON response body on success.
 */
export async function callGeminiWithRetry(
  url: string,
  body: unknown,
  maxRetries = 4,
): Promise<any> {
  let lastError: any;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(60_000),
      });

      if (res.ok) return await res.json();

      if (res.status === 404) {
        throw new Error(`Gemini model not available (404)`);
      }

      if (RETRY_STATUSES.includes(res.status) && attempt < maxRetries) {
        const wait = BACKOFFS_MS[attempt] ?? 20_000;
        const errSnippet = await res.text().catch(() => "");
        console.log(
          `[gemini retry] HTTP ${res.status} — ${errSnippet.slice(0, 120)} — waiting ${wait}ms (attempt ${attempt + 1}/${maxRetries})`,
        );
        lastError = new Error(`Gemini HTTP ${res.status}: ${errSnippet.slice(0, 200)}`);
        await new Promise((r) => setTimeout(r, wait));
        continue;
      }

      const errBody = await res.text().catch(() => "");
      throw new Error(`Gemini API ${res.status}: ${errBody.slice(0, 200)}`);
    } catch (e: any) {
      lastError = e;
      const isRetryable =
        e.name === "TimeoutError" ||
        e.code === "ECONNRESET" ||
        e.code === "ETIMEDOUT" ||
        /timeout|aborted/i.test(e.message ?? "");
      if (isRetryable && attempt < maxRetries) {
        const wait = BACKOFFS_MS[attempt] ?? 20_000;
        console.log(
          `[gemini retry] network error "${e.message?.slice(0, 80)}" — waiting ${wait}ms (attempt ${attempt + 1}/${maxRetries})`,
        );
        await new Promise((r) => setTimeout(r, wait));
        continue;
      }
      throw e;
    }
  }
  throw lastError ?? new Error("Gemini request failed after max retries");
}

/**
 * Try each model in GEMINI_MODELS in order until one succeeds.
 * Returns the parsed JSON body, or throws after all models are exhausted.
 */
export async function callGeminiWithModelFallback(
  body: unknown,
  maxRetriesPerModel = 4,
): Promise<any> {
  let lastErr: any;
  for (const model of GEMINI_MODELS) {
    try {
      return await callGeminiWithRetry(geminiUrl(model), body, maxRetriesPerModel);
    } catch (e: any) {
      lastErr = e;
      console.log(`[gemini fallback] model ${model} failed: ${e.message?.slice(0, 120)} — trying next model`);
    }
  }
  throw lastErr ?? new Error("All Gemini models exhausted");
}
