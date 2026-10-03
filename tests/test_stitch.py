import copy

import pytest
from fastapi.testclient import TestClient

from app.engine import ValidationError, review, stitch
from app.main import app
from app.models import ReviewRequest, StitchRequest
from pydantic import ValidationError as PydanticValidationError


def call_stitch(payload):
    return stitch(StitchRequest.model_validate(payload))


def call_review(payload):
    return review(ReviewRequest.model_validate(payload))


def by_id(nodes):
    return {n["id"]: n for n in nodes}


# Upstream: source -> filter -> projection export; one extra unreachable
# source must not travel into the composite graph.
UPSTREAM = {
    "nodes": [
        {
            "id": "u_src",
            "type": "source",
            "columns": [
                {"name": "uid", "sensitive": False},
                {"name": "email", "sensitive": True},
                {"name": "country", "sensitive": False},
            ],
        },
        {
            "id": "u_filter",
            "type": "filter",
            "input": "u_src",
            "references": ["country"],
        },
        {
            "id": "u_export",
            "type": "projection",
            "input": "u_filter",
            "columns": [
                {"name": "id", "copy": "uid"},
                {"name": "mail", "copy": "email"},
                {"name": "region", "copy": "country"},
            ],
        },
        {
            "id": "u_orphan",
            "type": "source",
            "columns": [{"name": "untouched"}],
        },
    ],
    "export": "u_export",
}

# Downstream: source 'ext' -> filter -> projection export, plus a second
# branch off the same source that the export does not use.
DOWNSTREAM = {
    "nodes": [
        {
            "id": "ext",
            "type": "source",
            "columns": [
                {"name": "a", "sensitive": True},
                {"name": "b", "sensitive": True},
            ],
        },
        {
            "id": "d_filter",
            "type": "filter",
            "input": "ext",
            "references": ["a"],
        },
        {
            "id": "d_export",
            "type": "projection",
            "input": "d_filter",
            "columns": [
                {"name": "x", "copy": "a"},
                {"name": "y", "copy": "b"},
            ],
        },
        {
            "id": "d_side_branch",
            "type": "projection",
            "input": "ext",
            "columns": [{"name": "only_a", "copy": "a"}],
        },
    ],
    "export": "d_export",
}

MAPPING = [
    {"from": "a", "to": "id"},
    {"from": "b", "to": "mail"},
]


def stitch_payload(**overrides):
    payload = {
        "upstream": copy.deepcopy(UPSTREAM),
        "downstream": copy.deepcopy(DOWNSTREAM),
        "source": "ext",
        "mapping": copy.deepcopy(MAPPING),
    }
    payload.update(overrides)
    return payload


# ----------------------------- happy paths --------------------------------


def test_basic_stitch_replaces_source_in_place_and_appends_ancestors():
    res = call_stitch(stitch_payload())
    nodes = res["nodes"]
    ids = [n["id"] for n in nodes]

    # The source is replaced in place at its original list position by a
    # one-layer projection with the same id and source-column order/names.
    assert nodes[0]["id"] == "ext"
    assert nodes[0] == {
        "id": "ext",
        "type": "projection",
        "input": "u_export",
        "columns": [
            {"name": "a", "copy": "id"},
            {"name": "b", "copy": "mail"},
        ],
    }

    # Downstream's other nodes keep their ids, order and contents untouched.
    assert nodes[1] == DOWNSTREAM["nodes"][1]
    assert nodes[2] == DOWNSTREAM["nodes"][2]
    # The non-export side branch survives: every branch reference stays valid.
    assert nodes[3] == DOWNSTREAM["nodes"][3]

    # Only the upstream export and its ancestors are appended, in upstream
    # request order; the unreachable orphan source stays behind.
    assert ids == [
        "ext",
        "d_filter",
        "d_export",
        "d_side_branch",
        "u_src",
        "u_filter",
        "u_export",
    ]
    assert all(n["id"] != "u_orphan" for n in nodes)

    assert res["export"] == "d_export"
    assert res["upstream_export"] == "u_export"
    assert res["imported_nodes"] == [
        {"old_id": "u_src", "new_id": "u_src"},
        {"old_id": "u_filter", "new_id": "u_filter"},
        {"old_id": "u_export", "new_id": "u_export"},
    ]


