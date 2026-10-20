import pytest

from app.models.enums import BotState
from app.strategy.state_machine import BotStateMachine, InvalidTransition


def test_happy_path() -> None:
    sm = BotStateMachine()
    assert sm.start() == BotState.STARTING
    assert sm.started() == BotState.RUNNING
    assert sm.pause() == BotState.PAUSED
    assert sm.resume() == BotState.RUNNING
    assert sm.stop() == BotState.STOPPED
    assert sm.start() == BotState.STARTING


def test_error_and_recovery() -> None:
    sm = BotStateMachine(BotState.RUNNING)
    sm.fail()
    assert sm.state == BotState.ERROR
    assert sm.start() == BotState.STARTING
    sm2 = BotStateMachine(BotState.ERROR)
    assert sm2.stop() == BotState.STOPPED


def test_kill_switch_requires_reset() -> None:
    sm = BotStateMachine(BotState.RUNNING)
    sm.kill()
    with pytest.raises(InvalidTransition):
        sm.start()
    assert sm.reset() == BotState.STOPPED
    assert sm.start() == BotState.STARTING


def test_invalid_transitions() -> None:
    with pytest.raises(InvalidTransition):
        BotStateMachine(BotState.IDLE).pause()
    with pytest.raises(InvalidTransition):
        BotStateMachine(BotState.STOPPED).resume()


def test_stop_is_idempotent() -> None:
    assert BotStateMachine(BotState.STOPPED).stop() == BotState.STOPPED
    assert BotStateMachine(BotState.IDLE).stop() == BotState.IDLE
