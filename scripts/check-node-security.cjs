// Security baseline for the October 21, 2026 Node advisories.
const supported = (version) => {
  const parts = version.replace(/^v/, "").split(".").map(Number);
  return parts.length === 3 && parts.every(Number.isInteger) &&
    parts[0] === 24 && (parts[1] > 22 || (parts[1] === 22 && parts[2] >= 1));
};
module.exports = { supported };
if (require.main === module) {
  if (!supported(process.versions.node)) {
    console.error("Use patched Node 24 LTS >=24.22.1 before building or running browser drivers.");
    process.exit(1);
  }
  console.log(`Node security baseline passed: ${process.version}`);
}
