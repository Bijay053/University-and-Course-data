import app from "./app";
import { logger } from "./lib/logger";

const rawPort = process.env["API_PORT"] ?? process.env["PORT"] ?? "8080";
const host = process.env["API_HOST"] ?? "0.0.0.0";

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid API_PORT/PORT value: "${rawPort}"`);
}

app.listen(port, host, (err) => {
  if (err) {
    logger.error({ err }, "Error listening on port");
    process.exit(1);
  }

  logger.info({ port, host }, "Server listening");
});
