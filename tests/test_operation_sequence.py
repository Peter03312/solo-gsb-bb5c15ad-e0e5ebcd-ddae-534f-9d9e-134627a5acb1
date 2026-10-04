"""作业车按工序顺序进入危险区复核测试。

POST /api/v1/trips/{trip_id}/operation-sequence：
请求含带时区 UTC 半开查询时段与至少两道有序工序（每道危险区 ID + 正且有限
连续停留秒数；第二道起另给非负且有限最长交接秒数），可选区域修订号与轨迹版本。
各区占用沿用既有插值、区域生效时段与带洞边界规则，区域与点集取同一已提交读视图。
每道工序须在一次连续占用中取得足额且互不重叠的证据时段；后一道开始不早于
前一道结束、间隔不超过交接上限；允许同一区重复出现。返回能完成时的最早最终
结束时刻及逐道证据区间与实际版本；无可行顺序明确返回未完成、空证据。
"""
from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _points(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


def _upload(client, trip, points):
    r = client.post(f"/api/v1/trips/{trip}/points", json=_points(points))
    assert r.status_code == 200, r.text
    return r


def _zone(client, zone_id, exterior, holes=None, windows=None):
    r = client.post(
        "/api/v1/zones",
        json={
            "zone_id": zone_id,
            "exterior": exterior,
            "holes": holes or [],
            "windows": windows or DAY_WINDOWS,
        },
    )
    assert r.status_code == 201, r.text
    return r


def _ask(client, trip, steps, **overrides):
    body = {
        "since": "2026-09-30T00:00:00Z",
        "until": "2026-10-01T00:00:00Z",
        "steps": steps,
    }
    body.update(overrides)
    return client.post(f"/api/v1/trips/{trip}/operation-sequence", json=body)


# 两个相邻 50m 宽区域：Z1=[0,50]x[0,100]，Z2=[50,100]x[0,100]
Z1_EXT = [[0, 0], [50, 0], [50, 100], [0, 100]]
Z2_EXT = [[50, 0], [100, 0], [100, 100], [50, 100]]

# 10:00:00 自 x=-50 出发，10:03:20 到 x=150：200m/200s=1m/s
# 进 Z1 在 10:00:50，x=50 在 10:01:40，进 Z2；出 Z2 在 10:02:30
CROSS = [
    ("p1", "2026-09-30T10:00:00Z", -50, 50),
    ("p2", "2026-09-30T10:03:20Z", 150, 50),
]

STEPS = [
    {"zone_id": "Z1", "duration_sec": 30},
    {"zone_id": "Z2", "duration_sec": 20, "max_handoff_sec": 120},
]


# ---------------------------------------------------------------------
# 基本可行顺序
# ---------------------------------------------------------------------

def test_basic_feasible_sequence_earliest_evidence(client):
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)

    r = _ask(client, "T", STEPS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["completed"] is True
    # 最早最终结束：第二道最早 10:01:40 开始 + 20s
    assert body["final_end"] == "2026-09-30T10:02:00Z"
    assert body["point_count"] == 2
    assert body["revision"] == 2
    assert body["track_version"] == 1

    ev = body["evidence"]
    assert [e["step"] for e in ev] == [1, 2]
    assert ev[0]["zone_id"] == "Z1"
    assert ev[0]["start"] == "2026-09-30T10:00:50Z"
    assert ev[0]["end"] == "2026-09-30T10:01:20Z"
    assert ev[0]["duration_sec"] == 30.0
    assert ev[0]["required_duration_sec"] == 30.0
    assert ev[0]["max_handoff_sec"] is None
    assert ev[0]["actual_handoff_sec"] is None

    assert ev[1]["zone_id"] == "Z2"
    assert ev[1]["start"] == "2026-09-30T10:01:40Z"
    assert ev[1]["end"] == "2026-09-30T10:02:00Z"
    assert ev[1]["duration_sec"] == 20.0
    assert ev[1]["max_handoff_sec"] == 120.0
    # 第一道结束 10:01:20 -> 第二道开始 10:01:40，实际交接 20s
    assert ev[1]["actual_handoff_sec"] == 20.0


def test_handoff_gap_exceeds_limit_uncompleted(client):
    # 两区物理分离：Z1=[0,50]，Z2=[80,130]，中间 30m 无覆盖
    z1 = [[0, 0], [50, 0], [50, 100], [0, 100]]
    z2 = [[80, 0], [130, 0], [130, 100], [80, 100]]
    _zone(client, "Z1", z1)
    _zone(client, "Z2", z2)
    # 1m/s：Z1 占用 [10:00:50,10:01:40)；Z2 占用 [10:02:10,10:03:00)
    # 即使把 Z1 证据顺延到占用末，交接间隔仍为 30s
    _upload(client, "T", CROSS)

    steps = [
        {"zone_id": "Z1", "duration_sec": 30},
        {"zone_id": "Z2", "duration_sec": 20, "max_handoff_sec": 20},
    ]
    r = _ask(client, "T", steps)
    body = r.json()
    assert body["completed"] is False
    assert body["final_end"] is None
    assert body["evidence"] == []

    # 交接上限恰为最小空隙 30s 时可完成：Z1 证据顺延到 [10:01:10,10:01:40)
    steps[1]["max_handoff_sec"] = 30
    body = _ask(client, "T", steps).json()
    assert body["completed"] is True
    assert body["evidence"][0]["end"] == "2026-09-30T10:01:40Z"
    assert body["evidence"][1]["actual_handoff_sec"] == 30.0


def test_handoff_zero_requires_adjacent_evidence(client):
    """handoff=0：后道开始必须恰为前道结束（相接，半开端点不算重叠）。"""
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)

    # Z1 最早 30s 证据 10:00:50-10:01:20，距 Z2 入口 10:01:40 有 20s，gap 0 不可行；
    # 但 Z1 可把证据顺延到 10:01:10-10:01:40 与 Z2 相接（贪心扫描到该起点）。
    steps = [
        {"zone_id": "Z1", "duration_sec": 30},
        {"zone_id": "Z2", "duration_sec": 10, "max_handoff_sec": 0},
    ]
    r = _ask(client, "T", steps)
    body = r.json()
    assert body["completed"] is True
    assert body["evidence"][0]["start"] == "2026-09-30T10:01:10Z"
    assert body["evidence"][0]["end"] == "2026-09-30T10:01:40Z"
    assert body["evidence"][1]["start"] == "2026-09-30T10:01:40Z"
    assert body["evidence"][1]["actual_handoff_sec"] == 0.0
    assert body["final_end"] == "2026-09-30T10:01:50Z"