def test_composite_review_matches_a_direct_review_and_drops_source_flags():
    res = call_stitch(stitch_payload())

    # The embedded review is exactly what /api/review says for the composite.
    direct = call_review({"nodes": res["nodes"], "export": res["export"]})
    assert direct == res["review"]

    cols = {c["name"]: c for c in res["review"]["columns"]}
    # Content/row influence at the replaced position comes from the upstream;
    # the replaced source's own sensitive markings are not carried over:
    # 'a' maps to the clean upstream 'id' column and is now safe, while
    # 'b' maps to 'mail' carrying u_src.email.
    assert cols["x"]["blocked"] is False
    assert cols["x"]["content_reasons"] == []
    assert cols["x"]["row_reasons"] == []
    assert cols["y"]["content_reasons"] == [
        {"node_id": "u_src", "column": "email"}
    ]
    assert cols["y"]["blocked"] is True


def test_several_source_columns_may_reference_the_same_upstream_column():
    payload = stitch_payload(
        mapping=[
            {"from": "a", "to": "id"},
            {"from": "b", "to": "id"},
        ]
    )
    res = call_stitch(payload)
    replacement = res["nodes"][0]
    assert replacement["columns"] == [
        {"name": "a", "copy": "id"},
        {"name": "b", "copy": "id"},
    ]
    # Both outputs are clean because 'id' carries no sensitive content.
    assert all(
        c["blocked"] is False for c in res["review"]["columns"]
    )


def test_row_influence_at_replaced_position_is_taken_from_upstream():
    upstream = {
        "nodes": [
            {
                "id": "u",
                "type": "source",
                "columns": [
                    {"name": "id_card", "sensitive": True},
                    {"name": "score", "sensitive": False},
                ],
            },
            {
                "id": "uf",
                "type": "filter",
                "input": "u",
                "references": ["id_card"],
            },
        ],
        "export": "uf",
    }
    downstream = {
        "nodes": [
            {"id": "ext", "type": "source",
             "columns": [{"name": "a", "sensitive": False}]},
            {
                "id": "e",
                "type": "projection",
                "input": "ext",
                "columns": [{"name": "a", "copy": "a"}],
            },
        ],
        "export": "e",
    }
    res = call_stitch(
        {
            "upstream": upstream,
            "downstream": downstream,
            "source": "ext",
            "mapping": [{"from": "a", "to": "score"}],
        }
    )
    col = res["review"]["columns"][0]
    # Even a constant-free copied clean column is row-tainted by the upstream
    # filter on a sensitive column; reasons use the synthetic graph ids.
    assert col["row_reasons"] == [{"node_id": "u", "column": "id_card"}]
    assert col["blocked"] is True


def test_downstream_export_may_be_the_replaced_source_itself():
    downstream = {
        "nodes": [
            {"id": "ext", "type": "source",
             "columns": [{"name": "a"}, {"name": "b"}]}
        ],
        "export": "ext",
    }
    res = call_stitch(stitch_payload(downstream=downstream))
    assert res["export"] == "ext"
    node = by_id(res["nodes"])["ext"]
    assert node["type"] == "projection"
    assert node["input"] == "u_export"
    assert [c["name"] for c in node["columns"]] == ["a", "b"]
    names = [c["name"] for c in res["review"]["columns"]]
    assert names == ["a", "b"]


