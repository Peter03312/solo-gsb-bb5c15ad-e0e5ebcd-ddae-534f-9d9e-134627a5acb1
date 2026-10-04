"""作业车工序顺序复核测试。

POST /api/v1/trips/{trip_id}/sequence-review：
每道工序须在一次连续占用中取得足额且互不重叠的证据时段；后一道开始不得早于
前一道结束，间隔不超过其交接上限；允许同一区重复出现；能完成时返回最早最终
结束时刻与逐道证据区间及实际版本，无可行顺序返回未完成与空证据。
区域与车次点集从同一已提交读视图读取。
"""
import threading

from tests.conftest import register_square

ZB_EXTERIOR = [[200, 0], [300, 0], [300, 100], [200, 100]]
ZB_FAR = [[500, 0], [600, 0], [600, 100], [500, 100]]
HOLE = [[[40, 40], [60, 40], [60, 60], [40, 60]]]


def _upload(client, trip, points):
    r = client.post(
        f"/api/v1/trips/{trip}/points",
        json={"points": [
            {"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points
        ]},
    )
    assert r.status_code == 200, r.text
    return r


def _register_zab(client):
    register_square(client, zone_id="ZA")
    register_square(client, zone_id="ZB", exterior=ZB_EXTERIOR)


def _ask(client, trip, **overrides):
    body = {
        "since": "2026-09-30T09:00:00Z",
        "until": "2026-09-30T12:00:00Z",
        "steps": [
            {"zone_id": "ZA", "min_stay_sec": 60},
            {"zone_id": "ZB", "min_stay_sec": 60, "max_handover_sec": 600},
        ],
    }
    body.update(overrides)
    return client.post(f"/api/v1/trips/{trip}/sequence-review", json=body)


# 10:00:00 自 (50,50)（ZA 内）出发，10:10:00 到 (250,50)（ZB 内）：200m/600s；
# ZA 占用 [10:00:00, 10:02:30)（x=100 于 10:02:30），ZB 占用 [10:07:30, 10:10:00)
BASIC = [
    ("p1", "2026-09-30T10:00:00Z", 50, 50),
    ("p2", "2026-09-30T10:10:00Z", 250, 50),
]


# ---------------------------------------------------------------------
# 基本完成 / 最早最终结束 / 证据区间
# ---------------------------------------------------------------------

def test_basic_two_steps_completed(client):
    _register_zab(client)
    _upload(client, "T1", BASIC)

    r = _ask(client, "T1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trip_id"] == "T1"
    assert body["completed"] is True
    # 最早最终结束：第二道最早可于 10:07:30 开始，停留 60 秒
    assert body["finished_at"] == "2026-09-30T10:08:30Z"
    assert body["point_count"] == 2
    # 省略版本参数时也回填实际使用的版本
    assert body["revision"] == 2
    assert body["track_version"] == 1
    assert body["steps"] == [
        {"zone_id": "ZA", "min_stay_sec": 60.0},
        {"zone_id": "ZB", "min_stay_sec": 60.0, "max_handover_sec": 600.0},
    ]
    ev = body["evidence"]
    assert len(ev) == 2
    assert ev[0]["step"] == 1 and ev[0]["zone_id"] == "ZA"
    assert ev[0]["start"] == "2026-09-30T10:01:30Z"
    assert ev[0]["end"] == "2026-09-30T10:02:30Z"
    assert ev[0]["duration_sec"] == 60.0
    assert ev[1]["step"] == 2 and ev[1]["zone_id"] == "ZB"
    assert ev[1]["start"] == "2026-09-30T10:07:30Z"
    assert ev[1]["end"] == "2026-09-30T10:08:30Z"
    assert ev[1]["duration_sec"] == 60.0
    # 证据有序且互不重叠
    assert ev[0]["end"] <= ev[1]["start"]


def test_handover_exactly_at_limit_completed(client):
    """交接间隔恰好等于上限仍算完成（不超过即允许）。"""
    _register_zab(client)
    _upload(client, "T2", BASIC)
    # ZA 证据最晚结束 10:02:30，ZB 证据最早开始 10:07:30：间隔恰为 300 秒
    r = _ask(
        client, "T2",
        steps=[
            {"zone_id": "ZA", "min_stay_sec": 60},
            {"zone_id": "ZB", "min_stay_sec": 60, "max_handover_sec": 300},
        ],
    )
    body = r.json()
    assert body["completed"] is True
    assert body["finished_at"] == "2026-09-30T10:08:30Z"
    ev = body["evidence"]
    assert ev[0]["start"] == "2026-09-30T10:01:30Z"
    assert ev[0]["end"] == "2026-09-30T10:02:30Z"
    assert ev[1]["start"] == "2026-09-30T10:07:30Z"


def test_handover_exceeded_incomplete(client):
    """交接间隔超过上限：无可行顺序，明确返回未完成与空证据。"""
    _register_zab(client)
    _upload(client, "T3", BASIC)
    r = _ask(
        client, "T3",
        steps=[
            {"zone_id": "ZA", "min_stay_sec": 60},
            {"zone_id": "ZB", "min_stay_sec": 60, "max_handover_sec": 299},
        ],
    )
    body = r.json()
    assert body["completed"] is False
    assert body["finished_at"] is None
    assert body["evidence"] == []
    # 未完成同样回填实际版本
    assert body["revision"] == 2
    assert body["track_version"] == 1


def test_insufficient_stay_incomplete(client):
    """连续占用不足停留秒数：该工序无法取得足额证据。"""
    _register_zab(client)
    _upload(client, "T4", BASIC)
    r = _ask(
        client, "T4",
        steps=[
            {"zone_id": "ZA", "min_stay_sec": 60},
            {"zone_id": "ZB", "min_stay_sec": 151, "max_handover_sec": 600},
        ],
    )
    body = r.json()
    assert body["completed"] is False
    assert body["finished_at"] is None
    assert body["evidence"] == []


def test_wrong_order_incomplete(client):
    """实际先进 ZB 再进 ZA，与工序顺序相反：未完成。"""
    _register_zab(client)
    _upload(client, "T5", [
        ("p1", "2026-09-30T10:00:00Z", 250, 50),
        ("p2", "2026-09-30T10:10:00Z", 50, 50),
    ])
    body = _ask(client, "T5").json()
    assert body["completed"] is False
    assert body["finished_at"] is None
    assert body["evidence"] == []


def test_deferred_first_step_not_naive_greedy(client):
    """第一道工序不能盲目取最早占用：须推迟到后一段连续占用才能接上后续工序。

    ZA 占用 [10:00,10:03:30) ∪ [10:08:30,10:12:15)，ZB 占用 [10:12:45,10:15:00)；
    若第一道取最早的 [10:00,10:02)，交接窗口（≤300s）够不到 ZB 占用段，
    只有推迟到 ZA 第二段占用才能完成。
    """
    _register_zab(client)
    _upload(client, "T6", [
        ("p1", "2026-09-30T10:00:00Z", 50, 50),
        ("p2", "2026-09-30T10:03:00Z", 50, 50),
        ("p3", "2026-09-30T10:04:00Z", 150, 50),
        ("p4", "2026-09-30T10:08:00Z", 150, 50),
        ("p5", "2026-09-30T10:09:00Z", 50, 50),
        ("p6", "2026-09-30T10:12:00Z", 50, 50),
        ("p7", "2026-09-30T10:13:00Z", 250, 50),
        ("p8", "2026-09-30T10:15:00Z", 250, 50),
    ])
    r = _ask(
        client, "T6",
        steps=[
            {"zone_id": "ZA", "min_stay_sec": 120},
            {"zone_id": "ZB", "min_stay_sec": 60, "max_handover_sec": 300},
        ],
    )
    body = r.json()
    assert body["completed"] is True
    assert body["finished_at"] == "2026-09-30T10:13:45Z"
    ev = body["evidence"]
    # 第一道证据取自 ZA 第二段连续占用（10:08:30–10:12:15）内
    assert ev[0]["start"] == "2026-09-30T10:10:15Z"
    assert ev[0]["end"] == "2026-09-30T10:12:15Z"
    assert ev[1]["start"] == "2026-09-30T10:12:45Z"
    assert ev[1]["end"] == "2026-09-30T10:13:45Z"


def test_same_zone_repeated_three_steps(client):
    """允许同一区重复出现：ZA → ZB → ZA 三道工序。"""
    _register_zab(client)
    _upload(client, "T7", [
        ("p1", "2026-09-30T10:00:00Z", 50, 50),
        ("p2", "2026-09-30T10:02:00Z", 50, 50),
        ("p3", "2026-09-30T10:03:00Z", 250, 50),
        ("p4", "2026-09-30T10:05:00Z", 250, 50),
        ("p5", "2026-09-30T10:06:00Z", 50, 50),
        ("p6", "2026-09-30T10:08:00Z", 50, 50),
    ])
    r = _ask(
        client, "T7",
        steps=[
            {"zone_id": "ZA", "min_stay_sec": 60},
            {"zone_id": "ZB", "min_stay_sec": 60, "max_handover_sec": 120},
            {"zone_id": "ZA", "min_stay_sec": 60, "max_handover_sec": 120},
        ],
    )
    body = r.json()
    assert body["completed"] is True
    assert body["finished_at"] == "2026-09-30T10:06:45Z"
    ev = body["evidence"]
    assert [e["zone_id"] for e in ev] == ["ZA", "ZB", "ZA"]
    assert [e["step"] for e in ev] == [1, 2, 3]
    assert ev[0]["start"] == "2026-09-30T10:01:15Z"
    assert ev[0]["end"] == "2026-09-30T10:02:15Z"
    assert ev[1]["start"] == "2026-09-30T10:04:15Z"
    assert ev[1]["end"] == "2026-09-30T10:05:15Z"
    assert ev[2]["start"] == "2026-09-30T10:05:45Z"
    assert ev[2]["end"] == "2026-09-30T10:06:45Z"
    # 证据互不重叠
    assert ev[0]["end"] <= ev[1]["start"]
    assert ev[1]["end"] <= ev[2]["start"]


# ---------------------------------------------------------------------
# 占用规则沿用：带洞边界 / 查询半开窗口 / 区域生效窗口
# ---------------------------------------------------------------------

def test_hole_splits_continuous_occupancy(client):
    """洞内时间切断连续占用：证据必须取自同一次连续占用。"""
    register_square(client, zone_id="ZAH", holes=HOLE)
    # 1m/s 沿 y=50 横穿：ZAH 占用 [10:00:10,10:00:50) ∪ [10:01:10,10:01:50)
    _upload(client, "T8", [
        ("p1", "2026-09-30T10:00:00Z", -10, 50),
        ("p2", "2026-09-30T10:02:00Z", 110, 50),
    ])
    # 单段连续占用只有 40 秒，停留 45 秒不足额
    body = _ask(
        client, "T8",
        steps=[
            {"zone_id": "ZAH", "min_stay_sec": 45},
            {"zone_id": "ZAH", "min_stay_sec": 10, "max_handover_sec": 1000},
        ],
    ).json()
    assert body["completed"] is False
    assert body["evidence"] == []
    # 两段各取 40 秒：跨洞的两次连续占用可分别作为两道证据
    body = _ask(
        client, "T8",
        steps=[
            {"zone_id": "ZAH", "min_stay_sec": 40},
            {"zone_id": "ZAH", "min_stay_sec": 40, "max_handover_sec": 70},
        ],
    ).json()
    assert body["completed"] is True
    assert body["finished_at"] == "2026-09-30T10:01:50Z"
    ev = body["evidence"]
    assert ev[0]["start"] == "2026-09-30T10:00:10Z"
    assert ev[0]["end"] == "2026-09-30T10:00:50Z"
    assert ev[1]["start"] == "2026-09-30T10:01:10Z"
    assert ev[1]["end"] == "2026-09-30T10:01:50Z"


def test_query_window_halfopen_clip(client):
    """查询时段按半开 [since, until) 裁剪占用后再排工序。"""
    _register_zab(client)
    _upload(client, "T9", BASIC)
    r = _ask(
        client, "T9",
        since="2026-09-30T10:01:00Z",
        until="2026-09-30T10:08:00Z",
        steps=[
            {"zone_id": "ZA", "min_stay_sec": 60},
            {"zone_id": "ZB", "min_stay_sec": 30, "max_handover_sec": 600},
        ],
    )
    body = r.json()
    assert body["completed"] is True
    # ZB 占用被裁剪为 [10:07:30, 10:08:00)，证据恰在其内
    assert body["finished_at"] == "2026-09-30T10:08:00Z"
    ev = body["evidence"]
    assert ev[0]["start"] == "2026-09-30T10:01:30Z"
    assert ev[0]["end"] == "2026-09-30T10:02:30Z"
    assert ev[1]["start"] == "2026-09-30T10:07:30Z"
    assert ev[1]["end"] == "2026-09-30T10:08:00Z"


def test_zone_window_clips_occupancy(client):
    """区域生效时段外的占用不计入；同一连续占用内两道证据可相接。"""
    register_square(
        client, zone_id="ZAW",
        windows=[{"start": "2026-09-30T10:05:00Z", "end": "2026-09-30T10:15:00Z"}],
    )
    # 静止在区内 20 分钟；生效窗口内占用 [10:05, 10:15)
    _upload(client, "T10", [
        ("p1", "2026-09-30T10:00:00Z", 50, 50),
        ("p2", "2026-09-30T10:20:00Z", 50, 50),
    ])
    r = _ask(
        client, "T10",
        steps=[
            {"zone_id": "ZAW", "min_stay_sec": 200},
            {"zone_id": "ZAW", "min_stay_sec": 200, "max_handover_sec": 60},
        ],
    )
    body = r.json()
    assert body["completed"] is True
    assert body["finished_at"] == "2026-09-30T10:11:40Z"
    ev = body["evidence"]
    assert ev[0]["start"] == "2026-09-30T10:05:00Z"
    assert ev[0]["end"] == "2026-09-30T10:08:20Z"
    assert ev[1]["start"] == "2026-09-30T10:08:20Z"
    assert ev[1]["end"] == "2026-09-30T10:11:40Z"


# ---------------------------------------------------------------------
# 版本复现：轨迹版本 / 区域修订号
# ---------------------------------------------------------------------

def test_track_version_reproduce(client):
    """按轨迹版本复现：v1 点集可完成，更正撤销关键点后的当前点集不可完成。"""
    _register_zab(client)
    _upload(client, "T11", BASIC)  # 版本 1
    r = client.post(
        "/api/v1/trips/T11/corrections",
        json={
            "base_version": 1,
            "reason": "现场复核：p2 为误录",
            "items": [{"action": "revoke", "point_id": "p2"}],
        },
    )
    assert r.status_code == 200, r.text
    # 当前版本（v2，仅剩 1 点，无占用）：未完成
    body = _ask(client, "T11").json()
    assert body["completed"] is False
    assert body["track_version"] == 2
    # 按 v1 点集复现：完成
    body = _ask(client, "T11", track_version=1).json()
    assert body["completed"] is True
    assert body["track_version"] == 1
    assert body["finished_at"] == "2026-09-30T10:08:30Z"


def test_revision_reproduce(client):
    """按区域修订号复现：旧配置（ZB 在原位）可完成，最新配置（ZB 已挪走）不可。"""
    register_square(client, zone_id="ZA")                      # 修订号 1
    register_square(client, zone_id="ZB", exterior=ZB_EXTERIOR)  # 修订号 2
    register_square(client, zone_id="ZB", exterior=ZB_FAR)     # 修订号 3：ZB 挪走
    _upload(client, "T12", BASIC)
    # 最新配置：ZB 占用为空 → 未完成
    body = _ask(client, "T12").json()
    assert body["completed"] is False
    assert body["revision"] == 3
    # 按修订号 2 复现：完成
    body = _ask(client, "T12", revision=2).json()
    assert body["completed"] is True
    assert body["revision"] == 2
    assert body["finished_at"] == "2026-09-30T10:08:30Z"


def test_revision_and_track_version_combined(client):
    """区域修订号与轨迹版本可同时指定。"""
    register_square(client, zone_id="ZA")
    register_square(client, zone_id="ZB", exterior=ZB_EXTERIOR)
    register_square(client, zone_id="ZB", exterior=ZB_FAR)
    _upload(client, "T13", BASIC)
    client.post(
        "/api/v1/trips/T13/corrections",
        json={
            "base_version": 1,
            "reason": "现场复核：p2 为误录",
            "items": [{"action": "revoke", "point_id": "p2"}],
        },
    )
    body = _ask(client, "T13", revision=2, track_version=1).json()
    assert body["completed"] is True
    assert body["revision"] == 2
    assert body["track_version"] == 1


# ---------------------------------------------------------------------
# 明确拒绝：未知区 / 不可追溯版本 / 非法时段与时长
# ---------------------------------------------------------------------

def test_unknown_zone_404(client):
    _register_zab(client)
    _upload(client, "T14", BASIC)
    r = _ask(
        client, "T14",
        steps=[
            {"zone_id": "ZA", "min_stay_sec": 60},
            {"zone_id": "ghost", "min_stay_sec": 60, "max_handover_sec": 600},
        ],
    )
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_unknown_trip_404(client):
    _register_zab(client)
    r = _ask(client, "ghost-trip")
    assert r.status_code == 404


def test_revision_out_of_range_404(client):
    _register_zab(client)
    _upload(client, "T15", BASIC)
    r = _ask(client, "T15", revision=999)
    assert r.status_code == 404


def test_track_version_out_of_range_404(client):
    _register_zab(client)
    _upload(client, "T16", BASIC)
    # 晚于当前版本
    assert _ask(client, "T16", track_version=9).status_code == 404
    # 早于最早可追溯基线（新上传车次基线为 1）
    assert _ask(client, "T16", track_version=0).status_code == 404


def test_zone_not_registered_at_revision_404(client):
    """指定修订号的配置中该工序危险区尚未登记。"""
    register_square(client, zone_id="ZA")  # 修订号 1（ZB 尚未登记）
    register_square(client, zone_id="ZB", exterior=ZB_EXTERIOR)  # 修订号 2
    _upload(client, "T17", BASIC)
    r = _ask(client, "T17", revision=1)
    assert r.status_code == 404


def test_invalid_time_range_400(client):
    _register_zab(client)
    _upload(client, "T18", BASIC)
    # since >= until
    r = _ask(client, "T18", since="2026-09-30T12:00:00Z", until="2026-09-30T09:00:00Z")
    assert r.status_code == 400
    # 不带时区
    r = _ask(client, "T18", since="2026-09-30T09:00:00")
    assert r.status_code == 400


def test_invalid_durations_rejected(client):
    _register_zab(client)
    _upload(client, "T19", BASIC)
    # 停留秒数非正：请求层 422
    r = _ask(client, "T19", steps=[
        {"zone_id": "ZA", "min_stay_sec": 0},
        {"zone_id": "ZB", "min_stay_sec": 60, "max_handover_sec": 600},
    ])
    assert r.status_code == 422
    r = _ask(client, "T19", steps=[
        {"zone_id": "ZA", "min_stay_sec": -5},
        {"zone_id": "ZB", "min_stay_sec": 60, "max_handover_sec": 600},
    ])
    assert r.status_code == 422
    # 停留秒数非数值：请求层 422
    r = _ask(client, "T19", steps=[
        {"zone_id": "ZA", "min_stay_sec": "abc"},
        {"zone_id": "ZB", "min_stay_sec": 60, "max_handover_sec": 600},
    ])
    assert r.status_code == 422
    # 交接秒数为负：请求层 422
    r = _ask(client, "T19", steps=[
        {"zone_id": "ZA", "min_stay_sec": 60},
        {"zone_id": "ZB", "min_stay_sec": 60, "max_handover_sec": -1},
    ])
    assert r.status_code == 422
    # 第二道起缺失交接秒数：400
    r = _ask(client, "T19", steps=[
        {"zone_id": "ZA", "min_stay_sec": 60},
        {"zone_id": "ZB", "min_stay_sec": 60},
    ])
    assert r.status_code == 400
    # 第一道工序误给交接秒数：400
    r = _ask(client, "T19", steps=[
        {"zone_id": "ZA", "min_stay_sec": 60, "max_handover_sec": 10},
        {"zone_id": "ZB", "min_stay_sec": 60, "max_handover_sec": 600},
    ])
    assert r.status_code == 400


def test_steps_too_few_422(client):
    _register_zab(client)
    _upload(client, "T20", BASIC)
    r = _ask(client, "T20", steps=[{"zone_id": "ZA", "min_stay_sec": 60}])
    assert r.status_code == 422
    r = _ask(client, "T20", steps=[])
    assert r.status_code == 422


def test_extra_field_422(client):
    _register_zab(client)
    _upload(client, "T21", BASIC)
    r = _ask(client, "T21", bogus=1)
    assert r.status_code == 422
    r = _ask(client, "T21", steps=[
        {"zone_id": "ZA", "min_stay_sec": 60, "bogus": 1},
        {"zone_id": "ZB", "min_stay_sec": 60, "max_handover_sec": 600},
    ])
    assert r.status_code == 422


# ---------------------------------------------------------------------
# 并发：同一已提交读视图，固定版本组合结果确定一致
# ---------------------------------------------------------------------

def test_concurrent_updates_pinned_result_deterministic(client):
    """并发登记/上传下，指定修订号与轨迹版本的复核结果确定一致。"""
    _register_zab(client)
    _upload(client, "T30", BASIC)

    stop = threading.Event()
    failures = []

    def writer() -> None:
        i = 0
        while not stop.is_set():
            size = 300 + (i % 3) * 50
            client.post(
                "/api/v1/zones",
                json={
                    "zone_id": "ZB",
                    "exterior": [[200, 0], [size, 0], [size, 100], [200, 100]],
                    "holes": [],
                    "windows": [
                        {"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"}
                    ],
                },
            )
            i += 1

    def reader() -> None:
        while not stop.is_set():
            r = _ask(client, "T30", revision=2, track_version=1)
            if r.status_code != 200:
                failures.append(("status", r.status_code, r.text))
                return
            body = r.json()
            if (
                body["completed"] is not True
                or body["finished_at"] != "2026-09-30T10:08:30Z"
                or body["revision"] != 2
                or body["track_version"] != 1
            ):
                failures.append(("nondeterministic", body))
                return

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    stop.wait(timeout=1.0)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not failures
