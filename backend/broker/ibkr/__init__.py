"""IBKR adapter package. Paper-only until the live gate opens."""

from broker.ibkr.adapter import IB_STATUS_MAP, IBKRBroker
from broker.ibkr.config import IBKRConfigError, IBKRTransportConfig
from broker.ibkr.instruments import (
    AmbiguousInstrument,
    IBKRInstrumentResolver,
    Instrument,
    InstrumentError,
    InstrumentNotFound,
    UnsupportedInstrument,
)
from broker.ibkr.transport import (
    FakeIBKRTransport,
    IBKRNotConfigured,
    IBKRTransport,
    UnconfiguredIBKRTransport,
)

__all__ = [
    "IB_STATUS_MAP",
    "AmbiguousInstrument",
    "FakeIBKRTransport",
    "IBKRBroker",
    "IBKRConfigError",
    "IBKRInstrumentResolver",
    "IBKRNotConfigured",
    "IBKRTransport",
    "IBKRTransportConfig",
    "Instrument",
    "InstrumentError",
    "InstrumentNotFound",
    "UnconfiguredIBKRTransport",
    "UnsupportedInstrument",
]
# `live_transport` is deliberately absent: importing it pulls in ib_async and
# implies a connection this deployment may not have. Import it explicitly.