def test_upstream_node_order_is_preserved_when_input_is_unordered():
    upstream = {
        "nodes": [
            {"id": "u_export", "type": "projection", "input": "u_src",
             "columns": [{"name": "id", "copy": "uid"}]},
            {"id": "u_src", "type": "source",
             "columns": [{"name": "uid"}]},
        ],
        "export": "u_export",
    }
    downstream = {
        "nodes": [
            {"id": "ext", "type": "source",
             "columns": [{"name": "a"}]},
            {"id": "e", "type": "projection", "input": "ext",
             "columns": [{"name": "a", "copy": "a"}]},
        ],
        "export": "e",
    }
    res = call_stitch(
        {
            "upstream": upstream,
            "downstream": downstream,
            "source": "ext",
            "mapping": [{"from": "a", "to": "id"}],
        }
    )
    # Imported nodes follow the upstream request order, not topo order.
    imported = [n["old_id"] for n in res["imported_nodes"]]
    assert imported == ["u_export", "u_src"]
    tail_ids = [n["id"] for n in res["nodes"][-2:]]
    assert tail_ids == ["u_export", "u_src"]
    assert res["nodes"][-2]["input"] == "u_src"


def test_join_inputs_inside_imported_closure_are_rewritten():
    upstream = {
        "nodes": [
            {"id": "orders", "type": "source",
             "columns": [{"name": "uid"}, {"name": "amount"}]},
            {"id": "users", "type": "source",
             "columns": [{"name": "uid"}, {"name": "name"}]},
            {
                "id": "j",
                "type": "join",
                "left_input": "orders",
                "right_input": "users",
                "left_key": "uid",
                "right_key": "uid",
                "outputs": [
                    {"alias": "amt", "side": "left", "column": "amount"},
                    {"alias": "uname", "side": "right", "column": "name"},
                ],
            },
        ],
        "export": "j",
    }
    downstream = {
        "nodes": [
            # 'orders' clashes with an upstream source id; 'j' clashes with the
            # upstream export id.
            {"id": "orders", "type": "source",
             "columns": [{"name": "a"}, {"name": "b"}]},
            {"id": "j", "type": "source", "columns": [{"name": "occupy"}]},
            {"id": "e", "type": "projection", "input": "orders", "columns": [
                {"name": "x", "copy": "a"}, {"name": "y", "copy": "b"}]},
        ],
        "export": "e",
    }
    res = call_stitch(
        {
            "upstream": upstream,
            "downstream": downstream,
            "source": "orders",
            "mapping": [
                {"from": "a", "to": "amt"},
                {"from": "b", "to": "uname"},
            ],
        }
    )
    nodes = by_id(res["nodes"])
    assert set(nodes) == {
        "orders",
        "orders__up",
        "users",
        "j",
        "j__up",
        "e",
    }
    join = nodes["j__up"]
    assert join["left_input"] == "orders__up"
    assert join["right_input"] == "users"
    # The replacement projection points at the renamed upstream export.
    assert nodes["orders"]["input"] == "j__up"
    assert res["upstream_export"] == "j__up"
    assert {
        (m["old_id"], m["new_id"]) for m in res["imported_nodes"]
    } == {
        ("orders", "orders__up"),
        ("users", "users"),
        ("j", "j__up"),
    }
    # The composite must itself be a valid reviewable graph.
    assert call_review({"nodes": res["nodes"], "export": "e"}) == res["review"]


def test_id_rename_cascades_when_suffixed_id_is_also_taken():
    # Downstream already occupies 'u__up', so the clashing upstream node must
    # fall through to 'u__up_2'.
    upstream = {
        "nodes": [
            {"id": "u", "type": "source", "columns": [{"name": "a"}]},
            {"id": "ue", "type": "projection", "input": "u",
             "columns": [{"name": "a", "copy": "a"}]},
        ],
        "export": "ue",
    }
    downstream = {
        "nodes": [
            {"id": "u", "type": "source", "columns": [{"name": "x"}]},
            {"id": "u__up", "type": "source",
             "columns": [{"name": "keep"}]},
            {"id": "e", "type": "projection", "input": "u",
             "columns": [{"name": "x", "copy": "x"}]},
        ],
        "export": "e",
    }
    res = call_stitch(
        {
            "upstream": upstream,
            "downstream": downstream,
            "source": "u",
            "mapping": [{"from": "x", "to": "a"}],
        }
    )
    nodes = by_id(res["nodes"])
    assert "u__up_2" in nodes
    assert nodes["ue"]["input"] == "u__up_2"
    # The unrelated downstream source keeps its seat and its id.
    assert nodes["u__up"]["columns"] == [{"name": "keep"}]


