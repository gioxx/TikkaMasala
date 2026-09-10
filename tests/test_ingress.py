from app.ingress import (
    IngressDiff,
    IngressRule,
    config_settings_delta,
    diff_ingress,
    extract_ingress,
    fragile_rules,
    is_fragile_service,
    origin_request_delta,
    service_host,
)

SNAPSHOT = {
    "configuration": {
        "config": {
            "ingress": [
                {
                    "hostname": "a.example.com",
                    "service": "http://localhost:3000",
                    "originRequest": {"noTLSVerify": True},
                },
                {
                    "hostname": "b.example.com",
                    "service": "http://172.17.0.1:8080",
                    "path": "/api",
                    "originRequest": {},
                },
                {"hostname": "c.example.com", "service": "ssh://127.0.0.1:22"},
                {"service": "http_status:404"},
            ]
        }
    }
}


def test_service_host_parses_url_services():
    assert service_host("http://localhost:3000") == "localhost"
    assert service_host("ssh://127.0.0.1:22") == "127.0.0.1"
    assert service_host("https://App.Example.COM") == "app.example.com"


def test_service_host_none_for_non_network_services():
    assert service_host("http_status:404") is None
    assert service_host("hello_world") is None
    assert service_host("unix:/var/run/app.sock") is None


def test_is_fragile_service():
    assert is_fragile_service("http://localhost:3000") is True
    assert is_fragile_service("tcp://127.0.0.5:9000") is True
    assert is_fragile_service("http://[::1]:80") is True
    assert is_fragile_service("http://[0:0:0:0:0:0:0:1]:8080") is True
    assert is_fragile_service("http://172.17.0.1:8080") is False
    assert is_fragile_service("http://127.example.com:80") is False
    assert is_fragile_service("http_status:404") is False


def test_extract_ingress_shapes_rules():
    rules = extract_ingress(SNAPSHOT)
    assert [r.hostname for r in rules] == [
        "a.example.com",
        "b.example.com",
        "c.example.com",
        None,
    ]
    assert rules[0].is_fragile is True
    assert rules[1].is_fragile is False
    assert rules[3].is_catch_all is True
    assert rules[1].path == "/api"


def test_extract_ingress_accepts_bare_config_and_bad_input():
    assert extract_ingress({"ingress": []}) == []
    assert extract_ingress({}) == []
    assert extract_ingress({"configuration": {"config": {}}}) == []


def test_fragile_rules_subset():
    frag = fragile_rules(extract_ingress(SNAPSHOT))
    assert [r.hostname for r in frag] == ["a.example.com", "c.example.com"]


def _rules(*triples):
    return [
        IngressRule(hostname=h, service=s, path=p, origin_request=o or {})
        for (h, s, p, o) in triples
    ]


def test_diff_ingress_classifies_rules():
    current = _rules(
        ("a.example.com", "http://10.0.0.1:80", None, None),
        ("b.example.com", "http://10.0.0.2:80", None, None),
        (None, "http_status:404", None, None),
    )
    incoming = _rules(
        ("a.example.com", "http://localhost:80", None, None),  # changed service
        ("c.example.com", "http://10.0.0.9:80", None, None),  # added
        (None, "http_status:404", None, None),  # unchanged
    )
    diff = diff_ingress(current, incoming)
    assert [r.hostname for r in diff.added] == ["c.example.com"]
    assert [r.hostname for r in diff.removed] == ["b.example.com"]
    assert [(c.hostname, c.service, n.service) for c, n in diff.changed] == [
        ("a.example.com", "http://10.0.0.1:80", "http://localhost:80"),
    ]
    assert [r.hostname for r in diff.unchanged] == [None]
    assert diff.has_changes is True


def test_diff_ingress_no_changes():
    same = _rules(("a.example.com", "http://10.0.0.1:80", None, None))
    diff = diff_ingress(same, list(same))
    assert isinstance(diff, IngressDiff)
    assert diff.has_changes is False
    assert diff.reordered is False


