"""Bot lifecycle state machine.

idle ──start──▶ starting ──▶ running ◀──resume── paused
                   │           │  └──pause──────▶ │
                   │           ▼                  ▼
                   └────▶ stopping ──▶ stopped ──start──▶ starting
any active state ──error──▶ error ──(stop / start)
any state ──kill switch──▶ killed ──(manual reset)──▶ stopped
"""

from __future__ import annotations

from app.models.enums import BotState

TRANSITIONS: dict[BotState, set[BotState]] = {
    BotState.IDLE: {BotState.STARTING, BotState.KILLED},
    BotState.STARTING: {BotState.RUNNING, BotState.STOPPING, BotState.STOPPED, BotState.ERROR, BotState.KILLED},
    BotState.RUNNING: {BotState.PAUSED, BotState.STOPPING, BotState.ERROR, BotState.KILLED},
    BotState.PAUSED: {BotState.RUNNING, BotState.STOPPING, BotState.ERROR, BotState.KILLED},
    BotState.STOPPING: {BotState.STOPPED, BotState.ERROR, BotState.KILLED},
    BotState.STOPPED: {BotState.STARTING, BotState.KILLED},
    BotState.ERROR: {BotState.STARTING, BotState.STOPPED, BotState.KILLED},
    BotState.KILLED: {BotState.STOPPED},  # only after manual review / reset
}

ACTIVE_STATES = {BotState.STARTING, BotState.RUNNING, BotState.PAUSED, BotState.STOPPING}


class InvalidTransition(Exception):
    pass


def can_transition(current: BotState, target: BotState) -> bool:
    return target in TRANSITIONS[current]


class BotStateMachine:
    def __init__(self, state: BotState = BotState.IDLE) -> None:
        self.state = state

    def can(self, target: BotState) -> bool:
        return can_transition(self.state, target)

    def transition(self, target: BotState) -> BotState:
        if not self.can(target):
            raise InvalidTransition(f"{self.state.value} -> {target.value}")
        self.state = target
        return self.state

    # Convenience events
    def start(self) -> BotState:
        return self.transition(BotState.STARTING)

    def started(self) -> BotState:
        return self.transition(BotState.RUNNING)

    def pause(self) -> BotState:
        return self.transition(BotState.PAUSED)

    def resume(self) -> BotState:
        return self.transition(BotState.RUNNING)

    def stop(self) -> BotState:
        if self.state in (BotState.IDLE, BotState.STOPPED):
            return self.state
        if self.state == BotState.ERROR:
            return self.transition(BotState.STOPPED)
        if self.state != BotState.STOPPING:
            self.transition(BotState.STOPPING)
        return self.transition(BotState.STOPPED)

    def fail(self) -> BotState:
        return self.transition(BotState.ERROR)

    def kill(self) -> BotState:
        if self.state == BotState.KILLED:
            return self.state
        return self.transition(BotState.KILLED)

    def reset(self) -> BotState:
        return self.transition(BotState.STOPPED)
