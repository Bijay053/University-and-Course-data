import os from "node:os";
import { logger } from "../lib/logger.js";
import { executeRuntimeScrapeJob } from "../routes/scrape.js";
import {
  appendRuntimeJobLogs,
  claimNextRuntimeJob,
} from "../services/scrape-runtime-jobs.js";

const workerId = `${os.hostname()}-${process.pid}`;
const pollDelayMs = Number(process.env.SCRAPE_WORKER_POLL_MS ?? "1000");
const parentPid = Number(process.env.SCRAPE_WORKER_PARENT_PID ?? "0");

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parentProcessAlive(pid: number) {
  if (!pid || Number.isNaN(pid)) return true;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function runLoop() {
  logger.info({ workerId, pid: process.pid }, "Scrape worker started");

  while (true) {
    try {
      if (!parentProcessAlive(parentPid)) {
        logger.warn({ workerId, pid: process.pid, parentPid }, "Parent API process is gone; stopping scrape worker");
        process.exit(0);
      }
      const claimed = await claimNextRuntimeJob(workerId, process.pid);
      if (!claimed) {
        await sleep(pollDelayMs);
        continue;
      }

      await appendRuntimeJobLogs(claimed.runtimeJobId, [{
        event: "status",
        message: "Worker claimed queued scrape job",
        phase: "queue",
      }]);

      await executeRuntimeScrapeJob(claimed.runtimeJobId);
    } catch (error) {
      logger.error({ err: error, workerId }, "Scrape worker loop error");
      await sleep(Math.max(pollDelayMs, 2000));
    }
  }
}

void runLoop();
