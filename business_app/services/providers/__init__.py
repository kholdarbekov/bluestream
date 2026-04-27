"""
Provider-specific payment components extracted from PaymentService (ARCH-002).

Modules in this package own the low-level protocol details for a single
payment provider or cross-cutting concern. PaymentService (the facade) wires
them together and exposes the public API that the rest of the codebase
depends on.
"""
