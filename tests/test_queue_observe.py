"""队列灰度观察的多实例 processing 与在途年龄测试。"""

from __future__ import annotations

import fnmatch
import json

from scripts.queue_observe import _processing_items, _stuck_deliveries


class _Redis:
    def __init__(self, lists):
        self.lists = lists

    def scan_iter(self, match=None):
        for key in self.lists:
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    def lrange(self, key, start, end):
        return list(self.lists.get(key, []))


def _raw(delivery_id: str, processing_started_at=None) -> str:
    env = {"id": delivery_id, "payload": {"msgid": delivery_id}}
    if processing_started_at is not None:
        env["processing_started_at"] = processing_started_at
    return json.dumps(env)


def test_processing_items_sums_all_consumer_lists():
    r = _Redis(
        {
            "wecom:msgq:proc": [_raw("legacy")],
            "wecom:msgq:proc:c1": [_raw("m1")],
            "wecom:msgq:proc:c2": [_raw("m2"), _raw("m3")],
        }
    )

    assert len(_processing_items(r)) == 4


def test_stuck_detection_catches_all_workers_stuck_at_equal_capacity():
    first_seen = {}
    items = [_raw("m1"), _raw("m2")]

    assert _stuck_deliveries(items, first_seen, now=100.0, max_age=60) == []
    stuck = _stuck_deliveries(items, first_seen, now=161.0, max_age=60)

    assert {delivery_id for delivery_id, _age in stuck} == {"m1", "m2"}


def test_stuck_tracking_forgets_completed_delivery():
    first_seen = {}
    _stuck_deliveries([_raw("m1")], first_seen, now=100.0, max_age=60)
    _stuck_deliveries([], first_seen, now=110.0, max_age=60)

    assert first_seen == {}


def test_stuck_detection_uses_envelope_start_time_on_late_observer_start():
    first_seen = {}
    stuck = _stuck_deliveries(
        [_raw("old", processing_started_at=100.0)],
        first_seen,
        now=800.0,
        max_age=660,
    )

    assert stuck == [("old", 700)]
