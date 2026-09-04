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
