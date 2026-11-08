from app.archive import ArchiveQuery, archive_url, build_archive_page, parse_archive_query
from app.backups import get_backup

GOOD = "0123456789abcdef0123456789abcdef"
OTHER = "acc-other"
T1 = "11111111-1111-4111-8111-111111111111"
T2 = "22222222-2222-4222-8222-222222222222"


def _seed(make_backup):
    home = make_backup(T1, "Home", account_id=GOOD, created_at="2026-01-01T00:00:00+00:00")
    renamed = make_backup(
        T1, "Home renamed", account_id=GOOD.upper(), origin="automatic", created_at="2026-01-02T00:00:00+00:00"
    )
    lab = make_backup(T2, "Lab", account_id=GOOD, notes="before upgrade")
    old = make_backup("t-old", "Old", account_id=OTHER)
    return home, renamed, lab, old


def test_parse_archive_query_defaults():
    assert parse_archive_query({}) == ArchiveQuery()
    bad = {"scope": "x", "origin": "evil", "page": "-2", "page_size": "7", "tunnel": " "}
    assert parse_archive_query(bad) == ArchiveQuery()


def test_parse_archive_query_values():
    q = parse_archive_query(
        {"scope": "other", "tunnel": f" {T1} ", "origin": "automatic", "page": "3", "page_size": "10"}
    )
    assert q == ArchiveQuery(scope="other", tunnel_id=T1, origin="automatic", page=3, page_size=10)
    assert parse_archive_query({"tunnel": "x" * 129}).tunnel_id is None
    assert parse_archive_query({"page": "abc"}).page == 1


def test_archive_url_order_and_defaults():
    assert archive_url(ArchiveQuery()) == "/backups"
    q = ArchiveQuery(tunnel_id=T1, origin="automatic", page_size=10)
    assert archive_url(q, page=2) == f"/backups?tunnel={T1}&origin=automatic&page=2&page_size=10"
    assert archive_url(ArchiveQuery(), scope="other") == "/backups?scope=other"


def test_profile_scope_case_insensitive(configured_default, make_backup):
    home, renamed, lab, _old = _seed(make_backup)
    page = build_archive_page(1, ArchiveQuery())
    assert [b.id for b in page.backups] == [lab, renamed, home]
    assert page.profile_count == 3
    assert page.other_count == 1


def test_other_scope(configured_default, make_backup):
    _seed(make_backup)
    page = build_archive_page(1, ArchiveQuery(scope="other"))
    assert [b.tunnel_name for b in page.backups] == ["Old"]


def test_other_scope_without_profile_accounts(make_backup):
    _seed(make_backup)
    other = build_archive_page(1, ArchiveQuery(scope="other"))
    assert len(other.backups) == 4
    mine = build_archive_page(1, ArchiveQuery())
    assert mine.backups == []
    assert mine.account_id is None


def test_filters_tunnel_and_origin(configured_default, make_backup):
    _seed(make_backup)
    assert len(build_archive_page(1, ArchiveQuery(tunnel_id=T1)).backups) == 2
    auto = build_archive_page(1, ArchiveQuery(origin="automatic"))
    assert [b.tunnel_name for b in auto.backups] == ["Home renamed"]
    both = build_archive_page(1, ArchiveQuery(tunnel_id=T1, origin="automatic"))
    assert len(both.backups) == 1
    assert both.filtered
    assert both.profile_count == 3 and both.other_count == 1


def test_tunnel_options_latest_name(configured_default, make_backup):
    _seed(make_backup)
    assert build_archive_page(1, ArchiveQuery()).tunnel_options == [(T1, "Home renamed"), (T2, "Lab")]


def test_page_beyond_last_clamps(configured_default, make_backup):
    _seed(make_backup)
    page = build_archive_page(1, ArchiveQuery(page=999, page_size=5))
    assert page.pagination["current_page"] == 1
    assert page.backups
    assert page.pagination["start_index"] == 1


def test_make_backup_notes_and_origin(configured_default, make_backup):
    _home, renamed, lab, _old = _seed(make_backup)
    assert get_backup(lab).notes == "before upgrade"
    assert get_backup(renamed).origin == "automatic"
