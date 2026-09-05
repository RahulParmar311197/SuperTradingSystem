"""Three-level kill switch (blueprint §58): strategy, account, global."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class KillSwitchState:
    global_kill: bool = False
    killed_accounts: set[str] = field(default_factory=set)
    killed_strategies: set[str] = field(default_factory=set)

    def kill_global(self) -> None:
        self.global_kill = True

    def resume_global(self) -> None:
        self.global_kill = False

    def kill_account(self, account_id: str) -> None:
        self.killed_accounts.add(account_id)

    def resume_account(self, account_id: str) -> None:
        self.killed_accounts.discard(account_id)

    def kill_strategy(self, strategy_id: str) -> None:
        self.killed_strategies.add(strategy_id)

    def resume_strategy(self, strategy_id: str) -> None:
        self.killed_strategies.discard(strategy_id)

    def is_blocked(self, account_id: str | None, strategy_id: str | None) -> str | None:
        if self.global_kill:
            return "Global kill switch is active"
        if account_id and account_id in self.killed_accounts:
            return f"Account {account_id} trading is stopped"
        if strategy_id and strategy_id in self.killed_strategies:
            return f"Strategy {strategy_id} is stopped"
        return None


async def load_kill_switch_state(account_id: str | None, strategy_id: str | None) -> KillSwitchState:
    """Builds a `KillSwitchState` reflecting only the global flag and the
    given account/strategy, backed by Redis (app.core.redis) so a kill
    triggered by an admin API call in one process is visible to the
    RiskEngine inside every trading stack in every process -- a plain
    `KillSwitchState()` constructed locally, which is all `RiskEngine` ever
    got before this existed, can't carry that signal between processes (see
    the "Kill switch" section of app.core.redis for the shared keys). Call
    this right before `RiskEngine.evaluate`/`evaluate_options_risk` so the
    check reflects the current state rather than whatever was true when the
    trading stack was constructed."""
    from app.core.redis import is_account_killed, is_global_killed, is_strategy_killed

    global_kill = await is_global_killed()
    killed_accounts = {account_id} if account_id and await is_account_killed(account_id) else set()
    killed_strategies = {strategy_id} if strategy_id and await is_strategy_killed(strategy_id) else set()
    return KillSwitchState(global_kill=global_kill, killed_accounts=killed_accounts, killed_strategies=killed_strategies)