def test_neither_input_graph_is_mutated():
    payload = stitch_payload()
    before = copy.deepcopy(payload)
    call_stitch(payload)
    assert payload["upstream"] == before["upstream"]
    assert payload["downstream"] == before["downstream"]
    assert payload["mapping"] == before["mapping"]


def test_upstream_export_id_equalling_source_id_is_renamed_and_rewired():
    upstream = {
        "nodes": [
            {"id": "raw", "type": "source",
             "columns": [{"name": "k", "sensitive": True}]},
            {"id": "ext", "type": "projection", "input": "raw",
             "columns": [{"name": "k", "copy": "k"}]},
        ],
        "export": "ext",
    }
    downstream = {
        "nodes": [
            {"id": "ext", "type": "source",
             "columns": [{"name": "col"}]},
            {"id": "f", "type": "filter", "input": "ext",
             "references": ["col"]},
            {"id": "out", "type": "join",
             "left_input": "ext", "right_input": "f",
             "left_key": "col", "right_key": "col",
             "outputs": [
                 {"alias": "a", "side": "left", "column": "col"},
                 {"alias": "b", "side": "right", "column": "col"},
             ]},
        ],
        "export": "out",
    }
    res = call_stitch(
        {
            "upstream": upstream,
            "downstream": downstream,
            "source": "ext",
            "mapping": [{"from": "col", "to": "k"}],
        }
    )
    nodes = by_id(res["nodes"])
    assert set(nodes) == {"ext", "ext__up", "raw", "f", "out"}
    # The replacement projection feeds from the renamed upstream export; the
    # upstream export itself keeps pointing inside its own closure.
    assert nodes["ext"]["input"] == "ext__up"
    assert nodes["ext__up"]["input"] == "raw"
    # Every downstream branch reference still names the replaced source id.
    assert nodes["f"]["input"] == "ext"
    assert nodes["out"]["left_input"] == "ext"
    assert nodes["out"]["right_input"] == "f"
    assert res["upstream_export"] == "ext__up"
    assert call_review(
        {"nodes": res["nodes"], "export": "out"}
    ) == res["review"]


def test_column_definitions_of_imported_nodes_are_copied_verbatim():
    res = call_stitch(stitch_payload())
    nodes = by_id(res["nodes"])
    # Sensitive flags and every column come over with the upstream definition.
    assert nodes["u_src"]["columns"] == UPSTREAM["nodes"][0]["columns"]
    assert nodes["u_filter"]["references"] == ["country"]
    assert nodes["u_export"]["columns"] == UPSTREAM["nodes"][2]["columns"]


# ----------------------------- rejections ---------------------------------


def _expect_validation_error(payload, code):
    with pytest.raises(ValidationError) as exc:
        call_stitch(payload)
    assert exc.value.code == code
    return exc.value


def test_target_missing_or_not_a_source_is_rejected():
    payload = stitch_payload(source="ghost")
    _expect_validation_error(payload, "STITCH_TARGET_NOT_SOURCE")

    payload = stitch_payload(
        source="d_filter",
        mapping=[{"from": "a", "to": "id"}],
    )
    exc = _expect_validation_error(payload, "STITCH_TARGET_NOT_SOURCE")
    assert exc.node_id == "d_filter"


def test_extra_mapping_entries_are_rejected_without_partial_graph():
    payload = stitch_payload(
        mapping=[
            {"from": "a", "to": "id"},
            {"from": "b", "to": "mail"},
            {"from": "ghost", "to": "id"},
        ]
    )
    exc = _expect_validation_error(payload, "STITCH_MAPPING_EXTRA")
    assert exc.extra["columns"] == ["ghost"]