def test_handoff_zero_across_uncovered_gap_uncompleted(client):
    """同一区重复出现：handoff=0 且第一段占用放不下两道时，证据不得跨过洞
    （端点落在洞内/另一段不算交接 0）。"""
    register_square(client, zone_id="HZ", exterior=SQUARE,
                    holes=[[[40, 40], [60, 40], [60, 60], [40, 60]]])
    # 1m/s 横穿：区内两段各约 34.3s（x 0-40 与 60-100），中间穿洞约 17.1s
    _upload(client, "T", [
        ("a", "2026-09-30T12:00:00Z", -20, 50),
        ("b", "2026-09-30T12:02:00Z", 120, 50),
    ])
    # 第一道 34s 几乎占满第一段（约 34.286s 放不下），只能放第二段；
    # 第二段同样约 34.286s，34s 证据结束后没有相接的 1s 占用 -> 不可行
    steps = [
        {"zone_id": "HZ", "duration_sec": 34},
        {"zone_id": "HZ", "duration_sec": 1, "max_handoff_sec": 0},
    ]
    r = _ask(client, "T", steps,
             since="2026-09-30T11:00:00Z", until="2026-09-30T13:00:00Z")
    body = r.json()
    assert body["completed"] is False

    # 交接上限放宽到 20s（穿洞约 17.1s）即可在两段间接续
    steps[1]["max_handoff_sec"] = 20
    r = _ask(client, "T", steps,
             since="2026-09-30T11:00:00Z", until="2026-09-30T13:00:00Z")
    assert r.json()["completed"] is True


def test_same_zone_repeated_within_one_continuous_occupancy(client):
    """同一区连续出现两次：同一次连续占用中切出两段互不重叠证据。"""
    _zone(client, "Z1", Z1_EXT)
    _upload(client, "T", CROSS)
    steps = [
        {"zone_id": "Z1", "duration_sec": 30},
        {"zone_id": "Z1", "duration_sec": 20, "max_handoff_sec": 0},
    ]
    r = _ask(client, "T", steps)
    body = r.json()
    assert body["completed"] is True
    e0, e1 = body["evidence"]
    assert e0["start"] == "2026-09-30T10:00:50Z"
    assert e0["end"] == "2026-09-30T10:01:20Z"
    assert e1["start"] == "2026-09-30T10:01:20Z"
    assert e1["end"] == "2026-09-30T10:01:40Z"
    assert e1["actual_handoff_sec"] == 0.0
    assert body["final_end"] == "2026-09-30T10:01:40Z"


