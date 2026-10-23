---
name: Hidden Node runtimes
description: Python browser drivers and Nix pnpm can use different Node versions from the shell.
---

Inventory each actual interpreter, not just `node --version`: Python browser
automation bundles Node, and Nix pnpm can hard-code a different Node in its
shebang. Nginx serving static React does not eliminate the driver's runtime risk.

**Why:** The shell was patched while pnpm and production Playwright still used
older vulnerable Node releases.

**How to apply:** Check package-manager interpreter, build subprocess, and
browser-driver executable independently. A supported external Node override
allows security patching without changing browser-automation versions. Check
certifi independently from OS trust; they can trust different roots.
