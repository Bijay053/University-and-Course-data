import app from "./app";
import { logger } from "./lib/logger";
import { startMonthlyScrapingScheduler } from "./services/monthly-scraping";
import { stopRunningRuntimeJobs } from "./services/scrape-runtime-jobs";
import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";

const rawPort = process.env["API_PORT"] ?? process.env["PORT"] ?? "8080";
const host = process.env["API_HOST"] ?? "0.0.0.0";

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid API_PORT/PORT value: "${rawPort}"`);
}

let scrapeWorkerProcess: ChildProcess | null = null;
const scrapeWorkerProcesses = new Set<ChildProcess>();
const scrapeWorkerCount = Math.max(1, Number(process.env["SCRAPE_WORKER_COUNT"] ?? "10"));

function stopScrapeWorkers(signal: NodeJS.Signals = "SIGTERM") {
  for (const child of scrapeWorkerProcesses) {
    if (child.killed) continue;
    try {
      child.kill(signal);
    } catch {}
  }
}

function startScrapeWorker() {
  if (process.env["SCRAPE_WORKER_DISABLED"] === "true") return;
  const workerPath = fileURLToPath(new URL("./workers/scrape-worker.mjs", import.meta.url));
  const child = spawn(process.execPath, [workerPath], {
    stdio: "inherit",
    env: {
      ...process.env,
      SCRAPE_WORKER_PROCESS: "1",
      SCRAPE_WORKER_PARENT_PID: String(process.pid),
    },
  });
  scrapeWorkerProcess = child;
  scrapeWorkerProcesses.add(child);
  child.on("exit", (code, signal) => {
    scrapeWorkerProcesses.delete(child);
    if (scrapeWorkerProcess === child) scrapeWorkerProcess = null;
    logger.warn({ code, signal }, "Scrape worker exited");
    if (process.exitCode == null) {
      setTimeout(() => startScrapeWorker(), 2000);
    }
  });
}

let shuttingDown = false;
function registerShutdownHandlers() {
  const shutdown = (signal: NodeJS.Signals) => {
    if (shuttingDown) return;
    shuttingDown = true;
    logger.info({ signal }, "Stopping scrape workers");
    stopScrapeWorkers(signal);
    setTimeout(() => stopScrapeWorkers("SIGKILL"), 3000).unref();
    process.exit(0);
  };

  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
  process.on("exit", () => {
    stopScrapeWorkers("SIGTERM");
  });
}

registerShutdownHandlers();

app.listen(port, host, async (err) => {
  if (err) {
    logger.error({ err }, "Error listening on port");
    process.exit(1);
  }

  logger.info({ port, host }, "Server listening");
  const stoppedJobs = await stopRunningRuntimeJobs();
  if (stoppedJobs.length > 0) {
    logger.warn({ count: stoppedJobs.length, runtimeJobIds: stoppedJobs }, "Stopped orphaned active scrape jobs on startup");
  }
  startMonthlyScrapingScheduler();
  for (let i = 0; i < scrapeWorkerCount; i++) {
    startScrapeWorker();
  }
});