def test_missing_mapping_entries_are_rejected_in_source_column_order():
    payload = stitch_payload(
        mapping=[{"from": "b", "to": "mail"}]
    )
    exc = _expect_validation_error(payload, "STITCH_MAPPING_MISSING")
    assert exc.extra["columns"] == ["a"]


def test_unknown_upstream_export_column_is_rejected():
    payload = stitch_payload(
        mapping=[
            {"from": "a", "to": "id"},
            {"from": "b", "to": "ghost"},
        ]
    )
    exc = _expect_validation_error(
        payload, "STITCH_UPSTREAM_COLUMN_NOT_FOUND"
    )
    assert exc.node_id == "u_export"
    assert exc.extra["columns"] == ["ghost"]


def test_mapping_target_is_resolved_against_export_not_upstream_source():
    # 'uid' exists on the upstream source but is not an export output column.
    payload = stitch_payload(
        mapping=[
            {"from": "a", "to": "id"},
            {"from": "b", "to": "uid"},
        ]
    )
    _expect_validation_error(payload, "STITCH_UPSTREAM_COLUMN_NOT_FOUND")


def test_invalid_upstream_graph_rejects_the_whole_stitch():
    bad_upstream = {
        "nodes": [
            {"id": "s", "type": "source", "columns": [{"name": "a"}]},
            {"id": "p", "type": "projection", "input": "missing",
             "columns": [{"name": "a", "copy": "a"}]},
        ],
        "export": "p",
    }
    _expect_validation_error(
        stitch_payload(upstream=bad_upstream), "INVALID_REFERENCE"
    )


def test_invalid_downstream_graph_rejects_the_whole_stitch():
    bad_downstream = copy.deepcopy(DOWNSTREAM)
    bad_downstream["nodes"][1]["input"] = "missing"
    _expect_validation_error(
        stitch_payload(downstream=bad_downstream), "INVALID_REFERENCE"
    )


def test_errors_in_unused_upstream_branch_still_reject():
    bad = copy.deepcopy(UPSTREAM)
    # The orphan branch already exists; give it an invalid reference.
    bad["nodes"].append(
        {"id": "broken", "type": "projection", "input": "u_orphan",
         "columns": [{"name": "z", "copy": "nope"}]}
    )
    _expect_validation_error(
        stitch_payload(upstream=bad), "INVALID_REFERENCE"
    )


def test_composite_over_60_nodes_is_rejected_but_60_is_allowed():
    upstream_nodes = [
        {"id": "u", "type": "source", "columns": [{"name": "a"}]}
    ]
    for i in range(2):
        upstream_nodes.append(
            {"id": f"uf{i}", "type": "filter",
             "input": "u" if i == 0 else f"uf{i - 1}",
             "references": ["a"]}
        )
    upstream = {"nodes": upstream_nodes, "export": "uf1"}

    def downstream_with(n):
        nodes = [
            {"id": "ext", "type": "source", "columns": [{"name": "x"}]}
        ]
        for i in range(n - 1):
            nodes.append(
                {"id": f"d{i}", "type": "filter",
                 "input": "ext" if i == 0 else f"d{i - 1}",
                 "references": ["x"]}
            )
        return {"nodes": nodes, "export": nodes[-1]["id"]}

    payload = stitch_payload(
        upstream=upstream,
        downstream=downstream_with(58),
        mapping=[{"from": "x", "to": "a"}],
    )
    exc = _expect_validation_error(payload, "STITCH_NODE_LIMIT")
    assert exc.extra["node_count"] == 61

    ok = stitch(
        StitchRequest.model_validate(
            stitch_payload(
                upstream=upstream,
                downstream=downstream_with(57),
                mapping=[{"from": "x", "to": "a"}],
            )
        )
    )
    assert len(ok["nodes"]) == 60


# ----------------------------- schema (422) -------------------------------


def test_duplicate_from_in_mapping_is_a_schema_error():
    payload = stitch_payload(
        mapping=[
            {"from": "a", "to": "id"},
            {"from": "a", "to": "mail"},
        ]
    )
    with pytest.raises(PydanticValidationError):
        StitchRequest.model_validate(payload)


