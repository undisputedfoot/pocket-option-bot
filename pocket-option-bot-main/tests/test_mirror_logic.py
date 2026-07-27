import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from logger import setup_logger
from mirror_trade import _deal_to_trade_event, create_trade_plan, execute_trade_plan, normalize_action


class FakeExecutor:
    def __init__(self, label):
        self.label = label
        self.calls = []

    def place_trade(self, asset, action, amount, duration):
        payload = {
            "label": self.label,
            "asset": asset,
            "action": action,
            "amount": amount,
            "duration": duration,
        }
        self.calls.append(payload)
        return payload


def test_create_trade_plan_marks_child_as_mirrored_only():
    plan = create_trade_plan("EURUSD", "call", 1.25, 60)

    assert plan["master"]["origin"] == "manual"
    assert plan["child"]["origin"] == "mirrored_from_master"
    assert plan["child"]["triggered_by"] == "manual_master"
    assert plan["child"]["source_label"] == "master"


def test_execute_trade_plan_runs_both_accounts_once():
    master = FakeExecutor("master")
    child = FakeExecutor("child")
    plan = create_trade_plan("EURUSD", "put", 2.0, 120)

    result = execute_trade_plan(master, child, plan)

    assert len(master.calls) == 1
    assert len(child.calls) == 1
    assert result["master"]["action"] == "put"
    assert result["child"]["action"] == "put"
    assert result["child"]["label"] == "child"


def test_extracts_ssid_and_payload_from_wrapper():
    from mirror_trade import PocketOptionTradeExecutor

    wrapped = '42["auth",{"session":"abc123","isDemo":1,"uid":42,"platform":2,"isFastHistory":true,"isOptimized":true}]'
    executor = PocketOptionTradeExecutor(wrapped, "test")

    assert executor.ssid == "abc123"
    assert executor.auth_payload["uid"] == 42
    assert executor.auth_payload["isDemo"] == 1
    assert executor.auth_payload["platform"] == 2


def test_normalize_action_maps_numeric_command_values_correctly():
    assert normalize_action(0) == "call"
    assert normalize_action("0") == "call"
    assert normalize_action(1) == "put"
    assert normalize_action("1") == "put"


def test_deal_to_trade_event_preserves_put_and_derives_duration():
    class Deal:
        id = "deal-1"
        command = 1
        asset = "EURUSD_otc"
        amount = 3.0
        open_timestamp = 1000
        close_timestamp = 1060

    event = _deal_to_trade_event(Deal())

    assert event["trade_id"] == "deal-1"
    assert event["asset"] == "EURUSD_otc"
    assert event["action"] == "put"
    assert event["amount"] == 3.0
    assert event["duration"] == 60


def test_setup_logger_writes_to_file(tmp_path):
    log_file = tmp_path / "mirror.log"
    logger = setup_logger("mirror-test", log_file=str(log_file))
    logger.info("hello logger")

    assert log_file.exists()
    assert "hello logger" in log_file.read_text(encoding="utf-8")