def test_diff_ingress_detects_pure_reorder():
    current = _rules(
        ("a.example.com", "http://10.0.0.1:80", None, None),
        ("b.example.com", "http://10.0.0.2:80", None, None),
        (None, "http_status:404", None, None),
    )
    incoming = _rules(
        ("b.example.com", "http://10.0.0.2:80", None, None),
        ("a.example.com", "http://10.0.0.1:80", None, None),
        (None, "http_status:404", None, None),
    )
    diff = diff_ingress(current, incoming)
    assert diff.added == [] and diff.removed == [] and diff.changed == []
    assert diff.reordered is True
    assert diff.has_changes is True


def test_diff_ingress_reorder_ignores_added_removed_positions():
    current = _rules(
        ("a.example.com", "http://10.0.0.1:80", None, None),
        ("b.example.com", "http://10.0.0.2:80", None, None),
    )
    incoming = _rules(
        ("c.example.com", "http://10.0.0.3:80", None, None),
        ("a.example.com", "http://10.0.0.1:80", None, None),
        ("b.example.com", "http://10.0.0.2:80", None, None),
    )
    diff = diff_ingress(current, incoming)
    assert [r.hostname for r in diff.added] == ["c.example.com"]
    assert diff.reordered is False


def test_diff_ingress_keeps_duplicate_hostpath_rules():
    current = _rules(
        ("a.example.com", "http://10.0.0.1:80", None, None),
        ("a.example.com", "http://10.0.0.2:80", None, None),
        (None, "http_status:404", None, None),
    )
    incoming = _rules(
        ("a.example.com", "http://localhost:80", None, None),  # first dup changed
        ("a.example.com", "http://10.0.0.2:80", None, None),  # second dup unchanged
        (None, "http_status:404", None, None),
    )
    diff = diff_ingress(current, incoming)
    assert [(c.service, n.service) for c, n in diff.changed] == [
        ("http://10.0.0.1:80", "http://localhost:80"),
    ]
    assert diff.added == [] and diff.removed == []
    assert diff.has_changes is True


def test_diff_ingress_ordered_marks_insertion_position():
    current = _rules(
        ("a.com", "http://x:1", "/special", None),
        ("a.com", "http://y:2", "/other", None),
    )
    incoming = _rules(
        ("a.com", "http://z:3", None, None),  # new host-wide rule, inserted first
        ("a.com", "http://x:1", "/special", None),
        ("a.com", "http://y:2", "/other", None),
    )
    diff = diff_ingress(current, incoming)
    assert [(r.position, r.status) for r in diff.ordered] == [
        (1, "add"),
        (2, "same"),
        (3, "same"),
    ]
    assert diff.ordered[0].incoming.service == "http://z:3"
    assert diff.removed_positions == []
    assert diff.has_changes is True


def test_diff_ingress_ordered_positions_and_removed():
    current = _rules(
        ("a", "http://x:1", None, None),
        ("b", "http://y:1", None, None),
    )
    incoming = _rules(("b", "http://y:1", None, None))
    diff = diff_ingress(current, incoming)
    assert [(r.position, r.status) for r in diff.ordered] == [(1, "same")]
    assert [(pos, rule.hostname) for pos, rule in diff.removed_positions] == [(1, "a")]


def test_origin_request_delta():
    before = {"noTLSVerify": False, "connectTimeout": 30, "httpHostHeader": "old"}
    after = {"noTLSVerify": True, "connectTimeout": 30, "originServerName": "x"}
    assert origin_request_delta(before, after) == [
        ("httpHostHeader", "old", None),
        ("noTLSVerify", False, True),
        ("originServerName", None, "x"),
    ]
    assert origin_request_delta({}, {}) == []


def test_config_settings_delta_ignores_ingress_and_reports_rest():
    before = {
        "ingress": [{"hostname": "a", "service": "http://x:1"}],
        "warp-routing": {"enabled": False},
        "originRequest": {"connectTimeout": 30},
    }
    after = {
        "ingress": [{"hostname": "b", "service": "http://y:2"}],  # ignored
        "warp-routing": {"enabled": True},  # changed
        "originRequest": {"connectTimeout": 30},  # unchanged
        "no-happy-eyeballs": True,  # added
    }
    assert config_settings_delta(before, after) == [
        ("no-happy-eyeballs", None, True),
        ("warp-routing", {"enabled": False}, {"enabled": True}),
    ]
    assert config_settings_delta({"ingress": []}, {"ingress": [1, 2]}) == []
