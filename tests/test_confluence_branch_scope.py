import datetime as dt

from confluence_analysis import PageInfo, scope_pages_from_starting_depth, compute_hierarchy_metrics


def _page(id: str, title: str, depth: int | None, parent: str | None, children: list[str] | None = None):
    p = PageInfo(
        id=id,
        title=title,
        url=f"http://example/wiki/pages/{id}/{title}",
        last_updated=dt.datetime.now(dt.timezone.utc),
        author="tester",
        labels=["test"],
    )
    if depth is None:
        p.depth = None
        p.parent_id = parent
        p.ancestors = []
    elif depth == 0:
        p.depth = 0
        p.parent_id = None
        p.ancestors = []
    else:
        p.depth = depth
        p.parent_id = parent
        p.ancestors = ["root"][:depth]
    p.children = list(children or [])
    return p


def build_tree_with_unknown():
    # r (0)
    # ├─ c1 (1)
    # │   └─ g1 (2)
    # │       └─ gg (3)
    # └─ c2 (1)
    #     └─ u (unknown depth)
    r = _page("r", "root", 0, None, ["c1", "c2"])
    c1 = _page("c1", "child1", 1, "r", ["g1"])
    c2 = _page("c2", "child2", 1, "r", ["u"])  # unknown child under c2
    g1 = _page("g1", "grand1", 2, "c1", ["gg"]) 
    gg = _page("gg", "great", 3, "g1", [])
    u = _page("u", "unknown", None, "c2", [])
    return [r, c1, c2, g1, gg, u]


def test_scope_start_1_depth_1_includes_children_and_unknown():
    pages = build_tree_with_unknown()
    scoped, groups = scope_pages_from_starting_depth(pages, starting_depth=1, relative_depth=1)
    ids = {p.id for p in scoped}
    assert ids == {"c1", "c2", "g1", "u"}
    # Check children trimming: c1 should only keep g1; c2 keeps u
    c1 = next(p for p in scoped if p.id == "c1")
    c2 = next(p for p in scoped if p.id == "c2")
    assert set(c1.children) == {"g1"}
    assert set(c2.children) == {"u"}
    # Metrics should see two leaves: g1 and u
    m = compute_hierarchy_metrics(scoped)
    assert m["leaf_pages"] == 2
    # Groups mapping
    assert set(groups.get("c1", [])) == {"c1", "g1"}
    assert set(groups.get("c2", [])) == {"c2", "u"}


def test_scope_start_2_depth_0_is_only_depth2_nodes():
    pages = build_tree_with_unknown()
    scoped, groups = scope_pages_from_starting_depth(pages, starting_depth=2, relative_depth=0)
    ids = {p.id for p in scoped}
    assert ids == {"g1"}
    # g1 becomes a leaf after trimming (gg excluded)
    m = compute_hierarchy_metrics(scoped)
    assert m["leaf_pages"] == 1