def test_same_zone_repeated_total_exceeds_occupancy(client):
    _zone(client, "Z1", Z1_EXT)
    _upload(client, "T", CROSS)  # Z1 内连续 50s
    steps = [
        {"zone_id": "Z1", "duration_sec": 40},
        {"zone_id": "Z1", "duration_sec": 40, "max_handoff_sec": 0},
    ]
    r = _ask(client, "T", steps)
    body = r.json()
    assert body["completed"] is False
    assert body["evidence"] == []


def test_evidence_must_come_from_single_continuous_occupancy(client):
    """停留秒数超过任一单段占用时，不得跨洞/跨间隙拼接。"""
    register_square(client, zone_id="HZ", exterior=SQUARE,
                    holes=[[[40, 40], [60, 40], [60, 60], [40, 60]]])
    # 120m/120s=1m/s：洞内剔除，两段各约 34.3s
    _upload(client, "T", [
        ("a", "2026-09-30T12:00:00Z", -20, 50),
        ("b", "2026-09-30T12:02:00Z", 120, 50),
    ])
    steps = [
        {"zone_id": "HZ", "duration_sec": 40},
        {"zone_id": "HZ", "duration_sec": 1, "max_handoff_sec": 1000},
    ]
    r = _ask(client, "T", steps,
             since="2026-09-30T11:00:00Z", until="2026-09-30T13:00:00Z")
    body = r.json()
    assert body["completed"] is False


def test_greedy_picks_later_occupancy_when_handoff_tight(client):
    """第一道在多段占用中：早段无法满足交接时，顺延到后续占用段。"""
    # Z1=[0,100] 带中央 20m 洞；Z2=[100,150]。1m/s 沿 y=50：
    # Z1 占用两段 [10:00:00,10:00:40)、[10:01:00,10:01:40)；Z2 从 10:01:40 起
    _zone(client, "Z1", SQUARE, holes=[[[40, 40], [60, 40], [60, 60], [40, 60]]])
    _zone(client, "Z2", [[100, 0], [150, 0], [150, 100], [100, 100]])
    _upload(client, "T", [
        ("p1", "2026-09-30T10:00:00Z", 0, 50),
        ("p2", "2026-09-30T10:02:30Z", 150, 50),
    ])
    steps = [
        {"zone_id": "Z1", "duration_sec": 30},
        {"zone_id": "Z2", "duration_sec": 30, "max_handoff_sec": 0},
    ]
    r = _ask(client, "T", steps)
    assert r.status_code == 200, r.text
    body = r.json()
    # 第一段 30s 证据结束最早 10:00:40，距 Z2 入口 60s，handoff=0 不可行；
    # 必须顺延到第二段 [10:01:10,10:01:40) 与 Z2 相接
    assert body["completed"] is True
    assert body["evidence"][0]["start"] == "2026-09-30T10:01:10Z"
    assert body["evidence"][0]["end"] == "2026-09-30T10:01:40Z"
    assert body["evidence"][1]["start"] == "2026-09-30T10:01:40Z"
    assert body["evidence"][1]["actual_handoff_sec"] == 0.0
    assert body["final_end"] == "2026-09-30T10:02:10Z"

    # handoff=10：第一段证据末（最晚 10:00:40）距 Z2 至少 60s 仍不可行；
    # 只能取第二段 [10:01:00,10:01:30)，交接恰为 10s
    steps[1]["max_handoff_sec"] = 10
    body = _ask(client, "T", steps).json()
    assert body["completed"] is True
    assert body["evidence"][0]["start"] == "2026-09-30T10:01:00Z"
    assert body["evidence"][1]["actual_handoff_sec"] == 10.0


def test_three_step_backtrack_through_later_piece(client):
    """三道工序、首道多段占用：前向集合传播与逆向回溯必须把首道证据落到
    能与后两道相接的晚段，而非最早占用段。"""
    # Z1=[0,100] 带 20m 中央洞（两段各 40s）；Z2=[100,150]（50s）
    _zone(client, "Z1", SQUARE, holes=[[[40, 40], [60, 40], [60, 60], [40, 60]]])
    _zone(client, "Z2", [[100, 0], [150, 0], [150, 100], [100, 100]])
    _upload(client, "T", [
        ("p1", "2026-09-30T10:00:00Z", 0, 50),
        ("p2", "2026-09-30T10:02:30Z", 150, 50),
    ])
    # Z1 20s（早段 [10:00:00,40) 或晚段 [10:01:00,40)）
    # -> Z2 10s（handoff 0）-> Z1 10s（handoff 60）
    # 首道若取早段：第二道最早 10:00:40，但 Z2 10:01:40 才占用，handoff0 不可行；
    # 故首道必须取晚段末 [10:01:20,10:01:40)，第二道 [10:01:40,10:01:50)，
    # 第三道回到 Z1 已在身后（Z1 最晚 10:01:40 结束），60s 内无占用 -> 不可行
    steps = [
        {"zone_id": "Z1", "duration_sec": 20},
        {"zone_id": "Z2", "duration_sec": 10, "max_handoff_sec": 0},
        {"zone_id": "Z1", "duration_sec": 10, "max_handoff_sec": 60},
    ]
    body = _ask(client, "T", steps).json()
    assert body["completed"] is False
    assert body["evidence"] == []

    # 去掉第三道后，前两道在 handoff=0 下可完成，首道证据落在晚段末
    body = _ask(client, "T", steps[:2]).json()
    assert body["completed"] is True
    assert body["evidence"][0]["start"] == "2026-09-30T10:01:20Z"
    assert body["final_end"] == "2026-09-30T10:01:50Z"


