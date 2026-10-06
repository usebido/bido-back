"""Onboarding: learn the tenant's RFQ template from old RFQs (sent mail) or uploads.

`service` backs `/v1/onboarding`; `jobs` holds the worker jobs (`template.scan`,
`template.build`); `candidates` and `standard` are pure helpers.
"""
