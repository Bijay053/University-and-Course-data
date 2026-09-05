"""Snapshot lifecycle setup must not destroy unrelated bucket rules."""
from app.services import snapshot_store


class _NoLifecycle(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "NoSuchLifecycleConfiguration"}}


class _Client:
    def __init__(self, rules=None, *, absent=False, transition_default=None):
        self.rules = rules or []
        self.absent = absent
        self.transition_default = transition_default
        self.put = None

    def get_bucket_lifecycle_configuration(self, **_kwargs):
        if self.absent:
            raise _NoLifecycle()
        response = {"Rules": self.rules}
        if self.transition_default is not None:
            response["TransitionDefaultMinimumObjectSize"] = self.transition_default
        return response

    def put_bucket_lifecycle_configuration(self, **kwargs):
        self.put = kwargs


def test_setup_lifecycle_preserves_unrelated_rules_and_replaces_owned(monkeypatch):
    unrelated = {
        "ID": "abort-incomplete-uploads",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
    }
    stale_owned = {
        "ID": "expire-html-snapshots-90d",
        "Status": "Disabled",
        "Filter": {"Prefix": "old"},
        "Expiration": {"Days": 999},
    }
    client = _Client(
        [unrelated, stale_owned],
        transition_default="varies_by_storage_class",
    )
    monkeypatch.setattr(snapshot_store, "is_enabled", lambda: True)
    monkeypatch.setattr(snapshot_store, "_bucket", lambda: "bucket")
    monkeypatch.setattr(snapshot_store, "_make_client", lambda: client)

    assert snapshot_store.setup_lifecycle_rules() is True

    rules = client.put["LifecycleConfiguration"]["Rules"]
    assert unrelated in rules
    html_rules = [rule for rule in rules if rule["ID"] == "expire-html-snapshots-90d"]
    assert len(html_rules) == 1
    assert html_rules[0]["Status"] == "Enabled"
    assert html_rules[0]["Expiration"] == {"Days": 90}
    assert client.put["TransitionDefaultMinimumObjectSize"] == (
        "varies_by_storage_class"
    )
    assert "TransitionDefaultMinimumObjectSize" not in client.put[
        "LifecycleConfiguration"
    ]


def test_setup_lifecycle_handles_bucket_without_existing_configuration(monkeypatch):
    client = _Client(absent=True)
    monkeypatch.setattr(snapshot_store, "is_enabled", lambda: True)
    monkeypatch.setattr(snapshot_store, "_bucket", lambda: "bucket")
    monkeypatch.setattr(snapshot_store, "_make_client", lambda: client)

    assert snapshot_store.setup_lifecycle_rules() is True
    assert len(client.put["LifecycleConfiguration"]["Rules"]) == 6