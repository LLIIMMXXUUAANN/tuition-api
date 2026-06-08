"""Module-level stop signal state — shared between /agent/chat and /agent/stop."""

# Soft-stop flags: set to True by the stop endpoint between rounds
stop_signals: dict[str, bool] = {}
