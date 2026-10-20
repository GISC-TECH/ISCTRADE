"""Bot lifecycle state machine."""

from enum import Enum


class BotState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    KILLED = "killed"


TRANSITIONS: dict[BotState, set[BotState]] = {
    BotState.IDLE: {BotState.STARTING},
    BotState.STARTING: {BotState.RUNNING, BotState.STOPPED, BotState.KILLED},
    BotState.RUNNING: {BotState.PAUSED, BotState.STOPPING, BotState.KILLED},
    BotState.PAUSED: {BotState.RUNNING, BotState.STOPPING, BotState.KILLED},
    BotState.STOPPING: {BotState.STOPPED, BotState.KILLED},
    BotState.STOPPED: {BotState.STARTING},
    BotState.KILLED: {BotState.STOPPED},  # requires manual review
}


class InvalidTransition(Exception):
    pass


class BotStateMachine:
    def __init__(self, state: BotState = BotState.IDLE) -> None:
        self.state = state

    def can(self, target: BotState) -> bool:
        return target in TRANSITIONS[self.state]

    def transition(self, target: BotState) -> BotState:
        if not self.can(target):
            raise InvalidTransition(f"{self.state.value} -> {target.value}")
        self.state = target
        return self.state