# ---------------------------------------------------------------------
# 查询窗口半开裁剪与区域生效窗口
# ---------------------------------------------------------------------

def test_query_window_clips_evidence_halfopen(client):
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)
    # until 恰为最早可行最终结束时刻：可行（半开，证据 end == until）
    r = _ask(client, "T", STEPS,
             since="2026-09-30T09:00:00Z", until="2026-09-30T10:02:00Z")
    assert r.json()["completed"] is True

    # until 早一秒：第二道证据放不下
    r = _ask(client, "T", STEPS,
             since="2026-09-30T09:00:00Z", until="2026-09-30T10:01:59Z")
    body = r.json()
    assert body["completed"] is False
    assert body["evidence"] == []

    # since 晚于第一道最早占用：直接未完成
    r = _ask(client, "T", STEPS,
             since="2026-09-30T10:02:00Z", until="2026-09-30T11:00:00Z")
    assert r.json()["completed"] is False


def test_zone_window_gates_occupancy(client):
    # 区域仅在 10:01:00 之后生效：Z1 有效占用 [10:01:00,10:01:40)
    _zone(client, "Z1", Z1_EXT, windows=[
        {"start": "2026-09-30T10:01:00Z", "end": "2026-09-30T11:00:00Z"}
    ])
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)
    steps = [
        {"zone_id": "Z1", "duration_sec": 30},
        {"zone_id": "Z2", "duration_sec": 20, "max_handoff_sec": 60},
    ]
    r = _ask(client, "T", steps,
             since="2026-09-30T09:00:00Z", until="2026-09-30T11:00:00Z")
    body = r.json()
    # Z1 证据 [10:01:00,10:01:30)；Z2 从 10:01:40 起，gap 10s
    assert body["completed"] is True
    assert body["evidence"][0]["start"] == "2026-09-30T10:01:00Z"


# ---------------------------------------------------------------------
# 区域修订号与轨迹版本复现
# ---------------------------------------------------------------------

def test_revision_reproduces_historical_zone_config(client):
    # revision 1：Z1 为 [0,100] 大方块（含 Z2 所在地）
    _zone(client, "Z1", SQUARE)
    _upload(client, "T", CROSS)
    # revision 2：Z1 收窄为左半；revision 3：另登记 Z2
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)

    # 最新配置可完成
    r = _ask(client, "T", STEPS)
    assert r.json()["completed"] is True

    # revision 3 同样可完成
    r = _ask(client, "T", STEPS, revision=3)
    body = r.json()
    assert body["completed"] is True
    assert body["revision"] == 3

    # revision 2 中 Z2 尚未登记 -> 404；revision 1 亦然
    assert _ask(client, "T", STEPS, revision=2).status_code == 404
    assert _ask(client, "T", STEPS, revision=1).status_code == 404

    # revision 1 下两道都用 Z1（大方块内连续 40s）可完成
    steps = [
        {"zone_id": "Z1", "duration_sec": 30},
        {"zone_id": "Z1", "duration_sec": 10, "max_handoff_sec": 0},
    ]
    r = _ask(client, "T", steps, revision=1)
    body = r.json()
    assert body["completed"] is True
    assert body["revision"] == 1


def test_revision_out_of_range_rejected(client):
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)
    r = _ask(client, "T", STEPS, revision=999)
    assert r.status_code == 404


