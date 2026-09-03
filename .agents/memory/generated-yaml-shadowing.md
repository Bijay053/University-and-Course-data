---
name: Generated YAML shadowing
description: Why a deployed shared university recipe may still have no effect in production.
---

An existing ID-specific YAML file takes precedence over the shared slug YAML, including when the ID-specific file is only an auto-generated minimal stub.

**Why:** Database IDs differ across environments. A production-only generated stub can survive deployments as an untracked file and silently shadow a later hostname-guarded shared recipe.

**How to apply:** After deploying a shared university recipe, inspect the effective loaded configuration using the production university ID. If it is missing expected values, check for an untracked ID-specific generated stub and remove it only after confirming it is the minimal auto-generated file.