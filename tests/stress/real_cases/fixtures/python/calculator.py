"""Simple calculator used as documentation target."""


class Calculator:
    """A tiny stateful calculator."""

    def __init__(self, initial: float = 0.0) -> None:
        self.value = initial

    def add(self, x: float) -> float:
        """Add x to the current value and return the new total."""
        self.value += x
        return self.value

    def subtract(self, x: float) -> float:
        """Subtract x from the current value and return the new total."""
        self.value -= x
        return self.value

    def reset(self) -> None:
        """Reset the running value to 0."""
        self.value = 0.0