def test_track_version_reproduces_historical_points(client):
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)  # 版本 1：横穿
    # 更正：两点都移出区域 -> 版本 2 当前不再占用
    r = client.post("/api/v1/trips/T/corrections", json={
        "base_version": 1,
        "reason": "现场复核：轨迹录错",
        "items": [
            {"action": "update", "point_id": "p1",
             "ts": "2026-09-30T10:00:00Z", "x": -500, "y": 50},
            {"action": "update", "point_id": "p2",
             "ts": "2026-09-30T10:03:20Z", "x": -300, "y": 50},
        ],
    })
    assert r.status_code == 200, r.text

    r = _ask(client, "T", STEPS)
    assert r.json()["completed"] is False
    r = _ask(client, "T", STEPS, track_version=1)
    body = r.json()
    assert body["completed"] is True
    assert body["track_version"] == 1
    assert body["final_end"] == "2026-09-30T10:02:00Z"


def test_track_version_and_revision_combined(client):
    _zone(client, "Z1", SQUARE)
    _upload(client, "T", CROSS)
    _zone(client, "Z1", Z1_EXT)
    r = _ask(
        client, "T",
        [{"zone_id": "Z1", "duration_sec": 30},
         {"zone_id": "Z1", "duration_sec": 5, "max_handoff_sec": 0}],
        revision=1, track_version=1,
    )
    body = r.json()
    assert body["completed"] is True
    assert body["revision"] == 1
    assert body["track_version"] == 1


def test_track_version_out_of_range_rejected(client):
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)
    assert _ask(client, "T", STEPS, track_version=99).status_code == 404
    assert _ask(client, "T", STEPS, track_version=0).status_code == 404


# ---------------------------------------------------------------------
# 明确拒绝：未知资源 / 非法时段时长
# ---------------------------------------------------------------------

def test_unknown_zone_rejected(client):
    _zone(client, "Z1", Z1_EXT)
    _upload(client, "T", CROSS)
    r = _ask(client, "T", STEPS)
    assert r.status_code == 404
    assert "Z2" in r.json()["detail"]


def test_unknown_trip_rejected(client):
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    r = _ask(client, "ghost", STEPS)
    assert r.status_code == 404


def test_invalid_time_window_rejected(client):
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)
    r = _ask(client, "T", STEPS,
             since="2026-09-30T11:00:00Z", until="2026-09-30T10:00:00Z")
    assert r.status_code == 400
    r = _ask(client, "T", STEPS,
             since="2026-09-30T10:00:00", until="2026-09-30T11:00:00Z")
    assert r.status_code == 400


def test_invalid_duration_rejected(client):
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)
    for bad in (0, -5):
        steps = [
            {"zone_id": "Z1", "duration_sec": bad},
            {"zone_id": "Z2", "duration_sec": 20, "max_handoff_sec": 120},
        ]
        assert _ask(client, "T", steps).status_code == 422


def test_invalid_handoff_rejected(client):
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)
    steps = [
        {"zone_id": "Z1", "duration_sec": 30},
        {"zone_id": "Z2", "duration_sec": 20, "max_handoff_sec": -1},
    ]
    assert _ask(client, "T", steps).status_code == 422


def test_handoff_rules_between_steps(client):
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)
    # 第一道不应给交接上限
    steps = [
        {"zone_id": "Z1", "duration_sec": 30, "max_handoff_sec": 5},
        {"zone_id": "Z2", "duration_sec": 20, "max_handoff_sec": 120},
    ]
    assert _ask(client, "T", steps).status_code == 400
    # 第二道必须给交接上限
    steps = [
        {"zone_id": "Z1", "duration_sec": 30},
        {"zone_id": "Z2", "duration_sec": 20},
    ]
    assert _ask(client, "T", steps).status_code == 400


def test_less_than_two_steps_rejected(client):
    _zone(client, "Z1", Z1_EXT)
    _upload(client, "T", CROSS)
    r = _ask(client, "T", [{"zone_id": "Z1", "duration_sec": 30}])
    assert r.status_code == 422


def test_extra_field_rejected(client):
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)
    body = {
        "since": "2026-09-30T00:00:00Z",
        "until": "2026-10-01T00:00:00Z",
        "steps": STEPS,
        "unexpected": 1,
    }
    r = client.post("/api/v1/trips/T/operation-sequence", json=body)
    assert r.status_code == 422


def test_versions_always_echoed(client):
    """本端点与超速/许可复核一致：总是回填实际使用的区域修订号与轨迹版本。"""
    _zone(client, "Z1", Z1_EXT)
    _zone(client, "Z2", Z2_EXT)
    _upload(client, "T", CROSS)
    body = _ask(client, "T", STEPS).json()
    assert body["revision"] == 2
    assert body["track_version"] == 1
    # 无任何区域登记的库上 revision 为 0（用隔离新库不可行，这里至少确认字段存在且为 int）
    assert isinstance(body["revision"], int)