@pytest.mark.parametrize(
    "patch",
    [
        {"mapping": []},
        {"source": ""},
        {"mapping": [{"from": "", "to": "id"}]},
        {"mapping": [{"from": "a", "to": ""}]},
        {"mapping": [{"from": "a"}]},
        {"mapping": [{"to": "id"}]},
        {"unexpected": 1},
    ],
)
def test_schema_problems_are_rejected_by_the_model(patch):
    payload = stitch_payload()
    payload.update(patch)
    with pytest.raises(PydanticValidationError):
        StitchRequest.model_validate(payload)


def test_input_graphs_keep_their_own_60_node_schema_cap():
    too_many = {
        "nodes": [
            {"id": f"n{i}", "type": "source",
             "columns": [{"name": "a"}]}
            for i in range(61)
        ],
        "export": "n0",
    }
    with pytest.raises(PydanticValidationError):
        StitchRequest.model_validate(stitch_payload(upstream=too_many))


# ----------------------------- HTTP endpoint ------------------------------


client = TestClient(app)


def test_http_stitch_success_and_follow_up_review():
    resp = client.post("/api/stitch", json=stitch_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["export"] == "d_export"
    assert body["source"] == "ext"
    assert body["upstream_export"] == "u_export"

    follow = client.post(
        "/api/review",
        json={"nodes": body["nodes"], "export": body["export"]},
    )
    assert follow.status_code == 200
    assert follow.json() == body["review"]


def test_http_stitch_id_clash_rewrites_and_reviews_with_synthetic_ids():
    upstream = {
        "nodes": [
            {"id": "s", "type": "source",
             "columns": [{"name": "secret", "sensitive": True}]},
            {"id": "e", "type": "projection", "input": "s",
             "columns": [{"name": "secret", "copy": "secret"}]},
        ],
        "export": "e",
    }
    downstream = {
        "nodes": [
            {"id": "s", "type": "source", "columns": [{"name": "a"}]},
            {"id": "e", "type": "projection", "input": "s",
             "columns": [{"name": "a", "copy": "a"}]},
        ],
        "export": "e",
    }
    resp = client.post(
        "/api/stitch",
        json={
            "upstream": upstream,
            "downstream": downstream,
            "source": "s",
            "mapping": [{"from": "a", "to": "secret"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["upstream_export"] == "e__up"
    # The sensitive reason points at the renamed synthetic upstream source id.
    col = body["review"]["columns"][0]
    assert col["content_reasons"] == [
        {"node_id": "s__up", "column": "secret"}
    ]
    follow = client.post(
        "/api/review",
        json={"nodes": body["nodes"], "export": body["export"]},
    )
    assert follow.json() == body["review"]


def test_http_stitch_rejections_return_400_without_partial_graph():
    resp = client.post("/api/stitch", json=stitch_payload(source="ghost"))
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "STITCH_TARGET_NOT_SOURCE"
    assert "nodes" not in body and "review" not in body

    resp = client.post(
        "/api/stitch",
        json=stitch_payload(
            mapping=[{"from": "a", "to": "id"}, {"from": "b", "to": "nope"}]
        ),
    )
    assert resp.status_code == 400
    assert (
        resp.json()["error"]["code"]
        == "STITCH_UPSTREAM_COLUMN_NOT_FOUND"
    )


def test_http_stitch_schema_errors_return_422():
    payload = stitch_payload(mapping=[])
    assert client.post("/api/stitch", json=payload).status_code == 422

    payload = stitch_payload(
        mapping=[
            {"from": "a", "to": "id"},
            {"from": "a", "to": "mail"},
        ]
    )
    assert client.post("/api/stitch", json=payload).status_code == 422


def test_http_other_endpoints_unchanged():
    resp = client.post("/api/review", json=DOWNSTREAM)
    assert resp.status_code == 200
    assert "imported_nodes" not in resp.json()
