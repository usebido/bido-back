"""Supplier quotes: deterministic validation of extractions and the QUOTE_EXTRACT job.

The job module (`extract_job`) imports the agent runner, and so Agno; import
it directly where the worker registers it, not through this package.
"""
