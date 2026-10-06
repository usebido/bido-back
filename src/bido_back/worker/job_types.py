"""Job type names shared by producers and the worker registry."""

APPROVAL_EXECUTE = "approval.execute"
QUOTE_EXTRACT = "quote.extract"  # payload: {"message_id": "<uuid>"}
FX_REFRESH = "fx.refresh"  # payload: {}
