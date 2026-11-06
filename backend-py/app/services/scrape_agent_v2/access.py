"""A master kill switch never grants access without an explicit user allowlist."""
def enabled_for(settings, user_id):
    allowed={part.strip() for part in settings.scrape_agent_v2_user_ids.split(",") if part.strip()}
    return bool(settings.scrape_agent_v2_enabled and str(user_id) in allowed)
