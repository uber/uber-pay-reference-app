"""Integration port only: no invented settlement format, SFTP client, or automatic delivery."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class SettlementWindow:
    start: datetime
    end: datetime
    cadence: timedelta  # Agreed bilaterally, not a schedule chosen by the sample.


class SettlementSink(Protocol):
    def publish(
        self,
        source_account_id: str,
        window: SettlementWindow,
        transactions: Sequence[Mapping],
    ) -> None:
        """Adopter defines reviewed format, netting, cutoffs, transport, and acknowledgment."""
        ...


def group_by_source(transactions: Sequence[Mapping]) -> dict[str, list[Mapping]]:
    """Group caller-selected synthetic snapshots; not a production settlement report."""
    groups = defaultdict(list)
    for transaction in transactions:
        source = transaction.get("source_account_id")
        if not source:
            raise ValueError("Settlement source account is required")
        groups[source].append(transaction)
    return dict(groups)
